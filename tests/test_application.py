import csv
import hashlib
import io
import stat
import struct
import sys
import zipfile
from dataclasses import replace
from threading import Event

import openpyxl
import pikepdf
import pytest
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image
from pptx import Presentation

from orca.config import MAX_BYTES
from orca.extraction import EngineError, ExifTool, run_command
from orca.main import create_app
from orca.metadata import properties
from orca.uploads import UploadError, consume, file_chunks
from conftest import AUTHOR, BODY, complete, upload, zip_bytes


def test_home_health_static_and_host(client):
    page = client.get("/")
    assert page.status_code == 200 and "Inspección de" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "grid-template-columns" in client.get("/static/app.css").text
    assert "createObjectURL" in client.get("/static/app.js").text
    engine = client.get("/api/health").json()
    assert engine["available"] and engine["name"] == "ExifTool" and engine["version"]
    assert client.get("/", headers={"host": "unrelated.example"}).status_code == 400
    assert client.post("/api/sessions", headers={"origin": "https://unrelated.example"}).status_code == 403


def test_missing_exiftool(settings, monkeypatch):
    monkeypatch.setenv("ORCA_EXIFTOOL", str(settings.data / "missing"))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        assert client.get("/api/health").json()["available"] is False
        response = upload(client, [("source.txt", b"sample")])
        assert response.status_code == 503 and "ExifTool" in response.json()["detail"]


@pytest.mark.parametrize(("suffix", "expected"), [
    ("pdf", "PDF"), ("docx", "DOCX"), ("pptx", "PPTX"), ("xlsx", "XLSX"),
    ("jpg", "JPEG"), ("png", "PNG"), ("webp", "WEBP"),
])
def test_extract_known_metadata(client, samples, suffix, expected):
    path = samples[suffix]
    base, session = complete(client, upload(client, [(path.name, path.read_bytes())]))
    row, = session["files"]
    assert row["format"] == expected and row["author"] == AUTHOR and row["status"] == "ok", row
    data = client.get(f"{base}/files/{row['id']}").json()
    authors = [item for item in data["normalized"]["identity"] if item["field"] == "author"]
    assert authors and any(item["value"] == AUTHOR for item in authors)
    for item in authors:
        raw = data["raw"]
        for segment in item["source"]:
            raw = raw[segment]
        assert raw == item["value"]
    assert data["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert data["findings"]
    if suffix == "pdf":
        assert any(item["field"] == "path" and item["value"] == "/home/ada/proyectos/orca.txt"
                   for item in data["findings"])
        assert any(item["field"] == "username" and item["value"] == "ada" for item in data["findings"])
        assert data["normalized"]["dates"][0]["field"] == "created"
        assert row["software"] == "Orca Fixtures"
    if suffix == "docx":
        assert any(item["field"] == "comments" and item["value"] == "Comentario de control"
                   for item in data["findings"])


def test_batch_zip_and_correlation(client, samples):
    arch = zip_bytes([("sub/case.pdf", samples["pdf"].read_bytes()), ("otro.docx", samples["docx"].read_bytes())])
    base, session = complete(client, upload(client, [("colección.zip", arch), ("direct.jpg", samples["jpg"].read_bytes())]))
    assert session["summary"]["count"] == 3
    assert session["summary"]["formats"] == {"PDF": 1, "DOCX": 1, "JPEG": 1}
    shared = next(item for item in session["summary"]["correlations"] if item["field"] == "author")
    assert shared["value"] == AUTHOR
    assert {item["name"] for item in shared["files"]} == {"colección.zip / sub/case.pdf", "colección.zip / otro.docx", "direct.jpg"}
    assert client.get(f"{base}/files/{shared['files'][0]['id']}").json()["name"] == "colección.zip / sub/case.pdf"


def test_exact_count_with_unknown_and_hidden_files(client):
    entries = [(f".hidden/{index}.unknown", b"inert") for index in range(100)]
    _, session = complete(client, upload(client, [("max.zip", zip_bytes(entries))]))
    assert session["summary"]["statuses"] == {"ok": 0, "empty": 0, "unsupported": 100, "error": 0}
    resp = upload(client, [("max.zip", zip_bytes(entries)), ("another.txt", b"inert")])
    assert resp.status_code == 413 and "100 archivos" in resp.text


def test_reject_more_than_100_direct_files(client):
    result = upload(client, [(f"{index}.unknown", b"inert") for index in range(101)])
    assert result.status_code == 413 and "100 archivos" in result.text


def test_reject_more_than_100_zip_files(client, samples):
    entries = [(f"{index}.jpg", samples["jpg"].read_bytes()) for index in range(101)]
    result = upload(client, [("many.zip", zip_bytes(entries))])
    assert result.status_code == 413 and "100 archivos" in result.text


def test_reject_oversized_zip(client, tmp_path):
    path = tmp_path / "large.zip"
    with path.open("wb") as output:
        output.truncate(MAX_BYTES + 1)
    with path.open("rb") as source:
        result = upload(client, [(path.name, source)])
    assert result.status_code == 413 and "100 000 000" in result.text


def test_reject_combined_direct_size(client, tmp_path):
    path = tmp_path / "portion.png"
    with path.open("wb") as output:
        output.truncate(MAX_BYTES // 2 + 1)
    with path.open("rb") as first, path.open("rb") as second:
        result = upload(client, [("first.png", first), ("second.png", second)])
    assert result.status_code == 413 and "individuales" in result.text


@pytest.mark.parametrize("name", ["../outside.jpg", "/absolute.jpg", "alpha/../beta.jpg",
                                  "C:/absolute.jpg", r"alpha\beta.jpg"])
def test_reject_unsafe_zip_paths(client, settings, name):
    result = upload(client, [("unsafe.zip", zip_bytes([(name, b"inert")]))])
    assert result.status_code == 422 and "ruta insegura" in result.text
    assert not list((settings.data / "temporary").iterdir())


def test_reject_symlinks_encryption_and_collisions(client):
    symlink = zipfile.ZipInfo("link.jpg")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    result = upload(client, [("link.zip", zip_bytes([(symlink, b"some/path")]))])
    assert result.status_code == 422 and "enlaces" in result.text
    data = bytearray(zip_bytes([("sealed.jpg", b"inert")]))
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, 6, 1)
    struct.pack_into("<H", data, central + 8, 1)
    result = upload(client, [("closed.zip", bytes(data))])
    assert result.status_code == 422 and "cifrados" in result.text
    data = zip_bytes([("a.jpg", b"inert"), ("A.jpg", b"inert")])
    result = upload(client, [("collision.zip", data)])
    assert result.status_code == 422 and "duplicadas" in result.text


def test_zip_byte_ratio_and_stream_limits(client, settings):
    archive = zip_bytes([("a.png", b"0" * 1_100_000)], zipfile.ZIP_DEFLATED)
    response = upload(client, [("packed.zip", archive)])
    assert response.status_code == 413 and "relación de compresión" in response.text
    with pytest.raises(UploadError, match="límite de bytes"):
        consume(file_chunks(io.BytesIO(b"one two three")), io.BytesIO(), 4)
    small = replace(settings, data=settings.data / "small", expanded_limit=10)
    with TestClient(create_app(small), base_url="http://127.0.0.1") as limited:
        response = upload(limited, [
            ("first.zip", zip_bytes([("a.bin", b"123456")])),
            ("second.zip", zip_bytes([("b.bin", b"123456")])),
        ])
        assert response.status_code == 413 and "descomprimido" in response.text


def test_actual_request_budget_without_content_length(settings):
    limited = replace(settings, upload_limit=40)
    with TestClient(create_app(limited), base_url="http://127.0.0.1") as client:
        chunks = iter([b"--files\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.jpg\"\r\n\r\n",
                       b"0" * 520_000, b"\r\n--files--\r\n"])
        response = client.post("/api/sessions", headers={"content-type": "multipart/form-data; boundary=files"},
                               content=chunks)
        assert response.status_code == 413 and "límite total" in response.text
        assert not list((limited.data / "temporary").iterdir())


def test_partial_errors_and_spoofed_extensions(client, samples):
    malformed = b"%PDF-1.7\nEntrada truncada\n"
    response = upload(client, [
        ("disguise.pdf", samples["png"].read_bytes()), ("damaged.pdf", malformed),
        ("damaged.zip", b"garbage"), ("works.jpg", samples["jpg"].read_bytes()),
    ])
    _, session = complete(client, response)
    files = {item["name"]: item for item in session["files"]}
    assert files["works.jpg"]["author"] == AUTHOR
    assert files["disguise.pdf"]["status"] == "unsupported"
    assert files["damaged.zip"]["status"] == "error"
    assert files["damaged.pdf"]["status"] == "error" and files["damaged.pdf"]["errors"]


def test_empty_metadata(client):
    contents = io.BytesIO()
    Image.new("RGB", (4, 3)).save(contents, "JPEG")
    _, result = complete(client, upload(client, [("blank.jpg", contents.getvalue())]))
    assert result["summary"]["statuses"]["empty"] == 1
    assert result["summary"]["properties"] > 0


def test_history_restart_exports_and_remove(settings, samples):
    name = "=fixture<svg>.jpg"
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        base, result = complete(client, upload(client, [(name, samples["jpg"].read_bytes())], keep=True))
        row, = result["files"]
        _, ephemeral = complete(client, upload(client, [("temp.jpg", samples["jpg"].read_bytes())]))
        assert client.get("/api/history").json()[0]["id"] == result["id"]
    assert not (settings.data / "temporary").exists()
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        assert client.get(f"/api/sessions/{ephemeral['id']}").status_code == 404
        restored = client.get(base).json()
        assert not restored["temporary"] and restored["saved"] and restored["files"] == [row]
        assert client.get(f"{base}/files/{row['id']}").json()["normalized"]["identity"][0]["value"] == AUTHOR
        assert client.post(f"{base}/files/{row['id']}/clean").status_code == 410
        response = client.get(base + "/export/json")
        report = response.json()
        assert report["created"] == restored["created"] and report["engine"]["name"] == "ExifTool"
        assert report["files"][0]["raw"] and report["files"][0]["findings"]
        csv_response = client.get(base + "/export/csv")
        csv_rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
        assert csv_rows[0]["archivo"] == "'" + name
        assert any(item["campo"] == "author" and item["valor"] == AUTHOR for item in csv_rows)
        html = client.get(base + "/export/html")
        assert "attachment" in html.headers["content-disposition"]
        assert name not in html.text and "=fixture&lt;svg&gt;.jpg" in html.text and AUTHOR in html.text
        assert client.delete("/api/history").status_code == 204
        assert client.get("/api/history").json() == []
        assert client.get(base).status_code == 404


@pytest.mark.parametrize("suffix", ["jpg", "png", "pdf", "docx", "pptx", "xlsx"])
def test_cleanup_reinspection_and_content(client, settings, samples, suffix):
    path = samples[suffix]
    original = path.read_bytes()
    base, result = complete(client, upload(client, [(path.name, original)]))
    row, = result["files"]
    resource = f"{base}/files/{row['id']}"
    before = client.get(resource).json()
    action = client.post(resource + "/clean")
    assert action.status_code == 200, action.text
    cleaned = action.json()
    assert cleaned["after"]["status"] in {"ok", "empty"}
    assert cleaned["after"]["sha256"] != before["sha256"]
    assert not any(item["value"] == AUTHOR for item in properties(cleaned["after"]["normalized"]))
    assert any(item["before"] == AUTHOR and not item["technical"] for item in cleaned["comparison"]["removed"])
    assert any(item["technical"] for item in cleaned["comparison"]["retained"])
    copy = client.get(resource + "/copy")
    assert copy.status_code == 200 and "attachment" in copy.headers["content-disposition"]
    assert hashlib.sha256(copy.content).hexdigest() == cleaned["after"]["sha256"]
    assert client.get(resource).json() == before and path.read_bytes() == original
    assert any(item.read_bytes() == original for item in (settings.data / "temporary" / result["id"]).iterdir())
    assert client.post(resource + "/clean").json() == cleaned
    stream = io.BytesIO(copy.content)
    if suffix in {"jpg", "png"}:
        with Image.open(stream) as after, Image.open(path) as image:
            assert after.tobytes() == image.tobytes()
            assert after.getexif().get(274) == image.getexif().get(274)
    elif suffix == "pdf":
        with pikepdf.Pdf.open(stream) as pdf:
            assert len(pdf.pages) == 1
            assert pdf.pages[0].obj.Contents.read_bytes() == b"1 0 0 rg 20 20 100 80 re f"
            assert "/Info" not in pdf.trailer
        assert AUTHOR.encode() not in copy.content
    elif suffix == "docx":
        assert Document(stream).paragraphs[0].text == BODY
    elif suffix == "pptx":
        assert Presentation(stream).slides[0].shapes.title.text == BODY
    else:
        assert openpyxl.load_workbook(stream).active["A1"].value == BODY


def test_refuses_signed_pdf_and_office(client, samples):
    signed = io.BytesIO()
    with pikepdf.Pdf.open(samples["pdf"]) as pdf:
        pdf.Root.Perms = pikepdf.Dictionary()
        pdf.save(signed)
    with zipfile.ZipFile(samples["docx"]) as archive:
        entries = [(item, archive.read(item)) for item in archive.namelist()]
    office = zip_bytes([*entries, ("_xmlsignatures/sig1.xml", b"<Signature/>")])
    base, result = complete(client, upload(client, [("signed.pdf", signed.getvalue()), ("signed.docx", office)]))
    for row in result["files"]:
        action = client.post(f"{base}/files/{row['id']}/clean")
        assert action.status_code == 422 and "firma" in action.json()["detail"]
        assert client.get(f"{base}/files/{row['id']}/copy").status_code == 404


def test_progress_is_real_and_server_responds(settings, samples):
    ready, release = Event(), Event()
    real = ExifTool(15)

    class HeldEngine:
        info = real.info

        def extract(self, path):
            ready.set()
            assert release.wait(10)
            return real.extract(path)

    with TestClient(create_app(settings, extractor=HeldEngine()), base_url="http://127.0.0.1") as client:
        files = [("single.jpg", samples["jpg"].read_bytes())]
        try:
            response = upload(client, files)
            assert response.status_code == 202 and ready.wait(5)
            result = client.get("/api/sessions/" + response.json()["id"]).json()
            assert result["state"] == "processing" and result["processed"] == 0
            assert result["total"] == 1 and result["current"] == "single.jpg"
            assert client.get("/api/health").json()["available"]
            assert upload(client, files).status_code == 409
        finally:
            release.set()
        complete(client, response)


def test_subprocess_limits():
    with pytest.raises(EngineError, match="tiempo permitido"):
        run_command([sys.executable, "-c", "import time; time.sleep(4)"], .05)
    with pytest.raises(EngineError, match="salida"):
        run_command([sys.executable, "-c", "print('x' * 100000)"], 5, limit=256)


def test_temporary_copies_expire_but_history_survives(settings, samples):
    duration = replace(settings, ttl=1)
    with TestClient(create_app(duration), base_url="http://127.0.0.1") as client:
        base, result = complete(client, upload(client, [("photo.jpg", samples["jpg"].read_bytes())], keep=True))
        row, = result["files"]
        resource = f"{base}/files/{row['id']}"
        assert client.post(resource + "/clean").status_code == 200
        job = client.app.state.service.active[result["id"]]
        with client.app.state.service.guard:
            job.expires = 0
        assert client.get(resource + "/copy").status_code == 404
        assert not job.work.exists()
        assert not client.get(base).json()["temporary"]
        assert client.delete(base).status_code == 204 and client.get(base).status_code == 404


def test_corrupt_member_leaves_valid_zip_siblings_readable(client, samples):
    marker = b"prueba de integridad"
    good = samples["jpg"].read_bytes()
    contents = zip_bytes([("damaged.txt", marker), ("valid.jpg", good)])
    contents = contents.replace(marker, b"X" * len(marker), 1)
    _, session = complete(client, upload(client, [("mixed.zip", contents)]))
    first, second = session["files"]
    assert first["status"] == "error" and not first["sha256"]
    assert second["status"] == "ok" and second["sha256"] == hashlib.sha256(good).hexdigest()


def test_pdf_cleanup_removes_catalog_xmp(client):
    original = io.BytesIO()
    value = "Descripción XMP sintética"
    with pikepdf.new() as pdf:
        pdf.add_blank_page()
        with pdf.open_metadata(set_pikepdf_as_editor=False) as metadata:
            metadata["dc:description"] = value
        pdf.save(original)
    base, result = complete(client, upload(client, [("xmp.pdf", original.getvalue())]))
    row, = result["files"]
    resource = f"{base}/files/{row['id']}"
    before = client.get(resource).json()
    assert any(item["value"] == value for item in properties(before["normalized"]))
    response = client.post(resource + "/clean")
    assert response.status_code == 200, response.text
    assert any(item["before"] == value for item in response.json()["comparison"]["removed"])
    with pikepdf.Pdf.open(io.BytesIO(client.get(resource + "/copy").content)) as pdf:
        assert "/Metadata" not in pdf.Root and len(pdf.pages) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="resource limits use POSIX/Linux")
def test_subprocess_enforces_linux_resource_limits():
    code, output, _ = run_command([
        sys.executable, "-c",
        "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0]);"
        "print(resource.getrlimit(resource.RLIMIT_FSIZE)[0]);"
        "print(resource.getrlimit(resource.RLIMIT_CORE)[0])",
    ], 5)
    assert code == 0
    assert output.splitlines() == [b"805306368", b"105000000", b"0"]
