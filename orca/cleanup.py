import shutil
import sys

from .analysis import inspect_file
from .exposure import analyze_exposure, exposure_difference
from .extraction import EngineError, run_command
from .metadata import compare
from .uploads import Input

POLICIES = {
    "JPEG": "Reduce EXIF, XMP, IPTC y comentarios. Conserva orientación, perfil de color y píxeles.",
    "PNG": "Reduce EXIF, XMP y textos comunes. Conserva transparencia, perfil de color y píxeles.",
    "PDF": "Reescribe el PDF sin el diccionario Info ni el XMP del catálogo. Los adjuntos, anotaciones, páginas y otros metadatos incrustados se conservan.",
    **dict.fromkeys(("DOCX", "XLSX", "PPTX"),
                    "Reduce las propiedades principales de Open XML. Conserva las demás partes, incluidas las propiedades avanzadas, comentarios, revisiones y contenido incrustado."),
}


def clean(source, target, record, extractor, settings):
    kind = record["format"]
    if kind not in POLICIES or record["status"] not in {"ok", "empty"}:
        raise EngineError("Este archivo no admite limpieza verificada.")
    if any("C2PA" in key or "JUMBF" in key for key in record["raw"]):
        raise EngineError("Se detectaron datos de autenticidad. La limpieza podría invalidarlos.")
    if kind in {"JPEG", "PNG"}:
        shutil.copyfile(source, target)
        extractor.strip_image(target)
        action = "image"
    else:
        action = "pdf" if kind == "PDF" else "office"
    code, _, errors = run_command([sys.executable, "-m", "orca.rewrite", action, str(source), str(target)],
                                  settings.timeout)
    if code:
        raise EngineError(errors.decode("utf-8", errors="replace").strip())
    if target.stat().st_size > 100_000_000:
        raise EngineError("La copia superó el límite de tamaño.")
    after = inspect_file(Input(record["name"], target, target.stat().st_size), extractor, settings, expected=kind)
    if after["status"] not in {"ok", "empty"}:
        raise EngineError("La copia no superó la extracción de verificación.")
    return {"after": after, "comparison": compare(record["raw"], after["raw"]), "policy": POLICIES[kind],
            "exposure_comparison": exposure_difference(analyze_exposure(record), analyze_exposure(after)),
            "limits": "La comparación abarca los campos que ExifTool leyó. Pueden persistir datos en el contenido, en partes incrustadas o en campos que el extractor no reconoce."}
