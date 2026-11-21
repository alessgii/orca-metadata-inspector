import json
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal

from .metadata import SEMANTIC_LABELS, interpreted_field, properties, raw_value, semantic_field, technical
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


def compare_files(first, second):
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
    return {"a": {key: first.get(key) for key in ("id", "name", "status", *GENERAL)},
            "b": {key: second.get(key) for key in ("id", "name", "status", *GENERAL)},
            "summary": {status: counts[status] for status in STATUSES}, "notes": notes, "rows": rows}
