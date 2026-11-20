import asyncio
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from urllib.parse import quote
from uuid import uuid4

from anyio import to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .cleanup import POLICIES
from .config import CHUNK, MAX_BYTES, MAX_FILES, ROOT, Settings
from .extraction import ExifTool
from .reports import as_csv, payload
from .sessions import Sessions
from .uploads import FORMATS, UploadError, prepare, safe_upload_name

ASSETS = ROOT / "orca"
TEMPLATES = Jinja2Templates(directory=ASSETS / "templates")
TEMPLATES.env.policies["json.dumps_kwargs"] = {"sort_keys": False}


class EngineInfo(BaseModel):
    name: str
    version: str | None
    available: bool
    options: str


class Started(BaseModel):
    id: str


def attachment(name):
    filename = safe_upload_name(name)
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"


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

    return app


app = create_app()
