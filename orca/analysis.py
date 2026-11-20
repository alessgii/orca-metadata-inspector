import re
from uuid import uuid4

from .extraction import EngineError, Extractor, sha256
from .metadata import correlation, findings, normalize, properties
from .uploads import Input, extension, identify

PDF_END = re.compile(rb"startxref[\x00\s]+[0-9]+[\x00\s]+%%EOF")
TYPE_ALIASES = {"Extended WEBP": "WEBP", "Lossless WEBP": "WEBP"}


def structural_problem(file, kind):
    if kind == "PDF":
        with file.path.open("rb") as stream:
            stream.seek(max(0, file.size - 65536))
            if not PDF_END.search(stream.read()):
                return "El PDF está incompleto o carece de una referencia de cierre válida."
    if kind == "PNG":
        with file.path.open("rb") as stream:
            stream.seek(max(0, file.size - 65536))
            if b"\0\0\0\0IEND\xaeB`\x82" not in stream.read():
                return "El PNG está incompleto o carece del bloque de cierre IEND."
    return None


def inspect_file(file: Input, extractor: Extractor, settings, expected=None, expansion=None):
    raw, normalized, detected = {}, {}, expected
    status, errors, warnings = "ok", [], []
    if file.error:
        status, errors = "error", [file.error]
    if not file.error and not expected:
        detected, problem = identify(file, settings, expansion)
        if not detected:
            status, errors = "unsupported", [problem]
    if detected:
        try:
            raw = extractor.extract(file.path)
            actual = next((value for key, value in raw.items() if key.endswith(":FileType")), None)
            errors = [str(value) for key, value in raw.items() if key.endswith(":Error")
                      and key.startswith("ExifTool:")]
            warnings = [str(value) for key, value in raw.items() if key.endswith(":Warning")
                        and key.startswith("ExifTool:")]
            if TYPE_ALIASES.get(actual, actual) != detected:
                errors.append(f"ExifTool no confirmó el formato {detected}.")
            if problem := structural_problem(file, detected):
                errors.append(problem)
            if errors:
                status = "error"
        except EngineError as exc:
            status, errors = "error", [str(exc)]
        try:
            normalized = normalize(raw)
        except (RecursionError, ValueError):
            status = "error"
            errors.append("Los metadatos exceden la profundidad o cantidad de valores admitida.")
        if not normalized and status == "ok":
            status = "empty"
    return {
        "id": uuid4().hex, "name": file.name, "extension": extension(file.name), "format": detected,
        "size": file.size, "sha256": None if file.partial else sha256(file.path), "status": status,
        "raw": raw, "normalized": normalized, "findings": findings(normalized),
        "errors": errors, "warnings": warnings,
    }


def file_row(record):
    result = {key: value for key, value in record.items() if key not in {"normalized", "raw", "findings"}}
    values = list(properties(record["normalized"]))
    for field in ("author", "software", "created", "modified"):
        result[field] = next((item["value"] for item in values if item["field"] == field), None)
    result["field_count"] = len(record["raw"])
    result["finding_count"] = len(record["findings"])
    return result


def summarize(records):
    formats = {}
    statuses = {"ok": 0, "empty": 0, "unsupported": 0, "error": 0}
    authors, software = {}, {}
    for record in records:
        kind = record["format"] or "Sin confirmar"
        formats[kind] = formats.get(kind, 0) + 1
        statuses[record["status"]] += 1
        for item in properties(record["normalized"]):
            value = str(item["value"]).strip()
            if item["field"] == "author" and value:
                authors.setdefault(value.casefold(), value)
            if item["field"] == "software" and value:
                software.setdefault(value.casefold(), value)
    return {"count": len(records), "formats": formats, "statuses": statuses,
            "properties": sum(len(record["raw"]) for record in records),
            "findings": sum(len(record["findings"]) for record in records),
            "authors": sorted(authors.values(), key=str.casefold),
            "software": sorted(software.values(), key=str.casefold),
            "correlations": correlation(records)}
