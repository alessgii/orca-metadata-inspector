import json
import re
import shlex
import unicodedata

from .exposure import CATEGORIES
from .extraction import READ_OPTIONS
from .metadata import SEMANTIC_LABELS, interpreted_field, raw_value, semantic_field
from .timeline import date_kind

TAG = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?::[A-Za-z][A-Za-z0-9_-]*)+")
WINDOWS_INVALID = re.compile(r"""[<>:"/\\|?*‘’]""")


def reference_commands(filename, tag):
    if (not TAG.fullmatch(tag) or not filename or "/" in filename or
            any(unicodedata.category(character).startswith("C") for character in filename)):
        return {}
    options = [*READ_OPTIONS, "-" + tag, "--"]
    result = {"posix": shlex.join(["exiftool", *options, "./" + filename])}
    if not WINDOWS_INVALID.search(filename) and not filename.endswith((".", " ")):
        quoted = (".\\" + filename).replace("'", "''")
        result["powershell"] = "& exiftool.exe " + " ".join(options) + " '" + quoted + "'"
    return result


def value_text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def format_evidence(record, session, source, item=None, kind=None):
    value = raw_value(record["raw"], source)
    qualified = source[0]
    group, _, tag = qualified.rpartition(":")
    field = interpreted_field(item) if item and kind != "exposure" else (item["field"] if item else semantic_field(source))
    date = date_kind(qualified, value)
    label = (item and item.get("label")) or SEMANTIC_LABELS.get(field) or (date and date[1]) or tag or qualified
    result = {
        "file": record["name"], "property": label, "source": source,
        "group": group or None, "tag": tag or qualified, "raw_value": value,
        "analysis_date": session.get("created"), "engine": session.get("engine", {}).get("name"),
        "engine_version": session.get("engine", {}).get("version"),
    }
    if record.get("sha256"):
        result["sha256"] = record["sha256"]
    if item and item["value"] != value:
        result["interpreted_value"] = item["value"]
    if kind == "finding":
        result["finding"] = {"category": label, "description": item["description"], "level": item["level"]}
    if kind == "exposure":
        result["exposure"] = {"category": CATEGORIES[item["category"]], "rule": item["rule"],
                              "description": item["description"], "interpretation": item["reason"],
                              "level": item["level"], "sources": item["sources"]}
    result = {key: element for key, element in result.items() if element is not None or key == "raw_value"}
    body = "\n".join(f"{label}: {value_text(result[key])}" for key, label in {
        "file": "Archivo", "sha256": "SHA-256", "property": "Propiedad", "raw_value": "Valor original",
        "interpreted_value": "Valor interpretado", "source": "Fuente", "analysis_date": "Fecha del análisis",
        "engine": "Herramienta", "engine_version": "Versión", "finding": "Hallazgo", "exposure": "Exposición",
    }.items() if key in result)
    fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", body)), default=3))
    return {"value": value_text(value), "structured": result,
            "markdown": f"### Evidencia de metadatos\n\n{fence}text\n{body}\n{fence}\n",
            "commands": reference_commands(record["name"], qualified)}
