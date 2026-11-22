"""Defensive image analysis, JPEG/PNG structural inspection, exact pixel comparison,
perceptual metrics, and difference map generation for La Orca."""

from __future__ import annotations

import hashlib
import io
import math
import struct
import zlib
from pathlib import Path

from PIL import Image, ImageChops, ImageCms, ImageOps, UnidentifiedImageError

IMAGE_ENGINE_VERSION = "1.0"
MAX_IMAGE_PIXELS = 40_000_000  # 40 MP safety ceiling
MAX_IMAGE_DIMENSION = 16_384
MAX_DIFF_MAP_PIXELS = 16_000_000  # 16 MP ceiling for inline PNG diff maps
MAX_STRUCTURAL_ITEMS = 4096
MAX_DECOMPRESSED_SCANLINES = 64_000_000  # 64 MB ceiling for 16-bit PNG scanlines
PERCEPTUAL_RESIZE_DIM = 256

# Pillow global bomb guard aligned with our limit.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_COLOR_TYPES = {
    0: ("L", 1),
    2: ("RGB", 3),
    3: ("P", 1),
    4: ("LA", 2),
    6: ("RGBA", 4),
}
PNG_METADATA_CHUNKS = {"tEXt", "zTXt", "iTXt", "eXIf", "tIME"}
PNG_COLOR_CHUNKS = {"iCCP", "sRGB", "gAMA", "cHRM", "cICP", "mDCv", "cLLI"}
PNG_APNG_CHUNKS = {"acTL", "fcTL", "fdAT"}

JPEG_MARKER_NAMES = {
    0xD8: "SOI",
    0xD9: "EOI",
    0xDA: "SOS",
    0xDB: "DQT",
    0xC4: "DHT",
    0xDD: "DRI",
    0xFE: "COM",
    0xC0: "SOF0",
    0xC1: "SOF1",
    0xC2: "SOF2",
    0xC3: "SOF3",
    0xC5: "SOF5",
    0xC6: "SOF6",
    0xC7: "SOF7",
    0xC9: "SOF9",
    0xCA: "SOF10",
    0xCB: "SOF11",
    0xCD: "SOF13",
    0xCE: "SOF14",
    0xCF: "SOF15",
}
JPEG_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
JPEG_STANDALONE_MARKERS = {0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9}

# Precomputed 32x32 DCT-II cosine table for deterministic 64-bit pHash.
_DCT_SIZE = 32
_DCT_COS = [
    [math.cos(((2 * x + 1) * u * math.pi) / (2 * _DCT_SIZE)) for x in range(_DCT_SIZE)]
    for u in range(8)
]


class ImageSafetyError(Exception):
    """Raised when an image violates safety limits or is structurally corrupted."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_raw_format(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith(PNG_SIGNATURE):
        return "PNG"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "WEBP"
    return None


# ---------------------------------------------------------------------------
# Defensive JPEG Structural Parser
# ---------------------------------------------------------------------------

def _classify_jpeg_app(marker: int, payload: bytes) -> str:
    idx = marker - 0xE0
    base = f"APP{idx}"
    if marker == 0xE0 and payload.startswith(b"JFIF\x00"):
        return "APP0 (JFIF)"
    if marker == 0xE0 and payload.startswith(b"JFXX\x00"):
        return "APP0 (JFXX)"
    if marker == 0xE1:
        if payload.startswith(b"Exif\x00\x00"):
            return "APP1 (Exif)"
        if payload.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
            return "APP1 (XMP)"
        if payload.startswith(b"http://ns.adobe.com/xmp/extension/\x00"):
            return "APP1 (XMP-Ext)"
    if marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"):
        return "APP2 (ICC)"
    if marker == 0xED and payload.startswith(b"Photoshop 3.0\x00"):
        return "APP13 (IPTC/Photoshop)"
    if marker == 0xEE and payload.startswith(b"Adobe"):
        return "APP14 (Adobe)"
    return base


def inspect_jpeg_structure(path: Path) -> dict:
    """Defensively parse JPEG markers, segments, coding parameters, and trailing data."""
    data = path.read_bytes()
    size = len(data)
    if size < 4 or data[:2] != b"\xff\xd8":
        return {
            "format": "JPEG",
            "valid": False,
            "error": "El archivo no comienza con el marcador SOI (FF D8) de JPEG.",
            "segments": [],
            "warnings": [],
        }

    segments: list[dict] = [
        {"index": 0, "marker": "SOI", "code": "FFD8", "offset": 0, "length": 2, "category": "framing"}
    ]
    warnings: list[str] = []
    coding: dict = {}
    pos = 2
    seen_eoi = False
    trailing_bytes = 0
    scan_bytes_total = 0

    while pos < size:
        if len(segments) >= MAX_STRUCTURAL_ITEMS:
            warnings.append("Se alcanzó el límite máximo de segmentos JPEG inspeccionados.")
            break
        # Locate next 0xFF marker prefix
        if data[pos] != 0xFF:
            warnings.append(f"Bytes inesperados fuera de marcador en el desplazamiento {pos}.")
            nxt = data.find(b"\xff", pos)
            if nxt == -1:
                break
            pos = nxt
        # Skip fill 0xFF bytes
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            warnings.append("Marcador JPEG truncado al final del archivo.")
            break
        marker = data[pos]
        marker_offset = pos - 1
        pos += 1

        if marker == 0x00:
            continue

        if marker == 0xD9:  # EOI
            segments.append({
                "index": len(segments),
                "marker": "EOI",
                "code": "FFD9",
                "offset": marker_offset,
                "length": 2,
                "category": "framing",
            })
            seen_eoi = True
            trailing_bytes = size - pos
            if trailing_bytes > 0:
                trailing_data = data[pos:]
                segments.append({
                    "index": len(segments),
                    "marker": "TRAILING_DATA",
                    "code": "POST-EOI",
                    "offset": pos,
                    "length": trailing_bytes,
                    "sha256": _sha256_bytes(trailing_data),
                    "category": "trailing",
                })
                warnings.append(
                    f"Se detectaron {trailing_bytes} bytes posteriores al marcador EOI. "
                    "Esto puede deberse a datos auxiliares o contenedores incrustados y no implica contenido malicioso."
                )
            break

        if marker in JPEG_STANDALONE_MARKERS:
            name = JPEG_MARKER_NAMES.get(marker, f"RST{marker - 0xD0}" if 0xD0 <= marker <= 0xD7 else f"FF{marker:02X}")
            segments.append({
                "index": len(segments),
                "marker": name,
                "code": f"FF{marker:02X}",
                "offset": marker_offset,
                "length": 2,
                "category": "framing",
            })
            continue

        if pos + 2 > size:
            warnings.append(f"Longitud de segmento truncada en el marcador FF{marker:02X}.")
            break
        seg_len = struct.unpack_from(">H", data, pos)[0]
        if seg_len < 2 or pos + seg_len > size:
            warnings.append(f"Segmento FF{marker:02X} con longitud inválida o truncada ({seg_len} B).")
            break
        payload = data[pos + 2: pos + seg_len]
        pos += seg_len

        if 0xE0 <= marker <= 0xEF:
            name = _classify_jpeg_app(marker, payload)
            cat = "color" if "ICC" in name else "metadata"
        elif marker == 0xFE:
            name = "COM"
            cat = "metadata"
        elif marker in JPEG_SOF_MARKERS:
            name = JPEG_MARKER_NAMES.get(marker, f"SOF({marker:02X})")
            cat = "coding"
            if len(payload) >= 6:
                prec, h, w, comps = struct.unpack_from(">BHHB", payload, 0)
                coding["sof_marker"] = name
                coding["progressive"] = marker in {0xC2, 0xC6, 0xCA, 0xCE}
                coding["precision_bits"] = prec
                coding["width"] = w
                coding["height"] = h
                coding["components"] = comps
        elif marker in {0xDB, 0xC4, 0xDD}:
            name = JPEG_MARKER_NAMES[marker]
            cat = "coding"
        elif marker == 0xDA:
            name = "SOS"
            cat = "scan"
        else:
            name = f"MARKER_FF{marker:02X}"
            cat = "other"

        seg_entry = {
            "index": len(segments),
            "marker": name,
            "code": f"FF{marker:02X}",
            "offset": marker_offset,
            "length": seg_len + 2,
            "payload_length": len(payload),
            "sha256": _sha256_bytes(payload),
            "category": cat,
        }
        segments.append(seg_entry)

        if marker == 0xDA:
            # Entropy-coded scan data until next non-stuffed, non-RST marker
            scan_start = pos
            while pos < size:
                ff = data.find(b"\xff", pos)
                if ff == -1:
                    pos = size
                    break
                if ff + 1 >= size:
                    pos = size
                    break
                nxt = data[ff + 1]
                if nxt == 0x00 or (0xD0 <= nxt <= 0xD7):
                    pos = ff + 2
                    continue
                if nxt == 0xFF:
                    pos = ff + 1
                    continue
                pos = ff
                break
            scan_len = pos - scan_start
            scan_bytes_total += scan_len
            segments.append({
                "index": len(segments),
                "marker": "ENTROPY_SCAN",
                "code": "SCAN",
                "offset": scan_start,
                "length": scan_len,
                "sha256": _sha256_bytes(data[scan_start:pos]),
                "category": "scan",
            })

    if not seen_eoi:
        warnings.append("El archivo JPEG no contiene marcador de cierre EOI (FF D9).")

    dqt_hashes = [s["sha256"] for s in segments if s["marker"] == "DQT"]
    dht_hashes = [s["sha256"] for s in segments if s["marker"] == "DHT"]
    metadata_segs = [s for s in segments if s["category"] == "metadata"]
    color_segs = [s for s in segments if s["category"] == "color"]
    scan_segs = [s for s in segments if s["marker"] == "ENTROPY_SCAN"]

    return {
        "format": "JPEG",
        "valid": seen_eoi and bool(coding.get("sof_marker")),
        "error": None if seen_eoi else "Falta marcador EOI en el flujo JPEG.",
        "file_size": size,
        "segment_count": len(segments),
        "segments": segments,
        "coding": coding,
        "dqt_count": len(dqt_hashes),
        "dqt_sha256": _sha256_bytes("|".join(dqt_hashes).encode()) if dqt_hashes else None,
        "dht_count": len(dht_hashes),
        "dht_sha256": _sha256_bytes("|".join(dht_hashes).encode()) if dht_hashes else None,
        "scan_bytes": scan_bytes_total,
        "scan_sha256": _sha256_bytes("|".join(s["sha256"] for s in scan_segs).encode()) if scan_segs else None,
        "metadata_segments": [s["marker"] for s in metadata_segs],
        "color_segments": [s["marker"] for s in color_segs],
        "trailing_bytes": trailing_bytes,
        "warnings": warnings,
    }


def compare_jpeg_structures(struct_a: dict, struct_b: dict) -> dict:
    """Compare two inspected JPEG structures across framing, coding, metadata, color, and scans."""
    segs_a = struct_a.get("segments", [])
    segs_b = struct_b.get("segments", [])
    order_a = [s["marker"] for s in segs_a]
    order_b = [s["marker"] for s in segs_b]

    def _category_sig(segs: list[dict], cat: str) -> list[tuple]:
        return [(s["marker"], s.get("length"), s.get("sha256")) for s in segs if s.get("category") == cat]

    metadata_changed = _category_sig(segs_a, "metadata") != _category_sig(segs_b, "metadata")
    color_changed = _category_sig(segs_a, "color") != _category_sig(segs_b, "color")
    coding_changed = (
        struct_a.get("coding") != struct_b.get("coding")
        or struct_a.get("dqt_sha256") != struct_b.get("dqt_sha256")
        or struct_a.get("dht_sha256") != struct_b.get("dht_sha256")
    )
    scan_changed = struct_a.get("scan_sha256") != struct_b.get("scan_sha256")
    trailing_changed = (struct_a.get("trailing_bytes", 0) != struct_b.get("trailing_bytes", 0)) or (
        _category_sig(segs_a, "trailing") != _category_sig(segs_b, "trailing")
    )
    order_changed = order_a != order_b

    differences: list[dict] = []
    if metadata_changed:
        differences.append({
            "area": "metadata_segments",
            "label": "Segmentos de metadatos (APPn / COM)",
            "detail": (
                f"A: {', '.join(struct_a.get('metadata_segments') or ['ninguno'])} · "
                f"B: {', '.join(struct_b.get('metadata_segments') or ['ninguno'])}"
            ),
        })
    if color_changed:
        differences.append({
            "area": "color_segments",
            "label": "Segmentos de perfil de color (ICC)",
            "detail": "Los segmentos APP2 (ICC) difieren en presencia, tamaño o contenido.",
        })
    if coding_changed:
        differences.append({
            "area": "coding_parameters",
            "label": "Parámetros de codificación (SOF / DQT / DHT)",
            "detail": "Difieren las tablas de cuantización, tablas Huffman o parámetros del frame SOF.",
        })
    if scan_changed:
        differences.append({
            "area": "entropy_scan",
            "label": "Datos codificados de imagen (SOS / scan)",
            "detail": (
                f"A: {struct_a.get('scan_bytes', 0)} B · B: {struct_b.get('scan_bytes', 0)} B. "
                "Los datos comprimidos difieren (requiere comparación de píxeles para confirmar cambio visual)."
            ),
        })
    if trailing_changed:
        differences.append({
            "area": "trailing_data",
            "label": "Datos posteriores a EOI",
            "detail": f"A: {struct_a.get('trailing_bytes', 0)} B · B: {struct_b.get('trailing_bytes', 0)} B.",
        })
    if order_changed and not differences:
        differences.append({
            "area": "segment_order",
            "label": "Orden de segmentos JPEG",
            "detail": "La secuencia de marcadores difiere aunque el contenido principal coincida.",
        })

    identical = not differences and not order_changed
    return {
        "format": "JPEG",
        "status": "matched" if identical else "different",
        "identical_structure": identical,
        "metadata_segments_changed": metadata_changed,
        "color_segments_changed": color_changed,
        "coding_parameters_changed": coding_changed,
        "scan_data_changed": scan_changed,
        "trailing_data_changed": trailing_changed,
        "segment_order_changed": order_changed,
        "differences": differences,
        "summary_a": {
            "segment_count": struct_a.get("segment_count", 0),
            "coding": struct_a.get("coding", {}),
            "metadata_segments": struct_a.get("metadata_segments", []),
            "color_segments": struct_a.get("color_segments", []),
            "scan_bytes": struct_a.get("scan_bytes", 0),
            "trailing_bytes": struct_a.get("trailing_bytes", 0),
        },
        "summary_b": {
            "segment_count": struct_b.get("segment_count", 0),
            "coding": struct_b.get("coding", {}),
            "metadata_segments": struct_b.get("metadata_segments", []),
            "color_segments": struct_b.get("color_segments", []),
            "scan_bytes": struct_b.get("scan_bytes", 0),
            "trailing_bytes": struct_b.get("trailing_bytes", 0),
        },
        "warnings": list(dict.fromkeys([*struct_a.get("warnings", []), *struct_b.get("warnings", [])])),
        "limitations": [
            "Una diferencia estructural en segmentos JPEG no implica automáticamente una modificación visual de los píxeles.",
            "Los segmentos desconocidos o bytes posteriores a EOI se reportan de forma descriptiva y no se interpretan como contenido malicioso.",
        ],
    }


# ---------------------------------------------------------------------------
# Defensive PNG Structural Parser (with CRC32 & APNG detection)
# ---------------------------------------------------------------------------

def inspect_png_structure(path: Path) -> dict:
    """Defensively parse PNG chunks, validate CRC32, detect APNG, and summarize sections."""
    data = path.read_bytes()
    size = len(data)
    if size < 8 or data[:8] != PNG_SIGNATURE:
        return {
            "format": "PNG",
            "valid": False,
            "error": "La firma de 8 bytes no corresponde al formato PNG.",
            "chunks": [],
            "crc_errors": [],
            "warnings": [],
        }

    chunks: list[dict] = []
    crc_errors: list[dict] = []
    warnings: list[str] = []
    ihdr: dict = {}
    idat_hash = hashlib.sha256()
    idat_bytes = 0
    is_apng = False
    apng_frames = None
    has_iend = False
    trailing_bytes = 0
    pos = 8

    while pos < size:
        if len(chunks) >= MAX_STRUCTURAL_ITEMS:
            warnings.append("Se alcanzó el límite máximo de chunks PNG inspeccionados.")
            break
        if pos + 12 > size:
            warnings.append(f"Chunk PNG truncado en el desplazamiento {pos}.")
            break
        length = struct.unpack_from(">I", data, pos)[0]
        ctype_raw = data[pos + 4: pos + 8]
        ctype = ctype_raw.decode("latin1")
        data_start = pos + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > size:
            warnings.append(f"Datos o CRC truncados en el chunk {ctype} (longitud declarada {length} B).")
            break
        payload = data[data_start:data_end]
        declared_crc = struct.unpack_from(">I", data, data_end)[0]
        computed_crc = zlib.crc32(ctype_raw + payload) & 0xFFFFFFFF
        crc_ok = declared_crc == computed_crc
        if not crc_ok:
            crc_errors.append({
                "index": len(chunks),
                "type": ctype,
                "offset": pos,
                "declared_crc": f"{declared_crc:08x}",
                "computed_crc": f"{computed_crc:08x}",
            })

        if ctype == "IHDR" and length == 13:
            w, h, bit_depth, color_type, comp, flt, interlace = struct.unpack(">IIBBBBB", payload)
            mode_name, channels = PNG_COLOR_TYPES.get(color_type, ("unknown", 0))
            ihdr = {
                "width": w,
                "height": h,
                "bit_depth": bit_depth,
                "color_type": color_type,
                "mode_hint": mode_name,
                "channels": channels,
                "compression": comp,
                "filter": flt,
                "interlace": interlace,
                "interlaced": interlace != 0,
            }
        elif ctype == "IDAT":
            idat_hash.update(payload)
            idat_bytes += length
        elif ctype in PNG_APNG_CHUNKS:
            is_apng = True
            if ctype == "acTL" and length >= 8:
                apng_frames = struct.unpack_from(">I", payload, 0)[0]

        if ctype in {"IHDR", "PLTE", "IDAT", "IEND"}:
            cat = "critical"
        elif ctype in PNG_METADATA_CHUNKS:
            cat = "metadata"
        elif ctype in PNG_COLOR_CHUNKS:
            cat = "color"
        elif ctype == "tRNS":
            cat = "transparency"
        elif ctype in PNG_APNG_CHUNKS:
            cat = "animation"
        else:
            cat = "ancillary"

        chunks.append({
            "index": len(chunks),
            "type": ctype,
            "offset": pos,
            "length": length,
            "crc_ok": crc_ok,
            "sha256": _sha256_bytes(payload),
            "category": cat,
        })
        pos = crc_end
        if ctype == "IEND":
            has_iend = True
            trailing_bytes = size - pos
            if trailing_bytes > 0:
                warnings.append(f"Se detectaron {trailing_bytes} bytes posteriores al chunk IEND.")
            break

    if not ihdr:
        warnings.append("No se encontró un chunk IHDR válido.")
    if not has_iend:
        warnings.append("El archivo PNG carece de chunk de cierre IEND.")
    if crc_errors:
        warnings.append(f"Se detectaron {len(crc_errors)} error(es) de CRC32 en chunks PNG.")
    if is_apng:
        warnings.append(
            f"Se detectó animación APNG ({apng_frames or 'múltiples'} fotograma(s)). "
            "La comparación de fotogramas animados no está soportada; no se declarará equivalencia completa solo por el primer fotograma."
        )

    valid = bool(ihdr) and has_iend and not crc_errors
    return {
        "format": "PNG",
        "valid": valid,
        "error": (
            f"Se detectaron {len(crc_errors)} chunk(s) PNG con CRC32 inválido."
            if crc_errors else (None if (ihdr and has_iend) else "Estructura obligatoria PNG incompleta (IHDR/IEND).")
        ),
        "file_size": size,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "ihdr": ihdr,
        "crc_errors": crc_errors,
        "is_apng": is_apng,
        "apng_frames": apng_frames,
        "idat_bytes": idat_bytes,
        "idat_sha256": idat_hash.hexdigest() if idat_bytes else None,
        "metadata_chunks": [c["type"] for c in chunks if c["category"] == "metadata"],
        "color_chunks": [c["type"] for c in chunks if c["category"] == "color"],
        "has_trns": any(c["type"] == "tRNS" for c in chunks),
        "trailing_bytes": trailing_bytes,
        "warnings": warnings,
    }


def compare_png_structures(struct_a: dict, struct_b: dict) -> dict:
    """Compare two inspected PNG structures."""
    chunks_a = struct_a.get("chunks", [])
    chunks_b = struct_b.get("chunks", [])
    order_a = [c["type"] for c in chunks_a]
    order_b = [c["type"] for c in chunks_b]

    def _cat_sig(chunks: list[dict], cat: str) -> list[tuple]:
        return [(c["type"], c["length"], c["sha256"]) for c in chunks if c["category"] == cat]

    ihdr_changed = struct_a.get("ihdr") != struct_b.get("ihdr")
    meta_changed = _cat_sig(chunks_a, "metadata") != _cat_sig(chunks_b, "metadata")
    color_changed = _cat_sig(chunks_a, "color") != _cat_sig(chunks_b, "color")
    trns_changed = _cat_sig(chunks_a, "transparency") != _cat_sig(chunks_b, "transparency")
    idat_changed = struct_a.get("idat_sha256") != struct_b.get("idat_sha256")
    order_changed = order_a != order_b

    differences: list[dict] = []
    if ihdr_changed:
        differences.append({
            "area": "ihdr",
            "label": "Cabecera IHDR (dimensiones, profundidad o tipo de color)",
            "detail": f"A: {struct_a.get('ihdr')} · B: {struct_b.get('ihdr')}",
        })
    if meta_changed:
        differences.append({
            "area": "metadata_chunks",
            "label": "Chunks de metadatos (tEXt / zTXt / iTXt / eXIf / tIME)",
            "detail": (
                f"A: {', '.join(struct_a.get('metadata_chunks') or ['ninguno'])} · "
                f"B: {', '.join(struct_b.get('metadata_chunks') or ['ninguno'])}"
            ),
        })
    if color_changed:
        differences.append({
            "area": "color_chunks",
            "label": "Chunks de color (iCCP / sRGB / gAMA / cHRM)",
            "detail": (
                f"A: {', '.join(struct_a.get('color_chunks') or ['ninguno'])} · "
                f"B: {', '.join(struct_b.get('color_chunks') or ['ninguno'])}"
            ),
        })
    if trns_changed:
        differences.append({
            "area": "transparency_chunk",
            "label": "Chunk de transparencia (tRNS)",
            "detail": "La información auxiliar de transparencia difiere entre ambos PNG.",
        })
    if idat_changed:
        differences.append({
            "area": "idat_stream",
            "label": "Flujo comprimido IDAT",
            "detail": (
                f"A: {struct_a.get('idat_bytes', 0)} B · B: {struct_b.get('idat_bytes', 0)} B. "
                "Diferentes secuencias comprimidas IDAT pueden representar exactamente los mismos píxeles."
            ),
        })
    if struct_a.get("crc_errors") or struct_b.get("crc_errors"):
        differences.append({
            "area": "crc_integrity",
            "label": "Validación CRC32 de chunks",
            "detail": (
                f"Errores CRC32 — A: {len(struct_a.get('crc_errors', []))}, "
                f"B: {len(struct_b.get('crc_errors', []))}."
            ),
        })
    if order_changed and not differences:
        differences.append({
            "area": "chunk_order",
            "label": "Orden de chunks PNG",
            "detail": "La secuencia de chunks difiere entre ambos archivos.",
        })

    identical = not differences and not order_changed
    status = "matched" if identical else "different"
    if struct_a.get("crc_errors") or struct_b.get("crc_errors"):
        status = "partial"

    return {
        "format": "PNG",
        "status": status,
        "identical_structure": identical,
        "ihdr_changed": ihdr_changed,
        "metadata_chunks_changed": meta_changed,
        "color_chunks_changed": color_changed,
        "transparency_chunk_changed": trns_changed,
        "idat_stream_changed": idat_changed,
        "chunk_order_changed": order_changed,
        "crc_errors_a": struct_a.get("crc_errors", []),
        "crc_errors_b": struct_b.get("crc_errors", []),
        "is_apng": bool(struct_a.get("is_apng") or struct_b.get("is_apng")),
        "differences": differences,
        "summary_a": {
            "chunk_count": struct_a.get("chunk_count", 0),
            "ihdr": struct_a.get("ihdr", {}),
            "metadata_chunks": struct_a.get("metadata_chunks", []),
            "color_chunks": struct_a.get("color_chunks", []),
            "idat_bytes": struct_a.get("idat_bytes", 0),
            "crc_errors": len(struct_a.get("crc_errors", [])),
            "is_apng": struct_a.get("is_apng", False),
        },
        "summary_b": {
            "chunk_count": struct_b.get("chunk_count", 0),
            "ihdr": struct_b.get("ihdr", {}),
            "metadata_chunks": struct_b.get("metadata_chunks", []),
            "color_chunks": struct_b.get("color_chunks", []),
            "idat_bytes": struct_b.get("idat_bytes", 0),
            "crc_errors": len(struct_b.get("crc_errors", [])),
            "is_apng": struct_b.get("is_apng", False),
        },
        "warnings": list(dict.fromkeys([*struct_a.get("warnings", []), *struct_b.get("warnings", [])])),
        "limitations": [
            "Secuencias IDAT distintas (por ejemplo, distinto nivel de compresión zlib) pueden decodificarse a píxeles idénticos.",
            "Si alguno de los archivos es APNG, la comparación de animaciones no está soportada.",
        ],
    }


# ---------------------------------------------------------------------------
# True 16-bit PNG Exact Sample Decoder (pure Python, bounded)
# ---------------------------------------------------------------------------

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_png_16bit_samples(path: Path, png_struct: dict) -> tuple[bytes, int, int, int] | None:
    """Decode non-interlaced 16-bit PNG raw samples without 8-bit truncation.

    Returns (raw_big_endian_16bit_bytes, width, height, channels) or None if unsupported.
    """
    ihdr = png_struct.get("ihdr") or {}
    if ihdr.get("bit_depth") != 16 or ihdr.get("interlaced"):
        return None
    channels = ihdr.get("channels", 0)
    width = ihdr.get("width", 0)
    height = ihdr.get("height", 0)
    if channels not in {1, 2, 3, 4} or width <= 0 or height <= 0:
        return None
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
        raise ImageSafetyError("La imagen PNG de 16 bits excede los límites de dimensiones o píxeles permitidos.")
    bpp = channels * 2  # 2 bytes per channel
    stride = width * bpp
    expected_uncompressed = height * (1 + stride)
    if expected_uncompressed > MAX_DECOMPRESSED_SCANLINES:
        return None

    raw_data = path.read_bytes()
    idat_buf = bytearray()
    for chunk in png_struct.get("chunks", []):
        if chunk["type"] == "IDAT":
            start = chunk["offset"] + 8
            idat_buf.extend(raw_data[start: start + chunk["length"]])

    decompressor = zlib.decompressobj()
    try:
        decompressed = decompressor.decompress(bytes(idat_buf), MAX_DECOMPRESSED_SCANLINES + 1)
    except zlib.error as exc:
        raise ImageSafetyError("El flujo IDAT de la imagen PNG de 16 bits está corrupto.") from exc
    if len(decompressed) > MAX_DECOMPRESSED_SCANLINES or len(decompressed) != expected_uncompressed:
        raise ImageSafetyError("El tamaño descomprimido de las líneas de escaneo PNG de 16 bits es inconsistente.")

    out = bytearray(height * stride)
    prev_row = bytes(stride)
    src_pos = 0
    dst_pos = 0
    for _ in range(height):
        flt = decompressed[src_pos]
        src_pos += 1
        row = bytearray(decompressed[src_pos: src_pos + stride])
        src_pos += stride
        if flt == 0:
            pass
        elif flt == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif flt == 2:
            for i in range(stride):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif flt == 3:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        elif flt == 4:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                up_left = prev_row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(left, up, up_left)) & 0xFF
        else:
            raise ImageSafetyError(f"Filtro PNG desconocido ({flt}) en imagen de 16 bits.")
        out[dst_pos: dst_pos + stride] = row
        dst_pos += stride
        prev_row = bytes(row)
    return bytes(out), width, height, channels


# ---------------------------------------------------------------------------
# Safe Image Loading, Normalization & Perceptual Hashing
# ---------------------------------------------------------------------------

def _safe_open_image(path: Path) -> Image.Image:
    """Open an image defensively enforcing dimension and pixel ceilings."""
    try:
        img = Image.open(path)
    except Image.DecompressionBombError as exc:
        raise ImageSafetyError(
            f"La imagen supera el límite de seguridad de {MAX_IMAGE_PIXELS:,} píxeles."
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageSafetyError(f"No se pudo abrir o decodificar la imagen: {exc}") from exc

    w, h = img.size
    if w <= 0 or h <= 0:
        img.close()
        raise ImageSafetyError("La imagen declara dimensiones inválidas.")
    if w > MAX_IMAGE_DIMENSION or h > MAX_IMAGE_DIMENSION or (w * h) > MAX_IMAGE_PIXELS:
        img.close()
        raise ImageSafetyError(
            f"Dimensiones ({w}×{h}) superan los límites de seguridad "
            f"(máximo {MAX_IMAGE_DIMENSION}px por lado y {MAX_IMAGE_PIXELS:,} píxeles)."
        )
    try:
        img.load()
    except Image.DecompressionBombError as exc:
        img.close()
        raise ImageSafetyError(
            f"La imagen supera el límite de seguridad de {MAX_IMAGE_PIXELS:,} píxeles."
        ) from exc
    except (OSError, ValueError, SyntaxError, zlib.error) as exc:
        img.close()
        raise ImageSafetyError(f"Error al decodificar los píxeles de la imagen: {exc}") from exc
    return img


def _exif_orientation(img: Image.Image) -> int:
    try:
        exif = img.getexif()
        val = exif.get(274, 1) if exif else 1
        return int(val) if isinstance(val, int) and 1 <= val <= 8 else 1
    except Exception:
        return 1


def _icc_info(img: Image.Image) -> dict:
    icc = img.info.get("icc_profile")
    if not icc:
        return {"present": False, "size": 0, "sha256": None, "description": None}
    desc = None
    try:
        prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        desc = ImageCms.getProfileDescription(prof).strip() or None
    except Exception:
        desc = None
    return {
        "present": True,
        "size": len(icc),
        "sha256": _sha256_bytes(icc),
        "description": desc,
    }


def _normalize_to_srgb_rgba(img: Image.Image) -> tuple[Image.Image, list[str], bool]:
    """Apply explicit visual normalization: EXIF orientation -> ICC to sRGB -> RGBA 8-bit.

    Returns (normalized_rgba_image, transformations_applied, lossy_conversion).
    """
    transforms: list[str] = []
    lossy = False
    work = img

    orientation = _exif_orientation(work)
    if orientation != 1:
        work = ImageOps.exif_transpose(work)
        transforms.append(f"Orientación EXIF ({orientation}) aplicada a píxeles.")

    if work.mode in {"I;16", "I;16B", "I;16L", "I", "F"}:
        # Scale 16-bit grayscale cleanly to 8-bit L before RGBA
        lossy = True
        transforms.append(f"Conversión de profundidad {work.mode} a 8 bits por canal (con pérdida de precisión).")
        if work.mode.startswith("I;16"):
            work = work.point(lambda v: v >> 8).convert("L")
        else:
            work = work.convert("L")

    icc_bytes = img.info.get("icc_profile")
    if icc_bytes:
        try:
            src_prof = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
            srgb_prof = ImageCms.createProfile("sRGB")
            if work.mode in {"RGB", "RGBA", "CMYK", "L", "LA"}:
                if work.mode == "CMYK":
                    lossy = True
                    work = ImageCms.profileToProfile(work, src_prof, srgb_prof, outputMode="RGB")
                    transforms.append("Conversión de perfil ICC CMYK a espacio sRGB.")
                elif work.mode in {"RGBA", "LA"}:
                    alpha = work.getchannel("A")
                    rgb = work.convert("RGB")
                    rgb = ImageCms.profileToProfile(rgb, src_prof, srgb_prof, outputMode="RGB")
                    work = rgb.convert("RGBA")
                    work.putalpha(alpha)
                    transforms.append("Conversión de perfil ICC embebido a sRGB conservando canal alfa.")
                else:
                    work = ImageCms.profileToProfile(work, src_prof, srgb_prof, outputMode="RGB")
                    transforms.append("Conversión de perfil ICC embebido a espacio sRGB.")
            else:
                work = work.convert("RGBA")
                transforms.append(f"Conversión de modo {img.mode} a RGBA.")
        except Exception:
            transforms.append("Perfil ICC presente pero no convertible por ImageCms; se usó conversión directa a RGBA.")
            if work.mode == "CMYK":
                lossy = True
            work = work.convert("RGBA")
    else:
        if work.mode == "CMYK":
            lossy = True
            transforms.append("Conversión directa CMYK a RGBA sin perfil ICC (aproximada).")
        elif work.mode != "RGBA":
            transforms.append(f"Conversión de modo {work.mode} a RGBA 8-bit.")
        work = work.convert("RGBA")

    if work.mode != "RGBA":
        work = work.convert("RGBA")
    return work, transforms, lossy


def compute_dhash(rgba_img: Image.Image) -> str:
    """Compute 64-bit difference hash (dHash) over 9x8 grayscale LANCZOS."""
    # Composite transparency onto neutral white for consistent perceptual hashing
    base = Image.new("RGB", rgba_img.size, (255, 255, 255))
    base.paste(rgba_img, mask=rgba_img.getchannel("A"))
    small = base.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = small.tobytes()
    bits = 0
    for y in range(8):
        row = y * 9
        for x in range(8):
            bits = (bits << 1) | (1 if px[row + x] > px[row + x + 1] else 0)
    return f"{bits:016x}"


def compute_phash(rgba_img: Image.Image) -> str:
    """Compute 64-bit perceptual hash (pHash) via 32x32 grayscale separable 2D DCT-II."""
    base = Image.new("RGB", rgba_img.size, (255, 255, 255))
    base.paste(rgba_img, mask=rgba_img.getchannel("A"))
    small = base.convert("L").resize((_DCT_SIZE, _DCT_SIZE), Image.Resampling.LANCZOS)
    px = small.tobytes()
    grid = [px[y * _DCT_SIZE:(y + 1) * _DCT_SIZE] for y in range(_DCT_SIZE)]

    # Row transform (only first 8 horizontal frequencies needed)
    row_dct = [
        [sum(grid[y][x] * _DCT_COS[u][x] for x in range(_DCT_SIZE)) for u in range(8)]
        for y in range(_DCT_SIZE)
    ]
    # Column transform (only first 8 vertical frequencies needed)
    coeffs: list[float] = []
    for v in range(8):
        for u in range(8):
            val = sum(row_dct[y][u] * _DCT_COS[v][y] for y in range(_DCT_SIZE))
            coeffs.append(val)

    # Exclude DC component (0,0) when computing median threshold
    ac = coeffs[1:]
    sorted_ac = sorted(ac)
    mid = len(sorted_ac) // 2
    median = (sorted_ac[mid - 1] + sorted_ac[mid]) / 2.0 if len(sorted_ac) % 2 == 0 else sorted_ac[mid]
    bits = 0
    for val in coeffs:
        bits = (bits << 1) | (1 if val > median else 0)
    return f"{bits:016x}"


def hamming_distance_hex(hex_a: str | None, hex_b: str | None) -> int | None:
    if not hex_a or not hex_b or len(hex_a) != len(hex_b):
        return None
    try:
        return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()
    except ValueError:
        return None


def _compute_ssim_grayscale(img_a: Image.Image, img_b: Image.Image) -> float:
    """Compute mean windowed SSIM (8x8 blocks) over aligned grayscale images."""
    w, h = img_a.size
    a_bytes = img_a.tobytes()
    b_bytes = img_b.tobytes()
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    block = 8
    bw = max(1, w // block)
    bh = max(1, h // block)
    if w < block or h < block:
        # Single global window for tiny images
        n = len(a_bytes)
        if n == 0:
            return 1.0
        ma = sum(a_bytes) / n
        mb = sum(b_bytes) / n
        va = sum((x - ma) ** 2 for x in a_bytes) / n
        vb = sum((y - mb) ** 2 for y in b_bytes) / n
        cov = sum((x - ma) * (y - mb) for x, y in zip(a_bytes, b_bytes)) / n
        num = (2 * ma * mb + c1) * (2 * cov + c2)
        den = (ma * ma + mb * mb + c1) * (va + vb + c2)
        return round(num / den, 6) if den else 1.0

    total = 0.0
    count = 0
    for by in range(bh):
        y0 = by * block
        for bx in range(bw):
            x0 = bx * block
            sa = sb = saa = sbb = sab = 0.0
            for dy in range(block):
                row_off = (y0 + dy) * w + x0
                for dx in range(block):
                    va_i = a_bytes[row_off + dx]
                    vb_i = b_bytes[row_off + dx]
                    sa += va_i
                    sb += vb_i
                    saa += va_i * va_i
                    sbb += vb_i * vb_i
                    sab += va_i * vb_i
            n = 64.0
            ma = sa / n
            mb = sb / n
            va = max(0.0, saa / n - ma * ma)
            vb = max(0.0, sbb / n - mb * mb)
            cov = sab / n - ma * mb
            num = (2 * ma * mb + c1) * (2 * cov + c2)
            den = (ma * ma + mb * mb + c1) * (va + vb + c2)
            total += (num / den) if den else 1.0
            count += 1
    return round(total / count, 6) if count else 1.0


def _rgba_pixel_metrics(img_a: Image.Image, img_b: Image.Image) -> dict:
    """Compute exact per-pixel metrics on two same-size RGBA 8-bit images."""
    w, h = img_a.size
    total_pixels = w * h
    diff = ImageChops.difference(img_a, img_b)
    extrema = diff.getextrema()  # ((min,max) x 4)
    max_per_channel = {
        "R": extrema[0][1],
        "G": extrema[1][1],
        "B": extrema[2][1],
        "A": extrema[3][1],
    }
    max_channel = max(max_per_channel.values())

    if max_channel == 0:
        return {
            "total_pixels": total_pixels,
            "different_pixels": 0,
            "different_ratio": 0.0,
            "different_percent": 0.0,
            "max_channel_diff": max_per_channel,
            "alpha_only_diff": False,
            "mae": 0.0,
            "mse": 0.0,
            "psnr": None,
            "psnr_infinite": True,
            "exact_match": True,
        }

    # Count differing pixels fast via 4-channel max mask
    r_d, g_d, b_d, a_d = diff.split()
    any_diff = ImageChops.lighter(ImageChops.lighter(r_d, g_d), ImageChops.lighter(b_d, a_d))
    hist = any_diff.histogram()
    same_pixels = hist[0]
    diff_pixels = total_pixels - same_pixels
    alpha_only = (max_per_channel["R"] == 0 and max_per_channel["G"] == 0
                  and max_per_channel["B"] == 0 and max_per_channel["A"] > 0)

    # MAE / MSE across all 4 channels using histograms (O(256) instead of O(W*H) in Python!)
    abs_sum = 0
    sq_sum = 0
    for ch in (r_d, g_d, b_d, a_d):
        ch_hist = ch.histogram()
        for val, cnt in enumerate(ch_hist):
            if cnt:
                abs_sum += val * cnt
                sq_sum += val * val * cnt
    n_samples = total_pixels * 4
    mae = abs_sum / n_samples
    mse = sq_sum / n_samples
    psnr = (10.0 * math.log10((255.0 * 255.0) / mse)) if mse > 0 else None

    return {
        "total_pixels": total_pixels,
        "different_pixels": diff_pixels,
        "different_ratio": round(diff_pixels / total_pixels, 8) if total_pixels else 0.0,
        "different_percent": round((diff_pixels / total_pixels) * 100.0, 4) if total_pixels else 0.0,
        "max_channel_diff": max_per_channel,
        "alpha_only_diff": alpha_only,
        "mae": round(mae, 6),
        "mse": round(mse, 6),
        "psnr": round(psnr, 4) if psnr is not None else None,
        "psnr_infinite": mse == 0,
        "exact_match": diff_pixels == 0,
    }


# ---------------------------------------------------------------------------
# Reusable Single-Image Profile Extraction
# ---------------------------------------------------------------------------

def extract_image_profile(path: Path, expected_format: str | None = None) -> dict:
    """Analyze a single JPEG/PNG/WEBP image once and return a reusable JSON-safe profile."""
    detected = _detect_raw_format(path) or expected_format
    structure = None
    warnings: list[str] = []

    if detected == "JPEG":
        structure = inspect_jpeg_structure(path)
        warnings.extend(structure.get("warnings", []))
    elif detected == "PNG":
        structure = inspect_png_structure(path)
        warnings.extend(structure.get("warnings", []))

    try:
        img = _safe_open_image(path)
    except ImageSafetyError as exc:
        return {
            "engine_version": IMAGE_ENGINE_VERSION,
            "format": detected,
            "status": "error",
            "error": str(exc),
            "structure": structure,
            "warnings": list(dict.fromkeys(warnings)),
        }

    try:
        width, height = img.size
        mode = img.mode
        orientation = _exif_orientation(img)
        icc = _icc_info(img)
        has_transparency = (
            "A" in mode
            or "transparency" in img.info
            or (structure and structure.get("has_trns"))
        )
        interlaced = bool(
            (structure and structure.get("ihdr", {}).get("interlaced"))
            or (structure and structure.get("coding", {}).get("progressive"))
            or img.info.get("progressive")
        )
        is_apng = bool(structure and structure.get("is_apng"))

        # Determine true bit depth per channel
        if detected == "PNG" and structure and structure.get("ihdr"):
            bit_depth = structure["ihdr"].get("bit_depth", 8)
        elif detected == "JPEG" and structure and structure.get("coding"):
            bit_depth = structure["coding"].get("precision_bits", 8)
        elif mode.startswith("I;16"):
            bit_depth = 16
        elif mode == "1":
            bit_depth = 1
        else:
            bit_depth = 8

        # Raw decoded sample fingerprint (without color/orientation transforms)
        sample_fingerprint = None
        sample_repr = f"{mode}:{width}x{height}:{bit_depth}b"
        if detected == "PNG" and bit_depth == 16 and structure and not structure.get("ihdr", {}).get("interlaced"):
            decoded_16 = _decode_png_16bit_samples(path, structure)
            if decoded_16 is not None:
                raw_16_bytes, w16, h16, ch16 = decoded_16
                sample_fingerprint = _sha256_bytes(raw_16_bytes)
                sample_repr = f"PNG16:ch{ch16}:{w16}x{h16}:16b"
        elif not is_apng:
            sample_fingerprint = _sha256_bytes(img.tobytes())

        # Normalized visual representation (EXIF orientation + ICC -> sRGB + RGBA 8-bit)
        norm_img, norm_transforms, norm_lossy = _normalize_to_srgb_rgba(img)
        visual_fingerprint = _sha256_bytes(norm_img.tobytes()) if not is_apng else None
        dhash = compute_dhash(norm_img)
        phash = compute_phash(norm_img)
        norm_size = norm_img.size
        norm_img.close()

        return {
            "engine_version": IMAGE_ENGINE_VERSION,
            "format": detected or img.format,
            "status": "ok",
            "error": None,
            "width": width,
            "height": height,
            "normalized_width": norm_size[0],
            "normalized_height": norm_size[1],
            "pixels": width * height,
            "mode": mode,
            "bit_depth": bit_depth,
            "orientation": orientation,
            "icc": icc,
            "has_transparency": bool(has_transparency),
            "interlaced": interlaced,
            "is_apng": is_apng,
            "apng_frames": structure.get("apng_frames") if structure else None,
            "sample_fingerprint": sample_fingerprint,
            "sample_representation": sample_repr,
            "visual_fingerprint": visual_fingerprint,
            "normalization_transforms": norm_transforms,
            "normalization_lossy": norm_lossy,
            "dhash": dhash,
            "phash": phash,
            "structure": structure,
            "warnings": list(dict.fromkeys(warnings)),
        }
    finally:
        img.close()


# ---------------------------------------------------------------------------
# Full Pairwise Image Comparison (Exact Samples + Visual Normalized + Perceptual + Structure)
# ---------------------------------------------------------------------------

def _compare_16bit_png_samples(path_a: Path, struct_a: dict, path_b: Path, struct_b: dict) -> dict:
    """Exact 16-bit sample-by-sample comparison for two non-interlaced 16-bit PNGs."""
    dec_a = _decode_png_16bit_samples(path_a, struct_a)
    dec_b = _decode_png_16bit_samples(path_b, struct_b)
    if dec_a is None or dec_b is None:
        return {
            "status": "unsupported",
            "exact_match": None,
            "reason": "PNG de 16 bits entrelazado o supera el presupuesto de memoria para comparación directa de 16 bits.",
        }
    bytes_a, wa, ha, cha = dec_a
    bytes_b, wb, hb, chb = dec_b
    if (wa, ha) != (wb, hb) or cha != chb:
        return {
            "status": "different",
            "exact_match": False,
            "reason": f"Dimensiones o canales distintos en 16 bits: A ({wa}×{ha}, {cha} ch) vs B ({wb}×{hb}, {chb} ch).",
            "dimensions_a": [wa, ha],
            "dimensions_b": [wb, hb],
        }
    if bytes_a == bytes_b:
        return {
            "status": "matched",
            "exact_match": True,
            "total_pixels": wa * ha,
            "different_pixels": 0,
            "different_ratio": 0.0,
            "different_percent": 0.0,
            "max_channel_diff": {f"C{i}": 0 for i in range(cha)},
            "bit_depth": 16,
            "representation": f"Muestras PNG nativas de 16 bits ({cha} canales, sin truncamiento a 8 bits).",
        }
    # Compute exact 16-bit differences using memoryview/struct
    total_pixels = wa * ha
    vals_a = struct.unpack(f">{total_pixels * cha}H", bytes_a)
    vals_b = struct.unpack(f">{total_pixels * cha}H", bytes_b)
    diff_pixels = 0
    max_ch = [0] * cha
    for p in range(0, total_pixels * cha, cha):
        changed = False
        for c in range(cha):
            d = abs(vals_a[p + c] - vals_b[p + c])
            if d:
                changed = True
                if d > max_ch[c]:
                    max_ch[c] = d
        if changed:
            diff_pixels += 1
    ch_names = {1: ["L"], 2: ["L", "A"], 3: ["R", "G", "B"], 4: ["R", "G", "B", "A"]}[cha]
    return {
        "status": "different",
        "exact_match": False,
        "total_pixels": total_pixels,
        "different_pixels": diff_pixels,
        "different_ratio": round(diff_pixels / total_pixels, 8),
        "different_percent": round((diff_pixels / total_pixels) * 100.0, 4),
        "max_channel_diff": {ch_names[i]: max_ch[i] for i in range(cha)},
        "bit_depth": 16,
        "representation": f"Muestras PNG nativas de 16 bits ({cha} canales, sin truncamiento a 8 bits).",
    }


def compare_images(
    path_a: Path | None,
    path_b: Path | None,
    profile_a: dict | None = None,
    profile_b: dict | None = None,
) -> dict:
    """Run complete multi-level image comparison between two images."""
    if profile_a is None and path_a is not None:
        profile_a = extract_image_profile(path_a)
    if profile_b is None and path_b is not None:
        profile_b = extract_image_profile(path_b)

    if not profile_a or not profile_b:
        return {
            "engine_version": IMAGE_ENGINE_VERSION,
            "status": "pending",
            "reason": "Se requieren los bytes originales o perfiles de imagen para ejecutar la comparación visual.",
        }

    if profile_a.get("status") == "error" or profile_b.get("status") == "error":
        errs = [e for e in (profile_a.get("error"), profile_b.get("error")) if e]
        return {
            "engine_version": IMAGE_ENGINE_VERSION,
            "status": "failed",
            "reason": " ; ".join(errs) or "Error al decodificar una de las imágenes.",
            "profile_a": profile_a,
            "profile_b": profile_b,
        }

    # Structural comparison (when same format JPEG-JPEG or PNG-PNG)
    fmt_a = profile_a.get("format")
    fmt_b = profile_b.get("format")
    structural = None
    if fmt_a == "JPEG" and fmt_b == "JPEG" and profile_a.get("structure") and profile_b.get("structure"):
        structural = compare_jpeg_structures(profile_a["structure"], profile_b["structure"])
    elif fmt_a == "PNG" and fmt_b == "PNG" and profile_a.get("structure") and profile_b.get("structure"):
        structural = compare_png_structures(profile_a["structure"], profile_b["structure"])
    else:
        structural = {
            "status": "not_applicable",
            "reason": f"Inspección estructural comparativa no aplicable entre formatos distintos ({fmt_a} vs {fmt_b}); se conserva la inspección individual de cada imagen.",
            "individual_a": profile_a.get("structure"),
            "individual_b": profile_b.get("structure"),
        }

    # APNG explicit guard
    if profile_a.get("is_apng") or profile_b.get("is_apng"):
        return {
            "engine_version": IMAGE_ENGINE_VERSION,
            "status": "unsupported",
            "reason": "Se detectó una imagen animada APNG. No se declara equivalencia de píxeles comparando únicamente el primer fotograma.",
            "profile_a": profile_a,
            "profile_b": profile_b,
            "structural_comparison": structural,
            "decoded_samples": {
                "status": "unsupported",
                "reason": "APNG animado no soportado para equivalencia completa de muestras.",
            },
            "visual_normalized": {
                "status": "unsupported",
                "reason": "APNG animado no soportado para equivalencia visual completa.",
            },
            "perceptual": {
                "status": "partial",
                "dhash_distance": hamming_distance_hex(profile_a.get("dhash"), profile_b.get("dhash")),
                "phash_distance": hamming_distance_hex(profile_a.get("phash"), profile_b.get("phash")),
                "note": "Solo calculado sobre el primer fotograma; no representa la animación completa.",
            },
            "diff_map_available": False,
        }

    # If original file paths are not available (e.g., stored investigation profiles only),
    # return profile-based comparison without simulating pixel-level diff metrics.
    if path_a is None or path_b is None or not path_a.is_file() or not path_b.is_file():
        d_dist = hamming_distance_hex(profile_a.get("dhash"), profile_b.get("dhash"))
        p_dist = hamming_distance_hex(profile_a.get("phash"), profile_b.get("phash"))
        same_samples = (
            bool(profile_a.get("sample_fingerprint"))
            and profile_a.get("sample_fingerprint") == profile_b.get("sample_fingerprint")
            and profile_a.get("sample_representation") == profile_b.get("sample_representation")
        )
        same_visual = (
            bool(profile_a.get("visual_fingerprint"))
            and profile_a.get("visual_fingerprint") == profile_b.get("visual_fingerprint")
            and profile_a.get("normalized_width") == profile_b.get("normalized_width")
            and profile_a.get("normalized_height") == profile_b.get("normalized_height")
        )
        return {
            "engine_version": IMAGE_ENGINE_VERSION,
            "status": "partial",
            "reason": "Comparación basada en perfiles persistidos (los bytes originales no están disponibles para generar mapa de diferencias o SSIM/PSNR completo).",
            "profile_a": profile_a,
            "profile_b": profile_b,
            "structural_comparison": structural,
            "decoded_samples": {
                "status": "matched" if same_samples else "different",
                "exact_match": same_samples,
                "reason": "Evaluado mediante huella SHA-256 de muestras decodificadas persistida.",
            },
            "visual_normalized": {
                "status": "matched" if same_visual else "different",
                "exact_match": same_visual,
                "reason": "Evaluado mediante huella SHA-256 visual normalizada persistida.",
            },
            "perceptual": {
                "status": "partial",
                "dhash_a": profile_a.get("dhash"),
                "dhash_b": profile_b.get("dhash"),
                "dhash_distance": d_dist,
                "phash_a": profile_a.get("phash"),
                "phash_b": profile_b.get("phash"),
                "phash_distance": p_dist,
                "ssim": None,
                "mae": None,
                "mse": None,
                "psnr": None,
                "note": "Los hashes perceptuales permiten identificar candidatos, no probar identidad. Aporta los archivos originales para métricas completas.",
            },
            "diff_map_available": False,
        }

    # Open both images safely for exact & normalized comparison
    img_a = _safe_open_image(path_a)
    img_b = _safe_open_image(path_b)
    try:
        # ---- Level 1: Exact Decoded Samples (NO hidden transforms) ----
        is_16_a = profile_a.get("bit_depth") == 16
        is_16_b = profile_b.get("bit_depth") == 16
        if fmt_a == "PNG" and fmt_b == "PNG" and (is_16_a or is_16_b):
            if is_16_a and is_16_b:
                decoded_samples = _compare_16bit_png_samples(
                    path_a, profile_a["structure"], path_b, profile_b["structure"]
                )
            else:
                decoded_samples = {
                    "status": "different",
                    "exact_match": False,
                    "reason": (
                        f"Profundidad de bits distinta: A ({profile_a.get('bit_depth')} bits) vs "
                        f"B ({profile_b.get('bit_depth')} bits). No se trunca a 8 bits para declarar equivalencia exacta de muestras."
                    ),
                }
        elif img_a.size != img_b.size:
            decoded_samples = {
                "status": "different",
                "exact_match": False,
                "dimensions_a": list(img_a.size),
                "dimensions_b": list(img_b.size),
                "reason": (
                    f"Dimensiones decodificadas distintas: A ({img_a.size[0]}×{img_a.size[1]}) vs "
                    f"B ({img_b.size[0]}×{img_b.size[1]}). No se redimensionan imágenes para comparación exacta."
                ),
            }
        elif img_a.mode != img_b.mode:
            decoded_samples = {
                "status": "not_applicable",
                "exact_match": None,
                "dimensions_a": list(img_a.size),
                "dimensions_b": list(img_b.size),
                "mode_a": img_a.mode,
                "mode_b": img_b.mode,
                "reason": (
                    f"Modos de color decodificados distintos ({img_a.mode} vs {img_b.mode}). "
                    "Consulta la equivalencia visual normalizada bajo política explícita."
                ),
            }
        else:
            raw_a = img_a.tobytes()
            raw_b = img_b.tobytes()
            if raw_a == raw_b:
                decoded_samples = {
                    "status": "matched",
                    "exact_match": True,
                    "dimensions_a": list(img_a.size),
                    "dimensions_b": list(img_b.size),
                    "mode": img_a.mode,
                    "bit_depth": profile_a.get("bit_depth", 8),
                    "total_pixels": img_a.size[0] * img_a.size[1],
                    "different_pixels": 0,
                    "different_ratio": 0.0,
                    "different_percent": 0.0,
                    "reason": (
                        f"Muestras decodificadas idénticas en modo {img_a.mode} ({img_a.size[0]}×{img_a.size[1]}) "
                        "sin aplicar orientación EXIF ni conversión ICC."
                    ),
                }
            else:
                # Compute exact pixel diff on raw mode when 8-bit compatible
                if img_a.mode in {"RGB", "RGBA", "L", "LA", "CMYK"}:
                    diff_raw = ImageChops.difference(img_a, img_b)
                    ext = diff_raw.getextrema()
                    if isinstance(ext[0], tuple):
                        max_ch = {band: ext[i][1] for i, band in enumerate(img_a.getbands())}
                        ch_imgs = diff_raw.split()
                        mask = ch_imgs[0]
                        for c in ch_imgs[1:]:
                            mask = ImageChops.lighter(mask, c)
                    else:
                        max_ch = {img_a.getbands()[0]: ext[1]}
                        mask = diff_raw
                    total_px = img_a.size[0] * img_a.size[1]
                    diff_px = total_px - mask.histogram()[0]
                    decoded_samples = {
                        "status": "different",
                        "exact_match": False,
                        "dimensions_a": list(img_a.size),
                        "dimensions_b": list(img_b.size),
                        "mode": img_a.mode,
                        "bit_depth": profile_a.get("bit_depth", 8),
                        "total_pixels": total_px,
                        "different_pixels": diff_px,
                        "different_ratio": round(diff_px / total_px, 8),
                        "different_percent": round((diff_px / total_px) * 100.0, 4),
                        "max_channel_diff": max_ch,
                        "reason": (
                            f"{diff_px:,} de {total_px:,} píxeles difieren en muestras decodificadas ({img_a.mode})."
                        ),
                    }
                else:
                    decoded_samples = {
                        "status": "different",
                        "exact_match": False,
                        "dimensions_a": list(img_a.size),
                        "dimensions_b": list(img_b.size),
                        "mode": img_a.mode,
                        "reason": f"Las muestras decodificadas en modo {img_a.mode} difieren.",
                    }

        # ---- Level 2: Normalized Visual Equivalence ----
        norm_a, trans_a, lossy_a = _normalize_to_srgb_rgba(img_a)
        norm_b, trans_b, lossy_b = _normalize_to_srgb_rgba(img_b)
        try:
            policy = {
                "orientation": "Se aplica orientación EXIF (tag 274) a los píxeles antes de comparar.",
                "color_management": "Si existe perfil ICC embebido, se convierte a espacio sRGB mediante ImageCms.",
                "transparency": "Se preserva el canal alfa completo en formato RGBA de 8 bits por canal.",
                "resizing": "Nunca se redimensiona para declarar equivalencia visual exacta.",
                "transforms_a": trans_a,
                "transforms_b": trans_b,
                "lossy_conversion": bool(lossy_a or lossy_b or is_16_a or is_16_b),
                "caveat": (
                    "La equivalencia visual normalizada describe coincidencia bajo esta política explícita (sRGB/RGBA 8-bit); "
                    "no garantiza renderizado idéntico en todos los dispositivos o aplicaciones."
                ),
            }
            if is_16_a or is_16_b:
                policy["lossy_note"] = (
                    "Al menos una imagen es de 16 bits por canal; la representación normalizada RGBA de 8 bits implica cuantización "
                    "y no sustituye la comparación exacta de muestras de 16 bits."
                )

            diff_map_available = False
            if norm_a.size == norm_b.size:
                metrics = _rgba_pixel_metrics(norm_a, norm_b)
                diff_map_available = (norm_a.size[0] * norm_a.size[1]) <= MAX_DIFF_MAP_PIXELS
                visual_normalized = {
                    "status": "matched" if metrics["exact_match"] else "different",
                    "exact_match": metrics["exact_match"],
                    "dimensions": list(norm_a.size),
                    **metrics,
                    "policy": policy,
                    "reason": (
                        "Las imágenes coinciden exactamente bajo la política visual normalizada (sRGB/RGBA 8-bit)."
                        if metrics["exact_match"] else (
                            "Solo difiere el canal alfa (transparencia); los canales RGB coinciden."
                            if metrics["alpha_only_diff"] else
                            f"Difieren {metrics['different_pixels']:,} píxeles ({metrics['different_percent']}%) bajo la política visual normalizada."
                        )
                    ),
                }
            else:
                metrics = None
                visual_normalized = {
                    "status": "different",
                    "exact_match": False,
                    "dimensions_a": list(norm_a.size),
                    "dimensions_b": list(norm_b.size),
                    "policy": policy,
                    "reason": (
                        f"Las dimensiones normalizadas difieren: A ({norm_a.size[0]}×{norm_a.size[1]}) vs "
                        f"B ({norm_b.size[0]}×{norm_b.size[1]}). No se redimensiona para equivalencia exacta."
                    ),
                }

            # ---- Level 3: Perceptual Similarity ----
            dhash_a = profile_a.get("dhash") or compute_dhash(norm_a)
            dhash_b = profile_b.get("dhash") or compute_dhash(norm_b)
            phash_a = profile_a.get("phash") or compute_phash(norm_a)
            phash_b = profile_b.get("phash") or compute_phash(norm_b)
            d_dist = hamming_distance_hex(dhash_a, dhash_b)
            p_dist = hamming_distance_hex(phash_a, phash_b)

            # Luminance SSIM & error metrics (if sizes differ, resize explicitly to 256x256 LANCZOS for perceptual-only comparison)
            resized_for_perceptual = norm_a.size != norm_b.size
            comp_a = Image.new("RGB", norm_a.size, (255, 255, 255))
            comp_a.paste(norm_a, mask=norm_a.getchannel("A"))
            comp_b = Image.new("RGB", norm_b.size, (255, 255, 255))
            comp_b.paste(norm_b, mask=norm_b.getchannel("A"))
            gray_a = comp_a.convert("L")
            gray_b = comp_b.convert("L")
            if resized_for_perceptual or (gray_a.size[0] * gray_a.size[1] > 1_000_000):
                target_sz = (
                    (PERCEPTUAL_RESIZE_DIM, PERCEPTUAL_RESIZE_DIM)
                    if resized_for_perceptual
                    else (min(gray_a.size[0], 512), min(gray_a.size[1], 512))
                )
                gray_a_s = gray_a.resize(target_sz, Image.Resampling.LANCZOS)
                gray_b_s = gray_b.resize(target_sz, Image.Resampling.LANCZOS)
                ssim_scale_note = f"Evaluado en luminancia reescalada a {target_sz[0]}×{target_sz[1]} (LANCZOS)."
            else:
                gray_a_s = gray_a
                gray_b_s = gray_b
                ssim_scale_note = f"Evaluado en luminancia a resolución original ({gray_a.size[0]}×{gray_a.size[1]})."

            ssim_val = _compute_ssim_grayscale(gray_a_s, gray_b_s)

            # Perceptual interpretation band (documented thresholds, never a claim of shared origin)
            if ssim_val >= 0.995 and (p_dist is not None and p_dist <= 2):
                perceptual_band = "Muy alta similitud perceptual"
            elif ssim_val >= 0.92 and (p_dist is not None and p_dist <= 8):
                perceptual_band = "Alta similitud perceptual"
            elif ssim_val >= 0.75 or (p_dist is not None and p_dist <= 14):
                perceptual_band = "Similitud perceptual moderada"
            else:
                perceptual_band = "Baja similitud perceptual"

            perceptual = {
                "status": "matched" if (ssim_val >= 0.9999 and p_dist == 0 and d_dist == 0 and not resized_for_perceptual) else "different",
                "band": perceptual_band,
                "ssim": ssim_val,
                "dhash_a": dhash_a,
                "dhash_b": dhash_b,
                "dhash_distance": d_dist,
                "dhash_bits": 64,
                "phash_a": phash_a,
                "phash_b": phash_b,
                "phash_distance": p_dist,
                "phash_bits": 64,
                "mae": metrics["mae"] if metrics else None,
                "mse": metrics["mse"] if metrics else None,
                "psnr": metrics["psnr"] if metrics else None,
                "psnr_infinite": metrics["psnr_infinite"] if metrics else False,
                "resized_for_perceptual": resized_for_perceptual,
                "methodology": {
                    "color_space": "Luminancia sRGB (canal L tras composición de transparencia sobre fondo blanco para SSIM/hashes).",
                    "orientation": "Orientación EXIF aplicada.",
                    "interpolation": "LANCZOS cuando se reescala.",
                    "ssim_scope": ssim_scale_note,
                    "thresholds": "pHash/dHash ≤ 8 bits (de 64) y SSIM ≥ 0.92 sugieren alta similitud perceptual como candidatos.",
                    "limitations": [
                        "Los hashes perceptuales (pHash y dHash) sirven para identificar candidatos, no como prueba de identidad.",
                        "La distancia de Hamming nunca se convierte arbitrariamente en un porcentaje visual.",
                        "La similitud perceptual no demuestra origen común ni equivalencia forense.",
                    ],
                },
            }

            # Detect ICC / orientation / format notes
            notes: list[str] = []
            if profile_a.get("icc", {}).get("sha256") != profile_b.get("icc", {}).get("sha256"):
                notes.append(
                    "Los perfiles ICC difieren entre A y B; las muestras decodificadas y la representación visual normalizada en sRGB pueden arrojar resultados distintos."
                )
            if profile_a.get("orientation") != profile_b.get("orientation"):
                notes.append(
                    f"La orientación EXIF difiere (A: {profile_a.get('orientation')}, B: {profile_b.get('orientation')}). "
                    "La comparación de muestras decodificadas no rota los píxeles; la visual normalizada sí aplica la orientación."
                )
            if fmt_a != fmt_b:
                notes.append(f"Comparación entre formatos distintos ({fmt_a} vs {fmt_b}).")

            return {
                "engine_version": IMAGE_ENGINE_VERSION,
                "status": "analyzed",
                "profile_a": profile_a,
                "profile_b": profile_b,
                "decoded_samples": decoded_samples,
                "visual_normalized": visual_normalized,
                "perceptual": perceptual,
                "structural_comparison": structural,
                "diff_map_available": diff_map_available,
                "notes": notes,
            }
        finally:
            norm_a.close()
            norm_b.close()
    finally:
        img_a.close()
        img_b.close()


# ---------------------------------------------------------------------------
# Difference Map PNG Generator
# ---------------------------------------------------------------------------

def generate_diff_map_png(
    path_a: Path,
    path_b: Path,
    mode: str = "normalized",
    amplify: bool = True,
) -> tuple[bytes, dict]:
    """Generate a PNG difference map highlighting modified pixels.

    Returns (png_bytes, metadata_dict).
    """
    img_a = _safe_open_image(path_a)
    img_b = _safe_open_image(path_b)
    try:
        if mode == "raw":
            if img_a.size != img_b.size or img_a.mode != img_b.mode:
                raise ImageSafetyError("Las dimensiones o modos decodificados no coinciden para mapa de diferencias en crudo.")
            cmp_a = img_a.convert("RGBA")
            cmp_b = img_b.convert("RGBA")
            policy_label = "Muestras decodificadas (sin transformación EXIF/ICC)"
        else:
            cmp_a, _, _ = _normalize_to_srgb_rgba(img_a)
            cmp_b, _, _ = _normalize_to_srgb_rgba(img_b)
            policy_label = "Visual normalizada (EXIF + ICC→sRGB + RGBA 8-bit)"
            if cmp_a.size != cmp_b.size:
                cmp_a.close()
                cmp_b.close()
                raise ImageSafetyError("Las dimensiones normalizadas difieren; no se redimensiona para mapa de diferencias exactas.")

        try:
            w, h = cmp_a.size
            if w * h > MAX_DIFF_MAP_PIXELS:
                raise ImageSafetyError("La imagen supera el límite de píxeles para generar un mapa de diferencias en línea.")
            diff = ImageChops.difference(cmp_a, cmp_b)
            r_d, g_d, b_d, a_d = diff.split()
            mag = ImageChops.lighter(ImageChops.lighter(r_d, g_d), ImageChops.lighter(b_d, a_d))
            total_pixels = w * h
            diff_pixels = total_pixels - mag.histogram()[0]

            # Build high-contrast visual diff map: dimmed grayscale reference + magenta/red highlight on modified pixels
            base_gray = cmp_a.convert("L").point(lambda v: int(v * 0.28))
            out_r = base_gray.copy()
            out_g = base_gray.copy()
            out_b = base_gray.copy()

            if diff_pixels > 0:
                if amplify:
                    # Amplify subtle 1-level differences so even a single 1-bit pixel change is clearly visible
                    boost = mag.point(lambda v: 0 if v == 0 else min(255, 140 + v * 4))
                else:
                    boost = mag
                # Highlight modified pixels in vivid magenta/red
                out_r = ImageChops.lighter(out_r, boost)
                out_g = ImageChops.darker(out_g, mag.point(lambda v: 255 if v == 0 else 40))
                out_b = ImageChops.lighter(out_b, boost.point(lambda v: int(v * 0.65)))

            vis = Image.merge("RGB", (out_r, out_g, out_b))
            buf = io.BytesIO()
            vis.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), {
                "width": w,
                "height": h,
                "total_pixels": total_pixels,
                "different_pixels": diff_pixels,
                "mode": mode,
                "amplified": amplify,
                "policy": policy_label,
                "note": (
                    "Las diferencias pequeñas han sido amplificadas visualmente para facilitar su localización."
                    if amplify else
                    "Mapa sin amplificación visual."
                ),
            }
        finally:
            cmp_a.close()
            cmp_b.close()
    finally:
        img_a.close()
        img_b.close()
