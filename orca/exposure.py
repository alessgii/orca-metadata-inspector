import json
from collections import defaultdict

from .metadata import SEMANTIC_LABELS, interpreted_field, properties
from .timeline import build_timeline

CATEGORIES = {
    "identity": "Identidad", "location": "Ubicación", "environment": "Entorno de trabajo",
    "history": "Historia documental", "identifiers": "Identificadores", "additional": "Información adicional",
}
RULES = {**dict.fromkeys(("author", "creator", "last_editor", "organization"), (
    "identity", "identity.attribution", "potential", "El campo declara una atribución.",
    "El valor puede vincularse al archivo. Una atribución registrada requiere verificación independiente.")),
    "username": ("identity", "identity.account", "potential", "Se registró una cuenta o carpeta de usuario.",
                 "Puede revelar un nombre de cuenta o la estructura del perfil local."),
    **dict.fromkeys(("latitude", "longitude", "gps"), (
        "location", "location.gps", "potential", "El campo contiene información de GPS.",
        "Puede revelar una ubicación asociada al archivo.")),
    "place": ("location", "location.place", "potential", "El campo declara un lugar.",
              "El lugar puede aportar contexto geográfico sobre el archivo."),
    "path": ("environment", "environment.path", "potential", "Se registró una ruta.",
             "Puede revelar nombres de carpetas, equipos, cuentas o proyectos."),
    "computer": ("environment", "environment.host", "potential", "El campo declara un equipo.",
                 "El nombre puede revelar convenciones internas de infraestructura."),
    **dict.fromkeys(("software", "version", "creation_tool", "producer", "operating_system", "make", "model"), (
        "environment", "environment.tools", "informational", "Se registró una herramienta, versión o dispositivo.",
        "Aporta contexto técnico sobre la producción del archivo.")),
    "serial": ("identifiers", "identifier.serial", "potential", "El campo registra un número de serie.",
               "Puede correlacionar archivos asociados con un dispositivo."),
    **dict.fromkeys(("document_id", "identifier"), (
        "identifiers", "identifier.document", "informational", "Se registró un identificador técnico.",
        "Puede servir para correlación si aparece en varios registros.")),
    **dict.fromkeys(("title", "subject", "keywords"), (
        "additional", "additional.description", "informational", "Se registró texto descriptivo.",
        "Puede aportar contexto sobre el contenido o la finalidad del documento.")),
    "comments": ("additional", "additional.comments", "potential", "Se registró un comentario o descripción.",
                 "El texto libre puede revelar notas internas u otro contexto."),
}
HISTORY_RULE = ("history", "history.date", "informational", "Se registró una fecha.",
                "Ayuda a contextualizar las marcas temporales examinadas.")


def identity(item):
    return item["rule"], item["field"], json.dumps(item["value"], ensure_ascii=False, sort_keys=True)


def analyze_exposure(record, timeline=None):
    groups = defaultdict(list)
    seen = {}
    findings = {(item["field"], tuple(item["source"]), str(item["value"])): item for item in record["findings"]}

    def add(rule, field, value, source, label, finding=None):
        if not rule or value in (None, "", [], {}) or isinstance(value, str) and not value.strip():
            return
        category, code, level, description, reason = rule
        item = {"category": category, "rule": code, "field": field, "level": level, "label": label,
                "value": value, "source": source, "sources": [source], "description": description,
                "reason": finding["description"] if finding and level == "potential" else reason}
        key = identity(item)
        if key not in seen:
            groups[category].append(item)
            seen[key] = item
        elif source not in seen[key]["sources"]:
            seen[key]["sources"].append(source)

    for item in properties(record["normalized"]):
        field = interpreted_field(item)
        existing = findings.get((item["field"], tuple(item["source"]), str(item["value"])))
        add(RULES.get(field), field, item["value"], item["source"], SEMANTIC_LABELS.get(field, field), existing)
    timeline = timeline if timeline is not None else build_timeline(record["raw"])
    for section in ("instant", "local", "partial"):
        for event in timeline["groups"][section]:
            add(HISTORY_RULE, event["kind"], event["original"], event["source"], event["label"])
    return {"categories": [{"id": key, "label": label, "items": groups[key]}
                           for key, label in CATEGORIES.items() if key in groups],
            "total": len(seen)}


def exposure_difference(before, after):
    old, new = ({identity(item): item for category in result["categories"] for item in category["items"]}
                for result in (before, after))
    return {
        "before": before["total"], "after": after["total"],
        "removed": [item for key, item in old.items() if key not in new],
        "retained": [item for key, item in new.items() if key in old],
        "added": [item for key, item in new.items() if key not in old],
    }
