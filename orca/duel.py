import hmac
import json
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal

from .content import compare_content
from .metadata import SEMANTIC_LABELS, interpreted_field, properties, raw_value, semantic_field, technical
from .similarity import compute_similarity
from .timeline import date_context, date_kind

STATUSES = ("matching", "different", "only_a", "only_b", "not_comparable")
GENERAL = {"sha256": "SHA-256", "size": "Tamaño", "format": "Formato"}
IDENTITY = ("author", "creator", "last_editor")
SOFTWARE = ("software", "creation_tool", "producer")
LOCATION = ("gps", "latitude", "longitude", "place")
AXES = {"latitude": ("GPSLatitudeRef", "N", "S", 90), "longitude": ("GPSLongitudeRef", "E", "W", 180)}


def token(value):
    # Keep JSON types distinct (e.g. false, 0, null, and the string "0").
    if isinstance(value, str):
        value = unicodedata.normalize("NFC", value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def entries(record):
    raw = record["raw"]
    result = defaultdict(list)
    for field in GENERAL:
        if field in record:
            result[field].append({"source": None, "original": record[field]})
    for source, value in raw.items():
        date = date_kind(source, value)
        is_technical = technical(source)
        field = (date[0] if date and date[0] != "other_date" else
                 "property" if is_technical else semantic_field([source]))
        entry = {"source": [source], "original": value}
        if field == "property":
            field = "tag:" + source
            entry.update(ordered=True, technical=is_technical)
        if date:
            entry["temporal"] = date
        result[field].append(entry)
    for item in properties(record["normalized"]):
        field = interpreted_field(item)
        source = item["source"]
        if field not in {"path", "username"}:
            continue
        if not any(entry["source"] == source and entry.get("interpreted", entry["original"]) == item["value"]
                   for entry in result[field]):
            result[field].append({"source": source, "original": raw_value(raw, source),
                                  "interpreted": item["value"]})
    return result


def coordinate_value(item, raw, field):
    original = item["original"]
    source = item["source"][0]
    if original is None or original == "":
        return token(original)
    if type(original) not in {int, float}:
        return None
    ref_tag, positive, negative, maximum = AXES[field]
    value = Decimal(str(original))
    if not value.is_finite() or abs(value) > maximum:
        return None
    if value and source.startswith("EXIF:GPS:"):
        # ExifTool numbers duplicate names per tag. A GPS value can have Copy1
        # because Composite emitted the same tag, even when its reference has no suffix.
        tag = source.rsplit(":", 1)[-1]
        gps_keys = [key for key in raw if key.startswith("EXIF:GPS:")]
        values = [key for key in gps_keys if key.rsplit(":", 1)[-1] == tag]
        references = [key for key in gps_keys if key.rsplit(":", 1)[-1] == ref_tag]
        if len(values) != 1 or len(references) != 1 or value < 0:
            return None
        reference, = references
        direction = raw[reference]
        if direction not in (positive, negative):
            return None
        if direction == negative:
            value = -value
        item["interpreted"] = float(value)
        item["interpretation_source"] = [reference]
    return str(value.normalize()) if value else "0"


def comparable_values(items, raw, field):
    values = set()
    uncertain = False
    for item in items:
        if field in AXES:
            coordinate = coordinate_value(item, raw, field)
            if coordinate is None:
                uncertain = True
            else:
                values.add(coordinate)
            continue
        value = item.get("interpreted", item["original"])
        candidates = value if isinstance(value, list) and value and not item.get("ordered") else [value]
        dates = []
        for candidate in candidates:
            if item.get("temporal"):
                parsed = date_context(raw, item["source"][0], candidate)
                dates.append(parsed)
                uncertain |= not bool(parsed["utc"])
                if parsed["utc"]:
                    values.add(token(parsed["utc"]))
            else:
                values.add(token(candidate))
        if dates:
            item["dates"] = dates
    return values, uncertain


def binary_identity_summary(first, second):
    sha_a = first.get("sha256")
    sha_b = second.get("sha256")
    size_a = first.get("size")
    size_b = second.get("size")
    size_delta = (size_b - size_a) if isinstance(size_a, int) and isinstance(size_b, int) else None
    if isinstance(sha_a, str) and isinstance(sha_b, str):
        identical = hmac.compare_digest(sha_a.lower(), sha_b.lower())
        status = "identical" if identical else "different"
        verdict = (
            "Los archivos son idénticos a nivel de bytes (SHA-256 coincidente)."
            if identical else
            "Los archivos difieren a nivel de bytes (SHA-256 distintos)."
        )
    else:
        identical = None
        status = "not_comparable"
        verdict = "No fue posible confirmar la identidad binaria porque falta el SHA-256 de al menos un archivo."
    return {
        "status": status,
        "identical_bytes": identical,
        "sha256_a": sha_a,
        "sha256_b": sha_b,
        "size_a": size_a,
        "size_b": size_b,
        "size_delta": size_delta,
        "verdict": verdict,
        "notes": [
            "Hashes SHA-256 distintos no implican necesariamente contenidos visibles distintos: cambiar metadatos, fechas o recompresión altera el hash.",
            "No debe interpretarse ninguna coincidencia parcial de caracteres entre hashes criptográficos como un porcentaje de similitud.",
        ],
    }


def compare_files(first, second, path_a=None, path_b=None, settings=None,
                  extracted_a=None, extracted_b=None):
    a, b = entries(first), entries(second)
    rows = []
    keys = [*GENERAL, *sorted((a.keys() | b.keys()) - GENERAL.keys())]
    for key in keys:
        left, right = a.get(key, []), b.get(key, [])
        value_a, uncertain_a = comparable_values(left, first["raw"], key)
        value_b, uncertain_b = comparable_values(right, second["raw"], key)
        explanation = None
        if key in GENERAL and (not left or not right or left[0]["original"] is None or right[0]["original"] is None):
            status = "not_comparable"
            explanation = "Falta una medición confirmada."
        elif ((not left and first.get("status") in {"unsupported", "error"}) or
              (not right and second.get("status") in {"unsupported", "error"})):
            status = "not_comparable"
            explanation = "La extracción incompleta impide confirmar si el campo está ausente."
        elif not left:
            status = "only_b"
        elif not right:
            status = "only_a"
        elif uncertain_a or uncertain_b:
            status = "not_comparable"
            explanation = ("Alguna coordenada carece de signo interpretable o de un valor válido."
                           if key in AXES else "Al menos una fecha carece de un instante preciso con zona horaria.")
        else:
            status = "matching" if value_a == value_b else "different"
        date = next((item["temporal"][1] for item in left + right if "temporal" in item), None)
        label = GENERAL.get(key) or date or SEMANTIC_LABELS.get(key) or key.removeprefix("tag:")
        rows.append({"field": key, "label": label, "status": status, "a": left, "b": right,
                     "explanation": explanation, "technical": all(item.get("technical") for item in left + right)})
    by_field = {row["field"]: row for row in rows}
    counts = Counter(row["status"] for row in rows)
    descriptive_rows = [row for row in rows if row["field"] not in GENERAL and not row["technical"]]
    technical_rows = [row for row in rows if row["technical"]]
    descriptive_counts = Counter(row["status"] for row in descriptive_rows)
    notes = []
    if by_field["sha256"]["status"] == "matching":
        notes.append("Los archivos tienen el mismo SHA-256. Sus contenidos coinciden según la huella.")
    elif by_field["sha256"]["status"] == "different":
        notes.append("Los archivos tienen hashes distintos.")
    for field in (*IDENTITY, *SOFTWARE):
        row = by_field.get(field)
        if not row or row["status"] not in {"matching", "different"}:
            continue
        if not all(any(entry["original"] not in (None, "", []) for entry in row[side]) for side in ("a", "b")):
            continue
        if row["status"] == "matching":
            notes.append(f"Ambos registran los mismos valores en «{row['label']}».")
        elif field in IDENTITY:
            notes.append(f"Ambos registran valores distintos en «{row['label']}».")
    locations = [any(entry["original"] not in (None, "", []) for field in LOCATION for entry in side.get(field, []))
                 for side in (a, b)]
    if locations.count(True) == 1 and all(record.get("status") not in {"unsupported", "error"}
                                         for record in (first, second)):
        notes.append(f"Solo el archivo {'A' if locations[0] else 'B'} registra datos de ubicación examinados.")

    binary_identity = binary_identity_summary(first, second)
    content = compare_content(
        first, second, path_a=path_a, path_b=path_b, settings=settings,
        extracted_a=extracted_a, extracted_b=extracted_b,
    )
    similarity = compute_similarity(binary_identity, content, rows)
    metadata_comparison = {
        "summary": {status: counts[status] for status in STATUSES},
        "descriptive_summary": {status: descriptive_counts[status] for status in STATUSES},
        "descriptive_count": len(descriptive_rows),
        "technical_count": len(technical_rows),
        "note": "Las diferencias en metadatos (autor, fechas, software, IDs) son independientes del contenido visible del documento.",
    }
    return {
        "a": {key: first.get(key) for key in ("id", "name", "status", *GENERAL)},
        "b": {key: second.get(key) for key in ("id", "name", "status", *GENERAL)},
        "summary": {status: counts[status] for status in STATUSES},
        "notes": notes,
        "rows": rows,
        "binary_identity": binary_identity,
        "content_comparison": content,
        "metadata_comparison": metadata_comparison,
        "similarity": similarity,
    }
