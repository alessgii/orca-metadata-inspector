"""Portable investigation packages (.zip): export, validation, single & batch import."""
import hashlib
import io
import json
import zipfile
from pathlib import Path

from .config import Settings
from .investigations import (
    ANALYZER_VERSION,
    CLASSIFICATIONS,
    INVESTIGATION_KINDS,
    MANUAL_RELATION_TYPES,
    NOTE_TARGETS,
    STATUSES,
    InvestigationError,
    InvestigationService,
    _dict_factory,
    new_uuid,
    utc_now,
    validate_uuid,
)
from .manifests import INTEGRITY_DISCLAIMER, ManifestError, build_manifest, verify_manifest
from .uploads import (
    ZIP_ERRORS,
    ExpansionBudget,
    UploadError,
    check_budget,
    checked_zip,
    member_chunks,
    safe_members,
    signatures,
)

PACKAGE_FORMAT = "orca-investigation"
PACKAGE_SCHEMA_VERSION = 1
COMPONENT_FILES = (
    "package.json",
    "investigation.json",
    "files.json",
    "comparisons.json",
    "relations.json",
    "events.json",
    "notes.json",
    "closures.json",
)
MAX_RECORDS_PER_TABLE = 5000
MAX_JSON_MEMBER_BYTES = 25_000_000


class PortabilityError(InvestigationError):
    pass


def _canonical_json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pretty_json(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _compute_content_fingerprint(bundle: dict) -> str:
    """Deterministic fingerprint of an investigation's logical content (independent of export timestamp)."""
    payload = {
        "investigation": {
            key: bundle["investigation"].get(key)
            for key in ("uuid", "name", "responsible", "description", "kind", "classification",
                        "status", "created_at", "updated_at", "closed_at", "origin_uuid", "origin_note")
        },
        "files": sorted(bundle["files"], key=lambda x: x["uuid"]),
        "comparisons": sorted(bundle["comparisons"], key=lambda x: x["uuid"]),
        "relations": sorted(bundle["relations"], key=lambda x: x["uuid"]),
        "events": sorted(bundle["events"], key=lambda x: x["uuid"]),
        "notes": sorted(bundle["notes"], key=lambda x: x["uuid"]),
        "closures": sorted(bundle["closures"], key=lambda x: x["uuid"]),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _extract_bundle_from_db(db, inv_uuid: str) -> dict | None:
    db.row_factory = _dict_factory
    inv = db.execute("SELECT * FROM investigation WHERE uuid = ?", (inv_uuid,)).fetchone()
    if not inv:
        return None
    inv_data = {
        "uuid": inv["uuid"],
        "name": inv["name"],
        "responsible": inv["responsible"],
        "description": inv["description"],
        "kind": inv["kind"],
        "classification": inv["classification"],
        "status": inv["status"],
        "created_at": inv["created_at"],
        "updated_at": inv["updated_at"],
        "closed_at": inv["closed_at"],
        "origin_uuid": inv["origin_uuid"],
        "origin_note": inv["origin_note"],
        "schema_version": inv["schema_version"],
    }

    files = []
    for f in db.execute(
        """
        SELECT f.*, a.engine_name, a.engine_version, a.analyzer_version,
               a.raw_json, a.normalized_json, a.findings_json, a.errors_json,
               a.warnings_json, a.structure_json, a.analyzed_at
        FROM investigation_file f
        LEFT JOIN investigation_analysis a ON a.file_uuid = f.uuid
        WHERE f.investigation_uuid = ?
        ORDER BY f.id ASC
        """,
        (inv_uuid,),
    ).fetchall():
        files.append({
            "uuid": f["uuid"],
            "name": f["name"],
            "relative_path": f["relative_path"],
            "extension": f["extension"],
            "format": f["format"],
            "size": f["size"],
            "sha256": f["sha256"],
            "status": f["status"],
            "duplicate_of_uuid": f["duplicate_of_uuid"],
            "manual_order": f["manual_order"],
            "internal_created": f["internal_created"],
            "internal_modified": f["internal_modified"],
            "added_at": f["added_at"],
            "removed_at": f["removed_at"],
            "analysis": {
                "engine_name": f["engine_name"],
                "engine_version": f["engine_version"],
                "analyzer_version": f["analyzer_version"] or ANALYZER_VERSION,
                "raw": json.loads(f["raw_json"]) if f["raw_json"] else {},
                "normalized": json.loads(f["normalized_json"]) if f["normalized_json"] else {},
                "findings": json.loads(f["findings_json"]) if f["findings_json"] else [],
                "errors": json.loads(f["errors_json"]) if f["errors_json"] else [],
                "warnings": json.loads(f["warnings_json"]) if f["warnings_json"] else [],
                "structure": json.loads(f["structure_json"]) if f["structure_json"] else None,
                "analyzed_at": f["analyzed_at"] or f["added_at"],
            } if f["raw_json"] is not None else None,
        })

    comparisons = [
        {
            "uuid": c["uuid"],
            "file_a_uuid": c["file_a_uuid"],
            "file_b_uuid": c["file_b_uuid"],
            "created_at": c["created_at"],
            "algorithms": json.loads(c["algorithms_json"]),
            "analyzer_version": c["analyzer_version"],
            "identical_bytes": None if c["identical_bytes"] is None else bool(c["identical_bytes"]),
            "similarity_overall": c["similarity_overall"],
            "similarity_available": bool(c["similarity_available"]),
            "result": json.loads(c["result_json"]),
            "limitations": json.loads(c["limitations_json"]),
        }
        for c in db.execute(
            "SELECT * FROM investigation_comparison WHERE investigation_uuid = ? ORDER BY id ASC",
            (inv_uuid,),
        ).fetchall()
    ]

    relations = [
        {
            "uuid": r["uuid"],
            "source_file_uuid": r["source_file_uuid"],
            "target_file_uuid": r["target_file_uuid"],
            "relation_type": r["relation_type"],
            "origin": r["origin"],
            "confidence_label": r["confidence_label"],
            "label": r["label"],
            "notes": r["notes"],
            "actor": r["actor"],
            "created_at": r["created_at"],
        }
        for r in db.execute(
            "SELECT * FROM investigation_relation WHERE investigation_uuid = ? ORDER BY id ASC",
            (inv_uuid,),
        ).fetchall()
    ]

    events = [
        {
            "uuid": e["uuid"],
            "event_type": e["event_type"],
            "created_at": e["created_at"],
            "actor": e["actor"],
            "description": e["description"],
            "related_uuids": json.loads(e["related_uuids_json"]),
            "details": json.loads(e["details_json"]),
        }
        for e in db.execute(
            "SELECT * FROM investigation_event WHERE investigation_uuid = ? ORDER BY id ASC",
            (inv_uuid,),
        ).fetchall()
    ]

    notes = []
    for n in db.execute(
        "SELECT * FROM investigation_note WHERE investigation_uuid = ? ORDER BY id ASC",
        (inv_uuid,),
    ).fetchall():
        versions = db.execute(
            "SELECT version, author, text, created_at FROM investigation_note_version WHERE note_uuid = ? ORDER BY version ASC",
            (n["uuid"],),
        ).fetchall()
        notes.append({
            "uuid": n["uuid"],
            "target_type": n["target_type"],
            "target_uuid": n["target_uuid"],
            "author": n["author"],
            "text": n["text"],
            "version": n["version"],
            "created_at": n["created_at"],
            "updated_at": n["updated_at"],
            "versions": versions,
        })

    closures = [
        {
            "uuid": c["uuid"],
            "action": c["action"],
            "actor": c["actor"],
            "summary": c["summary"],
            "conclusion": c["conclusion"],
            "final_notes": c["final_notes"],
            "reason": c["reason"],
            "created_at": c["created_at"],
        }
        for c in db.execute(
            "SELECT * FROM investigation_closure WHERE investigation_uuid = ? ORDER BY id ASC",
            (inv_uuid,),
        ).fetchall()
    ]

    return {
        "investigation": inv_data,
        "files": files,
        "comparisons": comparisons,
        "relations": relations,
        "events": events,
        "notes": notes,
        "closures": closures,
    }


def export_investigation_zip(service: InvestigationService, inv_uuid: str) -> tuple[bytes, str]:
    inv_uuid = validate_uuid(inv_uuid, "UUID de investigación")
    with service._connect() as db:
        bundle = _extract_bundle_from_db(db, inv_uuid)
        if not bundle:
            raise PortabilityError("La investigación solicitada no existe.", 404)

    fingerprint = _compute_content_fingerprint(bundle)
    pkg_meta = {
        "package_format": PACKAGE_FORMAT,
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "exported_at": utc_now(),
        "investigation_uuid": inv_uuid,
        "content_fingerprint": fingerprint,
        "includes_originals": False,
        "integrity_disclaimer": INTEGRITY_DISCLAIMER,
    }

    components = {
        "package.json": _pretty_json(pkg_meta),
        "investigation.json": _pretty_json(bundle["investigation"]),
        "files.json": _pretty_json(bundle["files"]),
        "comparisons.json": _pretty_json(bundle["comparisons"]),
        "relations.json": _pretty_json(bundle["relations"]),
        "events.json": _pretty_json(bundle["events"]),
        "notes.json": _pretty_json(bundle["notes"]),
        "closures.json": _pretty_json(bundle["closures"]),
    }

    manifest_entries = [
        {"path": name, "size": len(data), "hash": hashlib.sha256(data).hexdigest()}
        for name, data in components.items()
    ]
    manifest = build_manifest(manifest_entries, algorithm="SHA-256")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in components.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", _pretty_json(manifest))
    filename = f"orca-investigacion-{inv_uuid[:8]}.zip"
    return buf.getvalue(), filename


def parse_and_validate_package(zip_path: Path, settings: Settings) -> dict:
    """Safely inspect, integrity-check, and validate an exported Orca investigation ZIP."""
    if signatures(zip_path) != "ZIP":
        raise PortabilityError("El archivo no corresponde a un paquete ZIP válido.")
    budget = ExpansionBudget(settings.expanded_limit)
    try:
        with checked_zip(zip_path, max_entries=64) as archive:
            entries = safe_members(archive)
            check_budget(entries, budget.remaining)
            by_name = {item.filename: item for item in entries}
            required = set(COMPONENT_FILES) | {"manifest.json"}
            missing = sorted(required - set(by_name))
            if missing:
                raise PortabilityError(f"Al paquete le faltan archivos obligatorios: {', '.join(missing)}.")
            extra = sorted(set(by_name) - required)
            if extra:
                raise PortabilityError(f"El paquete contiene archivos no permitidos: {', '.join(extra)}.")

            raw_bytes: dict[str, bytes] = {}
            for name in sorted(required):
                item = by_name[name]
                if item.file_size > MAX_JSON_MEMBER_BYTES:
                    raise PortabilityError(f"El componente «{name}» excede el tamaño máximo permitido.", 413)
                data = bytearray()
                for chunk in member_chunks(archive, item):
                    budget.account(len(chunk))
                    data.extend(chunk)
                raw_bytes[name] = bytes(data)
    except (UploadError, *ZIP_ERRORS) as exc:
        raise PortabilityError(f"Paquete ZIP corrupto o inseguro: {exc}") from exc

    # Verify manifest integrity over all 8 JSON components
    try:
        manifest_obj = json.loads(raw_bytes["manifest.json"].decode("utf-8"))
        observed = [
            {"path": name, "size": len(raw_bytes[name]), "hash": hashlib.sha256(raw_bytes[name]).hexdigest()}
            for name in COMPONENT_FILES
        ]
        ver = verify_manifest(manifest_obj, observed)
    except (UnicodeDecodeError, ValueError, ManifestError) as exc:
        raise PortabilityError(f"Manifiesto de integridad inválido dentro del paquete: {exc}") from exc
    if ver["status"] != "verified":
        raise PortabilityError("La verificación de integridad del paquete falló: uno o más archivos JSON fueron alterados.")

    parsed = {}
    for name in COMPONENT_FILES:
        try:
            parsed[name] = json.loads(raw_bytes[name].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PortabilityError(f"El componente «{name}» no es un JSON UTF-8 válido.") from exc

    meta = parsed["package.json"]
    if not isinstance(meta, dict) or meta.get("package_format") != PACKAGE_FORMAT:
        raise PortabilityError("El paquete no corresponde al formato de investigación de Orca.")
    if meta.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise PortabilityError(f"Versión de esquema de paquete no compatible: {meta.get('schema_version')!r}.")

    inv = parsed["investigation.json"]
    if not isinstance(inv, dict):
        raise PortabilityError("Metadatos de investigación inválidos.")
    inv_uuid = validate_uuid(inv.get("uuid"), "UUID de investigación")
    if meta.get("investigation_uuid") != inv_uuid:
        raise PortabilityError("El UUID declarado en package.json no coincide con investigation.json.")
    if not isinstance(inv.get("name"), str) or not inv["name"].strip():
        raise PortabilityError("La investigación del paquete carece de nombre.")
    if not isinstance(inv.get("responsible"), str) or not inv["responsible"].strip():
        raise PortabilityError("La investigación del paquete carece de responsable.")
    if inv.get("kind") not in INVESTIGATION_KINDS:
        raise PortabilityError("Tipo de investigación inválido en el paquete.")
    if inv.get("classification") not in CLASSIFICATIONS:
        raise PortabilityError("Clasificación inválida en el paquete.")
    if inv.get("status") not in STATUSES:
        raise PortabilityError("Estado de investigación inválido en el paquete.")
    if inv.get("origin_uuid") is not None:
        validate_uuid(inv["origin_uuid"], "UUID de origen")

    for list_name in ("files.json", "comparisons.json", "relations.json", "events.json", "notes.json", "closures.json"):
        if not isinstance(parsed[list_name], list) or len(parsed[list_name]) > MAX_RECORDS_PER_TABLE:
            raise PortabilityError(f"Estructura o cantidad de registros inválida en «{list_name}».")

    # Validate UUIDs and cross-references strictly
    file_ids: set[str] = set()
    for f in parsed["files.json"]:
        if not isinstance(f, dict):
            raise PortabilityError("Registro de archivo inválido en el paquete.")
        uid = validate_uuid(f.get("uuid"), "UUID de archivo")
        if uid in file_ids:
            raise PortabilityError("El paquete contiene UUIDs de archivo duplicados.")
        file_ids.add(uid)
        if f.get("status") not in {"ok", "empty", "unsupported", "error"}:
            raise PortabilityError("Estado de archivo inválido en el paquete.")
        if type(f.get("size")) is not int or f["size"] < 0:
            raise PortabilityError("Tamaño de archivo inválido en el paquete.")

    comp_ids: set[str] = set()
    for c in parsed["comparisons.json"]:
        if not isinstance(c, dict):
            raise PortabilityError("Registro de comparación inválido en el paquete.")
        uid = validate_uuid(c.get("uuid"), "UUID de comparación")
        if uid in comp_ids:
            raise PortabilityError("El paquete contiene UUIDs de comparación duplicados.")
        comp_ids.add(uid)
        fa = validate_uuid(c.get("file_a_uuid"), "UUID de archivo A")
        fb = validate_uuid(c.get("file_b_uuid"), "UUID de archivo B")
        if fa == fb or fa not in file_ids or fb not in file_ids:
            raise PortabilityError("Inconsistencia de referencias en comparisons.json.")

    rel_ids: set[str] = set()
    for r in parsed["relations.json"]:
        if not isinstance(r, dict):
            raise PortabilityError("Registro de relación inválido en el paquete.")
        uid = validate_uuid(r.get("uuid"), "UUID de relación")
        if uid in rel_ids:
            raise PortabilityError("El paquete contiene UUIDs de relación duplicados.")
        rel_ids.add(uid)
        src = validate_uuid(r.get("source_file_uuid"), "UUID de archivo origen")
        dst = validate_uuid(r.get("target_file_uuid"), "UUID de archivo destino")
        if src == dst or src not in file_ids or dst not in file_ids:
            raise PortabilityError("Inconsistencia de referencias en relations.json.")

    event_ids: set[str] = set()
    for e in parsed["events.json"]:
        if not isinstance(e, dict):
            raise PortabilityError("Evento de auditoría inválido en el paquete.")
        uid = validate_uuid(e.get("uuid"), "UUID de evento")
        if uid in event_ids:
            raise PortabilityError("El paquete contiene UUIDs de evento duplicados.")
        event_ids.add(uid)

    note_ids: set[str] = set()
    for n in parsed["notes.json"]:
        if not isinstance(n, dict):
            raise PortabilityError("Observación inválida en el paquete.")
        uid = validate_uuid(n.get("uuid"), "UUID de observación")
        if uid in note_ids:
            raise PortabilityError("El paquete contiene UUIDs de observación duplicados.")
        note_ids.add(uid)
        ttype = n.get("target_type")
        if ttype not in NOTE_TARGETS:
            raise PortabilityError("Tipo de destino de observación inválido.")
        tuid = n.get("target_uuid")
        if ttype != "investigation":
            tuid = validate_uuid(tuid, f"UUID destino ({ttype})")
            valid_pool = {"file": file_ids, "comparison": comp_ids, "event": event_ids, "relation": rel_ids}[ttype]
            if tuid not in valid_pool:
                raise PortabilityError("Inconsistencia de referencias en notes.json.")

    closure_ids: set[str] = set()
    for cl in parsed["closures.json"]:
        if not isinstance(cl, dict):
            raise PortabilityError("Registro de cierre inválido en el paquete.")
        uid = validate_uuid(cl.get("uuid"), "UUID de cierre")
        if uid in closure_ids or cl.get("action") not in {"closed", "reopened"}:
            raise PortabilityError("Registro de cierre duplicado o inválido en closures.json.")
        closure_ids.add(uid)

    bundle = {
        "investigation": inv,
        "files": parsed["files.json"],
        "comparisons": parsed["comparisons.json"],
        "relations": parsed["relations.json"],
        "events": parsed["events.json"],
        "notes": parsed["notes.json"],
        "closures": parsed["closures.json"],
    }
    computed_fp = _compute_content_fingerprint(bundle)
    if meta.get("content_fingerprint") and meta["content_fingerprint"] != computed_fp:
        raise PortabilityError("La huella de contenido del paquete no coincide con sus registros.")

    return {
        "meta": meta,
        "bundle": bundle,
        "fingerprint": computed_fp,
        "manifest_disclaimer": INTEGRITY_DISCLAIMER,
    }


def _remap_bundle_for_copy(bundle: dict, source_filename: str = "") -> dict:
    """Create an independent copy with fresh UUIDs while preserving every internal relationship."""
    old_inv_uuid = bundle["investigation"]["uuid"]
    new_inv_uuid = new_uuid()

    id_map: dict[str, str] = {old_inv_uuid: new_inv_uuid}
    for group in ("files", "comparisons", "relations", "events", "notes", "closures"):
        for item in bundle[group]:
            id_map[item["uuid"]] = new_uuid()

    def remap(uid: str | None) -> str | None:
        if uid is None:
            return None
        return id_map.get(uid, uid)

    new_inv = {
        **bundle["investigation"],
        "uuid": new_inv_uuid,
        "origin_uuid": old_inv_uuid,
        "origin_note": f"Copia independiente importada desde expediente original {old_inv_uuid}"
                       + (f" ({source_filename})" if source_filename else "") + ".",
    }
    new_files = [
        {**f, "uuid": id_map[f["uuid"]], "duplicate_of_uuid": remap(f.get("duplicate_of_uuid"))}
        for f in bundle["files"]
    ]
    new_comparisons = [
        {
            **c,
            "uuid": id_map[c["uuid"]],
            "file_a_uuid": id_map[c["file_a_uuid"]],
            "file_b_uuid": id_map[c["file_b_uuid"]],
        }
        for c in bundle["comparisons"]
    ]
    new_relations = [
        {
            **r,
            "uuid": id_map[r["uuid"]],
            "source_file_uuid": id_map[r["source_file_uuid"]],
            "target_file_uuid": id_map[r["target_file_uuid"]],
        }
        for r in bundle["relations"]
    ]
    new_events = [
        {
            **e,
            "uuid": id_map[e["uuid"]],
            "related_uuids": [remap(u) for u in e.get("related_uuids", [])],
        }
        for e in bundle["events"]
    ]
    new_notes = [
        {
            **n,
            "uuid": id_map[n["uuid"]],
            "target_uuid": remap(n.get("target_uuid")),
        }
        for n in bundle["notes"]
    ]
    new_closures = [
        {**cl, "uuid": id_map[cl["uuid"]]}
        for cl in bundle["closures"]
    ]
    return {
        "investigation": new_inv,
        "files": new_files,
        "comparisons": new_comparisons,
        "relations": new_relations,
        "events": new_events,
        "notes": new_notes,
        "closures": new_closures,
    }


def _insert_bundle_atomic(service: InvestigationService, bundle: dict, import_note: str | None = None) -> dict:
    inv = bundle["investigation"]
    inv_uuid = inv["uuid"]
    with service._connect() as db:
        db.execute(
            """
            INSERT INTO investigation
                (uuid, name, responsible, description, kind, classification, status,
                 created_at, updated_at, closed_at, origin_uuid, origin_note, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inv_uuid, inv["name"].strip(), inv["responsible"].strip(),
                (inv.get("description") or "").strip(), inv["kind"], inv["classification"],
                inv["status"], inv["created_at"], inv["updated_at"], inv.get("closed_at"),
                inv.get("origin_uuid"), inv.get("origin_note") or "", inv.get("schema_version", 1),
            ),
        )
        for f in bundle["files"]:
            db.execute(
                """
                INSERT INTO investigation_file
                    (uuid, investigation_uuid, name, relative_path, extension, format, size, sha256,
                     status, duplicate_of_uuid, manual_order, internal_created, internal_modified,
                     added_at, removed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f["uuid"], inv_uuid, f["name"], f.get("relative_path"), f.get("extension", ""),
                    f.get("format"), f["size"], f.get("sha256"), f["status"], f.get("duplicate_of_uuid"),
                    f.get("manual_order", 0), f.get("internal_created"), f.get("internal_modified"),
                    f["added_at"], f.get("removed_at"),
                ),
            )
            analysis = f.get("analysis")
            if isinstance(analysis, dict):
                db.execute(
                    """
                    INSERT INTO investigation_analysis
                        (file_uuid, engine_name, engine_version, analyzer_version, raw_json, normalized_json,
                         findings_json, errors_json, warnings_json, structure_json, analyzed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f["uuid"], analysis.get("engine_name"), analysis.get("engine_version"),
                        analysis.get("analyzer_version", ANALYZER_VERSION),
                        json.dumps(analysis.get("raw", {}), ensure_ascii=False),
                        json.dumps(analysis.get("normalized", {}), ensure_ascii=False),
                        json.dumps(analysis.get("findings", []), ensure_ascii=False),
                        json.dumps(analysis.get("errors", []), ensure_ascii=False),
                        json.dumps(analysis.get("warnings", []), ensure_ascii=False),
                        json.dumps(analysis["structure"], ensure_ascii=False) if analysis.get("structure") is not None else None,
                        analysis.get("analyzed_at") or f["added_at"],
                    ),
                )
        for c in bundle["comparisons"]:
            db.execute(
                """
                INSERT INTO investigation_comparison
                    (uuid, investigation_uuid, file_a_uuid, file_b_uuid, created_at, algorithms_json,
                     analyzer_version, identical_bytes, similarity_overall, similarity_available,
                     result_json, limitations_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    c["uuid"], inv_uuid, c["file_a_uuid"], c["file_b_uuid"], c["created_at"],
                    json.dumps(c.get("algorithms", []), ensure_ascii=False),
                    c.get("analyzer_version", ANALYZER_VERSION),
                    None if c.get("identical_bytes") is None else int(bool(c["identical_bytes"])),
                    c.get("similarity_overall"), int(bool(c.get("similarity_available"))),
                    json.dumps(c.get("result", {}), ensure_ascii=False),
                    json.dumps(c.get("limitations", []), ensure_ascii=False),
                ),
            )
        for r in bundle["relations"]:
            db.execute(
                """
                INSERT INTO investigation_relation
                    (uuid, investigation_uuid, source_file_uuid, target_file_uuid, relation_type,
                     origin, confidence_label, label, notes, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["uuid"], inv_uuid, r["source_file_uuid"], r["target_file_uuid"], r["relation_type"],
                    r.get("origin", "manual"), r["confidence_label"], r.get("label", ""),
                    r.get("notes", ""), r.get("actor", inv["responsible"]), r["created_at"],
                ),
            )
        for e in bundle["events"]:
            db.execute(
                """
                INSERT INTO investigation_event
                    (uuid, investigation_uuid, event_type, created_at, actor, description,
                     related_uuids_json, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    e["uuid"], inv_uuid, e["event_type"], e["created_at"], e["actor"], e["description"],
                    json.dumps(e.get("related_uuids", []), ensure_ascii=False),
                    json.dumps(e.get("details", {}), ensure_ascii=False),
                ),
            )
        for n in bundle["notes"]:
            db.execute(
                """
                INSERT INTO investigation_note
                    (uuid, investigation_uuid, target_type, target_uuid, author, text, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    n["uuid"], inv_uuid, n["target_type"], n.get("target_uuid"), n["author"],
                    n["text"], n.get("version", 1), n["created_at"], n["updated_at"],
                ),
            )
            versions = n.get("versions") or [{"version": 1, "author": n["author"], "text": n["text"], "created_at": n["created_at"]}]
            for v in versions:
                db.execute(
                    """
                    INSERT INTO investigation_note_version (note_uuid, version, author, text, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (n["uuid"], v["version"], v["author"], v["text"], v["created_at"]),
                )
        for cl in bundle["closures"]:
            db.execute(
                """
                INSERT INTO investigation_closure
                    (uuid, investigation_uuid, action, actor, summary, conclusion, final_notes, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cl["uuid"], inv_uuid, cl["action"], cl["actor"], cl.get("summary", ""),
                    cl.get("conclusion", ""), cl.get("final_notes", ""), cl.get("reason", ""),
                    cl["created_at"],
                ),
            )
        if import_note:
            service._log_event(
                db, inv_uuid, "investigation_imported", inv["responsible"],
                import_note,
                details={"origin_uuid": inv.get("origin_uuid")},
            )
    return service.get(inv_uuid)


def preview_packages(service: InvestigationService, packages: list[tuple[str, Path]]) -> list[dict]:
    """Validate 1..N packages independently, detecting local DB conflicts and intra-batch duplicates."""
    seen_in_batch: dict[str, tuple[str, str]] = {}  # uuid -> (filename, fingerprint)
    results = []
    for idx, (filename, path) in enumerate(packages):
        try:
            parsed = parse_and_validate_package(path, service.settings)
        except PortabilityError as exc:
            results.append({
                "index": idx,
                "filename": filename,
                "valid": False,
                "error": str(exc),
                "conflict": "invalid",
                "recommended_action": "skip",
                "available_actions": ["skip"],
            })
            continue

        inv = parsed["bundle"]["investigation"]
        inv_uuid = inv["uuid"]
        fp = parsed["fingerprint"]
        active_files = sum(1 for f in parsed["bundle"]["files"] if not f.get("removed_at"))

        with service._connect() as db:
            local = _extract_bundle_from_db(db, inv_uuid)

        if local is not None:
            local_fp = _compute_content_fingerprint(local)
            if local_fp == fp:
                conflict = "identical_existing"
                conflict_detail = "Esta investigación ya existe en tu base local con contenido idéntico."
                recommended = "skip"
                actions = ["skip", "copy"]
            else:
                conflict = "uuid_conflict"
                conflict_detail = (
                    "Ya existe una investigación local con el mismo UUID pero contenido distinto. "
                    "Para preservar trazabilidad sin sobrescrituras destructivas, puedes omitirla o importarla como copia independiente."
                )
                recommended = "copy"
                actions = ["skip", "copy"]
        elif inv_uuid in seen_in_batch:
            prev_name, prev_fp = seen_in_batch[inv_uuid]
            if prev_fp == fp:
                conflict = "duplicate_in_batch"
                conflict_detail = f"Paquete duplicado en este mismo lote (idéntico a «{prev_name}»)."
                recommended = "skip"
                actions = ["skip", "copy"]
            else:
                conflict = "uuid_conflict_in_batch"
                conflict_detail = f"Conflicto de UUID dentro del lote con «{prev_name}» (contenido distinto)."
                recommended = "copy"
                actions = ["skip", "copy"]
        else:
            conflict = "none"
            conflict_detail = "Listo para importar conservando su UUID original."
            recommended = "import"
            actions = ["import", "skip"]
            seen_in_batch[inv_uuid] = (filename, fp)

        results.append({
            "index": idx,
            "filename": filename,
            "valid": True,
            "error": None,
            "uuid": inv_uuid,
            "short_uuid": f"{inv_uuid[:8]}…{inv_uuid[-6:]}",
            "name": inv["name"],
            "responsible": inv["responsible"],
            "status": inv["status"],
            "created_at": inv["created_at"],
            "file_count": active_files,
            "comparison_count": len(parsed["bundle"]["comparisons"]),
            "note_count": len(parsed["bundle"]["notes"]),
            "event_count": len(parsed["bundle"]["events"]),
            "conflict": conflict,
            "conflict_detail": conflict_detail,
            "recommended_action": recommended,
            "available_actions": actions,
            "integrity_disclaimer": INTEGRITY_DISCLAIMER,
        })
    return results


def import_package(service: InvestigationService, path: Path, filename: str,
                   action: str = "import") -> dict:
    """Atomically import a single investigation package using explicit conflict resolution."""
    if action not in {"import", "copy", "skip"}:
        raise PortabilityError("Acción de importación no válida. Usa: import, copy o skip.")
    if action == "skip":
        return {"filename": filename, "outcome": "skipped", "message": "Importación omitida por el usuario."}

    parsed = parse_and_validate_package(path, service.settings)
    bundle = parsed["bundle"]
    inv_uuid = bundle["investigation"]["uuid"]
    fp = parsed["fingerprint"]

    with service._connect() as db:
        local = _extract_bundle_from_db(db, inv_uuid)

    if local is not None:
        local_fp = _compute_content_fingerprint(local)
        if action == "import":
            if local_fp == fp:
                return {
                    "filename": filename,
                    "outcome": "already_exists",
                    "uuid": inv_uuid,
                    "message": "La investigación ya está importada con contenido idéntico. No se duplicaron registros.",
                }
            return {
                "filename": filename,
                "outcome": "conflict",
                "uuid": inv_uuid,
                "message": "Conflicto: ya existe una investigación local con el mismo UUID y contenido distinto. Elige cancelar o importar como copia independiente.",
            }
        # action == "copy"
        copied = _remap_bundle_for_copy(bundle, source_filename=filename)
        created = _insert_bundle_atomic(
            service, copied,
            import_note=f"Expediente importado como copia independiente desde UUID original {inv_uuid} ({filename}).",
        )
        return {
            "filename": filename,
            "outcome": "imported_copy",
            "uuid": created["uuid"],
            "origin_uuid": inv_uuid,
            "investigation": created,
            "message": f"Importado como copia independiente con nuevo UUID {created['short_uuid']}… (origen: {inv_uuid[:8]}…).",
        }

    # UUID does not exist locally
    if action == "copy":
        copied = _remap_bundle_for_copy(bundle, source_filename=filename)
        created = _insert_bundle_atomic(
            service, copied,
            import_note=f"Expediente importado como copia independiente desde UUID original {inv_uuid} ({filename}).",
        )
        return {
            "filename": filename,
            "outcome": "imported_copy",
            "uuid": created["uuid"],
            "origin_uuid": inv_uuid,
            "investigation": created,
            "message": f"Importado como copia independiente con nuevo UUID {created['short_uuid']}….",
        }

    created = _insert_bundle_atomic(service, bundle)
    return {
        "filename": filename,
        "outcome": "imported",
        "uuid": created["uuid"],
        "investigation": created,
        "message": f"Investigación «{created['name']}» importada conservando su UUID original.",
    }
