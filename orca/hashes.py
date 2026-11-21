import csv
import hashlib
import hmac
import io
import re
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from .config import CHUNK
from .reports import safe_cell

ALGORITHMS = {
    "MD5": {"hashlib": "md5", "hex_length": 32, "legacy": True,
            "warning": "MD5 es un algoritmo heredado vulnerable a colisiones. No debe usarse como garantía criptográfica moderna."},
    "SHA-1": {"hashlib": "sha1", "hex_length": 40, "legacy": True,
              "warning": "SHA-1 es un algoritmo heredado vulnerable a colisiones. Prefiere SHA-256 o SHA-512."},
    "SHA-256": {"hashlib": "sha256", "hex_length": 64, "legacy": False, "warning": None},
    "SHA-512": {"hashlib": "sha512", "hex_length": 128, "legacy": False, "warning": None},
}
DEFAULT_ALGORITHM = "SHA-256"
ALIASES = {
    "md5": "MD5",
    "sha1": "SHA-1",
    "sha-1": "SHA-1",
    "sha256": "SHA-256",
    "sha-256": "SHA-256",
    "sha512": "SHA-512",
    "sha-512": "SHA-512",
}
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
AUTHENTICITY_NOTE = (
    "Esta comparación verifica únicamente la igualdad de los valores hexadecimales introducidos. "
    "No demuestra por sí sola la procedencia ni la autenticidad de un archivo."
)


class HashError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def normalize_algorithm(name: str | None) -> str:
    if not isinstance(name, str) or not name.strip():
        return DEFAULT_ALGORITHM
    key = name.strip().lower().replace("_", "-")
    canonical = ALIASES.get(key)
    if not canonical:
        allowed = ", ".join(ALGORITHMS)
        raise HashError(f"Algoritmo no admitido. Usa uno de: {allowed}.")
    return canonical


def normalize_algorithms(names: Iterable[str] | None) -> list[str]:
    if not names:
        return [DEFAULT_ALGORITHM]
    seen = []
    for raw in names:
        canonical = normalize_algorithm(raw)
        if canonical not in seen:
            seen.append(canonical)
    return seen or [DEFAULT_ALGORITHM]


def algorithm_info(name: str) -> dict:
    canonical = normalize_algorithm(name)
    spec = ALGORITHMS[canonical]
    return {"name": canonical, "hex_length": spec["hex_length"],
            "legacy": spec["legacy"], "warning": spec["warning"]}


def validate_hex_digest(value: str | None, algorithm: str) -> str:
    canonical = normalize_algorithm(algorithm)
    expected_len = ALGORITHMS[canonical]["hex_length"]
    if not isinstance(value, str):
        raise HashError(f"Introduce un valor hexadecimal válido para {canonical}.")
    cleaned = value.strip()
    if not cleaned:
        raise HashError(f"El hash no puede estar vacío ({canonical} requiere {expected_len} caracteres hexadecimales).")
    if not HEX_RE.fullmatch(cleaned):
        raise HashError(
            f"El valor contiene caracteres no hexadecimales. Solo se admiten dígitos 0–9 y letras A–F."
        )
    if len(cleaned) != expected_len:
        raise HashError(
            f"Longitud inválida para {canonical}: se esperaban {expected_len} caracteres hexadecimales y se recibieron {len(cleaned)}."
        )
    return cleaned.lower()


def compare_digests(hash_a: str | None, hash_b: str | None, algorithm: str = DEFAULT_ALGORITHM) -> dict:
    canonical = normalize_algorithm(algorithm)
    info = algorithm_info(canonical)
    errors = {}
    norm_a = norm_b = None
    try:
        norm_a = validate_hex_digest(hash_a, canonical)
    except HashError as exc:
        errors["hash_a"] = str(exc)
    try:
        norm_b = validate_hex_digest(hash_b, canonical)
    except HashError as exc:
        errors["hash_b"] = str(exc)
    if errors:
        return {
            "valid": False,
            "algorithm": canonical,
            "expected_length": info["hex_length"],
            "legacy": info["legacy"],
            "warning": info["warning"],
            "errors": errors,
            "match": None,
            "message": "Corrige el formato de los hashes antes de compararlos.",
            "disclaimer": AUTHENTICITY_NOTE,
        }
    matched = hmac.compare_digest(norm_a, norm_b)
    return {
        "valid": True,
        "algorithm": canonical,
        "expected_length": info["hex_length"],
        "legacy": info["legacy"],
        "warning": info["warning"],
        "hash_a": norm_a,
        "hash_b": norm_b,
        "match": matched,
        "message": "Los hashes coinciden." if matched else "Los hashes son diferentes.",
        "disclaimer": AUTHENTICITY_NOTE,
    }


def _new_hashers(algorithms: list[str]) -> dict[str, "hashlib._Hash"]:
    return {name: hashlib.new(ALGORITHMS[name]["hashlib"]) for name in algorithms}


def _format_results(hashers: dict[str, "hashlib._Hash"]) -> list[dict]:
    results = []
    for name, hasher in hashers.items():
        spec = ALGORITHMS[name]
        results.append({
            "algorithm": name,
            "hash": hasher.hexdigest(),
            "hex_length": spec["hex_length"],
            "legacy": spec["legacy"],
            "warning": spec["warning"],
        })
    return results


def hash_chunks(chunks: Iterable[bytes], algorithms: Iterable[str] | None = None,
                max_bytes: int | None = None) -> tuple[int, list[dict]]:
    selected = normalize_algorithms(algorithms)
    hashers = _new_hashers(selected)
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise HashError("El archivo supera el límite máximo permitido para el cálculo de hashes.", 413)
        for hasher in hashers.values():
            hasher.update(chunk)
    return total, _format_results(hashers)


async def hash_async_chunks(chunks: AsyncIterator[bytes], algorithms: Iterable[str] | None = None,
                            max_bytes: int | None = None) -> tuple[int, list[dict]]:
    selected = normalize_algorithms(algorithms)
    hashers = _new_hashers(selected)
    total = 0
    async for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise HashError("El archivo supera el límite máximo permitido para el cálculo de hashes.", 413)
        for hasher in hashers.values():
            hasher.update(chunk)
    return total, _format_results(hashers)


def hash_file(path: Path, algorithms: Iterable[str] | None = None,
              max_bytes: int | None = None) -> tuple[int, list[dict]]:
    def reader():
        with path.open("rb") as stream:
            while block := stream.read(CHUNK):
                yield block
    return hash_chunks(reader(), algorithms=algorithms, max_bytes=max_bytes)


def hash_history_csv(records: list[dict]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["id", "fecha", "archivo", "bytes", "algoritmo", "hash"])
    for item in records:
        writer.writerow([
            safe_cell(item["id"]),
            safe_cell(item["created"]),
            safe_cell(item["name"]),
            safe_cell(item["size"]),
            safe_cell(item["algorithm"]),
            safe_cell(item["hash"]),
        ])
    return "\ufeff" + output.getvalue()
