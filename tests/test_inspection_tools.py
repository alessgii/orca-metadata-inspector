import hashlib
import json
import shlex
from datetime import datetime, timezone

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pptx import Presentation

from orca.duel import compare_files
from orca.evidence import format_evidence, reference_commands
from orca.exposure import analyze_exposure, exposure_difference
from orca.extraction import ExifTool, run_command
from orca.main import create_app
from orca.metadata import findings, normalize
from orca.timeline import build_timeline, parse_date
from conftest import AUTHOR, complete, upload

SHA = "c" * 64
CONTEXT = {"created": "2024-02-29T12:00:00+00:00", "engine": {"name": "ExifTool", "version": "13.59"}}


def document(raw, name="documento.pdf", sha=SHA):
    normalized = normalize(raw)
    return {"id": name, "name": name, "sha256": sha, "format": "PDF", "size": 12,
            "raw": raw, "normalized": normalized, "findings": findings(normalized)}


def items(exposure):
    return [item for category in exposure["categories"] for item in category["items"]]


def by_field(duel):
    return {row["field"]: row for row in duel["rows"]}


def detail(base, row):
    return f"{base}/files/{row['id']}"


def modify_image(sample, path, *options):
    path.write_bytes(sample.read_bytes())
    engine = ExifTool(15)
    code, output, errors = run_command([*engine.command, "-overwrite_original", *options, str(path.resolve())],
                                       15, env=engine.env)
    assert code == 0, (output, errors)
    return path


@pytest.mark.parametrize(("created", "modified", "seconds"), [
    ("2023:12:31 12:00:00Z", "2024-03-01T12:00:00+00:00", 61 * 86400),
    ("2024-02-29T12:00:00+02:00", "2024-02-29T10:00:00Z", 0),
    ("D:20240229120000-05'00'", "2024:02:29 16:59:59Z", -1),
    ("2024-01-01T00:00:00.125Z", "2024-01-01T00:00:00.250Z", .125),
])
def test_timeline_intervals(created, modified, seconds):
    timeline = build_timeline({"PDF:CreationDate": created, "PDF:ModifyDate": modified})
    assert timeline["interval"]["seconds"] == seconds
    assert timeline["interval"]["created_sources"] == [["PDF:CreationDate"]]
    assert any("anterior" in text for text in timeline["warnings"]) == (seconds < 0)
    events = timeline["groups"]["instant"]
    assert events[0]["utc"] <= events[1]["utc"]
    assert {event["original"] for event in events} == {created, modified}


@pytest.mark.parametrize(("value", "normalized", "timezone_value", "precision"), [
    ("2024:02:29 12:00:00", "2024-02-29T12:00:00", None, "second"),
    ("2024-02", "2024-02", None, "month"),
    ("D:2024", "2024", None, "year"),
    ("D:2024Z", "2024", "+00:00", "year"),
    ("2024:02-29", None, None, None),
    ("2024-02-29", "2024-02-29", None, "day"),
    ("2024-02-29T12:00:00-00:00", "2024-02-29T12:00:00", None, "second"),
    ("2021-02-29T04:00:00Z", None, None, "second"),
    ("0000:00:00 00:00:00", None, None, "second"),
    ("fecha imposible", None, None, None),
    ("2400-04-30", "2400-04-30", None, "day"),
])
def test_date_precision_and_invalid_values(value, normalized, timezone_value, precision):
    result = parse_date(value)
    assert (result["normalized"], result["timezone"], result["precision"]) == (normalized, timezone_value, precision)
    assert result["utc"] is None
    if normalized is None or "2400" in value or precision == "second":
        assert result["warnings"]


def test_timeline_duplicate_conflict_local_and_filesystem_sources():
    created = "2024:02:29 14:00:00Z"
    raw = {
        "PDF:CreationDate": created, "XMP:XMP-xmp:CreateDate": created,
        "PDF:ModifyDate": "2024:03:01 14:00:00Z",
        "File:System:FileModifyDate": "1999:01:01 12:00:00Z",
        "EXIF:ExifIFD:DateTimeOriginal": "2024:02:29 10:00:00",
        "EXIF:ExifIFD:OffsetTimeOriginal": "-04:00",
        "IPTC:DateCreated": "2024:02:29",
        "XMP:XMP-xmp:MetadataDate": "2024:02:28 12:00:00",
    }
    result = build_timeline(raw)
    assert not result["warnings"]
    assert result["interval"]["seconds"] == 86400
    assert len(result["interval"]["created_sources"]) == 2
    assert result["groups"]["instant"][0]["origin"] == "filesystem"
    event = next(item for item in result["groups"]["instant"] if item["kind"] == "captured")
    assert event["utc"].endswith("14:00:00+00:00")
    assert event["timezone_source"] == ["EXIF:ExifIFD:OffsetTimeOriginal"]
    assert result["groups"]["local"][0]["utc"] is None
    assert result["groups"]["partial"][0]["precision"] == "day"
    result = build_timeline({**raw, "XMP:XMP-xmp:ModifyDate": "2024:04:01 14:00:00Z"})
    assert result["interval"] is None and len(result["warnings"]) == 2
    assert not any(build_timeline({})["groups"].values())


def test_semantic_duel_preserves_roles_originals_and_exclusive_fields():
    a = document({
        "PDF:Author": "Ada", "PDF:Creator": "Orca Studio", "XML:LastModifiedBy": "Teo",
        "PDF:CreationDate": "2024:02:29 12:00:00+02:00",
        "XMP:XMP-dc:Subject": ["uno", "dos"], "PDF:Title": "", "XMP:XMP-dc:Description": None,
        "XMP:XMP-xmpMM:DocumentID": "urn:uuid:00000000-0000-0000-0000-000000000001",
        "XMP:XMP-dc:Custom": ["a", "b"],
    }, "a.pdf")
    b = document({
        "XMP:XMP-pdf:Author": "Ada", "XMP:XMP-xmp:CreatorTool": "Orca Studio",
        "XMP:XMP-dc:Creator": ["Teo"], "XMP:XMP-xmp:CreateDate": "2024-02-29T10:00:00Z",
        "IPTC:Keywords": ["dos", "uno"], "PDF:Title": None,
        "XMP:XMP-dc:Custom": ["b", "a"], "XMP:XMP-dc:Unique": {"part": 4},
    }, "b.pdf", sha="b" * 64)
    result = compare_files(a, b)
    rows = by_field(result)
    assert rows["sha256"]["status"] == "different"
    assert rows["author"]["status"] == "matching" and rows["author"]["b"][0]["source"] == ["XMP:XMP-pdf:Author"]
    assert rows["last_editor"]["status"] == "only_a" and rows["creator"]["status"] == "only_b"
    assert rows["creation_tool"]["status"] == "matching"
    assert rows["created"]["status"] == "matching" and rows["created"]["a"][0]["original"].endswith("+02:00")
    assert rows["title"]["status"] == "different"
    assert rows["title"]["a"][0]["original"] == "" and rows["title"]["b"][0]["original"] is None
    assert rows["comments"]["status"] == "only_a" and rows["comments"]["a"][0]["original"] is None
    assert rows["document_id"]["status"] == "only_a" and rows["document_id"]["b"] == []
    assert rows["tag:XMP:XMP-dc:Unique"]["status"] == "only_b"
    # XMP Dublin Core reports Subject while IPTC reports Keywords. Their roles remain distinct.
    assert rows["subject"]["status"] == "only_a" and rows["keywords"]["status"] == "only_b"
    assert rows["tag:XMP:XMP-dc:Custom"]["status"] == "different"
    swapped = by_field(compare_files(b, a))
    assert swapped["document_id"]["status"] == "only_b"


def test_duel_dates_without_zone_and_multivalue_exact_semantics():
    a = document({"PDF:CreationDate": "2025:02:28 10:00:00",
                  "PDF:Keywords": ["uno", "dos"], "PDF:Author": "Ada"})
    b = document({"PDF:CreationDate": "2025:02:28 10:00:00Z",
                  "IPTC:Keywords": ["dos", "uno"], "PDF:Author": "ada"}, "b.pdf")
    result = compare_files(a, b)
    rows = by_field(result)
    assert rows["sha256"]["status"] == "matching" and result["notes"][0].startswith("Los archivos tienen el mismo")
    assert rows["created"]["status"] == "not_comparable"
    assert rows["keywords"]["status"] == "matching"
    assert rows["keywords"]["a"][0]["original"] == ["uno", "dos"]
    assert rows["author"]["status"] == "different"
    assert by_field(compare_files(document({}, sha=None), b))["sha256"]["status"] == "not_comparable"


@pytest.mark.parametrize("value", [
    "Ada", "Árbol y conversación", "línea 1\nlínea 2\n```\n<script>",
    r"C:\Users\cuenta\Mi documento.docx", "/home/cuenta/Proyecto a/informe.pdf",
    "2024:02:29 11:12:13Z", "a" * 300, ["uno", "dos"], {"clave": "valor"}, None, "",
])
def test_evidence_formats_preserve_original(value):
    key = "PDF:Subject"
    record = document({key: value}, "O'Neil (prueba).pdf")
    evidence = format_evidence(record, CONTEXT, [key])
    structured = evidence["structured"]
    assert structured["raw_value"] == value
    assert structured["file"] == record["name"] and structured["sha256"] == SHA
    assert structured["source"] == [key] and structured["tag"] == "Subject"
    assert structured["engine_version"] == "13.59" and structured["analysis_date"] == CONTEXT["created"]
    assert evidence["value"] == (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
    fence = evidence["markdown"].splitlines()[2].removesuffix("text")
    assert evidence["markdown"].endswith(fence + "\n")
    assert fence not in evidence["value"]
    assert shlex.split(evidence["commands"]["posix"])[-1] == "./O'Neil (prueba).pdf"
    assert evidence["commands"]["powershell"].endswith(r"'.\O''Neil (prueba).pdf'")


def test_evidence_context_and_missing_fields():
    record = document({"PDF:Subject": "/home/ada/prueba.txt"}, sha=None)
    finding = next(value for value in record["findings"] if value["field"] == "username")
    result = format_evidence(record, CONTEXT, finding["source"], finding, "finding")["structured"]
    assert result["raw_value"] == "/home/ada/prueba.txt" and result["interpreted_value"] == "ada"
    assert "sha256" not in result and result["finding"]["description"] == finding["description"]
    with pytest.raises(KeyError):
        format_evidence(record, CONTEXT, ["PDF:Missing"])
    assert reference_commands("lote.zip / archivo.pdf", "PDF:Author") == {}
    assert reference_commands("archivo.pdf", "PDF:Author=unsafe") == {}
    assert reference_commands("archivo\n.pdf", "PDF:Author") == {}


def test_evidence_reference_command_reads_known_field(samples, tmp_path):
    fixture = tmp_path / "Prueba 'local'.pdf"
    fixture.write_bytes(samples["pdf"].read_bytes())
    engine = ExifTool(15)
    command = reference_commands(fixture.name, "PDF:Author")["posix"]
    code, output, errors = run_command([*engine.command, *shlex.split(command)[1:]], 15,
                                       env=engine.env, cwd=tmp_path)
    assert code == 0, errors
    assert json.loads(output)[0]["PDF:Author"] == AUTHOR


def test_exposure_categories_rules_and_deduplication():
    record = document({
        "PDF:Author": "Prueba", "XMP:XMP-pdf:Author": "Prueba", "XML:LastModifiedBy": "Editor",
        "PDF:Subject": "/home/ada/seminario.txt", "PDF:CreationDate": "2024:02:29 12:00:00Z",
        "EXIF:ExifIFD:Software": "Orca", "XML:AppVersion": "4.0",
        "XMP:XMP-xmpMM:DocumentID": "uuid:00000000-0000-0000-0000-000000000001",
        "EXIF:GPS:GPSLatitude": 19.25, "Composite:GPSLatitude": 19.25, "EXIF:GPS:GPSLongitude": -99.10,
        "XML:ComputerName": "WORKSTATION-A", "PDF:Unrelated": "ficticio",
    })
    result = analyze_exposure(record)
    assert {group["id"] for group in result["categories"]} == {
        "identity", "location", "environment", "history", "identifiers", "additional"}
    values = items(result)
    authors, = [item for item in values if item["field"] == "author"]
    assert authors["sources"] == [["PDF:Author"], ["XMP:XMP-pdf:Author"]]
    latitude, = [item for item in values if item["field"] == "latitude"]
    assert len(latitude["sources"]) == 2
    assert next(item for item in values if item["field"] == "document_id")["level"] == "informational"
    assert next(item for item in values if item["field"] == "username")["value"] == "ada"
    assert next(item for item in values if item["field"] == "computer")["value"] == "WORKSTATION-A"
    assert not any(item["value"] == "ficticio" for item in values)
    assert all(item["description"] and item["reason"] and item["rule"] and item["source"] for item in values)
    after = analyze_exposure(document({"XMP:XMP-pdf:Author": "Prueba", "PDF:Title": "Copia"}))
    delta = exposure_difference(result, after)
    assert delta["after"] == 2 and delta["retained"][0]["source"] == ["XMP:XMP-pdf:Author"]
    assert delta["added"][0]["value"] == "Copia" and delta["removed"]
    assert analyze_exposure(document({"PDF:Unrelated": "ficticio"})) == {"categories": [], "total": 0}


@pytest.fixture
def tool_samples(samples, tmp_path):
    created = datetime(2020, 2, 29, 15, 20, 30, tzinfo=timezone.utc)
    modified = datetime(2024, 2, 29, 15, 20, 30, tzinfo=timezone.utc)
    word = tmp_path / "cronología.docx"
    doc = Document(samples["docx"])
    doc.core_properties.created = created
    doc.core_properties.modified = modified
    doc.core_properties.last_modified_by = "Editora de prueba"
    doc.save(word)
    slides = tmp_path / "cronología.pptx"
    deck = Presentation(samples["pptx"])
    deck.core_properties.created = created
    deck.core_properties.modified = modified
    deck.save(slides)
    image = modify_image(samples["jpg"], tmp_path / "ubicación.jpg",
                         "-DateTimeOriginal=2024:02:29 10:20:30", "-OffsetTimeOriginal=-05:00",
                         "-GPSLatitude=19.25", "-GPSLatitudeRef=N", "-GPSLongitude=99.10", "-GPSLongitudeRef=W")
    return {"docx": word, "pptx": slides, "jpg": image, "pdf": samples["pdf"]}


def test_context_uses_real_exiftool_dates_for_priority_formats(client, tool_samples):
    base, session = complete(client, upload(client, [(path.name, path.read_bytes()) for path in tool_samples.values()]))
    expected = {"PDF": "2024-02-05T11:10:02+00:00", "JPEG": "2024-02-29T15:20:30+00:00",
                "DOCX": "2020-02-29T15:20:30+00:00", "PPTX": "2020-02-29T15:20:30+00:00"}
    for row in session["files"]:
        context = client.get(detail(base, row) + "/context").json()
        timeline = context["timeline"]
        event = next(item for item in timeline["groups"]["instant"] if item["utc"] == expected[row["format"]])
        assert event["source"] and event["original"] and event["origin"] == "internal"
        if row["format"] in {"DOCX", "PPTX"}:
            assert timeline["interval"]["seconds"] == 1461 * 86400


def test_duel_integration_validation_and_export_from_history(settings, tool_samples):
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        word, photo = tool_samples["docx"], tool_samples["jpg"]
        files = [(word.name, word.read_bytes()), ("duplicado.docx", word.read_bytes()), (photo.name, photo.read_bytes())]
        base, result = complete(client, upload(client, files, keep=True))
        first, second, third = result["files"]
        _, outside = complete(client, upload(client, [(photo.name, photo.read_bytes())]))
        wrong = {"file_a": first["id"], "file_b": outside["files"][0]["id"]}
        assert client.get(base + "/duel", params=wrong).status_code == 404
        assert client.get(base + "/duel", params={"file_a": first["id"], "file_b": first["id"]}).status_code == 422
        assert client.get(base + "/duel", params={"file_a": "bad-id", "file_b": second["id"]}).status_code == 422
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        assert not client.get(base).json()["temporary"]
        params = {"file_a": first["id"], "file_b": second["id"]}
        data = client.get(base + "/duel", params=params).json()
        assert data["summary"]["matching"] > 2 and by_field(data)["sha256"]["status"] == "matching"
        export = client.get(base + "/duel", params={**params, "download": "true"})
        assert "attachment" in export.headers["content-disposition"] and export.json() == data
        html = client.get(base + "/duel", params={**params, "download": "true", "format": "html"})
        assert "orca-duelo.html" in html.headers["content-disposition"]
        assert "Duelo de documentos" in html.text and AUTHOR in html.text and "XML:LastModifiedBy" in html.text
        params["file_b"] = third["id"]
        change = client.get(base + "/duel", params=params).json()
        assert by_field(change)["sha256"]["status"] == "different"
        assert by_field(change)["last_editor"]["status"] == "only_a"
        longitude = by_field(change)["longitude"]
        exif = next(item for item in longitude["b"] if item["source"] == ["EXIF:GPS:Copy1:GPSLongitude"])
        assert exif["original"] == 99.10 and exif["interpreted"] == -99.10
        assert exif["interpretation_source"] == ["EXIF:GPS:GPSLongitudeRef"]
        assert any("Solo el archivo B" in note for note in change["notes"])
        assert client.get(detail(base, first) + "/context").json()["exposure"]["total"] > 0
        assert word.read_bytes() == files[0][1]


def test_file_evidence_and_exposure_after_verified_cleanup(client, tool_samples):
    photo = tool_samples["jpg"]
    original = photo.read_bytes()
    base, result = complete(client, upload(client, [(photo.name, original)]))
    row, = result["files"]
    resource = detail(base, row)
    context = client.get(resource + "/context").json()
    values = items(context["exposure"])
    gps_index = next(i for i, item in enumerate(values) if item["field"] == "latitude")
    assert context["exposure_comparison"] is None
    gps = client.get(resource + "/evidence", params={"kind": "exposure", "index": gps_index}).json()
    assert gps["structured"]["raw_value"] == 19.25
    assert gps["structured"]["exposure"]["rule"] == "location.gps"
    evidence = client.get(resource + "/evidence", params={"tag": "EXIF:IFD0:Artist"}).json()
    assert evidence["value"] == AUTHOR and evidence["structured"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert client.get(resource + "/evidence", params={"kind": "finding"}).json()["structured"]["finding"]
    assert client.get(resource + "/evidence", params={"kind": "normalized"}).json()["structured"]["source"]
    assert client.get(resource + "/evidence", params={"tag": "absent"}).status_code == 404
    assert client.get(resource + "/evidence", params={"kind": "exposure", "index": 9999}).status_code == 404
    response = client.post(resource + "/clean")
    assert response.status_code == 200, response.text
    cleaned = response.json()
    change = cleaned["exposure_comparison"]
    assert change["before"] == context["exposure"]["total"]
    assert change["after"] < change["before"]
    assert any(item["field"] == "latitude" for item in change["removed"])
    assert not any(item["category"] == "location" for item in change["retained"])
    assert change == client.get(resource + "/context").json()["exposure_comparison"]
    assert photo.read_bytes() == original
    assert hashlib.sha256(client.get(resource + "/copy").content).hexdigest() == cleaned["after"]["sha256"]


def test_office_cleanup_keeps_registered_software_exposure(client, tool_samples):
    path = tool_samples["docx"]
    original = path.read_bytes()
    base, result = complete(client, upload(client, [(path.name, original)]))
    resource = detail(base, result["files"][0])
    response = client.post(resource + "/clean")
    assert response.status_code == 200, response.text
    delta = response.json()["exposure_comparison"]
    assert any(item["field"] == "creator" for item in delta["removed"])
    assert any(item["field"] == "software" for item in delta["retained"])
    assert path.read_bytes() == original


def test_duel_treats_incomplete_extraction_as_inconclusive():
    original = document({"PDF:Author": "Ada"})
    failed = {**document({}, "error.pdf"), "status": "error"}
    result = compare_files(original, failed)
    assert by_field(result)["author"]["status"] == "not_comparable"
    assert "extracción incompleta" in by_field(result)["author"]["explanation"]


def test_duel_uses_exif_hemisphere_without_changing_original_coordinates():
    latitude = "EXIF:GPS:GPSLatitude"
    reference = "EXIF:GPS:GPSLatitudeRef"
    south = document({"EXIF:GPS:Copy1:GPSLatitude": 19.25, reference: "S", "Composite:GPSLatitude": -19.25})
    signed = document({"XMP:XMP-exif:GPSLatitude": -19.25}, "signed.jpg")
    north = document({latitude: 19.25, reference: "N"}, "north.jpg")
    unsigned = document({latitude: 19.25}, "unsigned.jpg")

    matching = by_field(compare_files(south, signed))["latitude"]
    assert matching["status"] == "matching"
    assert matching["a"][0]["original"] == 19.25
    assert matching["a"][0]["interpreted"] == -19.25
    assert matching["a"][0]["interpretation_source"] == [reference]
    assert by_field(compare_files(north, south))["latitude"]["status"] == "different"
    assert by_field(compare_files(north, unsigned))["latitude"]["status"] == "not_comparable"
    ambiguous = document({latitude: 19.25, reference: "N", "EXIF:GPS:Copy1:GPSLatitudeRef": "S"})
    assert by_field(compare_files(north, ambiguous))["latitude"]["status"] == "not_comparable"


def test_timeline_distinguishes_status_flags_from_temporal_tags():
    raw = {
        "XML:LinksUpToDate": False,
        "XMP:XMP-app:LinksUpToDate": "2025-11-21T08:00:00Z",
        "XML:ReviewDate": "aprobado",
        "XML:EditTimestamp": 1234,
        "XML:CreationDate": "2025-02-30",
        "XML:LastModifiedDate": True,
        "XML:ModifyDate": "false",
        "XMP:XMP-custom:ReviewTimestamp": ["2025-02-28T18:00:00Z", "pendiente"],
    }
    result = build_timeline(raw)
    groups = result["groups"]
    assert [event["source"] for event in groups["instant"]] == [["XMP:XMP-custom:ReviewTimestamp", 0]]
    assert [event["source"] for event in groups["invalid"]] == [["XML:CreationDate"]]
    row = by_field(compare_files(document(raw), document({"XML:LinksUpToDate": True})))
    assert row["tag:XML:LinksUpToDate"]["status"] == "different"
    assert "temporal" not in row["tag:XML:LinksUpToDate"]["a"][0]
