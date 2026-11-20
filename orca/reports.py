import csv
import io
import json

from .metadata import properties


def payload(session, records):
    return {
        "schema_version": 1, "id": session["id"], "created": session["created"],
        "duration_ms": session["duration_ms"], "engine": session["engine"],
        "summary": session["summary"], "files": records,
    }


def safe_cell(value):
    if not isinstance(value, str):
        return value
    # Spreadsheet formula injection, including whitespace-prefixed formula markers.
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def as_csv(report):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["fecha_análisis", "motor", "versión", "opciones", "duración_ms", "archivo", "formato",
                     "bytes", "sha256", "estado", "tipo", "campo", "valor", "origen", "explicación"])
    engine = report["engine"]
    base = [report["created"], engine["name"], engine["version"], engine["options"], report["duration_ms"]]
    for record in report["files"]:
        prefix = base + [record[key] for key in ("name", "format", "size", "sha256", "status")]
        rows = [("archivo", "", "", "", "")]
        rows.extend(("propiedad", item["field"], item["value"], json.dumps(item["source"], ensure_ascii=False), "")
                    for item in properties(record["normalized"]))
        rows.extend(("hallazgo", item["field"], item["value"], json.dumps(item["source"], ensure_ascii=False),
                     item["description"]) for item in record["findings"])
        for level in ("errors", "warnings"):
            rows.extend((level, "", "", "", message) for message in record[level])
        writer.writerows([safe_cell(value) for value in [*prefix, *row]] for row in rows)
    return "\ufeff" + output.getvalue()
