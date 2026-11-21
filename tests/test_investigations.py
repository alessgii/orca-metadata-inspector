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
from orca.investigations import UUID_RE
from orca.main import create_app
from conftest import zip_bytes


def _make_pptx(path: Path, slides: list[tuple[str, str]], author: str = "Autora",
               img_color: tuple[int, int, int] | None = None) -> Path:
    prs = Presentation()
    prs.core_properties.author = author
    for title, body in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
        if img_color is not None:
            buf = io.BytesIO()
            Image.new("RGB", (20, 20), img_color).save(buf, format="PNG")
            slide.shapes.add_picture(io.BytesIO(buf.getvalue()), Inches(1), Inches(3.5), width=Inches(1.2))
    prs.save(path)
    return path


def test_create_validation_unique_uuids_and_identical_names(settings):
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        # Mandatory fields validation
        assert client.post("/api/investigations", json={"name": "", "responsible": "Ana"}).status_code == 422
        assert client.post("/api/investigations", json={"name": "Caso 1", "responsible": "   "}).status_code == 422

        # Identical name & responsible produce two distinct immutable UUIDs
        a = client.post("/api/investigations", json={
            "name": "Entrega Final", "responsible": "Carlos Pérez",
            "description": "Revisión inicial", "kind": "document_analysis", "classification": "confidential",
        })
        b = client.post("/api/investigations", json={
            "name": "Entrega Final", "responsible": "Carlos Pérez",
        })
        assert a.status_code == 201 and b.status_code == 201
        inv_a, inv_b = a.json(), b.json()
        assert UUID_RE.fullmatch(inv_a["uuid"]) and UUID_RE.fullmatch(inv_b["uuid"])
        assert inv_a["uuid"] != inv_b["uuid"]
        assert inv_a["status"] == "open"

        # Editing name/responsible does NOT change UUID
        edited = client.patch(f"/api/investigations/{inv_a['uuid']}", json={
            "name": "Entrega Final Revisada", "responsible": "Carlos A. Pérez",
        })
        assert edited.status_code == 200
        assert edited.json()["uuid"] == inv_a["uuid"]
        assert edited.json()["name"] == "Entrega Final Revisada"

    # Persistence across restart + search by UUID/name/responsible
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        all_items = client.get("/api/investigations").json()
        assert len(all_items) == 2
        by_uuid = client.get("/api/investigations", params={"q": inv_a["uuid"]}).json()
        assert len(by_uuid) == 1 and by_uuid[0]["uuid"] == inv_a["uuid"]
        assert inv_a["short_uuid"] != inv_b["short_uuid"]


def test_files_duplicates_comparisons_matrix_timelines_graph_notes_and_closure(settings, tmp_path):
    p1 = _make_pptx(tmp_path / "v1.pptx", [("Tema 1", "La normalización elimina redundancias.")], author="Autor A", img_color=(30, 80, 180))
    p2 = _make_pptx(tmp_path / "v2.pptx", [("Tema 1", "La normalización reduce redundancias.")], author="Autor B", img_color=(30, 80, 180))
    p1_bytes = p1.read_bytes()
    p2_bytes = p2.read_bytes()

    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        inv = client.post("/api/investigations", json={
            "name": "Auditoría PPTX", "responsible": "Dra. Sofía Ríos",
        }).json()
        base = f"/api/investigations/{inv['uuid']}"

        # Ingest p1, p2, and a duplicate copy of p1 with another name
        up = client.post(
            f"{base}/files",
            files=[
                ("files", ("entrega-v1.pptx", p1_bytes)),
                ("files", ("entrega-v2.pptx", p2_bytes)),
                ("files", ("copia-v1.pptx", p1_bytes)),
            ],
            data={"paths": ["lote/entrega-v1.pptx", "lote/entrega-v2.pptx", "respaldo/copia-v1.pptx"]},
        )
        assert up.status_code == 201, up.text
        added = up.json()["files"]
        assert len(added) == 3
        f1, f2, f3 = added
        assert f1["sha256"] == hashlib.sha256(p1_bytes).hexdigest()
        assert f3["duplicate_of_uuid"] == f1["uuid"]
        assert f3["duplicate_warning"] is not None
        assert f1["has_structure"] is True and f2["has_structure"] is True

        # Ensure original binary files are NOT kept in temporary workspace after ingestion
        temp_files = list((settings.data / "temporary").rglob("*.pptx"))
        assert temp_files == []

        # Verify hash on-demand (renamed file still verifies!)
        vh = client.post(
            f"{base}/files/{f1['uuid']}/verify-hash",
            headers={"Content-Type": "application/octet-stream", "X-Orca-Filename": "renombrado.pptx"},
            content=p1_bytes,
        ).json()
        assert vh["status"] == "verified" and vh["name_changed"] is True

        # Matrix shows instant binary identity + pending deep pair before comparison
        mat = client.get(f"{base}/matrix").json()
        assert mat["total_pairs"] == 3
        assert mat["pending_deep_pairs"] >= 1

        # Progressive batch computation + cached pair comparison (without original files on disk!)
        mat2 = client.post(f"{base}/matrix/compute", json={"max_pairs": 5}).json()
        assert mat2["computed_pairs"] >= 2
        comp = client.post(f"{base}/comparisons", json={"file_a_uuid": f1["uuid"], "file_b_uuid": f2["uuid"]}).json()
        assert comp["similarity_available"] is True
        assert comp["result"]["content_comparison"]["status"] == "analyzed"
        assert comp["result"]["binary_identity"]["identical_bytes"] is False

        # Document timeline & manual order + manual relation + graph clusters
        reordered = client.post(f"{base}/files/order", json={"file_uuids": [f2["uuid"], f1["uuid"], f3["uuid"]]}).json()
        assert [x["uuid"] for x in reordered] == [f2["uuid"], f1["uuid"], f3["uuid"]]
        doc_tl = client.get(f"{base}/timeline/documents", params={"sort": "manual"}).json()
        assert doc_tl["nodes"][0]["uuid"] == f2["uuid"]

        rel = client.post(f"{base}/relations", json={
            "source_file_uuid": f1["uuid"], "target_file_uuid": f2["uuid"],
            "relation_type": "manual_confirmed", "label": "Versión corregida",
        })
        assert rel.status_code == 201
        graph = client.get(f"{base}/graph").json()
        edge_types = {e["relation_type"] for e in graph["edges"]}
        assert "identical_hash" in edge_types
        assert "manual_confirmed" in edge_types
        assert len(graph["clusters"]) == 1 and len(graph["clusters"][0]) == 3

        # Notes with version history preserved on edit
        note = client.post(f"{base}/notes", json={
            "text": "Primera observación preliminar.", "target_type": "file", "target_uuid": f2["uuid"],
        }).json()
        assert note["version"] == 1 and len(note["versions"]) == 1
        updated_note = client.patch(f"{base}/notes/{note['uuid']}", json={
            "text": "Observación ampliada tras revisar diapositiva 1.",
        }).json()
        assert updated_note["version"] == 2 and len(updated_note["versions"]) == 2
        assert updated_note["versions"][1]["text"] == "Primera observación preliminar."

        # State transitions: pause -> resume -> close (requires summary & conclusion) -> block edits -> reopen (requires reason)
        assert client.post(f"{base}/state", json={"action": "pause"}).json()["status"] == "paused"
        assert client.post(f"{base}/state", json={"action": "resume"}).json()["status"] == "open"
        assert client.post(f"{base}/state", json={"action": "close", "summary": "", "conclusion": ""}).status_code == 422
        closed = client.post(f"{base}/state", json={
            "action": "close", "summary": "Se revisaron 3 archivos.", "conclusion": "v2 modifica texto de v1.",
        }).json()
        assert closed["status"] == "closed" and closed["closed_at"] is not None
        # Edits blocked when closed
        assert client.patch(base, json={"name": "No permitido"}).status_code == 409
        assert client.post(f"{base}/notes", json={"text": "Bloqueada"}).status_code == 409
        # Reopen requires explicit reason
        assert client.post(f"{base}/state", json={"action": "reopen", "reason": ""}).status_code == 422
        reopened = client.post(f"{base}/state", json={"action": "reopen", "reason": "Llegó nueva evidencia."}).json()
        assert reopened["status"] == "open"

        # Activity timeline contains full append-only audit trail
        activity = client.get(f"{base}/timeline/activity").json()
        ev_types = {e["event_type"] for e in activity["events"]}
        for expected in (
            "investigation_created", "file_added", "duplicate_detected",
            "analysis_started", "analysis_completed", "hash_verified",
            "comparison_executed", "relation_created", "note_created",
            "note_updated", "investigation_paused", "investigation_resumed",
            "investigation_closed", "investigation_reopened",
        ):
            assert expected in ev_types


def test_full_end_to_end_export_import_conflicts_copies_and_invalid_packages(tmp_path):
    student_settings = Settings(data=tmp_path / "student_data")
    prof_settings = Settings(data=tmp_path / "professor_data")
    pptx_a = _make_pptx(tmp_path / "a.pptx", [("Slide 1", "Contenido A original")], author="Estudiante 1")
    pptx_b = _make_pptx(tmp_path / "b.pptx", [("Slide 1", "Contenido A modificado")], author="Estudiante 1")

    # 1. Student creates, analyzes, compares, annotates, closes, and exports investigation
    with TestClient(create_app(student_settings), base_url="http://127.0.0.1") as student:
        inv = student.post("/api/investigations", json={
            "name": "Práctica Forense 1", "responsible": "Estudiante Uno",
            "description": "Expediente completo para entrega",
        }).json()
        inv_uuid = inv["uuid"]
        base = f"/api/investigations/{inv_uuid}"
        files = student.post(f"{base}/files", files=[
            ("files", ("a.pptx", pptx_a.read_bytes())),
            ("files", ("b.pptx", pptx_b.read_bytes())),
        ]).json()["files"]
        f_a, f_b = files[0]["uuid"], files[1]["uuid"]
        comp = student.post(f"{base}/comparisons", json={"file_a_uuid": f_a, "file_b_uuid": f_b}).json()
        rel = student.post(f"{base}/relations", json={
            "source_file_uuid": f_a, "target_file_uuid": f_b,
            "relation_type": "manual_confirmed", "label": "Revisión b desde a",
        }).json()
        note = student.post(f"{base}/notes", json={
            "text": "Versión 1 nota", "target_type": "comparison", "target_uuid": comp["uuid"],
        }).json()
        student.patch(f"{base}/notes/{note['uuid']}", json={"text": "Versión 2 nota"})
        manifest = student.post(f"{base}/manifest").json()
        assert manifest["version"] == 1 and len(manifest["files"]) == 2
        student.post(f"{base}/state", json={
            "action": "close", "summary": "Dos PPTX comparados.", "conclusion": "Diferencia textual en diapositiva 1.",
        })

        # Verify HTML & JSON reports
        html_rep = student.get(f"{base}/report/html")
        assert html_rep.status_code == 200 and "Práctica Forense 1" in html_rep.text
        json_rep = student.get(f"{base}/report/json").json()
        assert json_rep["ai_context"]["ready"] is True and json_rep["ai_context"]["external_transmission"] is False

        # Export v1 package
        exp1 = student.get(f"{base}/export")
        assert exp1.status_code == 200
        pkg_v1 = exp1.content

        # Ensure exported ZIP has manifest.json and NO original .pptx files
        with zipfile.ZipFile(io.BytesIO(pkg_v1)) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names and "investigation.json" in names
            assert not any(n.endswith(".pptx") for n in names)

        # Student reopens, adds a new note, and exports modified package (same UUID, different content!)
        student.post(f"{base}/state", json={"action": "reopen", "reason": "Agregar anexo"})
        student.post(f"{base}/notes", json={"text": "Nota agregada en versión 2 del paquete"})
        pkg_v2 = student.get(f"{base}/export").content

    # 2. Professor imports into a completely independent SQLite database
    with TestClient(create_app(prof_settings), base_url="http://127.0.0.1") as prof:
        # Case A: new UUID -> imports preserving exact UUID and all linked records
        imp1 = prof.post("/api/investigations/import", files=[("packages", ("est1.zip", pkg_v1))]).json()
        assert imp1["summary"]["imported"] == 1
        assert imp1["results"][0]["uuid"] == inv_uuid
        imported = prof.get(f"/api/investigations/{inv_uuid}").json()
        assert imported["name"] == "Práctica Forense 1" and imported["status"] == "closed"
        assert imported["file_count"] == 2 and imported["comparison_count"] == 1
        notes_prof = prof.get(f"/api/investigations/{inv_uuid}/notes").json()
        assert len(notes_prof) == 1 and len(notes_prof[0]["versions"]) == 2

        # Case B: repeat import of identical package -> detected as already_exists, no duplication
        prev_dup = prof.post("/api/investigations/import/preview", files=[("packages", ("est1.zip", pkg_v1))]).json()
        assert prev_dup["items"][0]["conflict"] == "identical_existing"
        imp_dup = prof.post("/api/investigations/import", files=[("packages", ("est1.zip", pkg_v1))]).json()
        assert imp_dup["results"][0]["outcome"] == "already_exists"
        assert len(prof.get("/api/investigations").json()) == 1

        # Case C: same UUID, different content -> detected as conflict; refuses overwrite; supports independent copy!
        prev_conf = prof.post("/api/investigations/import/preview", files=[("packages", ("est1-v2.zip", pkg_v2))]).json()
        assert prev_conf["items"][0]["conflict"] == "uuid_conflict"
        imp_refuse = prof.post("/api/investigations/import", files=[("packages", ("est1-v2.zip", pkg_v2))]).json()
        assert imp_refuse["results"][0]["outcome"] == "conflict"

        imp_copy = prof.post(
            "/api/investigations/import",
            files=[("packages", ("est1-v2.zip", pkg_v2))],
            data={"action": "copy"},
        ).json()
        assert imp_copy["results"][0]["outcome"] == "imported_copy"
        copy_uuid = imp_copy["results"][0]["uuid"]
        assert copy_uuid != inv_uuid
        copied = prof.get(f"/api/investigations/{copy_uuid}").json()
        assert copied["origin_uuid"] == inv_uuid
        # Verify remapped internal references work cleanly
        copy_files = prof.get(f"/api/investigations/{copy_uuid}/files").json()
        copy_comps = prof.get(f"/api/investigations/{copy_uuid}/comparisons").json()
        copy_notes = prof.get(f"/api/investigations/{copy_uuid}/notes").json()
        assert len(copy_files) == 2 and len(copy_comps) == 1 and len(copy_notes) == 2
        assert copy_files[0]["uuid"] != f_a
        assert {copy_comps[0]["file_a_uuid"], copy_comps[0]["file_b_uuid"]} == {copy_files[0]["uuid"], copy_files[1]["uuid"]}

        # Invalid packages: tampered JSON inside ZIP & Zip Slip rejected without corrupting DB
        tampered_buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(pkg_v1)) as zin, zipfile.ZipFile(tampered_buf, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == "investigation.json":
                    obj = json.loads(data)
                    obj["name"] = "ALTERADO SIN RECALCULAR MANIFIESTO"
                    data = json.dumps(obj).encode("utf-8")
                zout.writestr(info.filename, data)
        slip_zip = zip_bytes([("../escape.json", b"{}")])

        bad_preview = prof.post("/api/investigations/import/preview", files=[
            ("packages", ("tampered.zip", tampered_buf.getvalue())),
            ("packages", ("slip.zip", slip_zip)),
            ("packages", ("notazip.zip", b"basura")),
        ]).json()
        assert all(item["valid"] is False for item in bad_preview["items"])
        count_before = len(prof.get("/api/investigations").json())
        bad_imp = prof.post("/api/investigations/import", files=[
            ("packages", ("tampered.zip", tampered_buf.getvalue())),
            ("packages", ("notazip.zip", b"basura")),
        ]).json()
        assert bad_imp["summary"]["error"] == 2
        assert len(prof.get("/api/investigations").json()) == count_before


def test_batch_import_ten_independent_student_investigations_with_one_corrupt(tmp_path):
    """Simulate 10 students exporting independent investigations (even with identical names) + 1 corrupt ZIP."""
    packages = []
    for idx in range(10):
        st_settings = Settings(data=tmp_path / f"student_{idx}")
        with TestClient(create_app(st_settings), base_url="http://127.0.0.1") as student:
            inv = student.post("/api/investigations", json={
                "name": "Trabajo Práctico Unidad 4",  # deliberately identical across all 10 students!
                "responsible": f"Estudiante {idx + 1}",
            }).json()
            pkg = student.get(f"/api/investigations/{inv['uuid']}/export").content
            packages.append((f"estudiante-{idx + 1}.zip", pkg))

    # Add 1 corrupt package in the middle of the batch
    batch_files = [*packages[:5], ("corrupto.zip", b"no es un zip"), *packages[5:]]

    prof_settings = Settings(data=tmp_path / "prof_batch")
    with TestClient(create_app(prof_settings), base_url="http://127.0.0.1") as prof:
        prev = prof.post(
            "/api/investigations/import/preview",
            files=[("packages", item) for item in batch_files],
        ).json()
        assert prev["count"] == 11
        valid_items = [i for i in prev["items"] if i["valid"]]
        invalid_items = [i for i in prev["items"] if not i["valid"]]
        assert len(valid_items) == 10 and len(invalid_items) == 1
        assert len({i["uuid"] for i in valid_items}) == 10

        actions = [i["recommended_action"] for i in prev["items"]]
        imp = prof.post(
            "/api/investigations/import",
            files=[("packages", item) for item in batch_files],
            data={"actions": json.dumps(actions)},
        ).json()
        assert imp["summary"]["imported"] == 10
        assert imp["summary"]["skipped"] == 1
        assert imp["summary"]["error"] == 0
        stored = prof.get("/api/investigations").json()
        assert len(stored) == 10
        assert len({item["uuid"] for item in stored}) == 10
