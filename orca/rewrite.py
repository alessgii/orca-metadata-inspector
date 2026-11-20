"""Isolated copy transformations. Run via python -m orca.rewrite."""
import hashlib
import mmap
import shutil
import sys
import zipfile
from copy import copy
from pathlib import Path

import pikepdf
from defusedxml.ElementTree import fromstring
from lxml import etree
from PIL import Image

from .config import CHUNK

CORE = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
CORE_PART = "docProps/core.xml"
CORE_FIELDS = {f"{{{namespace}}}{name}" for namespace, names in [
    (CORE, "category contentStatus keywords lastModifiedBy lastPrinted revision version"),
    (DC, "creator description identifier language subject title"),
    (DCTERMS, "created modified"),
] for name in names.split()}


class RewriteError(Exception):
    pass


def verify_image(source, target):
    # Keep color profiles, transparent pixels and orientation intact.
    Image.MAX_IMAGE_PIXELS = 30_000_000
    with Image.open(source) as before, Image.open(target) as after:
        if (before.width * before.height > Image.MAX_IMAGE_PIXELS or
                getattr(before, "n_frames", 1) != 1 or getattr(after, "n_frames", 1) != 1):
            raise RewriteError("La limpieza de imágenes animadas o de más de 30 megapíxeles no está admitida.")
        if (before.format != after.format or before.size != after.size or before.mode != after.mode or
                before.info.get("icc_profile") != after.info.get("icc_profile") or
                before.getexif().get(274, 1) != after.getexif().get(274, 1)):
            raise RewriteError("La copia cambió propiedades necesarias para representar la imagen.")
        for top in range(0, before.height, 128):
            box = (0, top, before.width, min(top + 128, before.height))
            if before.crop(box).convert("RGBA").tobytes() != after.crop(box).convert("RGBA").tobytes():
                raise RewriteError("Los píxeles decodificados cambiaron durante la limpieza.")


def page_fingerprint(pdf):
    result = []
    for page in pdf.pages:
        content = page.obj.get("/Contents")
        streams = list(content) if isinstance(content, pikepdf.Array) else ([] if content is None else [content])
        result.append((
            tuple(page.mediabox), tuple(page.cropbox), int(page.obj.get("/Rotate", 0)),
            [hashlib.sha256(item.read_raw_bytes()).hexdigest() for item in streams],
            len(page.obj.get("/Annots", [])),
        ))
    return result


def rewrite_pdf(source, target):
    with source.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        if raw.find(b"/ByteRange") >= 0:
            raise RewriteError("Se detectó una firma PDF. La limpieza invalidaría la firma.")
    try:
        with pikepdf.Pdf.open(source, attempt_recovery=False) as pdf:
            if pdf.is_encrypted:
                raise RewriteError("La limpieza no admite PDF cifrados.")
            if "/Perms" in pdf.Root or any(
                isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)) and (
                    obj.get("/Type") == pikepdf.Name("/Sig") or obj.get("/FT") == pikepdf.Name("/Sig") or
                    "/ByteRange" in obj
                ) for obj in pdf.objects
            ):
                raise RewriteError("Se detectó una firma o un campo de firma PDF.")
            if "/OutputIntents" in pdf.Root:
                raise RewriteError("La limpieza no admite PDF con perfiles de salida declarados.")
            if "/Metadata" in pdf.Root:
                data = pdf.Root.Metadata.read_bytes()
                if any(marker in data.lower() for marker in (b"pdfa/ns/id", b"pdfua/ns/id", b"pdfx/ns/id")):
                    raise RewriteError("La copia podría perder su conformidad PDF/A, PDF/UA o PDF/X.")
                del pdf.Root.Metadata
            if "/Info" in pdf.trailer:
                del pdf.docinfo
            before = page_fingerprint(pdf)
            pdf.save(target, fix_metadata_version=False, compress_streams=False)
        with pikepdf.Pdf.open(target, attempt_recovery=False) as pdf:
            if (page_fingerprint(pdf) != before or "/Info" in pdf.trailer or "/Metadata" in pdf.Root):
                raise RewriteError("La copia PDF no superó la verificación de contenido y metadatos.")
    except pikepdf.PasswordError as exc:
        raise RewriteError("La limpieza no admite PDF protegidos por contraseña.") from exc


def clean_core(data):
    fromstring(data, forbid_dtd=True)
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    if root.tag != f"{{{CORE}}}coreProperties":
        raise RewriteError("La parte de propiedades principales no es válida.")
    for element in list(root):
        if element.tag in CORE_FIELDS:
            root.remove(element)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def rewrite_office(source, target):
    with zipfile.ZipFile(source) as package:
        members = package.infolist()
        names = package.namelist()
        if any("_xmlsignatures" in name.casefold() or "vbaproject" in name.casefold()
               or "macrosheets/" in name.casefold() for name in names):
            raise RewriteError("La limpieza no admite paquetes Office con macros o firmas.")
        controls = ["[Content_Types].xml", *(name for name in names if name.endswith(".rels"))]
        for name in controls:
            if package.getinfo(name).file_size > 1_000_000:
                raise RewriteError("El manifiesto o las relaciones Office superan el límite de revisión.")
            root = fromstring(package.read(name), forbid_dtd=True)
            if any(
                any(marker in value.casefold() for marker in ("digital-signature", "macroenabled", "vbaproject",
                                                               "macrosheet"))
                for element in root for value in element.attrib.values()
            ):
                raise RewriteError("La limpieza no admite paquetes Office con macros o firmas.")
        replacement = None
        if CORE_PART in names:
            if package.getinfo(CORE_PART).file_size > 1_000_000:
                raise RewriteError("Las propiedades principales de Office superan el límite de revisión.")
            replacement = clean_core(package.read(CORE_PART))
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as result:
            result.comment = package.comment
            for member in members:
                entry = copy(member)
                if member.filename == CORE_PART:
                    result.writestr(entry, replacement)
                else:
                    with package.open(member) as src, result.open(entry, "w") as dst:
                        shutil.copyfileobj(src, dst, CHUNK)
        with zipfile.ZipFile(target) as result:
            if (result.testzip() is not None or result.namelist() != names or
                    any(result.getinfo(member.filename).CRC != member.CRC
                        for member in members if member.filename != CORE_PART)):
                raise RewriteError("La copia Office no conservó todas las partes ajenas a las propiedades principales.")


if __name__ == "__main__":
    handlers = {"image": verify_image, "pdf": rewrite_pdf, "office": rewrite_office}
    try:
        handlers[sys.argv[1]](Path(sys.argv[2]), Path(sys.argv[3]))
    except RewriteError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    except Exception:
        print("El archivo no pudo reescribirse o verificarse de forma segura.", file=sys.stderr)
        sys.exit(2)
