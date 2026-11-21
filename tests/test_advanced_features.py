import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from orca.config import Settings
from orca.content import compare_content
from orca.duel import compare_files
from orca.hashes import HashError, compare_digests, hash_chunks, validate_hex_digest
from orca.main import create_app
from orca.manifests import ManifestError, build_manifest, normalize_relative_path, verify_manifest
from conftest import complete, upload, zip_bytes


def _png_bytes(color=(40, 110, 200), size=(24, 24)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_pptx(path: Path, slides_spec: list[dict], author: str = "Autor A",
               title_prop: str = "Presentación", last_modified_by: str | None = None) -> Path:
    prs = Presentation()
    prs.core_properties.author = author
    prs.core_properties.title = title_prop
    if last_modified_by is not None:
        prs.core_properties.last_modified_by = last_modified_by
    for spec in slides_spec:
        layout_idx = spec.get("layout", 1)
        slide = prs.slides.add_slide(prs.slide_layouts[layout_idx])
        if slide.shapes.title is not None and "title" in spec:
            slide.shapes.title.text = spec["title"]
        if "body" in spec and len(slide.placeholders) > 1:
            slide.placeholders[1].text = spec["body"]
        elif "body" in spec:
            box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(2))
            box.text_frame.text = spec["body"]
        if "image" in spec:
            slide.shapes.add_picture(io.BytesIO(spec["image"]), Inches(1), Inches(3.5), width=Inches(1.5))
        if "notes" in spec:
            slide.notes_slide.notes_text_frame.text = spec["notes"]
    prs.save(path)
    return path


# --- Phase 1: Hash Lab & Known Vectors ---

@pytest.mark.parametrize(("payload", "expected"), [
    (b"", {
        "MD5": "d41d8cd98f00b204e9800998ecf8427e",
        "SHA-1": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
        "SHA-256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "SHA-512": "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e",
    }),
    (b"abc", {
        "MD5": "900150983cd24fb0d6963f7d28e17f72",
        "SHA-1": "a9993e364706816aba3e25717850c26c9cd0d89d",
        "SHA-256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "SHA-512": "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
    }),
])
def test_hash_known_vectors_and_empty_files(client, payload, expected):
    # Raw streaming endpoint (0-byte and multi-algo in single pass)
    resp = client.post(
        "/api/hashes/compute?algorithms=MD5,SHA-1,SHA-256,SHA-512",
        headers={"Content-Type": "application/octet-stream", "X-Orca-Filename": "vector.bin"},
        content=payload,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["size"] == len(payload)
    by_algo = {item["algorithm"]: item for item in body["results"]}
    for algo, digest in expected.items():
        assert by_algo[algo]["hash"] == digest
    assert by_algo["MD5"]["legacy"] is True and by_algo["MD5"]["warning"]
    assert by_algo["SHA-1"]["legacy"] is True and by_algo["SHA-1"]["warning"]
    assert by_algo["SHA-256"]["legacy"] is False and by_algo["SHA-256"]["warning"] is None
    assert by_algo["SHA-512"]["legacy"] is False and by_algo["SHA-512"]["warning"] is None


def test_hash_rename_preserves_sha256_and_byte_edit_changes_it(client):
    content = b"Contenido de prueba para integridad en Orca"
    r1 = client.post(
        "/api/hashes/compute",
        headers={"Content-Type": "application/octet-stream", "X-Orca-Filename": "original.txt"},
        content=content,
    ).json()
    r2 = client.post(
        "/api/hashes/compute",
        headers={"Content-Type": "application/octet-stream", "X-Orca-Filename": "renombrado-2026.txt"},
        content=content,
    ).json()
    r3 = client.post(
        "/api/hashes/compute",
        headers={"Content-Type": "application/octet-stream", "X-Orca-Filename": "original.txt"},
        content=content + b"!",
    ).json()
    assert r1["results"][0]["algorithm"] == "SHA-256"
    assert r1["results"][0]["hash"] == r2["results"][0]["hash"]
    assert r1["results"][0]["hash"] != r3["results"][0]["hash"]


def test_hash_comparator_case_whitespace_and_validation(client):
    digest = hashlib.sha256(b"orca").hexdigest()
    res = client.post("/api/hashes/compare", json={
        "algorithm": "SHA-256",
        "hash_a": f"  {digest.upper()}  ",
        "hash_b": f"\n{digest.lower()}\t",
    }).json()
    assert res["valid"] is True and res["match"] is True
    assert res["message"] == "Los hashes coinciden."

    diff = client.post("/api/hashes/compare", json={
        "algorithm": "SHA-256",
        "hash_a": digest,
        "hash_b": "0" * 64,
    }).json()
    assert diff["valid"] is True and diff["match"] is False
    assert diff["message"] == "Los hashes son diferentes."

    invalid = client.post("/api/hashes/compare", json={
        "algorithm": "SHA-256",
        "hash_a": "not-hex-at-all",
        "hash_b": "abcd",
    }).json()
    assert invalid["valid"] is False and invalid["match"] is None
    assert "hash_a" in invalid["errors"] and "hash_b" in invalid["errors"]


# --- Phase 2: Hash History & Re-verification ---

def test_hash_history_persistence_search_export_and_reverify(settings):
    data = b"Documento de control criptografico"
    sha = hashlib.sha256(data).hexdigest()

    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        # Save via compute?save=1 (including formula-like name for CSV safety)
        comp = client.post(
            "/api/hashes/compute?algorithms=SHA-256,MD5&save=1",
            headers={"Content-Type": "application/octet-stream", "X-Orca-Filename": "=reporte.pdf"},
            content=data,
        )
        assert comp.status_code == 200
        saved = comp.json()["saved_records"]
        assert len(saved) == 2

    # Reload app to confirm SQLite persistence (no original file stored)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        all_rows = client.get("/api/hashes/history").json()
        assert len(all_rows) == 2
        sha_rec = next(r for r in all_rows if r["algorithm"] == "SHA-256")
        assert sha_rec["hash"] == sha and sha_rec["size"] == len(data)

        # Filter by algorithm and search by name
        filtered = client.get("/api/hashes/history", params={"algorithm": "MD5", "q": "reporte"}).json()
        assert len(filtered) == 1 and filtered[0]["algorithm"] == "MD5"

        # CSV export protects formula cells
        csv_resp = client.get("/api/hashes/history/export/csv")
        assert csv_resp.status_code == 200
        csv_rows = list(csv.DictReader(io.StringIO(csv_resp.content.decode("utf-8-sig"))))
        assert csv_rows[0]["archivo"] == "'=reporte.pdf"

        # Verify again with renamed file (same bytes -> verified, name_changed=True)
        ver_same = client.post(
            f"/api/hashes/history/{sha_rec['id']}/verify",
            headers={"Content-Type": "application/octet-stream", "X-Orca-Filename": "otro-nombre.pdf"},
            content=data,
        ).json()
        assert ver_same["status"] == "verified" and ver_same["match"] is True
        assert ver_same["name_changed"] is True

        # Verify again with modified bytes (same filename -> changed!)
        ver_diff = client.post(
            f"/api/hashes/history/{sha_rec['id']}/verify",
            headers={"Content-Type": "application/octet-stream", "X-Orca-Filename": "=reporte.pdf"},
            content=data + b" alterado",
        ).json()
        assert ver_diff["status"] == "changed" and ver_diff["match"] is False
        assert ver_diff["message"] == "El archivo ha cambiado."

        # Delete individual & clear all
        assert client.delete(f"/api/hashes/history/{sha_rec['id']}").status_code == 204
        assert len(client.get("/api/hashes/history").json()) == 1
        assert client.delete("/api/hashes/history").status_code == 204
        assert client.get("/api/hashes/history").json() == []


# --- Phase 3 & 4: Deep PPTX Comparison, Similarity & Reports ---

def test_pptx_equivalent_content_different_metadata_and_cleanup_hash_change(client, tmp_path):
    slides = [
        {"title": "Introducción", "body": "La normalización elimina las redundancias."},
        {"title": "Conclusión", "body": "Resultados reproducibles."},
    ]
    a_path = _make_pptx(tmp_path / "deck-a.pptx", slides, author="Autor Uno", last_modified_by="Editor A")
    b_path = _make_pptx(tmp_path / "deck-b.pptx", slides, author="Autor Dos", last_modified_by="Editor B")

    base, session = complete(client, upload(client, [
        (a_path.name, a_path.read_bytes()),
        (b_path.name, b_path.read_bytes()),
    ]))
    row_a, row_b = session["files"]
    duel = client.get(f"{base}/duel", params={"file_a": row_a["id"], "file_b": row_b["id"]}).json()

    # Deterministic check: repeat call yields identical payload
    duel_repeat = client.get(f"{base}/duel", params={"file_a": row_a["id"], "file_b": row_b["id"]}).json()
    assert duel == duel_repeat

    assert duel["binary_identity"]["identical_bytes"] is False
    assert duel["content_comparison"]["status"] == "analyzed"
    assert duel["content_comparison"]["summary"]["substantive_identical"] is True
    assert duel["content_comparison"]["summary"]["identical_slides"] == 2
    sim = duel["similarity"]
    assert sim["overall_available"] is True
    assert sim["dimensions"]["text"]["percentage"] == 100.0
    assert sim["dimensions"]["structure"]["percentage"] == 100.0
    assert sim["dimensions"]["media"]["available"] is False
    assert sim["dimensions"]["metadata"]["percentage"] < 100.0
    assert sim["overall"] < 100.0
    assert sim["metadata_only_change"] is True

    # Cleaning metadata changes SHA-256 while preserving visible content
    clean_resp = client.post(f"{base}/files/{row_a['id']}/clean").json()
    assert clean_resp["after"]["sha256"] != row_a["sha256"]
    cleaned_bytes = client.get(f"{base}/files/{row_a['id']}/copy").content
    base2, sess2 = complete(client, upload(client, [
        ("orig.pptx", a_path.read_bytes()),
        ("clean.pptx", cleaned_bytes),
    ]))
    duel_clean = client.get(f"{base2}/duel", params={
        "file_a": sess2["files"][0]["id"],
        "file_b": sess2["files"][1]["id"],
    }).json()
    assert duel_clean["binary_identity"]["identical_bytes"] is False
    assert duel_clean["content_comparison"]["summary"]["substantive_identical"] is True
    assert duel_clean["similarity"]["dimensions"]["text"]["percentage"] == 100.0


def test_pptx_modified_reordered_added_removed_and_media_changes(client, tmp_path):
    img_blue = _png_bytes((20, 80, 210))
    img_red = _png_bytes((200, 40, 60))

    a_path = _make_pptx(tmp_path / "v1.pptx", [
        {"title": "Portada", "body": "Proyecto Orca", "image": img_blue},
        {"title": "Tema Dos", "body": "Bloque que se moverá."},
        {"title": "Tema Tres", "body": "La normalización elimina las redundancias."},
        {"title": "Anexo financiero", "body": "Resumen histórico de costes del primer trimestre."},
    ], author="Ada")

    b_path = _make_pptx(tmp_path / "v2.pptx", [
        {"title": "Portada", "body": "Proyecto Orca", "image": img_red},  # same text, replaced image!
        {"title": "Tema Tres", "body": "La normalización reduce las redundancias."},  # modified + moved
        {"title": "Tema Dos", "body": "Bloque que se moverá."},  # reordered (2 -> 3)
        {"title": "Calendario de despliegue", "body": "Hitos técnicos previstos para producción."},
    ], author="Ada")

    base, session = complete(client, upload(client, [
        (a_path.name, a_path.read_bytes()),
        (b_path.name, b_path.read_bytes()),
    ]))
    row_a, row_b = session["files"]
    duel = client.get(f"{base}/duel", params={"file_a": row_a["id"], "file_b": row_b["id"]}).json()

    cc = duel["content_comparison"]
    assert cc["status"] == "analyzed"
    summary = cc["summary"]
    assert summary["slide_count_a"] == 4 and summary["slide_count_b"] == 4
    assert summary["modified_slides"] == 2
    assert summary["reordered_slides"] == 1
    assert summary["added_slides"] == 1
    assert summary["removed_slides"] == 1
    assert summary["media_replaced_on_slides"] == 1

    # Verify modified text diff explicitly identifies removed and added words
    mod_text = next(s for s in cc["slides"] if s["slide_a"] == 3 and s["status"] == "modified")
    assert "elimina" in mod_text["text_diff"]["removed_fragments"]
    assert "reduce" in mod_text["text_diff"]["added_fragments"]

    # Verify reordered slide detected
    reordered = next(s for s in cc["slides"] if s["status"] == "reordered")
    assert reordered["slide_a"] == 2 and reordered["slide_b"] == 3

    # Verify slide 1 has identical text but replaced image
    img_slide = next(s for s in cc["slides"] if s["slide_a"] == 1)
    assert img_slide["text_identical"] is True
    assert img_slide["media_identical"] is False
    assert img_slide["media_diff"]["replaced"] is True

    # Verify JSON and HTML exports include deep comparison, similarity & diffs
    json_export = client.get(f"{base}/duel", params={
        "file_a": row_a["id"], "file_b": row_b["id"], "download": "true",
    })
    assert json_export.status_code == 200 and json_export.json() == duel

    html_export = client.get(f"{base}/duel", params={
        "file_a": row_a["id"], "file_b": row_b["id"], "download": "true", "format": "html",
    })
    assert html_export.status_code == 200
    assert "Similitud documental" in html_export.text
    assert "<del>elimina</del>" in html_export.text
    assert "<ins>reduce</ins>" in html_export.text
    assert "Metodología y limitaciones" in html_export.text


def test_pptx_same_text_different_images_warns_explicitly(client, tmp_path):
    a_path = _make_pptx(tmp_path / "img-a.pptx", [
        {"title": "Gráfico", "body": "Mismo texto exacto", "image": _png_bytes((10, 20, 30))},
    ])
    b_path = _make_pptx(tmp_path / "img-b.pptx", [
        {"title": "Gráfico", "body": "Mismo texto exacto", "image": _png_bytes((220, 210, 200))},
    ])
    base, session = complete(client, upload(client, [
        (a_path.name, a_path.read_bytes()),
        (b_path.name, b_path.read_bytes()),
    ]))
    duel = client.get(f"{base}/duel", params={
        "file_a": session["files"][0]["id"],
        "file_b": session["files"][1]["id"],
    }).json()
    sim = duel["similarity"]
    assert sim["dimensions"]["text"]["percentage"] == 100.0
    assert sim["dimensions"]["media"]["percentage"] == 0.0
    assert sim["overall"] < 85.0
    assert any("mismo texto" in w.lower() for w in sim["warnings"])


def test_unsupported_formats_and_corrupt_files_do_not_emit_misleading_similarity(client, samples, tmp_path):
    # Unsupported format pair (PDF vs PDF)
    base, session = complete(client, upload(client, [
        ("a.pdf", samples["pdf"].read_bytes()),
        ("b.pdf", samples["pdf"].read_bytes()),
    ]))
    duel = client.get(f"{base}/duel", params={
        "file_a": session["files"][0]["id"],
        "file_b": session["files"][1]["id"],
    }).json()
    assert duel["content_comparison"]["status"] == "unsupported"
    assert duel["similarity"]["overall_available"] is False
    assert duel["similarity"]["overall"] is None

    # Corrupt PPTX directly to content analyzer produces controlled error, never crashes
    corrupt = tmp_path / "corrupt.pptx"
    corrupt.write_bytes(zip_bytes([("[Content_Types].xml", b"not xml"), ("ppt/presentation.xml", b"<bad")]))
    rec_a = {"format": "PPTX", "status": "ok", "raw": {}}
    rec_b = {"format": "PPTX", "status": "ok", "raw": {}}
    content_err = compare_content(rec_a, rec_b, path_a=corrupt, path_b=corrupt, settings=Settings(data=tmp_path / "d"))
    assert content_err["status"] == "error"
    assert content_err["metrics"]["text"]["available"] is False


# --- Phase 5: Integrity Manifests ---

@pytest.mark.parametrize("bad_path", [
    "../secret.txt",
    "a/../../b.txt",
    "/etc/passwd",
    "C:/Windows/win.ini",
    r"\\server\share\file.txt",
    "dir/file\x00.txt",
    "",
])
def test_manifest_rejects_unsafe_paths(bad_path):
    with pytest.raises(ManifestError):
        normalize_relative_path(bad_path)


def test_manifest_generate_verify_and_html_report(client):
    files_initial = [
        ("files", ("a.txt", b"archivo uno")),
        ("files", ("b.txt", b"archivo dos")),
        ("files", ("empty.txt", b"")),
    ]
    gen = client.post(
        "/api/manifests/generate",
        files=files_initial,
        data={"algorithm": "SHA-256", "paths": ["docs/a.txt", "docs/sub/b.txt", "empty.txt"]},
    )
    assert gen.status_code == 200, gen.text
    manifest = gen.json()
    assert manifest["version"] == 1
    assert manifest["algorithm"] == "SHA-256"
    assert [f["path"] for f in manifest["files"]] == ["docs/a.txt", "docs/sub/b.txt", "empty.txt"]
    assert manifest["files"][2]["size"] == 0
    assert manifest["files"][2]["hash"] == hashlib.sha256(b"").hexdigest()

    # Verify with: docs/a.txt unchanged, docs/sub/b.txt modified, empty.txt missing, extra.txt added
    verify_resp = client.post(
        "/api/manifests/verify",
        files=[
            ("files", ("a.txt", b"archivo uno")),
            ("files", ("b.txt", b"archivo dos MODIFICADO")),
            ("files", ("extra.txt", b"archivo adicional")),
        ],
        data={
            "manifest": json.dumps(manifest),
            "paths": ["docs/a.txt", "docs/sub/b.txt", "extra.txt"],
        },
    )
    assert verify_resp.status_code == 200, verify_resp.text
    report = verify_resp.json()
    assert report["status"] == "mismatch"
    assert report["summary"]["unchanged"] == 1
    assert report["summary"]["modified"] == 1
    assert report["summary"]["missing"] == 1
    assert report["summary"]["extra"] == 1
    assert report["summary"]["unreadable"] == 0

    by_path = {row["path"]: row for row in report["rows"]}
    assert by_path["docs/a.txt"]["status"] == "unchanged"
    assert by_path["docs/sub/b.txt"]["status"] == "modified"
    assert by_path["empty.txt"]["status"] == "missing"
    assert by_path["extra.txt"]["status"] == "extra"

    # Unreadable files handled explicitly via JSON verify endpoint
    unreadable_report = verify_manifest(manifest, [], unreadable_entries=[{"path": "docs/a.txt", "error": "Permiso denegado"}])
    assert unreadable_report["summary"]["unreadable"] == 1
    assert unreadable_report["summary"]["missing"] == 2

    # HTML report export
    html = client.post("/api/manifests/report", json=report)
    assert html.status_code == 200
    assert "Informe de verificación de manifiesto" in html.text
    assert "docs/sub/b.txt" in html.text
