import json
import logging
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4

from .analysis import file_row, inspect_file, summarize
from .cleanup import clean
from .extraction import EngineError
from .storage import History
from .uploads import ExpansionBudget, Input, UploadError

logger = logging.getLogger(__name__)
METADATA_BUDGET = 16_000_000


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    work: Path
    inputs: list[Input]
    keep: bool
    expansion: ExpansionBudget
    created: str = field(default_factory=utc_now)
    records: list[dict] = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    copies: dict = field(default_factory=dict)
    state: str = "queued"
    started: float = field(default_factory=time.monotonic)
    expires: float = float("inf")
    downloads: int = 0
    result: dict | None = None
    issue: str | None = None
    current: str | None = None


class Sessions:
    def __init__(self, settings, extractor):
        self.settings = settings
        self.engine = extractor
        self.workspace = settings.data.resolve() / "temporary"
        self.history = History(self.workspace.parent / "history.sqlite3")
        self.active: dict[str, Job] = {}
        self.guard = RLock()
        self.operation = Lock()

    def reserve(self):
        if not self.operation.acquire(blocking=False):
            raise UploadError("Hay otra operación de archivos en curso. Vuelve a intentarlo.", 409)

    def _drop(self, job):
        if job.downloads:
            raise UploadError("Espera a que termine la descarga antes de cerrar la sesión.", 409)
        shutil.rmtree(job.work)
        self.active.pop(job.id)

    def reap(self):
        with self.guard:
            for job in list(self.active.values()):
                if job.expires <= time.monotonic() and not job.downloads:
                    try:
                        self._drop(job)
                    except OSError:
                        logger.warning("No se pudo retirar un directorio temporal. Se volverá a intentar.")

    def create(self, work, inputs, keep, expansion):
        with self.guard:
            self.reap()
            if len(self.active) >= self.settings.max_live:
                # Retain database records if the user opted in.
                candidate = next((job for job in self.active.values() if not job.downloads), None)
                if candidate is None:
                    raise UploadError("Las sesiones temporales están ocupadas con descargas.", 409)
                self._drop(candidate)
            job = Job(work.name, work, inputs, keep, expansion)
            self.active[job.id] = job
            return job

    def process(self, job):
        try:
            with self.guard:
                job.state = "processing"
            remaining = METADATA_BUDGET
            for file in job.inputs:
                with self.guard:
                    job.current = file.name
                record = inspect_file(file, self.engine, self.settings, expansion=job.expansion)
                length = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
                if length > remaining:
                    record.update(raw={}, normalized={}, findings=[], status="error")
                    record["errors"].append("El resultado excede el presupuesto de metadatos de la sesión.")
                else:
                    remaining -= length
                with self.guard:
                    job.records.append(record)
                    job.sources[record["id"]] = file.path
            result = {
                "id": job.id, "created": job.created, "duration_ms": round((time.monotonic() - job.started) * 1000),
                "engine": self.engine.info, "saved": job.keep, "summary": summarize(job.records),
            }
            if job.keep:
                try:
                    self.history.save(result, job.records)
                except Exception:
                    logger.exception("Falló la escritura del historial local.")
                    result["saved"] = False
                    result["notice"] = "El historial local no pudo guardar la sesión."
            with self.guard:
                job.result = result
                job.state = "complete"
        except Exception:
            logger.exception("Falló una sesión de análisis.")
            with self.guard:
                job.state = "failed"
                job.issue = "La sesión no pudo finalizar por un error interno."
        finally:
            with self.guard:
                job.current = None
                job.expires = time.monotonic() + self.settings.ttl
            self.operation.release()

    def status(self, session_id, include_files=True):
        with self.guard:
            job = self.active.get(session_id)
            if job:
                data = {"id": job.id, "state": job.state, "created": job.created,
                        "processed": len(job.records), "total": len(job.inputs),
                        "current": job.current, "temporary": True, "error": job.issue}
                if job.result:
                    data.update(job.result)
                    if include_files:
                        data["files"] = [file_row(record) for record in job.records]
                return data
            result = self.history.session(session_id)
            if not result:
                raise UploadError("La sesión no existe o su tiempo de retención terminó.", 404)
            result.update(state="complete", temporary=False, processed=result["summary"]["count"],
                          total=result["summary"]["count"])
            if include_files:
                result["files"] = [file_row(record) for record in self.history.files(session_id)]
            return result

    def list_saved(self):
        return [{"id": item["id"], "created": item["created"], "duration_ms": item["duration_ms"],
                 "count": item["summary"]["count"], "findings": item["summary"]["findings"]}
                for item in self.history.list()]

    def records(self, session_id, file_id=None):
        with self.guard:
            job = self.active.get(session_id)
            if job:
                if job.state != "complete":
                    raise UploadError("Los resultados estarán disponibles al terminar el análisis.", 409)
                rows = [record for record in job.records if not file_id or record["id"] == file_id]
            else:
                self.status(session_id, include_files=False)
                rows = self.history.files(session_id, file_id)
            if file_id and not rows:
                raise UploadError("El archivo no existe en esta sesión.", 404)
            return rows

    def delete(self, session_id=None):
        self.reserve()
        try:
            with self.guard:
                if session_id:
                    job = self.active.get(session_id)
                    if job:
                        self._drop(job)
                    self.history.delete(session_id)
                else:
                    self.history.delete()
                    for job in self.active.values():
                        if job.result and job.result["saved"]:
                            job.result = {**job.result, "saved": False}
        finally:
            self.operation.release()

    def sanitize(self, session_id, file_id):
        self.reserve()
        job = None
        target = None
        try:
            with self.guard:
                self.reap()
                job = self.active.get(session_id)
                if not job:
                    raise UploadError("La copia temporal no está disponible. Vuelve a cargar el original.", 410)
                record, = self.records(session_id, file_id)
                if file_id in job.copies:
                    return job.copies[file_id][1]
                target = job.work / f"{uuid4().hex}.{record['extension']}"
                source = job.sources[file_id]
                job.expires = float("inf")
            result = clean(source, target, record, self.engine, self.settings)
            with self.guard:
                job.copies[file_id] = (target, result)
            return result
        except EngineError as exc:
            raise UploadError(str(exc)) from exc
        finally:
            with self.guard:
                if target and file_id not in job.copies:
                    target.unlink(missing_ok=True)
                if job:
                    job.expires = time.monotonic() + self.settings.ttl
            self.operation.release()

    def cached_cleanup(self, session_id, file_id):
        with self.guard:
            job = self.active.get(session_id)
            copy = job.copies.get(file_id) if job and job.expires > time.monotonic() else None
            return copy[1] if copy else None

    def open_copy(self, session_id, file_id):
        with self.guard:
            self.reap()
            job = self.active.get(session_id)
            if not job or file_id not in job.copies:
                raise UploadError("La copia no existe o caducó.", 404)
            path, result = job.copies[file_id]
            stream = path.open("rb")
            job.downloads += 1
            return stream, job, result["after"]

    @contextmanager
    def pin_sources(self, session_id, file_ids):
        job = None
        paths = {}
        with self.guard:
            self.reap()
            candidate = self.active.get(session_id)
            if candidate and candidate.state == "complete" and candidate.expires > time.monotonic():
                job = candidate
                job.downloads += 1
                for fid in file_ids:
                    path = job.sources.get(fid)
                    if path and path.is_file():
                        paths[fid] = path
        try:
            yield paths
        finally:
            if job is not None:
                with self.guard:
                    job.downloads -= 1
