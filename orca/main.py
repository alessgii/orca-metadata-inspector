import asyncio
import hmac
import json
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from urllib.parse import quote, unquote
from uuid import uuid4
from typing import Annotated, Literal

from anyio import to_thread
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .cleanup import POLICIES
from .config import CHUNK, MAX_BYTES, MAX_FILES, ROOT, Settings
from .duel import compare_files
from .duel_engine import DuelBatchJob, IMAGE_FORMATS, build_duel_matrix
from .evidence import format_evidence
from .exposure import analyze_exposure
from .extraction import ExifTool
from .image_analysis import ImageSafetyError, generate_diff_map_png
from .hashes import (
    AUTHENTICITY_NOTE,
    DEFAULT_ALGORITHM,
    HashError,
    compare_digests,
    hash_async_chunks,
    hash_chunks,
    hash_history_csv,
    normalize_algorithm,
    normalize_algorithms,
    validate_hex_digest,
)
from .investigations import InvestigationError, InvestigationService
from .manifests import (
    ManifestError,
    build_manifest,
    normalize_relative_path,
    verify_manifest,
)
from .metadata import properties
from .portability import export_investigation_zip, import_package, preview_packages
from .reports import as_csv, payload
from .sessions import Sessions, utc_now
from .timeline import build_timeline
from .uploads import FORMATS, UploadError, consume, extension, file_chunks, prepare, safe_upload_name

ASSETS = ROOT / "orca"
TEMPLATES = Jinja2Templates(directory=ASSETS / "templates")
TEMPLATES.env.policies["json.dumps_kwargs"] = {"sort_keys": False}


StoredId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
CompareId = Annotated[str, Query(pattern=r"^[a-f0-9]{32}$")]
UuidParam = Annotated[str, Path(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")]


class EngineInfo(BaseModel):
    name: str
    version: str | None
    available: bool
    options: str


class Started(BaseModel):
    id: str


class HashCompareRequest(BaseModel):
    algorithm: str = DEFAULT_ALGORITHM
    hash_a: str = ""
    hash_b: str = ""


class HashRecordRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    size: int = Field(ge=0)
    algorithm: str = DEFAULT_ALGORITHM
    hash: str


class InvestigationCreateRequest(BaseModel):
    name: str = ""
    responsible: str = ""
    description: str = ""
    kind: str = "document_analysis"
    classification: str = "private"


class InvestigationUpdateRequest(BaseModel):
    name: str | None = None
    responsible: str | None = None
    description: str | None = None
    kind: str | None = None
    classification: str | None = None
    actor: str | None = None


class InvestigationStateRequest(BaseModel):
    action: str
    actor: str | None = None
    summary: str = ""
    conclusion: str = ""
    final_notes: str = ""
    reason: str = ""


class InvestigationCompareRequest(BaseModel):
    file_a_uuid: str
    file_b_uuid: str
    actor: str | None = None
    force: bool = False


class InvestigationOrderRequest(BaseModel):
    file_uuids: list[str]
    actor: str | None = None


class InvestigationRelationRequest(BaseModel):
    source_file_uuid: str
    target_file_uuid: str
    relation_type: str = "manual_confirmed"
    label: str = ""
    notes: str = ""
    actor: str | None = None


class InvestigationNoteCreateRequest(BaseModel):
    text: str = ""
    target_type: str = "investigation"
    target_uuid: str | None = None
    author: str | None = None


class InvestigationNoteUpdateRequest(BaseModel):
    text: str = ""
    author: str | None = None


class InvestigationMatrixBatchRequest(BaseModel):
    max_pairs: int = 10
    actor: str | None = None


class DuelMatrixBatchRequest(BaseModel):
    max_pairs: int = Field(default=10, ge=1, le=200)
    reference_id: str | None = None


def attachment(name):
    filename = safe_upload_name(name)
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"


def parse_algorithm_params(values: list[str] | None) -> list[str]:
    if not values:
        return [DEFAULT_ALGORITHM]
    parsed = []
    for entry in values:
        for part in entry.split(","):
            if part.strip():
                parsed.append(part.strip())
    return normalize_algorithms(parsed)


def create_app(settings: Settings | None = None, extractor=None):
    settings = settings or Settings()

    async def housekeeping(service):
        while True:
            await asyncio.sleep(min(settings.ttl, 15))
            await to_thread.run_sync(service.reap)

    @asynccontextmanager
    async def lifespan(app):
        service = Sessions(settings, extractor or ExifTool(settings.timeout))
        root = service.workspace.parent
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = sqlite3.connect(root / "instance.sqlite3", timeout=0)
        try:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise RuntimeError("Ya hay una instancia usando este directorio de datos.") from exc
            if service.workspace.exists():
                shutil.rmtree(service.workspace)
            service.workspace.mkdir(mode=0o700)
            service.history.initialize()
            app.state.service = service
            app.state.investigations = InvestigationService(service.history, service.engine, settings)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orca")
            app.state.executor = executor
            cleaner = asyncio.create_task(housekeeping(service))
            try:
                yield
            finally:
                cleaner.cancel()
                with suppress(asyncio.CancelledError):
                    await cleaner
                await to_thread.run_sync(executor.shutdown)
                shutil.rmtree(service.workspace)
        finally:
            lock.close()

    app = FastAPI(title="Orca Metadata Inspector", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  telemetry={"tracing": False, "metrics": False, "logs": False, "auto_configure": False})
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")

    @app.middleware("http")
    async def local_requests(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD"} and (
            request.headers.get("sec-fetch-site") == "cross-site" or
            origin and origin != f"{request.url.scheme}://{request.headers.get('host')}"
        ):
            response = JSONResponse({"detail": "La operación requiere el mismo origen local."}, 403)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(UploadError)
    async def input_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(HashError)
    async def hash_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(ManifestError)
    async def manifest_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(InvestigationError)
    async def investigation_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "formats": sorted(set(FORMATS.values())), "policies": POLICIES,
            "minutes": f"{settings.ttl / 60:g}".replace(".", ","),
        })

    @app.get("/api/health", response_model=EngineInfo)
    def health(request: Request):
        return request.app.state.service.engine.info

    @app.post("/api/sessions", status_code=202, response_model=Started)
    async def start(request: Request):
        service = request.app.state.service
        if not service.engine.info["available"]:
            raise UploadError("ExifTool no está disponible. Consulta las instrucciones de instalación.", 503)
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raise UploadError("Usa un formulario multipart para enviar archivos.", 415)
        body_limit = settings.upload_limit + 512_000
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isascii() or not declared.isdigit() or len(declared) > 12):
            raise UploadError("La longitud de la solicitud no es válida.", 400)
        if declared is not None and int(declared) > body_limit:
            raise UploadError("La solicitud supera el límite total de carga.", 413)

        async def bounded_stream():
            length = 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > body_limit:
                    raise UploadError("La solicitud supera el límite total de carga.", 413)
                yield chunk

        # Parse explicitly so the actual body is counted even without Content-Length.
        from starlette.formparsers import MultiPartException, MultiPartParser

        service.reserve()
        handed_off = False
        work = service.workspace / uuid4().hex
        started = time.monotonic()
        try:
            parser = MultiPartParser(request.headers, bounded_stream(), max_files=MAX_FILES,
                                     max_fields=1, max_part_size=32)
            try:
                form = await parser.parse()
            except MultiPartException as exc:
                status = 413 if "Too many files" in str(exc) or "maximum size" in str(exc) else 400
                raise UploadError("El formulario es inválido o supera 100 archivos de entrada.", status) from exc
            try:
                uploads = form.getlist("files")
                if not uploads or any(not isinstance(file, UploadFile) for file in uploads):
                    raise UploadError("Selecciona al menos un archivo.")
                if any(key not in {"files", "keep"} for key, _ in form.multi_items()):
                    raise UploadError("El formulario contiene un campo desconocido.")
                if any(file.size is not None and file.size > MAX_BYTES for file in uploads):
                    raise UploadError("Cada ZIP o archivo debe ocupar como máximo 100 MB (100 000 000 bytes).", 413)
                work.mkdir(mode=0o700)
                inputs, expansion = await to_thread.run_sync(prepare, uploads, work, settings)
                keep = form.get("keep") in ("1", "true")
            finally:
                await form.close()
            job = service.create(work, inputs, keep, expansion)
            job.started = started
            request.app.state.executor.submit(service.process, job)
            handed_off = True
            return Started(id=job.id)
        finally:
            if not handed_off:
                try:
                    shutil.rmtree(work, ignore_errors=True)
                finally:
                    service.operation.release()

    @app.get("/api/sessions/{session_id}")
    def session(request: Request, session_id: str):
        return request.app.state.service.status(session_id)

    @app.get("/api/sessions/{session_id}/files/{file_id}")
    def detail(request: Request, session_id: str, file_id: str):
        record, = request.app.state.service.records(session_id, file_id)
        return record

    @app.get("/api/sessions/{session_id}/files/{file_id}/context")
    def inspection_context(request: Request, session_id: StoredId, file_id: StoredId):
        service = request.app.state.service
        with service.guard:
            record, = service.records(session_id, file_id)
            cleaned = service.cached_cleanup(session_id, file_id)
        timeline = build_timeline(record["raw"])
        return {"timeline": timeline, "exposure": analyze_exposure(record, timeline),
                "exposure_comparison": cleaned["exposure_comparison"] if cleaned else None}

    @app.get("/api/sessions/{session_id}/files/{file_id}/evidence")
    def evidence(request: Request, session_id: StoredId, file_id: StoredId,
                 kind: Literal["raw", "normalized", "finding", "exposure"] = "raw",
                 tag: Annotated[str | None, Query(max_length=1024)] = None,
                 index: Annotated[int, Query(ge=0)] = 0):
        service = request.app.state.service
        with service.guard:
            record, = service.records(session_id, file_id)
            session = service.status(session_id, include_files=False)
        if kind == "raw":
            if tag is None or tag not in record["raw"]:
                raise HTTPException(404, "El campo indicado no existe en el archivo.")
            return format_evidence(record, session, [tag])
        if kind == "normalized":
            items = list(properties(record["normalized"]))
        elif kind == "finding":
            items = record["findings"]
        else:
            items = [item for category in analyze_exposure(record)["categories"] for item in category["items"]]
        try:
            item = items[index]
            return format_evidence(record, session, item["source"], item, kind)
        except (IndexError, KeyError) as exc:
            raise HTTPException(404, "La evidencia indicada no existe en este archivo.") from exc

    @app.get("/api/sessions/{session_id}/files/{file_id}/preview")
    async def file_preview(request: Request, session_id: StoredId, file_id: StoredId):
        service = request.app.state.service
        with service.guard:
            record, = service.records(session_id, file_id)
        fmt = record.get("format")
        media_map = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
        if fmt not in media_map:
            raise HTTPException(415, "La previsualización directa solo está disponible para imágenes JPEG, PNG y WebP.")

        def read_preview():
            with service.pin_sources(session_id, (file_id,)) as paths:
                src = paths.get(file_id)
                if not src or not src.is_file():
                    raise HTTPException(410, "El archivo temporal ya no está disponible para previsualización.")
                return src.read_bytes()

        data = await to_thread.run_sync(read_preview)
        return Response(content=data, media_type=media_map[fmt])

    @app.get("/api/sessions/{session_id}/duel/diff-map")
    async def duel_diff_map(
        request: Request,
        session_id: StoredId,
        file_a: CompareId,
        file_b: CompareId,
        mode: Literal["normalized", "raw"] = "normalized",
        amplify: bool = True,
    ):
        if file_a == file_b:
            raise HTTPException(422, "Selecciona dos archivos distintos.")
        service = request.app.state.service
        with service.guard:
            first, = service.records(session_id, file_a)
            second, = service.records(session_id, file_b)
        if first.get("format") not in IMAGE_FORMATS or second.get("format") not in IMAGE_FORMATS:
            raise HTTPException(422, "El mapa de diferencias requiere dos imágenes compatibles (JPEG, PNG o WebP).")

        def build_map():
            with service.pin_sources(session_id, (file_a, file_b)) as paths:
                pa, pb = paths.get(file_a), paths.get(file_b)
                if not pa or not pb:
                    raise HTTPException(410, "Los archivos temporales ya no están disponibles para generar el mapa de diferencias.")
                try:
                    return generate_diff_map_png(pa, pb, mode=mode, amplify=amplify)
                except ImageSafetyError as exc:
                    raise HTTPException(422, str(exc)) from exc

        png_bytes, meta = await to_thread.run_sync(build_map)
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "X-Orca-Diff-Pixels": str(meta["different_pixels"]),
                "X-Orca-Total-Pixels": str(meta["total_pixels"]),
                "X-Orca-Diff-Amplified": "true" if meta["amplified"] else "false",
            },
        )

    @app.get("/api/sessions/{session_id}/duel/matrix")
    async def duel_matrix(
        request: Request,
        session_id: StoredId,
        reference_id: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
        download: bool = False,
        format: Literal["json", "html"] = "json",
    ):
        service = request.app.state.service
        with service.guard:
            records = service.records(session_id)
            job = service.active.get(session_id)
            image_profiles = job.image_profiles if job else {}
            duel_cache = job.duel_cache if job else {}
            batch_status = job.duel_batch.snapshot() if (job and job.duel_batch) else None

        def run_matrix():
            fids = [r["id"] for r in records]
            with service.pin_sources(session_id, fids) as paths:
                return build_duel_matrix(
                    records, paths, image_profiles, duel_cache, reference_id=reference_id
                )

        matrix = await to_thread.run_sync(run_matrix)
        result = {"schema_version": 2, "session_id": session_id, "batch": batch_status, **matrix}
        if download:
            headers = {"Content-Disposition": attachment(f"orca-duelo-matriz.{format}")}
            if format == "html":
                return TEMPLATES.TemplateResponse(
                    request, "duel_matrix_report.html", {"result": result}, headers=headers
                )
            return JSONResponse(result, headers=headers)
        return result

    @app.post("/api/sessions/{session_id}/duel/matrix/batch", status_code=202)
    async def duel_matrix_batch(
        request: Request,
        session_id: StoredId,
        body: DuelMatrixBatchRequest,
    ):
        service = request.app.state.service
        with service.guard:
            job = service.active.get(session_id)
            if not job or job.state != "complete":
                raise HTTPException(410, "La sesión temporal no está disponible para análisis por lotes.")
            if job.duel_batch and job.duel_batch.snapshot()["state"] == "running":
                raise HTTPException(409, "Ya hay un análisis por lotes en curso para esta sesión.")
            records = list(job.records)
            image_profiles = job.image_profiles
            duel_cache = job.duel_cache

        def prepare_pending():
            fids = [r["id"] for r in records]
            with service.pin_sources(session_id, fids) as paths:
                matrix = build_duel_matrix(
                    records, paths, image_profiles, duel_cache, reference_id=body.reference_id
                )
            pending = [
                (c["file_a_id"], c["file_b_id"])
                for c in matrix["cells"]
                if c["state"] in {"pending", "similar"} and not c["deep_evaluated"]
            ][: body.max_pairs]
            return pending

        pending_pairs = await to_thread.run_sync(prepare_pending)
        batch = DuelBatchJob(total=len(pending_pairs))
        with service.guard:
            job.duel_batch = batch

        rec_by_id = {r["id"]: r for r in records}

        def run_batch():
            try:
                for fa, fb in pending_pairs:
                    if batch.cancel_event.is_set():
                        with batch.lock:
                            batch.state = "cancelled"
                            batch.current = None
                        return
                    ra, rb = rec_by_id[fa], rec_by_id[fb]
                    with batch.lock:
                        batch.current = f"{ra['name']} ↔ {rb['name']}"
                    with service.pin_sources(session_id, (fa, fb)) as paths:
                        res = compare_files(
                            ra, rb,
                            path_a=paths.get(fa), path_b=paths.get(fb),
                            settings=settings,
                            profile_a=image_profiles.get(fa),
                            profile_b=image_profiles.get(fb),
                        )
                    pair_key = (fa, fb) if fa < fb else (fb, fa)
                    with service.guard:
                        duel_cache[pair_key] = res
                    with batch.lock:
                        batch.processed += 1
                with batch.lock:
                    batch.state = "cancelled" if batch.cancel_event.is_set() else "complete"
                    batch.current = None
            except Exception as exc:
                with batch.lock:
                    batch.state = "failed"
                    batch.error = str(exc)
                    batch.current = None

        request.app.state.executor.submit(run_batch)
        return batch.snapshot()

    @app.get("/api/sessions/{session_id}/duel/matrix/status")
    def duel_matrix_status(request: Request, session_id: StoredId):
        service = request.app.state.service
        with service.guard:
            job = service.active.get(session_id)
            if not job or not job.duel_batch:
                return {"state": "idle", "processed": 0, "total": 0, "current": None, "cancelled": False, "error": None}
            return job.duel_batch.snapshot()

    @app.post("/api/sessions/{session_id}/duel/matrix/cancel")
    def duel_matrix_cancel(request: Request, session_id: StoredId):
        service = request.app.state.service
        with service.guard:
            job = service.active.get(session_id)
            if job and job.duel_batch:
                job.duel_batch.cancel()
                return job.duel_batch.snapshot()
        return {"state": "idle", "processed": 0, "total": 0, "current": None, "cancelled": True, "error": None}

    @app.get("/api/sessions/{session_id}/duel")
    async def duel(request: Request, session_id: StoredId, file_a: CompareId, file_b: CompareId,
                   download: bool = False, format: Literal["json", "html"] = "json"):
        if file_a == file_b:
            raise HTTPException(422, "Selecciona dos archivos distintos.")
        service = request.app.state.service
        with service.guard:
            first, = service.records(session_id, file_a)
            second, = service.records(session_id, file_b)
            job = service.active.get(session_id)
            image_profiles = job.image_profiles if job else {}
            duel_cache = job.duel_cache if job else None

        def run_duel():
            with service.pin_sources(session_id, (file_a, file_b)) as paths:
                comp = compare_files(
                    first, second,
                    path_a=paths.get(file_a),
                    path_b=paths.get(file_b),
                    settings=settings,
                    profile_a=image_profiles.get(file_a),
                    profile_b=image_profiles.get(file_b),
                )
            # Reuse extracted image profiles & cache deep result in active session
            ic = comp.get("image_comparison")
            if job is not None and isinstance(ic, dict):
                with service.guard:
                    if ic.get("profile_a") and file_a not in image_profiles:
                        image_profiles[file_a] = ic["profile_a"]
                    if ic.get("profile_b") and file_b not in image_profiles:
                        image_profiles[file_b] = ic["profile_b"]
            if duel_cache is not None:
                pair_key = (file_a, file_b) if file_a < file_b else (file_b, file_a)
                with service.guard:
                    duel_cache[pair_key] = comp
            return comp

        comparison = await to_thread.run_sync(run_duel)
        result = {"schema_version": 2, "session_id": session_id, **comparison}
        if download:
            headers = {"Content-Disposition": attachment(f"orca-duelo.{format}")}
            if format == "html":
                return TEMPLATES.TemplateResponse(request, "duel_report.html", {"result": result}, headers=headers)
            return JSONResponse(result, headers=headers)
        return result

    @app.get("/api/history")
    def history(request: Request):
        return request.app.state.service.list_saved()

    @app.delete("/api/history", status_code=204)
    def clear_history(request: Request):
        request.app.state.service.delete()
        return Response(status_code=204)

    @app.delete("/api/sessions/{session_id}", status_code=204)
    def close_session(request: Request, session_id: str):
        request.app.state.service.delete(session_id)
        return Response(status_code=204)

    @app.post("/api/sessions/{session_id}/files/{file_id}/clean")
    def sanitize(request: Request, session_id: str, file_id: str):
        return request.app.state.service.sanitize(session_id, file_id)

    @app.get("/api/sessions/{session_id}/files/{file_id}/copy")
    def download(request: Request, session_id: str, file_id: str):
        service = request.app.state.service
        stream, job, record = service.open_copy(session_id, file_id)

        def release():
            with service.guard:
                if not stream.closed:
                    stream.close()
                    job.downloads -= 1

        async def chunks():
            try:
                while block := await to_thread.run_sync(stream.read, CHUNK):
                    yield block
            finally:
                release()

        stem = safe_upload_name(record["name"]).rsplit(".", 1)[0]
        return StreamingResponse(chunks(), media_type="application/octet-stream", background=BackgroundTask(release),
                                 headers={"Content-Disposition": attachment(f"{stem}-orca.{record['extension']}"),
                                          "Content-Length": str(record["size"])})

    @app.get("/api/sessions/{session_id}/export/{kind}")
    def export(request: Request, session_id: str, kind: str):
        service = request.app.state.service
        if kind not in {"json", "csv", "html"}:
            raise HTTPException(404, "Formato de exportación desconocido.")
        with service.guard:
            result = service.status(session_id, include_files=False)
            records = service.records(session_id)
        report = payload(result, records)
        headers = {"Content-Disposition": attachment(f"orca-{result['id']}.{kind}")}
        if kind == "json":
            return JSONResponse(report, headers=headers)
        if kind == "csv":
            return Response(as_csv(report), media_type="text/csv", headers=headers)
        return TEMPLATES.TemplateResponse(request, "report.html", {"report": report}, headers=headers)

    async def _stream_hash_upload(request: Request, algorithms: list[str]):
        content_type = request.headers.get("content-type", "").lower()
        if content_type.startswith("multipart/form-data"):
            from starlette.formparsers import MultiPartException, MultiPartParser

            body_limit = settings.hash_limit + 512_000

            async def bounded():
                seen = 0
                async for chunk in request.stream():
                    seen += len(chunk)
                    if seen > body_limit:
                        raise HashError("El archivo supera el límite máximo permitido para el cálculo de hashes.", 413)
                    yield chunk

            parser = MultiPartParser(request.headers, bounded(), max_files=1, max_fields=8, max_part_size=1024)
            try:
                form = await parser.parse()
            except MultiPartException as exc:
                raise HashError("El formulario de carga de archivo es inválido.", 400) from exc
            try:
                upload = form.get("file")
                if not isinstance(upload, UploadFile):
                    raise HashError("Selecciona un archivo para calcular su hash.")
                form_algos = form.getlist("algorithms") or ([form["algorithm"]] if "algorithm" in form else [])
                selected = parse_algorithm_params(form_algos) if form_algos else algorithms
                save_flag = form.get("save") in ("1", "true")
                name = safe_upload_name(upload.filename or "archivo")
                size, results = await to_thread.run_sync(
                    hash_chunks, file_chunks(upload.file), selected, settings.hash_limit
                )
                return name, size, results, save_flag
            finally:
                await form.close()

        # Zero-copy raw binary stream (O(1) RAM, no temporary disk file)
        declared = request.headers.get("content-length")
        if declared is not None and declared.isascii() and declared.isdigit() and int(declared) > settings.hash_limit:
            raise HashError("El archivo supera el límite máximo permitido para el cálculo de hashes.", 413)
        raw_name = unquote(request.headers.get("x-orca-filename", "archivo"))
        name = safe_upload_name(raw_name)
        size, results = await hash_async_chunks(request.stream(), algorithms, max_bytes=settings.hash_limit)
        save_flag = request.query_params.get("save") in ("1", "true")
        return name, size, results, save_flag

    @app.post("/api/hashes/compute")
    async def compute_hashes(request: Request,
                             algorithms: Annotated[list[str] | None, Query()] = None):
        selected = parse_algorithm_params(algorithms)
        name, size, results, save_flag = await _stream_hash_upload(request, selected)
        created = utc_now()
        saved_records = []
        if save_flag:
            history_store = request.app.state.service.history
            for item in results:
                rec = {
                    "id": uuid4().hex, "created": created, "name": name,
                    "size": size, "algorithm": item["algorithm"], "hash": item["hash"],
                }
                await to_thread.run_sync(history_store.save_hash, rec)
                saved_records.append(rec)
        return {
            "name": name,
            "size": size,
            "created": created,
            "results": results,
            "saved_records": saved_records,
        }

    @app.post("/api/hashes/compare")
    def compare_hashes(body: HashCompareRequest):
        return compare_digests(body.hash_a, body.hash_b, body.algorithm)

    @app.get("/api/hashes/history")
    def list_hash_history(request: Request,
                          q: Annotated[str | None, Query(max_length=200)] = None,
                          algorithm: Annotated[str | None, Query(max_length=20)] = None):
        canonical = normalize_algorithm(algorithm) if algorithm else None
        return request.app.state.service.history.list_hashes(query=q.strip() if q else None, algorithm=canonical)

    @app.post("/api/hashes/history", status_code=201)
    def save_hash_record(request: Request, body: HashRecordRequest):
        canonical = normalize_algorithm(body.algorithm)
        digest = validate_hex_digest(body.hash, canonical)
        record = {
            "id": uuid4().hex,
            "created": utc_now(),
            "name": safe_upload_name(body.name),
            "size": body.size,
            "algorithm": canonical,
            "hash": digest,
        }
        return request.app.state.service.history.save_hash(record)

    @app.delete("/api/hashes/history/{record_id}", status_code=204)
    def delete_hash_record(request: Request, record_id: StoredId):
        if not request.app.state.service.history.delete_hash(record_id):
            raise HTTPException(404, "El registro de hash no existe.")
        return Response(status_code=204)

    @app.delete("/api/hashes/history", status_code=204)
    def clear_hash_history(request: Request):
        request.app.state.service.history.delete_hash()
        return Response(status_code=204)

    @app.get("/api/hashes/history/export/{kind}")
    def export_hash_history(request: Request, kind: Literal["json", "csv"],
                            q: Annotated[str | None, Query(max_length=200)] = None,
                            algorithm: Annotated[str | None, Query(max_length=20)] = None):
        canonical = normalize_algorithm(algorithm) if algorithm else None
        records = request.app.state.service.history.list_hashes(query=q.strip() if q else None, algorithm=canonical)
        headers = {"Content-Disposition": attachment(f"orca-hashes.{kind}")}
        if kind == "json":
            return JSONResponse({"schema_version": 1, "exported_at": utc_now(), "records": records}, headers=headers)
        return Response(hash_history_csv(records), media_type="text/csv", headers=headers)

    @app.post("/api/hashes/history/{record_id}/verify")
    async def verify_hash_record(request: Request, record_id: StoredId):
        record = request.app.state.service.history.get_hash(record_id)
        if not record:
            raise HTTPException(404, "El registro de hash no existe.")
        name, size, results, _ = await _stream_hash_upload(request, [record["algorithm"]])
        actual = results[0]
        matched = hmac.compare_digest(record["hash"].lower(), actual["hash"].lower())
        name_changed = name != record["name"]
        if matched:
            msg = (
                "Integridad verificada. El nombre del archivo cambió, pero el contenido coincide exactamente según el hash."
                if name_changed else "Integridad verificada."
            )
            status = "verified"
        else:
            msg = "El archivo ha cambiado."
            status = "changed"
        return {
            "status": status,
            "match": matched,
            "message": msg,
            "name_changed": name_changed,
            "record": record,
            "actual": {"name": name, "size": size, "algorithm": actual["algorithm"], "hash": actual["hash"]},
            "warning": actual["warning"],
            "disclaimer": AUTHENTICITY_NOTE,
        }

    async def _parse_manifest_multipart(request: Request):
        from starlette.formparsers import MultiPartException, MultiPartParser

        body_limit = settings.hash_limit + 2_000_000

        async def bounded():
            seen = 0
            async for chunk in request.stream():
                seen += len(chunk)
                if seen > body_limit:
                    raise ManifestError("La solicitud supera el límite permitido para manifiestos.", 413)
                yield chunk

        parser = MultiPartParser(request.headers, bounded(), max_files=1000, max_fields=1020, max_part_size=5_000_000)
        try:
            return await parser.parse()
        except MultiPartException as exc:
            raise ManifestError("El formulario del manifiesto es inválido o supera los límites.", 400) from exc

    @app.post("/api/manifests/generate")
    async def generate_manifest(request: Request, download: bool = False):
        content_type = request.headers.get("content-type", "").lower()
        if content_type.startswith("multipart/form-data"):
            form = await _parse_manifest_multipart(request)
            try:
                algorithm = normalize_algorithm(form.get("algorithm") or DEFAULT_ALGORITHM)
                uploads = form.getlist("files")
                declared_paths = form.getlist("paths")
                if not uploads or any(not isinstance(item, UploadFile) for item in uploads):
                    raise ManifestError("Selecciona al menos un archivo para generar el manifiesto.")
                if declared_paths and len(declared_paths) != len(uploads):
                    raise ManifestError("La cantidad de rutas declaradas no coincide con la de archivos.")
                entries = []
                for idx, upload in enumerate(uploads):
                    raw_path = str(declared_paths[idx]) if declared_paths else (upload.filename or f"archivo-{idx + 1}")
                    rel_path = normalize_relative_path(raw_path)
                    size, results = await to_thread.run_sync(
                        hash_chunks, file_chunks(upload.file), [algorithm], settings.hash_limit
                    )
                    entries.append({"path": rel_path, "size": size, "hash": results[0]["hash"]})
                manifest = build_manifest(entries, algorithm=algorithm)
            finally:
                await form.close()
        else:
            try:
                body = await request.json()
            except ValueError as exc:
                raise ManifestError("El cuerpo JSON del manifiesto no es válido.", 400) from exc
            if not isinstance(body, dict):
                raise ManifestError("El cuerpo debe ser un objeto JSON.", 400)
            manifest = build_manifest(body.get("files"), algorithm=body.get("algorithm") or DEFAULT_ALGORITHM)

        if download:
            headers = {"Content-Disposition": attachment("orca-manifiesto.json")}
            return JSONResponse(manifest, headers=headers)
        return manifest

    @app.post("/api/manifests/verify")
    async def verify_manifest_endpoint(request: Request):
        content_type = request.headers.get("content-type", "").lower()
        if content_type.startswith("multipart/form-data"):
            form = await _parse_manifest_multipart(request)
            try:
                manifest_field = form.get("manifest")
                if isinstance(manifest_field, UploadFile):
                    raw_manifest = await manifest_field.read()
                    try:
                        manifest_data = json.loads(raw_manifest.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise ManifestError("El archivo de manifiesto no es un JSON UTF-8 válido.") from exc
                elif isinstance(manifest_field, str):
                    try:
                        manifest_data = json.loads(manifest_field)
                    except ValueError as exc:
                        raise ManifestError("El manifiesto JSON no tiene una sintaxis válida.") from exc
                else:
                    raise ManifestError("Debes proporcionar un manifiesto JSON para verificar.")
                from .manifests import validate_manifest
                validated = validate_manifest(manifest_data)
                algorithm = validated["algorithm"]
                uploads = form.getlist("files")
                declared_paths = form.getlist("paths")
                if declared_paths and len(declared_paths) != len(uploads):
                    raise ManifestError("La cantidad de rutas declaradas no coincide con la de archivos.")
                observed = []
                for idx, upload in enumerate(uploads):
                    if not isinstance(upload, UploadFile):
                        continue
                    raw_path = str(declared_paths[idx]) if declared_paths else (upload.filename or f"archivo-{idx + 1}")
                    rel_path = normalize_relative_path(raw_path)
                    size, results = await to_thread.run_sync(
                        hash_chunks, file_chunks(upload.file), [algorithm], settings.hash_limit
                    )
                    observed.append({"path": rel_path, "size": size, "hash": results[0]["hash"]})
                return verify_manifest(validated, observed)
            finally:
                await form.close()

        try:
            body = await request.json()
        except ValueError as exc:
            raise ManifestError("El cuerpo JSON de verificación no es válido.", 400) from exc
        if not isinstance(body, dict):
            raise ManifestError("El cuerpo debe ser un objeto JSON.", 400)
        return verify_manifest(body.get("manifest"), body.get("files") or [], body.get("unreadable") or [])

    @app.post("/api/manifests/report")
    async def manifest_html_report(request: Request):
        try:
            body = await request.json()
        except ValueError as exc:
            raise ManifestError("El informe JSON no es válido.", 400) from exc
        if not isinstance(body, dict) or "manifest" not in body or "rows" not in body or "summary" not in body:
            raise ManifestError("Faltan campos requeridos para generar el informe de verificación.")
        headers = {"Content-Disposition": attachment("orca-verificacion-manifiesto.html")}
        return TEMPLATES.TemplateResponse(request, "manifest_report.html", {"report": body}, headers=headers)

    # --- Investigations Module API ---

    @app.get("/api/investigations")
    def list_investigations(request: Request,
                            q: Annotated[str | None, Query(max_length=200)] = None,
                            status: Annotated[str | None, Query(max_length=20)] = None,
                            sort: Literal["updated_at", "created_at"] = "updated_at"):
        return request.app.state.investigations.list(query=q, status=status, sort=sort)

    @app.post("/api/investigations", status_code=201)
    def create_investigation(request: Request, body: InvestigationCreateRequest):
        return request.app.state.investigations.create(
            name=body.name, responsible=body.responsible, description=body.description,
            kind=body.kind, classification=body.classification,
        )

    async def _stage_package_uploads(request: Request):
        from starlette.formparsers import MultiPartException, MultiPartParser

        body_limit = settings.upload_limit + 1_000_000

        async def bounded():
            seen = 0
            async for chunk in request.stream():
                seen += len(chunk)
                if seen > body_limit:
                    raise InvestigationError("Los paquetes superan el límite total de carga.", 413)
                yield chunk

        parser = MultiPartParser(request.headers, bounded(), max_files=60, max_fields=80, max_part_size=262_144)
        try:
            form = await parser.parse()
        except MultiPartException as exc:
            raise InvestigationError("Formulario de importación inválido o excede 60 paquetes.", 400) from exc
        work = request.app.state.service.workspace / f"imp-{uuid4().hex}"
        work.mkdir(mode=0o700)
        staged = []
        try:
            uploads = form.getlist("packages") or form.getlist("files")
            if not uploads or any(not isinstance(u, UploadFile) for u in uploads):
                raise InvestigationError("Selecciona al menos un paquete de investigación (.zip).")
            actions_raw = form.get("actions")
            single_action = form.get("action")
            for idx, up in enumerate(uploads):
                name = safe_upload_name(up.filename or f"paquete-{idx + 1}.zip")
                dest = work / f"{idx:03d}-{uuid4().hex}.zip"
                with dest.open("xb") as out:
                    consume(file_chunks(up.file), out, MAX_BYTES)
                staged.append((name, dest))
            return work, staged, actions_raw, single_action
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        finally:
            await form.close()

    @app.post("/api/investigations/import/preview")
    async def preview_investigation_import(request: Request):
        work, staged, _, _ = await _stage_package_uploads(request)
        try:
            items = await to_thread.run_sync(preview_packages, request.app.state.investigations, staged)
            return {"count": len(items), "items": items}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @app.post("/api/investigations/import")
    async def import_investigations(request: Request):
        work, staged, actions_raw, single_action = await _stage_package_uploads(request)
        try:
            if actions_raw:
                try:
                    actions = json.loads(actions_raw)
                except ValueError as exc:
                    raise InvestigationError("El parámetro «actions» debe ser una lista JSON válida.", 400) from exc
                if not isinstance(actions, list) or len(actions) != len(staged):
                    raise InvestigationError("La cantidad de acciones debe coincidir con la cantidad de paquetes.")
            else:
                default_action = single_action if isinstance(single_action, str) and single_action else "import"
                actions = [default_action] * len(staged)

            def run_batch():
                outcomes = []
                for (name, path), act in zip(staged, actions):
                    try:
                        outcomes.append(import_package(request.app.state.investigations, path, name, action=str(act)))
                    except InvestigationError as exc:
                        outcomes.append({"filename": name, "outcome": "error", "message": str(exc)})
                return outcomes

            results = await to_thread.run_sync(run_batch)
            summary = {
                "total": len(results),
                "imported": sum(1 for r in results if r["outcome"] == "imported"),
                "imported_copy": sum(1 for r in results if r["outcome"] == "imported_copy"),
                "already_exists": sum(1 for r in results if r["outcome"] == "already_exists"),
                "conflict": sum(1 for r in results if r["outcome"] == "conflict"),
                "skipped": sum(1 for r in results if r["outcome"] == "skipped"),
                "error": sum(1 for r in results if r["outcome"] == "error"),
            }
            return {"summary": summary, "results": results}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @app.get("/api/investigations/{inv_uuid}")
    def get_investigation(request: Request, inv_uuid: UuidParam):
        return request.app.state.investigations.get(inv_uuid)

    @app.patch("/api/investigations/{inv_uuid}")
    def update_investigation(request: Request, inv_uuid: UuidParam, body: InvestigationUpdateRequest):
        return request.app.state.investigations.update(
            inv_uuid, name=body.name, responsible=body.responsible,
            description=body.description, kind=body.kind,
            classification=body.classification, actor=body.actor,
        )

    @app.delete("/api/investigations/{inv_uuid}", status_code=204)
    def delete_investigation(request: Request, inv_uuid: UuidParam):
        request.app.state.investigations.delete_investigation(inv_uuid)
        return Response(status_code=204)

    @app.post("/api/investigations/{inv_uuid}/state")
    def change_investigation_state(request: Request, inv_uuid: UuidParam, body: InvestigationStateRequest):
        return request.app.state.investigations.transition_state(
            inv_uuid, action=body.action, actor=body.actor,
            summary=body.summary, conclusion=body.conclusion,
            final_notes=body.final_notes, reason=body.reason,
        )

    @app.get("/api/investigations/{inv_uuid}/files")
    def list_investigation_files(request: Request, inv_uuid: UuidParam):
        return request.app.state.investigations.list_files(inv_uuid)

    @app.post("/api/investigations/{inv_uuid}/files", status_code=201)
    async def ingest_investigation_files(request: Request, inv_uuid: UuidParam):
        service = request.app.state.service
        inv_service = request.app.state.investigations
        if not service.engine.info["available"]:
            raise InvestigationError("ExifTool no está disponible. Consulta las instrucciones de instalación.", 503)
        # Validate investigation exists and is mutable before buffering upload
        inv_service.get(inv_uuid)
        from starlette.formparsers import MultiPartException, MultiPartParser

        body_limit = settings.upload_limit + 512_000

        async def bounded():
            seen = 0
            async for chunk in request.stream():
                seen += len(chunk)
                if seen > body_limit:
                    raise InvestigationError("La carga supera el límite total permitido.", 413)
                yield chunk

        parser = MultiPartParser(request.headers, bounded(), max_files=MAX_FILES, max_fields=MAX_FILES + 4, max_part_size=2048)
        try:
            form = await parser.parse()
        except MultiPartException as exc:
            raise InvestigationError("Formulario de archivos inválido o supera el límite de 100 archivos.", 400) from exc

        work = service.workspace / f"inv-{uuid4().hex}"
        work.mkdir(mode=0o700)
        try:
            uploads = form.getlist("files")
            if not uploads or any(not isinstance(u, UploadFile) for u in uploads):
                raise InvestigationError("Selecciona al menos un archivo para incorporar al expediente.")
            declared_paths = form.getlist("paths")
            actor = form.get("actor") if isinstance(form.get("actor"), str) else None
            if declared_paths and len(declared_paths) != len(uploads):
                raise InvestigationError("La cantidad de rutas relativas no coincide con los archivos enviados.")

            staged = []
            for idx, up in enumerate(uploads):
                name = safe_upload_name(up.filename or f"archivo-{idx + 1}")
                suffix = extension(name)
                dest = work / f"{uuid4().hex}.{suffix if suffix in FORMATS else 'bin'}"
                with dest.open("xb") as out:
                    size = consume(file_chunks(up.file), out, MAX_BYTES)
                rel = str(declared_paths[idx]) if declared_paths else None
                staged.append((dest, name, size, rel))
        finally:
            await form.close()

        def process_all():
            results = []
            for dest, name, size, rel in staged:
                results.append(inv_service.ingest_file(inv_uuid, dest, name, size, relative_path=rel, actor=actor))
            return results

        try:
            items = await to_thread.run_sync(process_all)
            return {"count": len(items), "files": items}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @app.post("/api/investigations/{inv_uuid}/files/order")
    def reorder_investigation_files(request: Request, inv_uuid: UuidParam, body: InvestigationOrderRequest):
        return request.app.state.investigations.reorder_files(inv_uuid, body.file_uuids, actor=body.actor)

    @app.get("/api/investigations/{inv_uuid}/files/{file_uuid}")
    def get_investigation_file(request: Request, inv_uuid: UuidParam, file_uuid: UuidParam):
        return request.app.state.investigations.get_file(inv_uuid, file_uuid)

    @app.delete("/api/investigations/{inv_uuid}/files/{file_uuid}")
    def remove_investigation_file(request: Request, inv_uuid: UuidParam, file_uuid: UuidParam):
        return request.app.state.investigations.remove_file(inv_uuid, file_uuid)

    @app.post("/api/investigations/{inv_uuid}/files/{file_uuid}/verify-hash")
    async def verify_investigation_file_hash(request: Request, inv_uuid: UuidParam, file_uuid: UuidParam):
        name, size, results, _ = await _stream_hash_upload(request, ["SHA-256"])
        return request.app.state.investigations.verify_file_hash(
            inv_uuid, file_uuid, name, size, results[0]["hash"],
        )

    @app.get("/api/investigations/{inv_uuid}/comparisons")
    def list_investigation_comparisons(request: Request, inv_uuid: UuidParam):
        return request.app.state.investigations.list_comparisons(inv_uuid)

    @app.post("/api/investigations/{inv_uuid}/comparisons")
    async def run_investigation_comparison(request: Request, inv_uuid: UuidParam, body: InvestigationCompareRequest):
        return await to_thread.run_sync(
            request.app.state.investigations.compare_pair,
            inv_uuid, body.file_a_uuid, body.file_b_uuid, body.actor, body.force,
        )

    @app.get("/api/investigations/{inv_uuid}/comparisons/{comp_uuid}")
    def get_investigation_comparison(request: Request, inv_uuid: UuidParam, comp_uuid: UuidParam):
        return request.app.state.investigations.get_comparison(inv_uuid, comp_uuid)

    @app.get("/api/investigations/{inv_uuid}/matrix")
    def get_investigation_matrix(request: Request, inv_uuid: UuidParam):
        return request.app.state.investigations.similarity_matrix(inv_uuid)

    @app.post("/api/investigations/{inv_uuid}/matrix/compute")
    async def compute_investigation_matrix(request: Request, inv_uuid: UuidParam, body: InvestigationMatrixBatchRequest):
        return await to_thread.run_sync(
            request.app.state.investigations.compute_matrix_batch,
            inv_uuid, body.max_pairs, body.actor,
        )

    @app.get("/api/investigations/{inv_uuid}/timeline/activity")
    def get_investigation_activity(request: Request, inv_uuid: UuidParam,
                                   event_type: Annotated[str | None, Query(max_length=64)] = None):
        return request.app.state.investigations.list_events(inv_uuid, event_type=event_type)

    @app.get("/api/investigations/{inv_uuid}/timeline/documents")
    def get_investigation_doc_timeline(request: Request, inv_uuid: UuidParam,
                                       sort: Literal["added", "created", "modified", "manual"] = "added"):
        return request.app.state.investigations.document_timeline(inv_uuid, sort=sort)

    @app.get("/api/investigations/{inv_uuid}/graph")
    def get_investigation_graph(request: Request, inv_uuid: UuidParam):
        return request.app.state.investigations.build_graph(inv_uuid)

    @app.post("/api/investigations/{inv_uuid}/relations", status_code=201)
    def add_investigation_relation(request: Request, inv_uuid: UuidParam, body: InvestigationRelationRequest):
        return request.app.state.investigations.add_relation(
            inv_uuid, body.source_file_uuid, body.target_file_uuid,
            relation_type=body.relation_type, label=body.label,
            notes=body.notes, actor=body.actor,
        )

    @app.delete("/api/investigations/{inv_uuid}/relations/{rel_uuid}", status_code=204)
    def remove_investigation_relation(request: Request, inv_uuid: UuidParam, rel_uuid: UuidParam):
        request.app.state.investigations.delete_relation(inv_uuid, rel_uuid)
        return Response(status_code=204)

    @app.get("/api/investigations/{inv_uuid}/notes")
    def list_investigation_notes(request: Request, inv_uuid: UuidParam):
        return request.app.state.investigations.list_notes(inv_uuid)

    @app.post("/api/investigations/{inv_uuid}/notes", status_code=201)
    def create_investigation_note(request: Request, inv_uuid: UuidParam, body: InvestigationNoteCreateRequest):
        return request.app.state.investigations.create_note(
            inv_uuid, body.text, target_type=body.target_type,
            target_uuid=body.target_uuid, author=body.author,
        )

    @app.patch("/api/investigations/{inv_uuid}/notes/{note_uuid}")
    def update_investigation_note(request: Request, inv_uuid: UuidParam, note_uuid: UuidParam,
                                  body: InvestigationNoteUpdateRequest):
        return request.app.state.investigations.update_note(inv_uuid, note_uuid, body.text, author=body.author)

    @app.post("/api/investigations/{inv_uuid}/manifest")
    def generate_investigation_manifest(request: Request, inv_uuid: UuidParam):
        return request.app.state.investigations.generate_manifest(inv_uuid)

    @app.get("/api/investigations/{inv_uuid}/report/{kind}")
    def export_investigation_report(request: Request, inv_uuid: UuidParam, kind: Literal["json", "html"]):
        report = request.app.state.investigations.build_report(inv_uuid)
        headers = {"Content-Disposition": attachment(f"orca-informe-{inv_uuid[:8]}.{kind}")}
        if kind == "json":
            return JSONResponse(report, headers=headers)
        return TEMPLATES.TemplateResponse(request, "investigation_report.html", {"report": report}, headers=headers)

    @app.get("/api/investigations/{inv_uuid}/export")
    async def export_investigation_package(request: Request, inv_uuid: UuidParam):
        data, filename = await to_thread.run_sync(export_investigation_zip, request.app.state.investigations, inv_uuid)
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": attachment(filename)},
        )

    return app


app = create_app()
