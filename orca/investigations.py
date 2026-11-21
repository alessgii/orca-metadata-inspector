"""Investigations (case management) domain service built on Orca's SQLite & analyzers."""
import hmac
import json
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .analysis import inspect_file
from .config import CHUNK, Settings
from .content import CONTENT_ANALYZERS, extract_content
from .duel import compare_files
from .exposure import analyze_exposure
from .hashes import AUTHENTICITY_NOTE, hash_chunks, normalize_algorithm
from .manifests import build_manifest, normalize_relative_path
from .similarity import ALGORITHM_VERSION as SIMILARITY_VERSION
from .storage import History
from .timeline import build_timeline
from .uploads import Input, safe_upload_name

ANALYZER_VERSION = f"orca-1.0/sim-{SIMILARITY_VERSION}"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

INVESTIGATION_KINDS = {
    "document_analysis": "Análisis documental",
    "integrity_verification": "Verificación de integridad",
    "exploratory_forensic": "Análisis forense exploratorio",
}
CLASSIFICATIONS = {
    "private": "Privada (informativa)",
    "confidential": "Confidencial (informativa)",
}
STATUSES = {
    "open": "Abierta",
    "paused": "Pausada",
    "closed": "Cerrada",
}
NOTE_TARGETS = {"investigation", "file", "comparison", "event", "relation"}
MANUAL_RELATION_TYPES = {"manual_confirmed", "manual_hypothesis"}

CLOCK_DISCLAIMER = (
    "Las fechas de operación proceden del reloj local del equipo (almacenadas en UTC) "
    "y no constituyen una fuente temporal certificada."
)
CLASSIFICATION_DISCLAIMER = (
    "La clasificación es únicamente informativa: Orca no aplica cifrado de base de datos ni control de acceso multiusuario."
)
ACTOR_DISCLAIMER = (
    "El actor corresponde al responsable declarado en la sesión local y no es una identidad criptográficamente verificada."
)
CONCLUSION_DISCLAIMER = (
    "Las conclusiones reflejan la valoración del investigador local y no constituyen una certificación pericial automática."
)


class InvestigationError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_uuid() -> str:
    """Generate a cryptographically secure, immutable UUIDv7 (or RFC 9562 v7 fallback)."""
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())
    ms = time.time_ns() // 1_000_000
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    b[6:16] = rand
    b[6] = (b[6] & 0x0F) | 0x70  # version 7
    b[8] = (b[8] & 0x3F) | 0x80  # RFC 4122/9562 variant
    return str(uuid.UUID(bytes=bytes(b)))


def validate_uuid(value: str, label: str = "UUID") -> str:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value.strip()):
        raise InvestigationError(f"El {label} proporcionado no tiene un formato UUID válido.")
    return value.strip().lower()


def _extract_declared_dates(raw: dict) -> tuple[str | None, str | None]:
    tl = build_timeline(raw)
    created = modified = None
    for section in ("instant", "local", "partial"):
        for event in tl["groups"][section]:
            if event.get("origin") != "internal":
                continue
            val = event.get("utc") or event.get("normalized")
            if not val:
                continue
            if event["kind"] == "created" and created is None:
                created = val
            elif event["kind"] == "modified" and modified is None:
                modified = val
    return created, modified


def _row_to_investigation(row) -> dict:
    u = row["uuid"]
    return {
        "uuid": u,
        "short_uuid": f"{u[:8]}…{u[-6:]}",
        "name": row["name"],
        "responsible": row["responsible"],
        "description": row["description"],
        "kind": row["kind"],
        "kind_label": INVESTIGATION_KINDS.get(row["kind"], row["kind"]),
        "classification": row["classification"],
        "classification_label": CLASSIFICATIONS.get(row["classification"], row["classification"]),
        "status": row["status"],
        "status_label": STATUSES.get(row["status"], row["status"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
        "origin_uuid": row["origin_uuid"],
        "origin_note": row["origin_note"],
        "schema_version": row["schema_version"],
    }


def _row_to_file(row) -> dict:
    return {
        "uuid": row["uuid"],
        "investigation_uuid": row["investigation_uuid"],
        "name": row["name"],
        "relative_path": row["relative_path"],
        "extension": row["extension"],
        "format": row["format"],
        "size": row["size"],
        "sha256": row["sha256"],
        "status": row["status"],
        "duplicate_of_uuid": row["duplicate_of_uuid"],
        "manual_order": row["manual_order"],
        "internal_created": row["internal_created"],
        "internal_modified": row["internal_modified"],
        "added_at": row["added_at"],
        "removed_at": row["removed_at"],
    }


class InvestigationService:
    def __init__(self, history: History, extractor, settings: Settings):
        self.history = history
        self.engine = extractor
        self.settings = settings

    def _connect(self):
        return self.history.connection()

    def _log_event(self, db, inv_uuid: str, event_type: str, actor: str, description: str,
                   related_uuids: list[str] | None = None, details: dict | None = None,
                   created_at: str | None = None) -> dict:
        ev = {
            "uuid": new_uuid(),
            "investigation_uuid": inv_uuid,
            "event_type": event_type,
            "created_at": created_at or utc_now(),
            "actor": actor.strip() or "Investigador local",
            "description": description,
            "related_uuids": related_uuids or [],
            "details": details or {},
        }
        db.execute(
            """
            INSERT INTO investigation_event
                (uuid, investigation_uuid, event_type, created_at, actor, description, related_uuids_json, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ev["uuid"], inv_uuid, ev["event_type"], ev["created_at"], ev["actor"],
                ev["description"], json.dumps(ev["related_uuids"], ensure_ascii=False),
                json.dumps(ev["details"], ensure_ascii=False),
            ),
        )
        db.execute("UPDATE investigation SET updated_at = ? WHERE uuid = ?", (ev["created_at"], inv_uuid))
        return ev

    def _require_investigation(self, db, inv_uuid: str, require_mutable: bool = False):
        inv_uuid = validate_uuid(inv_uuid, "UUID de investigación")
        db.row_factory = _dict_factory
        row = db.execute("SELECT * FROM investigation WHERE uuid = ?", (inv_uuid,)).fetchone()
        if not row:
            raise InvestigationError("La investigación solicitada no existe.", 404)
        if require_mutable and row["status"] == "closed":
            raise InvestigationError(
                "La investigación está cerrada y no admite modificaciones ordinarias. Reábrela de forma explícita si necesitas editarla.",
                409,
            )
        return row

    # --- Phase 1 & 2: CRUD & State Transitions ---

    def create(self, name: str, responsible: str, description: str = "",
               kind: str = "document_analysis", classification: str = "private") -> dict:
        clean_name = (name or "").strip()
        clean_resp = (responsible or "").strip()
        if not clean_name:
            raise InvestigationError("El nombre de la investigación es obligatorio.")
        if not clean_resp:
            raise InvestigationError("El nombre completo del responsable es obligatorio.")
        if len(clean_name) > 200:
            raise InvestigationError("El nombre de la investigación no puede superar 200 caracteres.")
        if len(clean_resp) > 160:
            raise InvestigationError("El nombre del responsable no puede superar 160 caracteres.")
        clean_desc = (description or "").strip()[:4000]
        if kind not in INVESTIGATION_KINDS:
            raise InvestigationError("Tipo de investigación no válido.")
        if classification not in CLASSIFICATIONS:
            raise InvestigationError("Clasificación no válida.")

        inv_uuid = new_uuid()
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO investigation
                    (uuid, name, responsible, description, kind, classification, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (inv_uuid, clean_name, clean_resp, clean_desc, kind, classification, now, now),
            )
            self._log_event(
                db, inv_uuid, "investigation_created", clean_resp,
                f"Investigación «{clean_name}» creada por {clean_resp}.",
                details={"kind": kind, "classification": classification},
                created_at=now,
            )
        return self.get(inv_uuid)

    def list(self, query: str | None = None, status: str | None = None, sort: str = "updated_at") -> list[dict]:
        clauses = []
        params = []
        if status:
            if status not in STATUSES:
                raise InvestigationError("Estado de filtro no válido.")
            clauses.append("i.status = ?")
            params.append(status)
        if query and query.strip():
            q = query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{q}%"
            clauses.append("(LOWER(i.name) LIKE ? ESCAPE '\\' OR LOWER(i.responsible) LIKE ? ESCAPE '\\' OR LOWER(i.uuid) LIKE ? ESCAPE '\\')")
            params.extend([like, like, like])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order_col = "i.created_at DESC, i.id DESC" if sort == "created_at" else "i.updated_at DESC, i.id DESC"
        sql = f"""
            SELECT i.*,
                   (SELECT COUNT(*) FROM investigation_file f WHERE f.investigation_uuid = i.uuid AND f.removed_at IS NULL) AS file_count,
                   (SELECT COUNT(*) FROM investigation_comparison c WHERE c.investigation_uuid = i.uuid) AS comparison_count,
                   (SELECT COUNT(*) FROM investigation_note n WHERE n.investigation_uuid = i.uuid) AS note_count
            FROM investigation i
            {where}
            ORDER BY {order_col}
        """
        with self._connect() as db:
            db.row_factory = _dict_factory
            rows = db.execute(sql, params).fetchall()
        return [
            {
                **_row_to_investigation(row),
                "file_count": row["file_count"],
                "comparison_count": row["comparison_count"],
                "note_count": row["note_count"],
            }
            for row in rows
        ]

    def get(self, inv_uuid: str) -> dict:
        with self._connect() as db:
            row = self._require_investigation(db, inv_uuid)
            file_count = db.execute(
                "SELECT COUNT(*) AS n FROM investigation_file WHERE investigation_uuid = ? AND removed_at IS NULL",
                (row["uuid"],),
            ).fetchone()["n"]
            comparison_count = db.execute(
                "SELECT COUNT(*) AS n FROM investigation_comparison WHERE investigation_uuid = ?",
                (row["uuid"],),
            ).fetchone()["n"]
            note_count = db.execute(
                "SELECT COUNT(*) AS n FROM investigation_note WHERE investigation_uuid = ?",
                (row["uuid"],),
            ).fetchone()["n"]
            event_count = db.execute(
                "SELECT COUNT(*) AS n FROM investigation_event WHERE investigation_uuid = ?",
                (row["uuid"],),
            ).fetchone()["n"]
            closures = db.execute(
                "SELECT * FROM investigation_closure WHERE investigation_uuid = ? ORDER BY id DESC",
                (row["uuid"],),
            ).fetchall()
        latest_closure = next((c for c in closures if c["action"] == "closed"), None)
        return {
            **_row_to_investigation(row),
            "file_count": file_count,
            "comparison_count": comparison_count,
            "note_count": note_count,
            "event_count": event_count,
            "latest_closure": latest_closure,
            "closures": closures,
            "disclaimers": {
                "clock": CLOCK_DISCLAIMER,
                "classification": CLASSIFICATION_DISCLAIMER,
                "actor": ACTOR_DISCLAIMER,
                "conclusion": CONCLUSION_DISCLAIMER,
            },
        }

    def update(self, inv_uuid: str, name: str | None = None, responsible: str | None = None,
               description: str | None = None, kind: str | None = None,
               classification: str | None = None, actor: str | None = None) -> dict:
        with self._connect() as db:
            row = self._require_investigation(db, inv_uuid, require_mutable=True)
            changes = {}
            new_name = row["name"]
            new_resp = row["responsible"]
            new_desc = row["description"]
            new_kind = row["kind"]
            new_class = row["classification"]

            if name is not None:
                clean = name.strip()
                if not clean:
                    raise InvestigationError("El nombre de la investigación no puede quedar vacío.")
                if len(clean) > 200:
                    raise InvestigationError("El nombre no puede superar 200 caracteres.")
                if clean != new_name:
                    changes["name"] = {"before": new_name, "after": clean}
                    new_name = clean
            if responsible is not None:
                clean = responsible.strip()
                if not clean:
                    raise InvestigationError("El responsable no puede quedar vacío.")
                if len(clean) > 160:
                    raise InvestigationError("El responsable no puede superar 160 caracteres.")
                if clean != new_resp:
                    changes["responsible"] = {"before": new_resp, "after": clean}
                    new_resp = clean
            if description is not None:
                clean = description.strip()[:4000]
                if clean != new_desc:
                    changes["description"] = {"before": new_desc, "after": clean}
                    new_desc = clean
            if kind is not None:
                if kind not in INVESTIGATION_KINDS:
                    raise InvestigationError("Tipo de investigación no válido.")
                if kind != new_kind:
                    changes["kind"] = {"before": new_kind, "after": kind}
                    new_kind = kind
            if classification is not None:
                if classification not in CLASSIFICATIONS:
                    raise InvestigationError("Clasificación no válida.")
                if classification != new_class:
                    changes["classification"] = {"before": new_class, "after": classification}
                    new_class = classification

            if changes:
                now = utc_now()
                db.execute(
                    """
                    UPDATE investigation
                    SET name = ?, responsible = ?, description = ?, kind = ?, classification = ?, updated_at = ?
                    WHERE uuid = ?
                    """,
                    (new_name, new_resp, new_desc, new_kind, new_class, now, row["uuid"]),
                )
                self._log_event(
                    db, row["uuid"], "investigation_edited", actor or new_resp,
                    "Información general de la investigación actualizada.",
                    details={"changes": changes},
                    created_at=now,
                )
        return self.get(inv_uuid)

    def transition_state(self, inv_uuid: str, action: str, actor: str | None = None,
                         summary: str = "", conclusion: str = "", final_notes: str = "",
                         reason: str = "") -> dict:
        with self._connect() as db:
            row = self._require_investigation(db, inv_uuid)
            current = row["status"]
            who = (actor or row["responsible"]).strip() or row["responsible"]
            now = utc_now()

            if action == "pause":
                if current != "open":
                    raise InvestigationError("Solo se puede pausar una investigación abierta.", 409)
                db.execute("UPDATE investigation SET status = 'paused', updated_at = ? WHERE uuid = ?", (now, row["uuid"]))
                self._log_event(
                    db, row["uuid"], "investigation_paused", who,
                    "Investigación pausada.",
                    details={"from": current, "to": "paused", "reason": reason.strip()},
                    created_at=now,
                )
            elif action == "resume":
                if current != "paused":
                    raise InvestigationError("Solo se puede reanudar una investigación pausada.", 409)
                db.execute("UPDATE investigation SET status = 'open', updated_at = ? WHERE uuid = ?", (now, row["uuid"]))
                self._log_event(
                    db, row["uuid"], "investigation_resumed", who,
                    "Investigación reanudada.",
                    details={"from": current, "to": "open"},
                    created_at=now,
                )
            elif action == "close":
                if current == "closed":
                    raise InvestigationError("La investigación ya se encuentra cerrada.", 409)
                clean_summary = (summary or "").strip()
                clean_conclusion = (conclusion or "").strip()
                clean_notes = (final_notes or "").strip()
                if not clean_summary:
                    raise InvestigationError("Debes incluir un resumen de hallazgos para cerrar la investigación.")
                if not clean_conclusion:
                    raise InvestigationError("Debes incluir una conclusión para cerrar la investigación.")
                closure_uuid = new_uuid()
                db.execute(
                    "UPDATE investigation SET status = 'closed', closed_at = ?, updated_at = ? WHERE uuid = ?",
                    (now, now, row["uuid"]),
                )
                db.execute(
                    """
                    INSERT INTO investigation_closure
                        (uuid, investigation_uuid, action, actor, summary, conclusion, final_notes, reason, created_at)
                    VALUES (?, ?, 'closed', ?, ?, ?, ?, '', ?)
                    """,
                    (closure_uuid, row["uuid"], who, clean_summary, clean_conclusion, clean_notes, now),
                )
                self._log_event(
                    db, row["uuid"], "investigation_closed", who,
                    f"Investigación cerrada por {who}.",
                    related_uuids=[closure_uuid],
                    details={"from": current, "to": "closed", "closure_uuid": closure_uuid},
                    created_at=now,
                )
            elif action == "reopen":
                if current != "closed":
                    raise InvestigationError("Solo se puede reabrir una investigación cerrada.", 409)
                clean_reason = (reason or "").strip()
                if not clean_reason:
                    raise InvestigationError("Debes indicar un motivo explícito para reabrir la investigación.")
                reopen_uuid = new_uuid()
                db.execute(
                    "UPDATE investigation SET status = 'open', closed_at = NULL, updated_at = ? WHERE uuid = ?",
                    (now, row["uuid"]),
                )
                db.execute(
                    """
                    INSERT INTO investigation_closure
                        (uuid, investigation_uuid, action, actor, summary, conclusion, final_notes, reason, created_at)
                    VALUES (?, ?, 'reopened', ?, '', '', '', ?, ?)
                    """,
                    (reopen_uuid, row["uuid"], who, clean_reason, now),
                )
                self._log_event(
                    db, row["uuid"], "investigation_reopened", who,
                    f"Investigación reabierta por {who}: {clean_reason}",
                    related_uuids=[reopen_uuid],
                    details={"from": current, "to": "open", "reason": clean_reason},
                    created_at=now,
                )
            else:
                raise InvestigationError("Acción de estado no válida. Usa: pause, resume, close o reopen.")
        return self.get(inv_uuid)

    def delete_investigation(self, inv_uuid: str) -> None:
        with self._connect() as db:
            row = self._require_investigation(db, inv_uuid)
            db.execute("DELETE FROM investigation WHERE uuid = ?", (row["uuid"],))

    # --- Phase 3: File Ingestion, Analysis & Duplicate Detection ---

    def ingest_file(self, inv_uuid: str, temp_path: Path, filename: str, size: int,
                    relative_path: str | None = None, actor: str | None = None) -> dict:
        """Analyze a temporary file using Orca's existing analyzer and persist ONLY extracted results."""
        clean_name = safe_upload_name(filename or "archivo")
        norm_rel = None
        if relative_path and relative_path.strip():
            try:
                norm_rel = normalize_relative_path(relative_path)
            except Exception:
                norm_rel = clean_name

        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid, require_mutable=True)
            who = (actor or inv["responsible"]).strip() or inv["responsible"]

        # Step 1: Compute SHA-256 & check duplicates in this investigation
        from .extraction import sha256 as file_sha256
        digest = file_sha256(temp_path)
        file_uuid = new_uuid()

        duplicate_of = None
        reused_analysis = None
        with self._connect() as db:
            db.row_factory = _dict_factory
            existing = db.execute(
                """
                SELECT f.*, a.engine_name, a.engine_version, a.analyzer_version,
                       a.raw_json, a.normalized_json, a.findings_json, a.errors_json,
                       a.warnings_json, a.structure_json
                FROM investigation_file f
                LEFT JOIN investigation_analysis a ON a.file_uuid = f.uuid
                WHERE f.investigation_uuid = ? AND f.sha256 = ? AND f.removed_at IS NULL
                ORDER BY f.id ASC LIMIT 1
                """,
                (inv["uuid"], digest),
            ).fetchone()
            if existing:
                duplicate_of = {"uuid": existing["uuid"], "name": existing["name"], "added_at": existing["added_at"]}
                if existing["raw_json"] is not None:
                    reused_analysis = existing

        # Step 2: Run existing analyzer (or safely reuse existing extracted results for identical SHA-256)
        now_start = utc_now()
        structure = None
        structure_warning = None

        if reused_analysis is not None:
            record = {
                "name": clean_name,
                "extension": reused_analysis["extension"],
                "format": reused_analysis["format"],
                "size": size,
                "sha256": digest,
                "status": reused_analysis["status"],
                "raw": json.loads(reused_analysis["raw_json"]),
                "normalized": json.loads(reused_analysis["normalized_json"]),
                "findings": json.loads(reused_analysis["findings_json"]),
                "errors": json.loads(reused_analysis["errors_json"]),
                "warnings": json.loads(reused_analysis["warnings_json"]),
            }
            structure = json.loads(reused_analysis["structure_json"]) if reused_analysis["structure_json"] else None
            engine_name = reused_analysis["engine_name"]
            engine_version = reused_analysis["engine_version"]
        else:
            record = inspect_file(Input(clean_name, temp_path, size), self.engine, self.settings)
            structure, structure_warning = extract_content(temp_path, record, self.settings)
            if structure_warning:
                record["warnings"] = [*record["warnings"], structure_warning]
            engine_info = self.engine.info
            engine_name = engine_info.get("name")
            engine_version = engine_info.get("version")

        internal_created, internal_modified = _extract_declared_dates(record["raw"])
        now_done = utc_now()

        with self._connect() as db:
            db.row_factory = _dict_factory
            max_order = db.execute(
                "SELECT COALESCE(MAX(manual_order), 0) AS m FROM investigation_file WHERE investigation_uuid = ?",
                (inv["uuid"],),
            ).fetchone()["m"]
            db.execute(
                """
                INSERT INTO investigation_file
                    (uuid, investigation_uuid, name, relative_path, extension, format, size, sha256,
                     status, duplicate_of_uuid, manual_order, internal_created, internal_modified, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_uuid, inv["uuid"], clean_name, norm_rel, record["extension"], record["format"],
                    size, digest, record["status"], duplicate_of["uuid"] if duplicate_of else None,
                    max_order + 1, internal_created, internal_modified, now_start,
                ),
            )
            db.execute(
                """
                INSERT INTO investigation_analysis
                    (file_uuid, engine_name, engine_version, analyzer_version, raw_json, normalized_json,
                     findings_json, errors_json, warnings_json, structure_json, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_uuid, engine_name, engine_version, ANALYZER_VERSION,
                    json.dumps(record["raw"], ensure_ascii=False),
                    json.dumps(record["normalized"], ensure_ascii=False),
                    json.dumps(record["findings"], ensure_ascii=False),
                    json.dumps(record["errors"], ensure_ascii=False),
                    json.dumps(record["warnings"], ensure_ascii=False),
                    json.dumps(structure, ensure_ascii=False) if structure is not None else None,
                    now_done,
                ),
            )
            self._log_event(
                db, inv["uuid"], "file_added", who,
                f"Archivo «{clean_name}» incorporado al expediente.",
                related_uuids=[file_uuid],
                details={"name": clean_name, "relative_path": norm_rel, "size": size, "sha256": digest, "format": record["format"]},
                created_at=now_start,
            )
            if duplicate_of:
                self._log_event(
                    db, inv["uuid"], "duplicate_detected", who,
                    f"Duplicado binario detectado: «{clean_name}» coincide en SHA-256 con «{duplicate_of['name']}». Se conservó como registro independiente.",
                    related_uuids=[file_uuid, duplicate_of["uuid"]],
                    details={"sha256": digest, "duplicate_of": duplicate_of, "reused_analysis": reused_analysis is not None},
                    created_at=now_start,
                )
            self._log_event(
                db, inv["uuid"], "analysis_started", who,
                f"Análisis iniciado para «{clean_name}»" + (" (reutilizando extracción previa verificada)." if reused_analysis else "."),
                related_uuids=[file_uuid],
                details={"reused": reused_analysis is not None},
                created_at=now_start,
            )
            if record["status"] == "error":
                self._log_event(
                    db, inv["uuid"], "analysis_error", who,
                    f"El análisis de «{clean_name}» finalizó con errores.",
                    related_uuids=[file_uuid],
                    details={"errors": record["errors"]},
                    created_at=now_done,
                )
            else:
                self._log_event(
                    db, inv["uuid"], "analysis_completed", who,
                    f"Análisis completado para «{clean_name}» ({record['format'] or 'Sin confirmar'}, {len(record['raw'])} propiedades).",
                    related_uuids=[file_uuid],
                    details={"status": record["status"], "format": record["format"], "property_count": len(record["raw"]),
                             "finding_count": len(record["findings"]), "has_structure": structure is not None},
                    created_at=now_done,
                )

        detail = self.get_file(inv["uuid"], file_uuid)
        detail["duplicate_warning"] = (
            f"Coincide en SHA-256 con «{duplicate_of['name']}» (incorporado el {duplicate_of['added_at']}). Se registró como evidencia independiente."
            if duplicate_of else None
        )
        return detail

    def list_files(self, inv_uuid: str, include_removed: bool = False) -> list[dict]:
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            clause = "" if include_removed else "AND f.removed_at IS NULL"
            rows = db.execute(
                f"""
                SELECT f.*, a.raw_json, a.findings_json, a.errors_json, a.warnings_json,
                       (a.structure_json IS NOT NULL) AS has_structure
                FROM investigation_file f
                LEFT JOIN investigation_analysis a ON a.file_uuid = f.uuid
                WHERE f.investigation_uuid = ? {clause}
                ORDER BY f.manual_order ASC, f.id ASC
                """,
                (inv["uuid"],),
            ).fetchall()
        result = []
        for row in rows:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
            findings = json.loads(row["findings_json"]) if row["findings_json"] else []
            errors = json.loads(row["errors_json"]) if row["errors_json"] else []
            warnings = json.loads(row["warnings_json"]) if row["warnings_json"] else []
            result.append({
                **_row_to_file(row),
                "field_count": len(raw),
                "finding_count": len(findings),
                "errors": errors,
                "warnings": warnings,
                "has_structure": bool(row["has_structure"]),
            })
        return result

    def get_file(self, inv_uuid: str, file_uuid: str) -> dict:
        file_uuid = validate_uuid(file_uuid, "UUID de archivo")
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            row = db.execute(
                """
                SELECT f.*, a.engine_name, a.engine_version, a.analyzer_version,
                       a.raw_json, a.normalized_json, a.findings_json,
                       a.errors_json, a.warnings_json, a.structure_json, a.analyzed_at
                FROM investigation_file f
                LEFT JOIN investigation_analysis a ON a.file_uuid = f.uuid
                WHERE f.investigation_uuid = ? AND f.uuid = ?
                """,
                (inv["uuid"], file_uuid),
            ).fetchone()
            if not row:
                raise InvestigationError("El archivo no existe en esta investigación.", 404)
        raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
        normalized = json.loads(row["normalized_json"]) if row["normalized_json"] else {}
        findings = json.loads(row["findings_json"]) if row["findings_json"] else []
        errors = json.loads(row["errors_json"]) if row["errors_json"] else []
        warnings = json.loads(row["warnings_json"]) if row["warnings_json"] else []
        structure = json.loads(row["structure_json"]) if row["structure_json"] else None
        record_for_tools = {
            "id": row["uuid"], "name": row["name"], "format": row["format"], "size": row["size"],
            "sha256": row["sha256"], "status": row["status"], "raw": raw, "normalized": normalized,
            "findings": findings, "errors": errors, "warnings": warnings,
        }
        tl = build_timeline(raw)
        exposure = analyze_exposure(record_for_tools, tl)
        return {
            **_row_to_file(row),
            "engine_name": row["engine_name"],
            "engine_version": row["engine_version"],
            "analyzer_version": row["analyzer_version"],
            "analyzed_at": row["analyzed_at"],
            "raw": raw,
            "normalized": normalized,
            "findings": findings,
            "errors": errors,
            "warnings": warnings,
            "has_structure": structure is not None,
            "structure_summary": _summarize_structure(structure),
            "timeline": tl,
            "exposure": exposure,
            "storage_note": "No se guarda copia permanente del archivo original. Algunas operaciones posteriores (como saneamiento o re-verificación física) requieren volver a seleccionar el original.",
        }

    def remove_file(self, inv_uuid: str, file_uuid: str, actor: str | None = None) -> dict:
        file_uuid = validate_uuid(file_uuid, "UUID de archivo")
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid, require_mutable=True)
            row = db.execute(
                "SELECT * FROM investigation_file WHERE investigation_uuid = ? AND uuid = ? AND removed_at IS NULL",
                (inv["uuid"], file_uuid),
            ).fetchone()
            if not row:
                raise InvestigationError("El archivo no existe o ya fue retirado.", 404)
            now = utc_now()
            db.execute("UPDATE investigation_file SET removed_at = ? WHERE uuid = ?", (now, file_uuid))
            who = (actor or inv["responsible"]).strip() or inv["responsible"]
            self._log_event(
                db, inv["uuid"], "file_removed", who,
                f"Archivo «{row['name']}» retirado del inventario activo del expediente.",
                related_uuids=[file_uuid],
                details={"name": row["name"], "sha256": row["sha256"]},
                created_at=now,
            )
        return {"uuid": file_uuid, "removed_at": now}

    def reorder_files(self, inv_uuid: str, ordered_uuids: list[str], actor: str | None = None) -> list[dict]:
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid, require_mutable=True)
            active = {
                row["uuid"]
                for row in db.execute(
                    "SELECT uuid FROM investigation_file WHERE investigation_uuid = ? AND removed_at IS NULL",
                    (inv["uuid"],),
                ).fetchall()
            }
            cleaned = [validate_uuid(u, "UUID de archivo") for u in ordered_uuids]
            if any(u not in active for u in cleaned):
                raise InvestigationError("La lista de orden contiene archivos que no pertenecen a esta investigación.")
            order_map = {u: idx + 1 for idx, u in enumerate(cleaned)}
            next_idx = len(cleaned) + 1
            for u in sorted(active - set(cleaned)):
                order_map[u] = next_idx
                next_idx += 1
            for u, pos in order_map.items():
                db.execute("UPDATE investigation_file SET manual_order = ? WHERE uuid = ?", (pos, u))
            now = utc_now()
            db.execute("UPDATE investigation SET updated_at = ? WHERE uuid = ?", (now, inv["uuid"]))
        return self.list_files(inv_uuid)

    def verify_file_hash(self, inv_uuid: str, file_uuid: str, actual_name: str,
                         actual_size: int, actual_sha256: str, actor: str | None = None) -> dict:
        file_uuid = validate_uuid(file_uuid, "UUID de archivo")
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            row = db.execute(
                "SELECT * FROM investigation_file WHERE investigation_uuid = ? AND uuid = ?",
                (inv["uuid"], file_uuid),
            ).fetchone()
            if not row:
                raise InvestigationError("El archivo no existe en esta investigación.", 404)
            expected = row["sha256"]
            if not expected:
                raise InvestigationError("El registro no tiene un SHA-256 guardado para verificar.", 409)
            matched = hmac.compare_digest(expected.lower(), actual_sha256.lower())
            name_changed = safe_upload_name(actual_name) != row["name"]
            status = "verified" if matched else "changed"
            msg = (
                "Integridad verificada (el nombre de archivo cambió, pero el contenido coincide según SHA-256)."
                if matched and name_changed else
                "Integridad verificada." if matched else
                "El archivo ha cambiado respecto al hash registrado en el expediente."
            )
            who = (actor or inv["responsible"]).strip() or inv["responsible"]
            self._log_event(
                db, inv["uuid"], "hash_verified", who,
                f"Verificación de hash sobre «{row['name']}»: {msg}",
                related_uuids=[file_uuid],
                details={"status": status, "expected_sha256": expected, "actual_sha256": actual_sha256,
                         "selected_name": safe_upload_name(actual_name), "name_changed": name_changed},
            )
        return {
            "status": status,
            "match": matched,
            "message": msg,
            "name_changed": name_changed,
            "expected": {"name": row["name"], "size": row["size"], "sha256": expected},
            "actual": {"name": safe_upload_name(actual_name), "size": actual_size, "sha256": actual_sha256},
            "disclaimer": AUTHENTICITY_NOTE,
        }

    # --- Phase 4: Multi-document Comparisons & Similarity Matrix ---

    def _load_file_for_duel(self, db, inv_uuid: str, file_uuid: str) -> tuple[dict, dict | None]:
        row = db.execute(
            """
            SELECT f.*, a.raw_json, a.normalized_json, a.findings_json, a.errors_json,
                   a.warnings_json, a.structure_json
            FROM investigation_file f
            LEFT JOIN investigation_analysis a ON a.file_uuid = f.uuid
            WHERE f.investigation_uuid = ? AND f.uuid = ? AND f.removed_at IS NULL
            """,
            (inv_uuid, file_uuid),
        ).fetchone()
        if not row:
            raise InvestigationError("Uno de los archivos no existe o fue retirado de la investigación.", 404)
        record = {
            "id": row["uuid"],
            "name": row["name"],
            "extension": row["extension"],
            "format": row["format"],
            "size": row["size"],
            "sha256": row["sha256"],
            "status": row["status"],
            "raw": json.loads(row["raw_json"]) if row["raw_json"] else {},
            "normalized": json.loads(row["normalized_json"]) if row["normalized_json"] else {},
            "findings": json.loads(row["findings_json"]) if row["findings_json"] else [],
            "errors": json.loads(row["errors_json"]) if row["errors_json"] else [],
            "warnings": json.loads(row["warnings_json"]) if row["warnings_json"] else [],
        }
        structure = json.loads(row["structure_json"]) if row["structure_json"] else None
        return record, structure

    def compare_pair(self, inv_uuid: str, file_a_uuid: str, file_b_uuid: str,
                     actor: str | None = None, force: bool = False) -> dict:
        a_uuid = validate_uuid(file_a_uuid, "UUID de archivo A")
        b_uuid = validate_uuid(file_b_uuid, "UUID de archivo B")
        if a_uuid == b_uuid:
            raise InvestigationError("Selecciona dos archivos distintos para comparar.")

        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            # Check cache in either direction when not forced
            if not force:
                cached = db.execute(
                    """
                    SELECT * FROM investigation_comparison
                    WHERE investigation_uuid = ?
                      AND ((file_a_uuid = ? AND file_b_uuid = ?) OR (file_a_uuid = ? AND file_b_uuid = ?))
                    """,
                    (inv["uuid"], a_uuid, b_uuid, b_uuid, a_uuid),
                ).fetchone()
                if cached and cached["file_a_uuid"] == a_uuid:
                    return self._format_comparison_row(cached)

            if inv["status"] == "closed":
                raise InvestigationError(
                    "La investigación está cerrada. Solo puedes consultar comparaciones ya guardadas.",
                    409,
                )

            rec_a, struct_a = self._load_file_for_duel(db, inv["uuid"], a_uuid)
            rec_b, struct_b = self._load_file_for_duel(db, inv["uuid"], b_uuid)

            result = compare_files(
                rec_a, rec_b, settings=self.settings,
                extracted_a=struct_a, extracted_b=struct_b,
            )
            now = utc_now()
            comp_uuid = new_uuid()
            algorithms = ["SHA-256", "ExifTool-semantic"]
            if result["content_comparison"]["status"] == "analyzed":
                algorithms.append(f"{result['content_comparison']['format']}-content-v{SIMILARITY_VERSION}")
            limitations = [
                *result["binary_identity"].get("notes", []),
                *result["similarity"]["methodology"].get("caveats", []),
            ]
            identical_bytes = result["binary_identity"]["identical_bytes"]
            sim_avail = 1 if result["similarity"]["overall_available"] else 0
            sim_overall = result["similarity"]["overall"]

            # Replace any prior cached comparison for the exact pair (or reverse pair)
            db.execute(
                """
                DELETE FROM investigation_comparison
                WHERE investigation_uuid = ?
                  AND ((file_a_uuid = ? AND file_b_uuid = ?) OR (file_a_uuid = ? AND file_b_uuid = ?))
                """,
                (inv["uuid"], a_uuid, b_uuid, b_uuid, a_uuid),
            )
            db.execute(
                """
                INSERT INTO investigation_comparison
                    (uuid, investigation_uuid, file_a_uuid, file_b_uuid, created_at,
                     algorithms_json, analyzer_version, identical_bytes, similarity_overall,
                     similarity_available, result_json, limitations_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comp_uuid, inv["uuid"], a_uuid, b_uuid, now,
                    json.dumps(algorithms, ensure_ascii=False), ANALYZER_VERSION,
                    None if identical_bytes is None else int(bool(identical_bytes)),
                    sim_overall, sim_avail,
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(limitations, ensure_ascii=False),
                ),
            )
            who = (actor or inv["responsible"]).strip() or inv["responsible"]
            sim_txt = f"{sim_overall:g}%" if sim_avail else "sin porcentaje global"
            self._log_event(
                db, inv["uuid"], "comparison_executed", who,
                f"Comparación ejecutada entre «{rec_a['name']}» y «{rec_b['name']}» ({sim_txt}).",
                related_uuids=[comp_uuid, a_uuid, b_uuid],
                details={"comparison_uuid": comp_uuid, "identical_bytes": identical_bytes,
                         "similarity_overall": sim_overall, "similarity_available": bool(sim_avail)},
                created_at=now,
            )
            saved = db.execute("SELECT * FROM investigation_comparison WHERE uuid = ?", (comp_uuid,)).fetchone()
        return self._format_comparison_row(saved)

    def _format_comparison_row(self, row) -> dict:
        return {
            "uuid": row["uuid"],
            "investigation_uuid": row["investigation_uuid"],
            "file_a_uuid": row["file_a_uuid"],
            "file_b_uuid": row["file_b_uuid"],
            "created_at": row["created_at"],
            "algorithms": json.loads(row["algorithms_json"]),
            "analyzer_version": row["analyzer_version"],
            "identical_bytes": None if row["identical_bytes"] is None else bool(row["identical_bytes"]),
            "similarity_overall": row["similarity_overall"],
            "similarity_available": bool(row["similarity_available"]),
            "limitations": json.loads(row["limitations_json"]),
            "result": json.loads(row["result_json"]),
        }

    def list_comparisons(self, inv_uuid: str) -> list[dict]:
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            rows = db.execute(
                """
                SELECT c.*, fa.name AS file_a_name, fb.name AS file_b_name
                FROM investigation_comparison c
                JOIN investigation_file fa ON fa.uuid = c.file_a_uuid
                JOIN investigation_file fb ON fb.uuid = c.file_b_uuid
                WHERE c.investigation_uuid = ?
                ORDER BY c.created_at DESC, c.id DESC
                """,
                (inv["uuid"],),
            ).fetchall()
        return [
            {
                "uuid": r["uuid"],
                "file_a_uuid": r["file_a_uuid"],
                "file_b_uuid": r["file_b_uuid"],
                "file_a_name": r["file_a_name"],
                "file_b_name": r["file_b_name"],
                "created_at": r["created_at"],
                "algorithms": json.loads(r["algorithms_json"]),
                "analyzer_version": r["analyzer_version"],
                "identical_bytes": None if r["identical_bytes"] is None else bool(r["identical_bytes"]),
                "similarity_overall": r["similarity_overall"],
                "similarity_available": bool(r["similarity_available"]),
            }
            for r in rows
        ]

    def get_comparison(self, inv_uuid: str, comp_uuid: str) -> dict:
        comp_uuid = validate_uuid(comp_uuid, "UUID de comparación")
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            row = db.execute(
                "SELECT * FROM investigation_comparison WHERE investigation_uuid = ? AND uuid = ?",
                (inv["uuid"], comp_uuid),
            ).fetchone()
            if not row:
                raise InvestigationError("La comparación solicitada no existe.", 404)
        return self._format_comparison_row(row)

    def similarity_matrix(self, inv_uuid: str) -> dict:
        """Build interactive similarity matrix using cached comparisons and instant SHA-256 identity."""
        files = self.list_files(inv_uuid)
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            comps = db.execute(
                "SELECT * FROM investigation_comparison WHERE investigation_uuid = ?",
                (inv["uuid"],),
            ).fetchall()
        by_pair = {}
        for c in comps:
            key = frozenset((c["file_a_uuid"], c["file_b_uuid"]))
            by_pair[key] = c

        cells = []
        pending_deep = 0
        total_pairs = len(files) * (len(files) - 1) // 2
        computed_pairs = 0

        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                fa, fb = files[i], files[j]
                key = frozenset((fa["uuid"], fb["uuid"]))
                cached = by_pair.get(key)
                same_bytes = bool(fa["sha256"] and fb["sha256"] and fa["sha256"] == fb["sha256"])
                deep_supported = (
                    fa["format"] == fb["format"]
                    and fa["format"] in CONTENT_ANALYZERS
                    and fa["has_structure"] and fb["has_structure"]
                )
                if cached:
                    computed_pairs += 1
                    cell = {
                        "file_a_uuid": fa["uuid"],
                        "file_b_uuid": fb["uuid"],
                        "comparison_uuid": cached["uuid"],
                        "status": "computed",
                        "identical_bytes": same_bytes,
                        "similarity_available": bool(cached["similarity_available"]),
                        "similarity_overall": cached["similarity_overall"],
                        "deep_supported": deep_supported,
                    }
                else:
                    if deep_supported and not same_bytes:
                        pending_deep += 1
                    cell = {
                        "file_a_uuid": fa["uuid"],
                        "file_b_uuid": fb["uuid"],
                        "comparison_uuid": None,
                        "status": "identical_binary" if same_bytes else ("pending" if deep_supported else "binary_only"),
                        "identical_bytes": same_bytes,
                        "similarity_available": False,
                        "similarity_overall": None,
                        "deep_supported": deep_supported,
                    }
                cells.append(cell)

        return {
            "files": [
                {"uuid": f["uuid"], "name": f["name"], "format": f["format"], "sha256": f["sha256"],
                 "has_structure": f["has_structure"]}
                for f in files
            ],
            "total_pairs": total_pairs,
            "computed_pairs": computed_pairs,
            "pending_deep_pairs": pending_deep,
            "cells": cells,
            "note": (
                "La matriz diferencia identidad binaria (SHA-256) de similitud de contenido profundo. "
                "Las parejas pendientes se calculan bajo demanda o en lotes acotados para evitar sobrecarga."
            ),
        }

    def compute_matrix_batch(self, inv_uuid: str, max_pairs: int = 10, actor: str | None = None) -> dict:
        """Progressively compute up to `max_pairs` uncomputed compatible pairs in the matrix."""
        max_pairs = max(1, min(25, int(max_pairs)))
        matrix = self.similarity_matrix(inv_uuid)
        pending = [c for c in matrix["cells"] if c["status"] == "pending"][:max_pairs]
        computed = 0
        for cell in pending:
            self.compare_pair(inv_uuid, cell["file_a_uuid"], cell["file_b_uuid"], actor=actor)
            computed += 1
        updated = self.similarity_matrix(inv_uuid)
        updated["batch_computed"] = computed
        return updated

    # --- Phase 5: Activity Timeline, Document Timeline & Relation Graph ---

    def list_events(self, inv_uuid: str, event_type: str | None = None) -> dict:
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            if event_type:
                rows = db.execute(
                    """
                    SELECT * FROM investigation_event
                    WHERE investigation_uuid = ? AND event_type = ?
                    ORDER BY id ASC
                    """,
                    (inv["uuid"], event_type),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM investigation_event WHERE investigation_uuid = ? ORDER BY id ASC",
                    (inv["uuid"],),
                ).fetchall()
        return {
            "append_only": True,
            "disclaimers": {"clock": CLOCK_DISCLAIMER, "actor": ACTOR_DISCLAIMER},
            "events": [
                {
                    "uuid": r["uuid"],
                    "event_type": r["event_type"],
                    "created_at": r["created_at"],
                    "actor": r["actor"],
                    "description": r["description"],
                    "related_uuids": json.loads(r["related_uuids_json"]),
                    "details": json.loads(r["details_json"]),
                }
                for r in rows
            ],
        }

    def add_relation(self, inv_uuid: str, source_file_uuid: str, target_file_uuid: str,
                     relation_type: str = "manual_confirmed", label: str = "",
                     notes: str = "", actor: str | None = None) -> dict:
        src = validate_uuid(source_file_uuid, "UUID de archivo origen")
        dst = validate_uuid(target_file_uuid, "UUID de archivo destino")
        if src == dst:
            raise InvestigationError("Una relación debe vincular dos archivos distintos.")
        if relation_type not in MANUAL_RELATION_TYPES:
            raise InvestigationError("Tipo de relación manual no válido. Usa manual_confirmed o manual_hypothesis.")
        confidence = "confirmada" if relation_type == "manual_confirmed" else "hipotesis"
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid, require_mutable=True)
            for uid in (src, dst):
                exists = db.execute(
                    "SELECT name FROM investigation_file WHERE investigation_uuid = ? AND uuid = ? AND removed_at IS NULL",
                    (inv["uuid"], uid),
                ).fetchone()
                if not exists:
                    raise InvestigationError("Ambos archivos deben existir y estar activos en la investigación.", 404)
            rel_uuid = new_uuid()
            now = utc_now()
            who = (actor or inv["responsible"]).strip() or inv["responsible"]
            clean_label = (label or ("Relación confirmada manualmente" if confidence == "confirmada" else "Hipótesis del investigador")).strip()[:160]
            clean_notes = (notes or "").strip()[:2000]
            db.execute(
                """
                INSERT INTO investigation_relation
                    (uuid, investigation_uuid, source_file_uuid, target_file_uuid, relation_type,
                     origin, confidence_label, label, notes, actor, created_at)
                VALUES (?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?)
                """,
                (rel_uuid, inv["uuid"], src, dst, relation_type, confidence, clean_label, clean_notes, who, now),
            )
            self._log_event(
                db, inv["uuid"], "relation_created", who,
                f"Relación manual ({confidence}) registrada: {clean_label}.",
                related_uuids=[rel_uuid, src, dst],
                details={"relation_type": relation_type, "confidence_label": confidence},
                created_at=now,
            )
            saved = db.execute("SELECT * FROM investigation_relation WHERE uuid = ?", (rel_uuid,)).fetchone()
        return dict(saved)

    def delete_relation(self, inv_uuid: str, rel_uuid: str, actor: str | None = None) -> None:
        rel_uuid = validate_uuid(rel_uuid, "UUID de relación")
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid, require_mutable=True)
            row = db.execute(
                "SELECT * FROM investigation_relation WHERE investigation_uuid = ? AND uuid = ? AND origin = 'manual'",
                (inv["uuid"], rel_uuid),
            ).fetchone()
            if not row:
                raise InvestigationError("La relación manual no existe.", 404)
            db.execute("DELETE FROM investigation_relation WHERE uuid = ?", (rel_uuid,))
            who = (actor or inv["responsible"]).strip() or inv["responsible"]
            self._log_event(
                db, inv["uuid"], "relation_removed", who,
                f"Relación manual retirada: {row['label']}.",
                related_uuids=[rel_uuid, row["source_file_uuid"], row["target_file_uuid"]],
            )

    def build_graph(self, inv_uuid: str) -> dict:
        """Build document relationship graph (nodes, automatic + manual edges, connected clusters)."""
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            file_rows = db.execute(
                """
                SELECT f.*, a.structure_json
                FROM investigation_file f
                LEFT JOIN investigation_analysis a ON a.file_uuid = f.uuid
                WHERE f.investigation_uuid = ? AND f.removed_at IS NULL
                ORDER BY f.manual_order ASC, f.id ASC
                """,
                (inv["uuid"],),
            ).fetchall()
            comp_rows = db.execute(
                "SELECT * FROM investigation_comparison WHERE investigation_uuid = ?",
                (inv["uuid"],),
            ).fetchall()
            manual_rows = db.execute(
                "SELECT * FROM investigation_relation WHERE investigation_uuid = ? ORDER BY id ASC",
                (inv["uuid"],),
            ).fetchall()

        active_ids = {r["uuid"] for r in file_rows}
        nodes = [
            {
                "uuid": r["uuid"],
                "name": r["name"],
                "format": r["format"],
                "size": r["size"],
                "sha256": r["sha256"],
                "added_at": r["added_at"],
                "internal_created": r["internal_created"],
                "internal_modified": r["internal_modified"],
                "manual_order": r["manual_order"],
                "status": r["status"],
            }
            for r in file_rows
        ]
        edges = []
        seen_auto = set()

        # 1. Automatic: identical SHA-256 hashes
        for i in range(len(file_rows)):
            for j in range(i + 1, len(file_rows)):
                a, b = file_rows[i], file_rows[j]
                if a["sha256"] and b["sha256"] and a["sha256"] == b["sha256"]:
                    key = ("identical_hash", min(a["uuid"], b["uuid"]), max(a["uuid"], b["uuid"]))
                    seen_auto.add(key)
                    edges.append({
                        "uuid": f"auto-hash-{a['uuid'][:8]}-{b['uuid'][:8]}",
                        "source": a["uuid"],
                        "target": b["uuid"],
                        "relation_type": "identical_hash",
                        "origin": "automatic",
                        "confidence_label": "confirmada",
                        "label": "SHA-256 idéntico (mismos bytes)",
                        "notes": "Ambos registros comparten exactamente la misma huella binaria.",
                    })

        # 2. Automatic: shared media assets in extracted PPTX structures
        media_sets = {}
        for r in file_rows:
            if r["structure_json"]:
                struct = json.loads(r["structure_json"])
                hashes = {m["sha256"] for m in struct.get("media", []) if m.get("sha256")}
                if hashes:
                    media_sets[r["uuid"]] = hashes
        for i in range(len(file_rows)):
            for j in range(i + 1, len(file_rows)):
                ua, ub = file_rows[i]["uuid"], file_rows[j]["uuid"]
                if ua in media_sets and ub in media_sets:
                    shared = media_sets[ua] & media_sets[ub]
                    if shared and ("identical_hash", min(ua, ub), max(ua, ub)) not in seen_auto:
                        edges.append({
                            "uuid": f"auto-media-{ua[:8]}-{ub[:8]}",
                            "source": ua,
                            "target": ub,
                            "relation_type": "shared_media",
                            "origin": "automatic",
                            "confidence_label": "sugerida",
                            "label": f"{len(shared)} recurso(s) multimedia compartido(s)",
                            "notes": "Relación sugerida por coincidencia SHA-256 de recursos incrustados; no prueba derivación histórica.",
                        })

        # 3. Automatic: from persisted comparisons (equivalent content or structural similarity)
        for c in comp_rows:
            ua, ub = c["file_a_uuid"], c["file_b_uuid"]
            if ua not in active_ids or ub not in active_ids:
                continue
            if ("identical_hash", min(ua, ub), max(ua, ub)) in seen_auto:
                continue
            res = json.loads(c["result_json"])
            cc = res.get("content_comparison", {})
            sim = res.get("similarity", {})
            if cc.get("status") == "analyzed":
                if cc.get("summary", {}).get("substantive_identical"):
                    edges.append({
                        "uuid": f"auto-eq-{c['uuid'][:8]}",
                        "source": ua,
                        "target": ub,
                        "relation_type": "equivalent_content",
                        "origin": "automatic",
                        "confidence_label": "confirmada",
                        "label": "Contenido equivalente con metadatos/empaquetado distintos",
                        "comparison_uuid": c["uuid"],
                        "similarity": sim.get("overall"),
                        "notes": "El contenido extraído coincide; las diferencias corresponden a metadatos o serialización.",
                    })
                elif sim.get("overall_available") and (sim.get("overall") or 0) >= 40.0:
                    edges.append({
                        "uuid": f"auto-sim-{c['uuid'][:8]}",
                        "source": ua,
                        "target": ub,
                        "relation_type": "structural_similarity",
                        "origin": "automatic",
                        "confidence_label": "hipotesis",
                        "label": f"Similitud documental {sim['overall']:g}% (hipótesis)",
                        "comparison_uuid": c["uuid"],
                        "similarity": sim.get("overall"),
                        "notes": "Posible relación inferida solo por similitud de contenido/estructura; requiere verificación humana.",
                    })

        # 4. Manual relations
        for m in manual_rows:
            if m["source_file_uuid"] in active_ids and m["target_file_uuid"] in active_ids:
                edges.append({
                    "uuid": m["uuid"],
                    "source": m["source_file_uuid"],
                    "target": m["target_file_uuid"],
                    "relation_type": m["relation_type"],
                    "origin": "manual",
                    "confidence_label": m["confidence_label"],
                    "label": m["label"],
                    "notes": m["notes"],
                    "actor": m["actor"],
                    "created_at": m["created_at"],
                })

        # Compute connected clusters
        adj = defaultdict(set)
        for e in edges:
            adj[e["source"]].add(e["target"])
            adj[e["target"]].add(e["source"])
        visited = set()
        clusters = []
        for node in nodes:
            uid = node["uuid"]
            if uid in visited:
                continue
            comp = []
            stack = [uid]
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                comp.append(cur)
                stack.extend(adj[cur] - visited)
            if len(comp) > 1:
                clusters.append(comp)

        return {
            "nodes": nodes,
            "edges": edges,
            "clusters": clusters,
            "disclaimer": (
                "Las conexiones automáticas describen coincidencias calculadas (hash, recursos o similitud) "
                "y no prueban por sí mismas parentesco histórico ni orden de autoría."
            ),
        }

    def document_timeline(self, inv_uuid: str, sort: str = "added") -> dict:
        if sort not in {"added", "created", "modified", "manual"}:
            raise InvestigationError("Criterio de orden documental no válido. Usa: added, created, modified o manual.")
        graph = self.build_graph(inv_uuid)
        nodes = list(graph["nodes"])
        if sort == "manual":
            nodes.sort(key=lambda n: (n["manual_order"], n["added_at"]))
        elif sort == "created":
            nodes.sort(key=lambda n: (n["internal_created"] is None, n["internal_created"] or "", n["added_at"]))
        elif sort == "modified":
            nodes.sort(key=lambda n: (n["internal_modified"] is None, n["internal_modified"] or "", n["added_at"]))
        else:
            nodes.sort(key=lambda n: n["added_at"])
        return {
            "sort": sort,
            "nodes": nodes,
            "relations": graph["edges"],
            "disclaimers": [
                "Las fechas internas de creación y modificación son valores declarados por cada archivo; pueden estar alteradas, incompletas o sin zona horaria.",
                "Una fecha más reciente no demuestra que un documento derive de otro.",
            ],
        }

    # --- Phase 6: Versioned Researcher Notes ---

    def _validate_note_target(self, db, inv_uuid: str, target_type: str, target_uuid: str | None) -> str | None:
        if target_type not in NOTE_TARGETS:
            raise InvestigationError("Tipo de destino de observación no válido.")
        if target_type == "investigation":
            return None
        uid = validate_uuid(target_uuid or "", f"UUID de {target_type}")
        table_map = {
            "file": "investigation_file",
            "comparison": "investigation_comparison",
            "event": "investigation_event",
            "relation": "investigation_relation",
        }
        row = db.execute(
            f"SELECT uuid FROM {table_map[target_type]} WHERE investigation_uuid = ? AND uuid = ?",
            (inv_uuid, uid),
        ).fetchone()
        if not row:
            raise InvestigationError("El elemento referenciado por la observación no existe en esta investigación.", 404)
        return uid

    def create_note(self, inv_uuid: str, text: str, target_type: str = "investigation",
                    target_uuid: str | None = None, author: str | None = None) -> dict:
        clean_text = (text or "").strip()
        if not clean_text:
            raise InvestigationError("El texto de la observación no puede estar vacío.")
        if len(clean_text) > 10_000:
            raise InvestigationError("La observación supera el límite de 10 000 caracteres.")
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid, require_mutable=True)
            resolved_target = self._validate_note_target(db, inv["uuid"], target_type, target_uuid)
            who = (author or inv["responsible"]).strip() or inv["responsible"]
            note_uuid = new_uuid()
            now = utc_now()
            db.execute(
                """
                INSERT INTO investigation_note
                    (uuid, investigation_uuid, target_type, target_uuid, author, text, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (note_uuid, inv["uuid"], target_type, resolved_target, who, clean_text, now, now),
            )
            db.execute(
                """
                INSERT INTO investigation_note_version (note_uuid, version, author, text, created_at)
                VALUES (?, 1, ?, ?, ?)
                """,
                (note_uuid, who, clean_text, now),
            )
            related = [note_uuid] + ([resolved_target] if resolved_target else [])
            self._log_event(
                db, inv["uuid"], "note_created", who,
                f"Observación del investigador registrada (vínculo: {target_type}).",
                related_uuids=related,
                details={"target_type": target_type, "target_uuid": resolved_target},
                created_at=now,
            )
        return self.get_note(inv_uuid, note_uuid)

    def update_note(self, inv_uuid: str, note_uuid: str, text: str, author: str | None = None) -> dict:
        note_uuid = validate_uuid(note_uuid, "UUID de observación")
        clean_text = (text or "").strip()
        if not clean_text:
            raise InvestigationError("El texto de la observación no puede estar vacío.")
        if len(clean_text) > 10_000:
            raise InvestigationError("La observación supera el límite de 10 000 caracteres.")
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid, require_mutable=True)
            row = db.execute(
                "SELECT * FROM investigation_note WHERE investigation_uuid = ? AND uuid = ?",
                (inv["uuid"], note_uuid),
            ).fetchone()
            if not row:
                raise InvestigationError("La observación no existe en esta investigación.", 404)
            if clean_text == row["text"]:
                return self.get_note(inv_uuid, note_uuid)
            next_version = row["version"] + 1
            who = (author or inv["responsible"]).strip() or inv["responsible"]
            now = utc_now()
            db.execute(
                """
                UPDATE investigation_note
                SET text = ?, author = ?, version = ?, updated_at = ?
                WHERE uuid = ?
                """,
                (clean_text, who, next_version, now, note_uuid),
            )
            db.execute(
                """
                INSERT INTO investigation_note_version (note_uuid, version, author, text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (note_uuid, next_version, who, clean_text, now),
            )
            self._log_event(
                db, inv["uuid"], "note_updated", who,
                f"Observación modificada (versión {next_version}; se conserva el historial anterior).",
                related_uuids=[note_uuid],
                details={"version": next_version},
                created_at=now,
            )
        return self.get_note(inv_uuid, note_uuid)

    def get_note(self, inv_uuid: str, note_uuid: str) -> dict:
        note_uuid = validate_uuid(note_uuid, "UUID de observación")
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            row = db.execute(
                "SELECT * FROM investigation_note WHERE investigation_uuid = ? AND uuid = ?",
                (inv["uuid"], note_uuid),
            ).fetchone()
            if not row:
                raise InvestigationError("La observación no existe.", 404)
            versions = db.execute(
                "SELECT version, author, text, created_at FROM investigation_note_version WHERE note_uuid = ? ORDER BY version DESC",
                (note_uuid,),
            ).fetchall()
        return {
            "uuid": row["uuid"],
            "investigation_uuid": row["investigation_uuid"],
            "target_type": row["target_type"],
            "target_uuid": row["target_uuid"],
            "author": row["author"],
            "text": row["text"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "source_kind": "human_observation",
            "versions": versions,
        }

    def list_notes(self, inv_uuid: str) -> list[dict]:
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            rows = db.execute(
                "SELECT * FROM investigation_note WHERE investigation_uuid = ? ORDER BY updated_at DESC, id DESC",
                (inv["uuid"],),
            ).fetchall()
            versions_by_note = defaultdict(list)
            for v in db.execute(
                """
                SELECT v.note_uuid, v.version, v.author, v.text, v.created_at
                FROM investigation_note_version v
                JOIN investigation_note n ON n.uuid = v.note_uuid
                WHERE n.investigation_uuid = ?
                ORDER BY v.version DESC
                """,
                (inv["uuid"],),
            ).fetchall():
                versions_by_note[v["note_uuid"]].append({
                    "version": v["version"], "author": v["author"],
                    "text": v["text"], "created_at": v["created_at"],
                })
        return [
            {
                "uuid": r["uuid"],
                "investigation_uuid": r["investigation_uuid"],
                "target_type": r["target_type"],
                "target_uuid": r["target_uuid"],
                "author": r["author"],
                "text": r["text"],
                "version": r["version"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "source_kind": "human_observation",
                "versions": versions_by_note[r["uuid"]],
            }
            for r in rows
        ]

    # --- Manifest & Complete Report (including AI-ready separation of facts vs human notes) ---

    def generate_manifest(self, inv_uuid: str, actor: str | None = None) -> dict:
        with self._connect() as db:
            inv = self._require_investigation(db, inv_uuid)
            rows = db.execute(
                """
                SELECT name, relative_path, size, sha256
                FROM investigation_file
                WHERE investigation_uuid = ? AND removed_at IS NULL AND sha256 IS NOT NULL
                ORDER BY id ASC
                """,
                (inv["uuid"],),
            ).fetchall()
            if not rows:
                raise InvestigationError("No hay archivos activos con SHA-256 para generar un manifiesto.")
            entries = []
            used_paths = set()
            for idx, r in enumerate(rows, start=1):
                base_path = r["relative_path"] or r["name"]
                candidate = normalize_relative_path(base_path)
                if candidate.casefold() in used_paths:
                    stem, dot, ext = candidate.rpartition(".")
                    candidate = f"{stem}-{idx}{dot}{ext}" if dot else f"{candidate}-{idx}"
                used_paths.add(candidate.casefold())
                entries.append({"path": candidate, "size": r["size"], "hash": r["sha256"]})
            manifest = build_manifest(entries, algorithm="SHA-256")
            who = (actor or inv["responsible"]).strip() or inv["responsible"]
            self._log_event(
                db, inv["uuid"], "manifest_generated", who,
                f"Manifiesto de integridad SHA-256 generado con {len(entries)} archivo(s).",
                details={"file_count": len(entries), "algorithm": "SHA-256"},
            )
        return manifest

    def build_report(self, inv_uuid: str) -> dict:
        """Complete report payload with explicit separation for future local/consented AI assistants."""
        overview = self.get(inv_uuid)
        files = [self.get_file(inv_uuid, f["uuid"]) for f in self.list_files(inv_uuid)]
        comparisons = [self.get_comparison(inv_uuid, c["uuid"]) for c in self.list_comparisons(inv_uuid)]
        activity = self.list_events(inv_uuid)
        doc_timeline = self.document_timeline(inv_uuid, sort="added")
        graph = self.build_graph(inv_uuid)
        notes = self.list_notes(inv_uuid)
        return {
            "schema_version": 1,
            "generated_at": utc_now(),
            "investigation": overview,
            "deterministic_facts": {
                "files": files,
                "comparisons": comparisons,
                "document_timeline": doc_timeline,
                "relation_graph": {
                    "nodes": graph["nodes"],
                    "automatic_edges": [e for e in graph["edges"] if e["origin"] == "automatic"],
                    "clusters": graph["clusters"],
                },
                "activity_timeline": activity["events"],
            },
            "human_observations": {
                "notes": notes,
                "manual_relations": [e for e in graph["edges"] if e["origin"] == "manual"],
                "closures": overview["closures"],
            },
            "ai_context": {
                "ready": True,
                "schema_version": 1,
                "external_transmission": False,
                "policy": "Solo consulta local. Cualquier asistencia futura de IA deberá distinguir explícitamente hipótesis generadas por IA de los hechos deterministas.",
                "capabilities_supported": [
                    "summarize_changes", "explain_differences", "suggest_potential_relations",
                    "draft_conclusions", "answer_file_questions",
                ],
            },
        }


def _summarize_structure(structure: dict | None) -> dict | None:
    if not isinstance(structure, dict):
        return None
    if structure.get("format") == "PPTX":
        return {
            "format": "PPTX",
            "slide_count": structure.get("slide_count", 0),
            "media_count": len(structure.get("media", [])),
            "titles": [s.get("title") for s in structure.get("slides", []) if s.get("title")][:10],
        }
    return {"format": structure.get("format")}


def _dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}
