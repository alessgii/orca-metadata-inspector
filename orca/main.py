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
from .evidence import format_evidence
from .exposure import analyze_exposure
from .extraction import ExifTool
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
from .manifests import (
    ManifestError,
    build_manifest,
    normalize_relative_path,
    verify_manifest,
)
from .metadata import properties
from .reports import as_csv, payload
from .sessions import Sessions, utc_now
from .timeline import build_timeline
from .uploads import FORMATS, UploadError, file_chunks, prepare, safe_upload_name

ASSETS = ROOT / "orca"
TEMPLATES = Jinja2Templates(directory=ASSETS / "templates")
TEMPLATES.env.policies["json.dumps_kwargs"] = {"sort_keys": False}


StoredId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
CompareId = Annotated[str, Query(pattern=r"^[a-f0-9]{32}$")]


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

    @app.get("/api/sessions/{session_id}/duel")
    async def duel(request: Request, session_id: StoredId, file_a: CompareId, file_b: CompareId,
                   download: bool = False, format: Literal["json", "html"] = "json"):
        if file_a == file_b:
            raise HTTPException(422, "Selecciona dos archivos distintos.")
        service = request.app.state.service
        with service.guard:
            first, = service.records(session_id, file_a)
            second, = service.records(session_id, file_b)

        def run_duel():
            with service.pin_sources(session_id, (file_a, file_b)) as paths:
                return compare_files(
                    first, second,
                    path_a=paths.get(file_a),
                    path_b=paths.get(file_b),
                    settings=settings,
                )

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

    return app


app = create_app()
