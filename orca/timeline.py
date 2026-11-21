import re
from collections import defaultdict
from datetime import datetime, timezone

from .metadata import leaves, tag_name

DATE = re.compile(
    r"(?P<year>[0-9]{4})(?:(?P<separator>[-:])(?P<month>[0-9]{2})(?:(?P=separator)(?P<day>[0-9]{2})"
    r"(?:[T ](?P<hour>[0-9]{2})(?::(?P<minute>[0-9]{2})(?::(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,6}))?)?)?)?)?)?(?P<zone>Z|[+-][0-9]{2}:?[0-9]{2})?$"
)
PDF = re.compile(
    r"D:(?P<year>[0-9]{4})(?P<month>[0-9]{2})?(?P<day>[0-9]{2})?"
    r"(?P<hour>[0-9]{2})?(?P<minute>[0-9]{2})?(?P<second>[0-9]{2})?"
    r"(?P<zone>Z|[+-][0-9]{2}'?[0-9]{2}'?)?$"
)
ZONE = re.compile(r"(?:Z|([+-])([0-9]{2}):?([0-9]{2}))$")
EVENTS = {
    "created": ("Creación registrada", "createdate datecreated creationdate created contentcreatedate "
                "contentcreated subseccreatedate contentcreationdate creationdatetime datecreatedutc"),
    "modified": ("Modificación registrada", "modifydate datemodified modificationdate modified "
                 "contentmodifydate subsecmodifydate contentmodified contentmodificationdate lastmodifieddate modificationdatetime"),
    "captured": ("Captura", "datetimeoriginal capturedate dateofcapture dateacquired subsectimeoriginal subsecdatetimeoriginal"),
    "digitized": ("Digitalización", "datetimedigitized digitizeddate digitalcreationdate digitalcreationdatetime"),
    "metadata_date": ("Edición de metadatos", "metadatadate"),
    "media_created": ("Creación del medio", "mediacreatedate trackcreatedate"),
    "media_modified": ("Modificación del medio", "mediamodifydate trackmodifydate"),
    "printed": ("Última impresión", "lastprinted printdate"),
    "gps_date": ("Fecha GPS", "gpsdatestamp gpsdatetime gpsdate"),
    "file_created": ("Creación en el sistema de archivos", "filecreatedate"),
    "file_modified": ("Modificación en el sistema de archivos", "filemodifydate"),
    "file_accessed": ("Acceso en el sistema de archivos", "fileaccessdate"),
    "file_changed": ("Cambio de atributos en el sistema de archivos", "fileinodechangedate"),
}
EVENT_KEYS = {tag: (kind, label) for kind, (label, tags) in EVENTS.items() for tag in tags.split()}
PRECISIONS = ("year", "month", "day", "hour", "minute", "second", "fraction")
OFFSETS = {"datetimeoriginal": "offsettimeoriginal", "datetimedigitized": "offsettimedigitized",
           "createdate": "offsettimedigitized", "modifydate": "offsettime"}


def date_match(value):
    if isinstance(value, str):
        text = value.strip()
        return PDF.fullmatch(text) or DATE.fullmatch(text)
    return None


def date_text(value):
    return (isinstance(value, str) and value.strip() and
            value.strip().casefold() not in {"true", "false", "on", "off", "yes", "no", "enabled", "disabled"})


def date_kind(source, value):
    name = tag_name(source)
    if name == "linksuptodate":
        return None
    known = EVENT_KEYS.get(name)
    label = source.rsplit(":", 1)[-1]
    if not known and (name.startswith(("zip", "file")) or
                      not label.endswith(("Date", "DateTime", "Timestamp", "TimeStamp"))):
        return None
    values = leaves(value, (source,)) if isinstance(value, (list, dict)) else ((value, [source]),)
    if not any(date_text(item) and (known or date_match(item)) for item, _ in values):
        return None
    if name in {"createdate", "subseccreatedate"} and source.startswith(("EXIF:", "Composite:")):
        return "digitized", EVENTS["digitized"][0]
    return known or ("other_date", label)


def parse_date(value, offset=None):
    result = {"normalized": None, "timezone": None, "utc": None, "precision": None, "warnings": []}
    match = date_match(value)
    if not match:
        result["warnings"].append("Fecha inválida o en un formato no reconocido.")
        return result
    parts = match.groupdict()
    precision = next(key for key in reversed(PRECISIONS) if parts.get(key))
    result["precision"] = precision
    numbers = {key: int(parts.get(key) or ("1" if key in {"month", "day"} else "0"))
               for key in PRECISIONS[:-1]}
    try:
        date = datetime(**numbers, microsecond=int((parts.get("fraction") or "").ljust(6, "0")))
        if date.year < 1600 or date.year > 2200:
            result["warnings"].append("Año fuera del intervalo habitual de revisión (1600–2200).")
        fraction = parts.get("fraction")
        if fraction:
            local = date.isoformat(timespec="microseconds")[:20 + len(fraction)]
        else:
            full = date.isoformat()
            local = full[:dict(year=4, month=7, day=10, hour=13, minute=16, second=19)[precision]]
        result["normalized"] = local
        zone = parts["zone"] or offset
        zone = zone.replace("'", "") if isinstance(zone, str) else None
        zone_match = ZONE.fullmatch(zone) if zone else None
        if not zone_match or zone in {"-00:00", "-0000"}:
            if precision in {"hour", "minute", "second", "fraction"}:
                result["warnings"].append("Hora registrada sin un desplazamiento horario utilizable.")
            elif zone:
                result["warnings"].append("Desplazamiento horario inválido.")
            return result
        sign, hours, minutes = zone_match.groups()
        if sign and (int(hours) > 23 or int(minutes) > 59):
            result["warnings"].append("Desplazamiento horario inválido.")
            return result
        result["timezone"] = "+00:00" if zone == "Z" else f"{sign}{hours}:{minutes}"
        if precision in {"hour", "minute", "second", "fraction"}:
            result["normalized"] += result["timezone"]
        if precision in {"second", "fraction"}:
            result["utc"] = datetime.fromisoformat(result["normalized"]).astimezone(timezone.utc).isoformat()
        else:
            result["warnings"].append("Precisión insuficiente para fijar un instante exacto.")
    except (ValueError, OverflowError):
        result.update(normalized=None, timezone=None, utc=None)
        result["warnings"].append("La fecha contiene valores de calendario o de hora imposibles.")
    return result


def date_context(raw, source, value):
    parsed = parse_date(value)
    group, _, tag = source.rpartition(":")
    if source.startswith("EXIF:") and not parsed["timezone"] and tag_name(tag) in OFFSETS:
        offset_tag = OFFSETS[tag_name(tag)]
        related = next((key for key in raw if key.rpartition(":")[0] == group and tag_name(key) == offset_tag), None)
        if related is None:
            # ExifTool writes these standard tags in ExifIFD while ModifyDate usually appears in IFD0.
            copy = next((part for part in group.split(":") if part.startswith("Copy")), "")
            sibling = f"EXIF:ExifIFD{':' + copy if copy else ''}"
            related = next((key for key in raw if key.rpartition(":")[0] == sibling and tag_name(key) == offset_tag), None)
        if related and parsed["normalized"] and isinstance(raw[related], str):
            with_offset = parse_date(value, raw[related])
            if with_offset["timezone"]:
                return {**with_offset, "timezone_source": [related]}
    return {**parsed, "timezone_source": None}


def build_timeline(raw):
    groups = {"instant": [], "local": [], "partial": [], "invalid": []}
    by_kind = defaultdict(list)
    for source, original in raw.items():
        if not (kind_info := date_kind(source, original)):
            continue
        kind, label = kind_info
        values = leaves(original, (source,)) if isinstance(original, (list, dict)) else ((original, [source]),)
        for value, pointer in values:
            if not date_text(value) or (kind == "other_date" and not date_match(value)):
                continue
            parsed = date_context(raw, source, value)
            group = ("instant" if parsed["utc"] else "invalid" if not parsed["normalized"] else
                     "partial" if parsed["precision"] in {"year", "month", "day"} else "local")
            event = {"kind": kind, "label": label, "original": value, "source": pointer,
                     "origin": "filesystem" if kind.startswith("file_") else "internal", **parsed}
            groups[group].append(event)
            if group == "instant" and event["origin"] == "internal":
                by_kind[kind].append(event)
    groups["instant"].sort(key=lambda event: datetime.fromisoformat(event["utc"]))
    warnings = []
    for kind, events in by_kind.items():
        if kind in EVENTS and len({event["utc"] for event in events}) > 1:
            warnings.append(f"{EVENTS[kind][0]} tiene valores distintos según la fuente. Revisa su contexto.")
    starts = {event["utc"]: event for event in by_kind["created"]}
    ends = {event["utc"]: event for event in by_kind["modified"]}
    interval = None
    if len(starts) == len(ends) == 1:
        start, end = next(iter(starts.values())), next(iter(ends.values()))
        seconds = (datetime.fromisoformat(end["utc"]) - datetime.fromisoformat(start["utc"])).total_seconds()
        interval = {"seconds": seconds, "created": start["utc"], "modified": end["utc"],
                    "created_sources": [event["source"] for event in by_kind["created"]],
                    "modified_sources": [event["source"] for event in by_kind["modified"]]}
        if seconds < 0:
            warnings.append("La modificación registrada es anterior a la creación registrada.")
    elif by_kind["created"] and by_kind["modified"]:
        warnings.append("Varias fechas impiden elegir un intervalo único de creación a modificación.")
    return {"groups": groups, "interval": interval, "warnings": warnings}
