import re
import shutil
import stat
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError

from .config import CHUNK, MAX_BYTES, MAX_FILES, Settings
from .xls_reader import OLE_SIGNATURE, is_valid_xls

FORMATS = {"pdf": "PDF", "docx": "DOCX", "xlsx": "XLSX", "xls": "XLS", "pptx": "PPTX", "jpg": "JPEG",
           "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
OOXML = {"DOCX": "word/document.xml", "XLSX": "xl/workbook.xml", "PPTX": "ppt/presentation.xml"}
MAIN_TYPES = {
    "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "PPTX": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
ZIP_ERRORS = (ValueError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error)


class UploadError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


@dataclass
class Input:
    name: str
    path: Path
    size: int
    error: str | None = None
    partial: bool = False


@dataclass
class ExpansionBudget:
    remaining: int

    def account(self, length):
        self.remaining -= length
        if self.remaining < 0:
            raise UploadError("El contenido descomprimido supera el límite permitido.", 413)


def extension(name):
    return PurePosixPath(name).suffix.lower().lstrip(".")


def safe_upload_name(name):
    name = re.split(r"[/\\]", name)[-1]
    return "".join(char for char in name if not unicodedata.category(char).startswith("C"))[:240] or "archivo"


def signatures(path):
    with path.open("rb") as file:
        head = file.read(1024)
    if head.startswith(b"%PDF-"):
        return "PDF"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "WEBP"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "ZIP"
    if head.startswith(OLE_SIGNATURE):
        return "OLE"
    return None


def checked_zip(path, max_entries, max_central=8_000_000):
    # Check the central directory envelope before zipfile allocates records for it.
    size = path.stat().st_size
    with path.open("rb") as file:
        tail_size = min(65557, size)
        file.seek(-tail_size, 2)
        tail = file.read(tail_size)
    cursor = len(tail)
    while True:
        cursor = tail.rfind(b"PK\x05\x06", 0, cursor)
        if cursor < 0:
            raise UploadError("El contenedor ZIP está dañado.")
        if len(tail) - cursor >= 22:
            fields = struct.unpack_from("<4s4H2IH", tail, cursor)
            if cursor + 22 + fields[-1] == len(tail):
                break
    _, disk, origin, disk_count, count, directory_size, offset, _ = fields
    if disk or origin or disk_count != count:
        raise UploadError("No se admiten contenedores ZIP multipartes.")
    if count > max_entries or directory_size > max_central:
        raise UploadError("El ZIP supera el límite de entradas o de directorio central.", 413)
    if offset + directory_size != size - tail_size + cursor:
        raise UploadError("El directorio central del ZIP es incompatible o está dañado.")
    # zipfile reads through the whole central directory independently of the declared count.
    if directory_size > max_entries * (46 + 3 * 65535):
        raise UploadError("El directorio central del ZIP excede el límite.", 413)
    with path.open("rb") as file:
        file.seek(offset)
        end = offset + directory_size
        actual = 0
        while file.tell() < end:
            header = file.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise UploadError("El directorio central del ZIP está dañado.")
            actual += 1
            if actual > max_entries:
                raise UploadError("El ZIP supera el límite de entradas.", 413)
            file.seek(sum(struct.unpack_from("<3H", header, 28)), 1)
        if file.tell() != end or count != actual:
            raise UploadError("El ZIP declara un directorio inconsistente.")
    return zipfile.ZipFile(path)


def safe_members(archive):
    files = set()
    folders = set()
    seen = set()
    output = []
    for item in archive.infolist():
        raw = item.orig_filename
        parts = raw.removesuffix("/").split("/")
        if (
            not raw or len(raw) > 512 or len(parts) > 24 or
            any(not part or part in {".", ".."} or part.endswith((".", " ")) or
                any(char in '\\:<>|" ?*'.replace(" ", "") or unicodedata.category(char).startswith("C")
                    for char in part) for part in parts)
        ):
            raise UploadError("El ZIP contiene una ruta insegura o incompatible.")
        mode = stat.S_IFMT(item.external_attr >> 16)
        dos_attributes = item.external_attr & 0xffff
        if (mode not in {0, stat.S_IFDIR, stat.S_IFREG} or dos_attributes & 0x440 or
                (mode == stat.S_IFDIR or dos_attributes & 0x10) and not item.is_dir()):
            raise UploadError("El ZIP contiene enlaces o entradas especiales.")
        if item.flag_bits & 1:
            raise UploadError("El ZIP contiene archivos cifrados.")
        if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise UploadError("El método de compresión ZIP no está admitido.")
        key = unicodedata.normalize("NFC", raw.rstrip("/")).casefold()
        parents = [key.rsplit("/", i)[0] for i in range(1, len(parts))]
        if key in seen or any(parent in files for parent in parents) or (key in folders and not item.is_dir()):
            raise UploadError("El ZIP contiene rutas duplicadas o en conflicto.")
        seen.add(key)
        folders.update(parents)
        if not item.is_dir():
            files.add(key)
            output.append(item)
        elif item.file_size:
            raise UploadError("Un directorio ZIP contiene datos inesperados.")
    return output


def check_budget(items, limit):
    if sum(item.file_size for item in items) > limit or any(item.file_size > MAX_BYTES for item in items):
        raise UploadError("El contenido descomprimido supera el límite permitido.", 413)
    if any(item.file_size > max(1_000_000, item.compress_size * 250) for item in items):
        raise UploadError("El ZIP supera la relación de compresión permitida.", 413)


def file_chunks(source):
    while chunk := source.read(CHUNK):
        yield chunk


def member_chunks(archive, item):
    # ZipExtFile can stop at the declared uncompressed length. Validate the entire
    # stored/deflated stream in bounded blocks before giving it to another parser.
    stream = archive.fp
    stream.seek(item.header_offset)
    header = stream.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise zipfile.BadZipFile("Invalid member header")
    flags, method = struct.unpack_from("<2H", header, 6)
    crc, packed, expanded, name_size, extra_size = struct.unpack_from("<3I2H", header, 14)
    raw_name = stream.read(name_size)
    name = raw_name.decode("utf-8" if flags & 0x800 else "cp437")
    stream.seek(extra_size, 1)
    if (name != item.orig_filename or flags != item.flag_bits or method != item.compress_type or
            stream.tell() + item.compress_size > archive.start_dir):
        raise zipfile.BadZipFile("Inconsistent member header")
    if not flags & 8 and (crc, packed, expanded) != (item.CRC, item.compress_size, item.file_size):
        raise zipfile.BadZipFile("Inconsistent member sizes or checksum")

    remaining = item.compress_size
    decoder = zlib.decompressobj(-zlib.MAX_WBITS) if method == zipfile.ZIP_DEFLATED else None
    total = checksum = 0

    def output(block):
        nonlocal total, checksum
        total += len(block)
        if total > item.file_size:
            raise UploadError("El tamaño real de un miembro ZIP supera lo declarado.", 413)
        checksum = zlib.crc32(block, checksum)
        return block

    while remaining:
        data = stream.read(min(remaining, CHUNK))
        if not data:
            raise zipfile.BadZipFile("Incomplete member data")
        remaining -= len(data)
        if decoder:
            while data:
                block = decoder.decompress(data, CHUNK)
                data = decoder.unconsumed_tail
                if block:
                    yield output(block)
                if decoder.eof:
                    if decoder.unused_data or remaining:
                        raise zipfile.BadZipFile("Trailing member data")
                    break
        else:
            yield output(data)

    if decoder and not decoder.eof:
        while block := decoder.decompress(b"", CHUNK):
            yield output(block)
        if not decoder.eof:
            raise zipfile.BadZipFile("Incomplete deflate stream")
    if total != item.file_size or checksum != item.CRC:
        raise zipfile.BadZipFile("Inconsistent member size or checksum")


def consume(chunks, destination, remaining, budget=None):
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > remaining:
            raise UploadError("El contenido supera el límite de bytes permitido.", 413)
        if budget is not None:
            budget.account(len(chunk))
        if destination is not None:
            destination.write(chunk)
    return total


def office_format(path, budget):
    with checked_zip(path, 4096) as archive:
        entries = safe_members(archive)
        check_budget(entries, budget.remaining)
        by_name = {item.filename: item for item in entries}
        content_types = by_name.get("[Content_Types].xml")
        if not content_types or content_types.file_size > 1_000_000 or "_rels/.rels" not in by_name:
            raise UploadError("El documento Office no contiene un paquete Open XML válido.")
        manifest_bytes = bytearray()
        for chunk in member_chunks(archive, content_types):
            budget.account(len(chunk))
            manifest_bytes.extend(chunk)
        manifest = fromstring(manifest_bytes, forbid_dtd=True)
        overrides = {child.get("PartName"): child.get("ContentType") for child in manifest
                     if child.tag == f"{{{PACKAGE_NS}}}Override"}
        matches = [kind for kind, part in OOXML.items()
                   if part in by_name and overrides.get("/" + part) == MAIN_TYPES[kind]]
        if manifest.tag != f"{{{PACKAGE_NS}}}Types" or len(matches) != 1:
            raise UploadError("El contenido no corresponde a un documento Office admitido.")
        for item in entries:
            if item is not content_types:
                consume(member_chunks(archive, item), None, MAX_BYTES, budget)
        return matches[0]


def identify(file, settings, expansion=None):
    requested = FORMATS.get(extension(file.name))
    if not requested:
        return None, "Formato no admitido."
    matched = signatures(file.path)
    if matched == "ZIP" and requested in OOXML:
        try:
            matched = office_format(file.path, expansion or ExpansionBudget(settings.expanded_limit))
        except (UploadError, DefusedXmlException, ParseError, *ZIP_ERRORS) as exc:
            return None, str(exc) if isinstance(exc, UploadError) else "Paquete Office corrupto o inseguro."
    elif matched == "OLE" and requested == "XLS":
        if is_valid_xls(file.path):
            matched = "XLS"
        else:
            return None, "El archivo OLE2/CFBF está corrupto o no contiene un libro Excel XLS válido."
    if matched != requested:
        return None, "El contenido no coincide con la extensión indicada."
    return matched, None


def prepare(uploads, work: Path, settings: Settings):
    # All non-directory entries count, including hidden, invalid and unsupported files.
    output, individual_bytes, total = [], 0, 0
    expansion = ExpansionBudget(settings.expanded_limit)
    for upload in uploads:
        name = safe_upload_name(upload.filename)
        suffix = extension(name)
        path = work / f"{uuid4().hex}.{suffix if suffix in FORMATS else 'bin'}"
        with path.open("xb") as target:
            size = consume(file_chunks(upload.file), target, MAX_BYTES)
        total += size
        if total > settings.upload_limit:
            raise UploadError("La carga acumulada supera el límite configurado.", 413)
        if suffix != "zip":
            individual_bytes += size
            if individual_bytes > MAX_BYTES:
                raise UploadError("Los archivos individuales superan el límite de la operación.", 413)
            output.append(Input(name, path, size))
        else:
            try:
                if signatures(path) != "ZIP":
                    raise UploadError("El contenido no corresponde a un ZIP.")
                with checked_zip(path, 2048) as archive:
                    entries = safe_members(archive)
                    check_budget(entries, expansion.remaining)
                    if len(output) + len(entries) > MAX_FILES:
                        raise UploadError("La operación admite hasta 100 archivos, incluidos los del ZIP.", 413)
                    for item in entries:
                        inner_suffix = extension(item.filename)
                        inner_path = work / f"{uuid4().hex}.{inner_suffix if inner_suffix in FORMATS else 'bin'}"
                        display = f"{name} / {item.filename}"
                        try:
                            with inner_path.open("xb") as target:
                                length = consume(member_chunks(archive, item), target, MAX_BYTES, expansion)
                        except ZIP_ERRORS:
                            inner_path.unlink(missing_ok=True)
                            output.append(Input(display, inner_path, item.file_size,
                                                "El miembro del ZIP está corrupto o es incompatible.", partial=True))
                        else:
                            output.append(Input(display, inner_path, length))
            except ZIP_ERRORS:
                output.append(Input(name, path, size, "El ZIP está corrupto o es incompatible."))
            except UploadError as exc:
                if str(exc) not in {"El contenido no corresponde a un ZIP.", "El contenedor ZIP está dañado."}:
                    raise
                output.append(Input(name, path, size, str(exc)))
            else:
                path.unlink()
        if len(output) > MAX_FILES:
            raise UploadError("La operación admite hasta 100 archivos, incluidos los del ZIP.", 413)
    if not output:
        raise UploadError("Selecciona al menos un archivo o un ZIP con archivos.")
    return output, expansion
