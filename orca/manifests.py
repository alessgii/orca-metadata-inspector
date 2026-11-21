"""Integrity manifest generation, validation, and verification (Schema v1)."""
import hmac
import re
import unicodedata
from datetime import datetime, timezone

from .hashes import (
    DEFAULT_ALGORITHM,
    HashError,
    algorithm_info,
    normalize_algorithm,
    validate_hex_digest,
)

MANIFEST_VERSION = 1
MAX_MANIFEST_FILES = 10_000
MAX_PATH_LEN = 512
MAX_PATH_DEPTH = 32
DRIVE_RE = re.compile(r"^[A-Za-z]:")
INVALID_SEGMENT_CHARS = set('<>:"|?*')
INTEGRITY_DISCLAIMER = (
    "Un manifiesto JSON permite verificar la integridad de los archivos respecto a esa referencia, "
    "pero no garantiza autenticidad: el propio archivo JSON puede modificarse al no incluir firma digital."
)


class ManifestError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _utc_z_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_relative_path(raw: str) -> str:
    """Validate and normalize a relative POSIX path, rejecting absolute or traversal paths."""
    if not isinstance(raw, str):
        raise ManifestError("La ruta del archivo debe ser una cadena de texto.")
    text = unicodedata.normalize("NFC", raw.strip())
    if not text:
        raise ManifestError("La ruta del archivo no puede estar vacía.")
    if len(text) > MAX_PATH_LEN:
        raise ManifestError("Una ruta supera el límite máximo de 512 caracteres.")
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        raise ManifestError("Una ruta contiene caracteres de control no permitidos.")
    # Reject Windows UNC, absolute POSIX, or drive-letter paths before normalizing
    if text.startswith(("\\\\", "//", "/")) or DRIVE_RE.match(text):
        raise ManifestError(f"No se permiten rutas absolutas ni unidades en el manifiesto: «{text}».")
    unified = text.replace("\\", "/")
    while unified.startswith("./"):
        unified = unified[2:]
    unified = unified.rstrip("/")
    if not unified:
        raise ManifestError("La ruta relativa no contiene un nombre de archivo válido.")
    segments = unified.split("/")
    if len(segments) > MAX_PATH_DEPTH:
        raise ManifestError("Una ruta supera la profundidad máxima permitida.")
    for segment in segments:
        if not segment or segment in {".", ".."}:
            raise ManifestError(f"Ruta relativa insegura o con saltos de directorio: «{text}».")
        if segment.endswith((".", " ")) or any(ch in INVALID_SEGMENT_CHARS for ch in segment):
            raise ManifestError(f"Ruta relativa con caracteres incompatibles: «{text}».")
    return "/".join(segments)


def build_manifest(entries: list[dict], algorithm: str = DEFAULT_ALGORITHM, created_at: str | None = None) -> dict:
    try:
        canonical = normalize_algorithm(algorithm)
    except HashError as exc:
        raise ManifestError(str(exc), exc.status) from exc
    if not isinstance(entries, list) or not entries:
        raise ManifestError("Selecciona al menos un archivo para generar el manifiesto.")
    if len(entries) > MAX_MANIFEST_FILES:
        raise ManifestError(f"El manifiesto admite como máximo {MAX_MANIFEST_FILES} archivos.", 413)

    normalized_files = []
    seen_casefold: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ManifestError("Cada entrada del manifiesto debe ser un objeto válido.")
        rel_path = normalize_relative_path(item.get("path", ""))
        key = rel_path.casefold()
        if key in seen_casefold:
            raise ManifestError(f"Ruta duplicada o en conflicto en el manifiesto: «{rel_path}».")
        seen_casefold.add(key)

        size = item.get("size")
        if type(size) is not int or size < 0:
            raise ManifestError(f"El tamaño declarado para «{rel_path}» debe ser un entero no negativo.")
        try:
            digest = validate_hex_digest(item.get("hash"), canonical)
        except HashError as exc:
            raise ManifestError(f"Hash inválido para «{rel_path}»: {exc}", exc.status) from exc

        normalized_files.append({
            "path": rel_path,
            "size": size,
            "hash": digest,
        })

    normalized_files.sort(key=lambda entry: entry["path"])
    return {
        "version": MANIFEST_VERSION,
        "algorithm": canonical,
        "createdAt": created_at or _utc_z_now(),
        "files": normalized_files,
    }


def validate_manifest(data: object) -> dict:
    if not isinstance(data, dict):
        raise ManifestError("El manifiesto debe ser un objeto JSON.")
    version = data.get("version")
    if version != MANIFEST_VERSION:
        raise ManifestError(f"Versión de manifiesto no compatible: {version!r}. Se esperaba la versión {MANIFEST_VERSION}.")
    created_at = data.get("createdAt")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ManifestError("El manifiesto debe incluir una fecha válida en «createdAt».")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise ManifestError("El manifiesto debe contener una lista no vacía en «files».")
    return build_manifest(files, algorithm=data.get("algorithm"), created_at=created_at.strip())


def verify_manifest(manifest_data: object, observed_entries: list[dict],
                    unreadable_entries: list[dict] | None = None) -> dict:
    manifest = validate_manifest(manifest_data)
    algorithm = manifest["algorithm"]
    info = algorithm_info(algorithm)

    expected_by_path = {item["path"]: item for item in manifest["files"]}
    observed_by_path: dict[str, dict] = {}
    seen_casefold: set[str] = set()

    for item in observed_entries:
        if not isinstance(item, dict):
            raise ManifestError("Cada archivo observado debe ser un objeto válido.")
        rel_path = normalize_relative_path(item.get("path", ""))
        key = rel_path.casefold()
        if key in seen_casefold:
            raise ManifestError(f"Ruta duplicada entre los archivos seleccionados: «{rel_path}».")
        seen_casefold.add(key)
        size = item.get("size")
        if type(size) is not int or size < 0:
            raise ManifestError(f"Tamaño inválido para el archivo observado «{rel_path}».")
        try:
            digest = validate_hex_digest(item.get("hash"), algorithm)
        except HashError as exc:
            raise ManifestError(f"Hash inválido para «{rel_path}»: {exc}", exc.status) from exc
        observed_by_path[rel_path] = {"path": rel_path, "size": size, "hash": digest}

    unreadable_by_path: dict[str, dict] = {}
    for item in (unreadable_entries or []):
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path") or "").strip()
        try:
            rel_path = normalize_relative_path(raw_path)
        except ManifestError:
            rel_path = raw_path or "archivo-sin-ruta"
        unreadable_by_path[rel_path] = {
            "path": rel_path,
            "error": str(item.get("error") or "No se pudo leer o procesar el archivo."),
        }

    rows = []
    counts = {"unchanged": 0, "modified": 0, "missing": 0, "extra": 0, "unreadable": 0}

    all_paths = sorted(expected_by_path.keys() | observed_by_path.keys() | unreadable_by_path.keys())
    for path in all_paths:
        expected = expected_by_path.get(path)
        observed = observed_by_path.get(path)
        failed = unreadable_by_path.get(path)

        if failed:
            counts["unreadable"] += 1
            rows.append({
                "path": path,
                "status": "unreadable",
                "expected_hash": expected["hash"] if expected else None,
                "actual_hash": None,
                "expected_size": expected["size"] if expected else None,
                "actual_size": None,
                "detail": failed["error"],
            })
        elif expected and observed:
            if hmac.compare_digest(expected["hash"], observed["hash"]):
                counts["unchanged"] += 1
                rows.append({
                    "path": path,
                    "status": "unchanged",
                    "expected_hash": expected["hash"],
                    "actual_hash": observed["hash"],
                    "expected_size": expected["size"],
                    "actual_size": observed["size"],
                    "detail": "El hash coincide con el manifiesto.",
                })
            else:
                counts["modified"] += 1
                delta = observed["size"] - expected["size"]
                delta_text = f" ({delta:+d} B)" if delta else " (mismo tamaño)"
                rows.append({
                    "path": path,
                    "status": "modified",
                    "expected_hash": expected["hash"],
                    "actual_hash": observed["hash"],
                    "expected_size": expected["size"],
                    "actual_size": observed["size"],
                    "detail": f"El hash difiere del registrado en el manifiesto{delta_text}.",
                })
        elif expected:
            counts["missing"] += 1
            rows.append({
                "path": path,
                "status": "missing",
                "expected_hash": expected["hash"],
                "actual_hash": None,
                "expected_size": expected["size"],
                "actual_size": None,
                "detail": "Archivo presente en el manifiesto pero ausente en la selección actual.",
            })
        else:
            counts["extra"] += 1
            rows.append({
                "path": path,
                "status": "extra",
                "expected_hash": None,
                "actual_hash": observed["hash"],
                "expected_size": None,
                "actual_size": observed["size"],
                "detail": "Archivo adicional no registrado en el manifiesto.",
            })

    if counts["modified"] or counts["missing"] or counts["unreadable"]:
        overall_status = "mismatch"
    elif counts["extra"]:
        overall_status = "verified_with_extra"
    else:
        overall_status = "verified"

    warnings = []
    if info["warning"]:
        warnings.append(info["warning"])
    warnings.append(INTEGRITY_DISCLAIMER)

    return {
        "schema_version": MANIFEST_VERSION,
        "verified_at": _utc_z_now(),
        "manifest": {
            "version": manifest["version"],
            "algorithm": manifest["algorithm"],
            "createdAt": manifest["createdAt"],
            "file_count": len(manifest["files"]),
        },
        "status": overall_status,
        "summary": {
            **counts,
            "expected_total": len(expected_by_path),
            "observed_total": len(observed_by_path),
        },
        "warnings": warnings,
        "rows": rows,
    }
