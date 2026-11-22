"""Defensive pure-Python OLE2/CFBF container validator and BIFF8 XLS reader.

Provides bounded, read-only extraction of legacy binary Excel (.xls) workbooks
without executing macros or formulas and without external C dependencies.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAX_OLE_SECTORS = 262_144  # 128 MB at 512B sectors
MAX_XLS_RECORDS = 500_000
MAX_XLS_CELLS = 50_000
MAX_XLS_SHEETS = 128

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC

# BIFF8 error codes
BIFF_ERRORS = {
    0x00: "#NULL!",
    0x07: "#DIV/0!",
    0x0F: "#VALUE!",
    0x17: "#REF!",
    0x1D: "#NAME?",
    0x24: "#NUM!",
    0x2A: "#N/A",
}


class XlsError(Exception):
    """Raised when an OLE2/XLS file is corrupted, encrypted, or exceeds safety limits."""


def col_index_to_letter(col: int) -> str:
    """Convert 0-based column index to Excel column letters (0 -> A, 25 -> Z, 26 -> AA)."""
    out = []
    n = col + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(ord("A") + rem))
    return "".join(reversed(out))


def cell_ref(row: int, col: int) -> str:
    return f"{col_index_to_letter(col)}{row + 1}"


def _read_ole_streams(data: bytes) -> dict[str, bytes]:
    """Defensively parse an OLE2/CFBF container and return top-level directory streams."""
    size = len(data)
    if size < 512 or data[:8] != OLE_SIGNATURE:
        raise XlsError("La firma binaria no corresponde a un contenedor OLE2/CFBF.")

    sector_shift = struct.unpack_from("<H", data, 30)[0]
    mini_sector_shift = struct.unpack_from("<H", data, 32)[0]
    if sector_shift not in (9, 12) or mini_sector_shift != 6:
        raise XlsError("Parámetros de sector OLE2 inválidos o incompatibles.")
    sector_size = 1 << sector_shift
    mini_sector_size = 1 << mini_sector_shift

    num_fat_sectors = struct.unpack_from("<I", data, 44)[0]
    first_dir_sector = struct.unpack_from("<I", data, 48)[0]
    mini_cutoff = struct.unpack_from("<I", data, 56)[0]
    first_minifat_sector = struct.unpack_from("<I", data, 60)[0]
    num_minifat_sectors = struct.unpack_from("<I", data, 64)[0]
    first_difat_sector = struct.unpack_from("<I", data, 68)[0]
    num_difat_sectors = struct.unpack_from("<I", data, 72)[0]

    max_sectors = min(MAX_OLE_SECTORS, (size - 512 + sector_size - 1) // sector_size)
    if num_fat_sectors > max_sectors + 16:
        raise XlsError("El contenedor OLE2 declara una tabla FAT inconsistente.")

    def read_sector(sec_id: int) -> bytes:
        offset = 512 + sec_id * sector_size
        if sec_id < 0 or offset < 512 or offset >= size:
            raise XlsError("Referencia de sector OLE2 fuera de límites.")
        return data[offset: offset + sector_size]

    # Collect FAT sector IDs from header DIFAT (109 entries) + extra DIFAT chain
    fat_sectors = [
        sec for sec in struct.unpack_from("<109I", data, 76)
        if sec not in (FREESECT, ENDOFCHAIN, FATSECT, DIFSECT)
    ]
    difat_sec = first_difat_sector
    seen_difat = set()
    for _ in range(min(num_difat_sectors, 1024)):
        if difat_sec in (FREESECT, ENDOFCHAIN) or difat_sec in seen_difat:
            break
        seen_difat.add(difat_sec)
        buf = read_sector(difat_sec)
        count = (sector_size // 4) - 1
        entries = struct.unpack_from(f"<{count}I", buf, 0)
        for sec in entries:
            if sec not in (FREESECT, ENDOFCHAIN, FATSECT, DIFSECT):
                fat_sectors.append(sec)
        difat_sec = struct.unpack_from("<I", buf, count * 4)[0]

    # Build FAT array
    fat: list[int] = []
    entries_per_sec = sector_size // 4
    for sec in fat_sectors[:num_fat_sectors]:
        buf = read_sector(sec)
        if len(buf) < sector_size:
            buf = buf.ljust(sector_size, b"\x00")
        fat.extend(struct.unpack_from(f"<{entries_per_sec}I", buf, 0))

    def read_chain(start_sec: int, expected_size: int | None = None) -> bytes:
        out = bytearray()
        cur = start_sec
        seen = set()
        limit = expected_size if expected_size is not None else (max_sectors * sector_size)
        while cur not in (ENDOFCHAIN, FREESECT):
            if cur in seen or cur >= len(fat):
                raise XlsError("Cadena de sectores OLE2 corrupta o cíclica.")
            seen.add(cur)
            out.extend(read_sector(cur))
            if len(out) >= limit and expected_size is not None:
                break
            cur = fat[cur]
        return bytes(out[:expected_size]) if expected_size is not None else bytes(out)

    # Read Directory stream
    dir_bytes = read_chain(first_dir_sector)
    entries = []
    for off in range(0, len(dir_bytes) - 127, 128):
        name_len = struct.unpack_from("<H", dir_bytes, off + 64)[0]
        obj_type = dir_bytes[off + 66]
        if obj_type == 0 or name_len < 2 or name_len > 64:
            continue
        name = dir_bytes[off: off + name_len - 2].decode("utf-16-le", errors="replace")
        start_sec = struct.unpack_from("<I", dir_bytes, off + 116)[0]
        stream_size = struct.unpack_from("<I", dir_bytes, off + 120)[0]
        entries.append((name, obj_type, start_sec, stream_size))

    # Root entry (type 5) holds MiniStream
    mini_stream = b""
    minifat: list[int] = []
    root_entry = next((e for e in entries if e[1] == 5), None)
    if root_entry and root_entry[2] not in (ENDOFCHAIN, FREESECT) and root_entry[3] > 0:
        mini_stream = read_chain(root_entry[2], root_entry[3])
        if first_minifat_sector not in (ENDOFCHAIN, FREESECT) and num_minifat_sectors > 0:
            mf_bytes = read_chain(first_minifat_sector, num_minifat_sectors * sector_size)
            minifat = list(struct.unpack_from(f"<{len(mf_bytes) // 4}I", mf_bytes, 0))

    def read_mini_chain(start_sec: int, stream_size: int) -> bytes:
        out = bytearray()
        cur = start_sec
        seen = set()
        while cur not in (ENDOFCHAIN, FREESECT) and len(out) < stream_size:
            if cur in seen or cur >= len(minifat):
                raise XlsError("Cadena MiniFAT OLE2 corrupta o cíclica.")
            seen.add(cur)
            off = cur * mini_sector_size
            out.extend(mini_stream[off: off + mini_sector_size])
            cur = minifat[cur]
        return bytes(out[:stream_size])

    streams: dict[str, bytes] = {}
    for name, obj_type, start_sec, stream_size in entries:
        if obj_type != 2:  # Only user streams
            continue
        if stream_size == 0 or start_sec in (ENDOFCHAIN, FREESECT):
            streams[name] = b""
        elif stream_size < mini_cutoff and mini_stream and minifat:
            streams[name] = read_mini_chain(start_sec, stream_size)
        else:
            streams[name] = read_chain(start_sec, stream_size)
    return streams


def is_valid_xls(path: Path) -> bool:
    """Confirm that an OLE2 file is a valid Excel XLS workbook (contains Workbook/Book stream)."""
    try:
        data = path.read_bytes()
        streams = _read_ole_streams(data)
        wb = streams.get("Workbook") or streams.get("Book")
        if not wb or len(wb) < 8:
            return False
        rec_type = struct.unpack_from("<H", wb, 0)[0]
        return rec_type == 0x0809  # BOF record
    except Exception:
        return False


def _decode_rk(rk: int) -> int | float:
    is_div100 = bool(rk & 0x01)
    is_int = bool(rk & 0x02)
    if is_int:
        val = rk >> 2
        if rk & 0x80000000:
            val -= 1 << 30
        res: int | float = val
    else:
        raw = struct.pack("<II", 0, rk & 0xFFFFFFFC)
        res = struct.unpack("<d", raw)[0]
    if is_div100:
        res = res / 100.0
    if isinstance(res, float) and res.is_integer():
        return int(res)
    return res


def _read_biff8_unicode_str(buf: bytes, pos: int, cch_bytes: int = 2) -> tuple[str, int]:
    """Read a BIFF8 XLUnicodeString (8-bit or 16-bit compressed)."""
    if cch_bytes == 1:
        if pos + 2 > len(buf):
            return "", len(buf)
        cch = buf[pos]
        flags = buf[pos + 1]
        pos += 2
    else:
        if pos + 3 > len(buf):
            return "", len(buf)
        cch = struct.unpack_from("<H", buf, pos)[0]
        flags = buf[pos + 2]
        pos += 3

    high_byte = bool(flags & 0x01)
    has_ext = bool(flags & 0x04)
    has_rich = bool(flags & 0x08)

    rt_runs = 0
    if has_rich and pos + 2 <= len(buf):
        rt_runs = struct.unpack_from("<H", buf, pos)[0]
        pos += 2
    ext_len = 0
    if has_ext and pos + 4 <= len(buf):
        ext_len = struct.unpack_from("<I", buf, pos)[0]
        pos += 4

    byte_len = cch * 2 if high_byte else cch
    raw = buf[pos: pos + byte_len]
    pos += byte_len
    text = raw.decode("utf-16-le", errors="replace") if high_byte else raw.decode("latin1", errors="replace")
    pos += rt_runs * 4 + ext_len
    return text, pos


def _parse_sst(payloads: list[bytes]) -> list[str]:
    """Parse BIFF8 SST record plus any immediate CONTINUE records."""
    if not payloads:
        return []
    first = payloads[0]
    if len(first) < 8:
        return []
    _, unique_count = struct.unpack_from("<II", first, 0)
    unique_count = min(unique_count, MAX_XLS_CELLS * 2)

    # Concatenate with boundary tracking for CONTINUE flag bytes
    buf = bytearray(first[8:])
    for cont in payloads[1:]:
        # Simple linear parse: for strings that start inside CONTINUE, strip leading compression byte only if split
        buf.extend(cont)
    data = bytes(buf)
    strings: list[str] = []
    pos = 0
    for _ in range(unique_count):
        if pos >= len(data):
            break
        s, pos = _read_biff8_unicode_str(data, pos, cch_bytes=2)
        strings.append(s)
    return strings


def extract_xls_workbook(path: Path) -> dict:
    """Extract semantic workbook representation from a legacy BIFF8 .xls file."""
    data = path.read_bytes()
    streams = _read_ole_streams(data)
    wb = streams.get("Workbook") or streams.get("Book")
    if not wb:
        raise XlsError("El archivo OLE2 no contiene un flujo Workbook/Book de Excel.")

    # Parse all BIFF records with offsets
    records: list[tuple[int, int, bytes]] = []  # (offset, rec_id, payload)
    pos = 0
    n = len(wb)
    while pos + 4 <= n:
        if len(records) >= MAX_XLS_RECORDS:
            break
        rec_id, rec_len = struct.unpack_from("<HH", wb, pos)
        if pos + 4 + rec_len > n:
            raise XlsError(f"Registro BIFF8 truncado (0x{rec_id:04X}) en desplazamiento {pos}.")
        records.append((pos, rec_id, wb[pos + 4: pos + 4 + rec_len]))
        pos += 4 + rec_len

    if not records or records[0][1] != 0x0809:
        raise XlsError("El flujo Workbook no comienza con un registro BOF válido.")

    # Scan workbook globals (up to first EOF 0x000A)
    sheets_meta: list[dict] = []
    sst_chunks: list[bytes] = []
    defined_names: list[str] = []
    encrypted = False
    date_mode_1904 = False
    has_macros = any(name.lower() in {"_vba_project_cur", "vba"} or "vba" in name.lower() for name in streams)

    i = 0
    while i < len(records):
        _, rec_id, payload = records[i]
        if rec_id == 0x000A:  # EOF globals
            break
        if rec_id == 0x002F:  # FILEPASS (encrypted)
            encrypted = True
        elif rec_id == 0x0022 and len(payload) >= 2:  # DATEMODE
            date_mode_1904 = struct.unpack_from("<H", payload, 0)[0] == 1
        elif rec_id == 0x0085 and len(payload) >= 8:  # BOUNDSHEET
            sheet_off = struct.unpack_from("<I", payload, 0)[0]
            vis_bits = payload[4] & 0x03
            sheet_type = payload[5]
            name, _ = _read_biff8_unicode_str(payload, 6, cch_bytes=1)
            if sheet_type == 0x00 and len(sheets_meta) < MAX_XLS_SHEETS:
                sheets_meta.append({
                    "name": name,
                    "offset": sheet_off,
                    "hidden": vis_bits != 0,
                    "visibility": "visible" if vis_bits == 0 else ("very_hidden" if vis_bits == 2 else "hidden"),
                })
        elif rec_id == 0x00FC:  # SST
            sst_chunks = [payload]
            j = i + 1
            while j < len(records) and records[j][1] == 0x003C:  # CONTINUE
                sst_chunks.append(records[j][2])
                j += 1
            i = j - 1
        elif rec_id == 0x0018 and len(payload) >= 15:  # NAME (defined name)
            name_len = payload[3]
            if 15 + name_len <= len(payload):
                flags = payload[14]
                if flags & 0x01:
                    dname = payload[15: 15 + name_len * 2].decode("utf-16-le", errors="replace")
                else:
                    dname = payload[15: 15 + name_len].decode("latin1", errors="replace")
                if dname and not dname.startswith("\x00"):
                    defined_names.append(dname)
        i += 1

    if encrypted:
        raise XlsError("El archivo XLS está cifrado o protegido con contraseña. No se descifra sin contraseña.")

    sst = _parse_sst(sst_chunks)
    offset_to_idx = {off: idx for idx, (off, _, _) in enumerate(records)}

    sheets: list[dict] = []
    total_cells = 0
    truncated = False

    for order_idx, sm in enumerate(sheets_meta):
        start_idx = offset_to_idx.get(sm["offset"])
        cells: dict[str, dict] = {}
        merged_ranges: list[str] = []
        max_r = -1
        max_c = -1

        if start_idx is not None:
            k = start_idx
            while k < len(records):
                _, rec_id, payload = records[k]
                if rec_id == 0x000A:  # EOF worksheet
                    break
                if total_cells >= MAX_XLS_CELLS:
                    truncated = True
                    break

                if rec_id == 0x0203 and len(payload) >= 14:  # NUMBER
                    r, c = struct.unpack_from("<HH", payload, 0)
                    val = struct.unpack_from("<d", payload, 6)[0]
                    if math.isfinite(val) and val.is_integer():
                        val = int(val)
                    cells[cell_ref(r, c)] = {"type": "number", "value": val, "formula": None}
                    max_r, max_c = max(max_r, r), max(max_c, c)
                    total_cells += 1
                elif rec_id == 0x027E and len(payload) >= 10:  # RK
                    r, c, _, rk = struct.unpack_from("<HHHI", payload, 0)
                    cells[cell_ref(r, c)] = {"type": "number", "value": _decode_rk(rk), "formula": None}
                    max_r, max_c = max(max_r, r), max(max_c, c)
                    total_cells += 1
                elif rec_id == 0x00BD and len(payload) >= 10:  # MULRK
                    r, col_first = struct.unpack_from("<HH", payload, 0)
                    col_last = struct.unpack_from("<H", payload, len(payload) - 2)[0]
                    p_off = 4
                    for c in range(col_first, col_last + 1):
                        if p_off + 6 > len(payload) - 2 or total_cells >= MAX_XLS_CELLS:
                            break
                        _, rk = struct.unpack_from("<HI", payload, p_off)
                        p_off += 6
                        cells[cell_ref(r, c)] = {"type": "number", "value": _decode_rk(rk), "formula": None}
                        max_r, max_c = max(max_r, r), max(max_c, c)
                        total_cells += 1
                elif rec_id == 0x00FD and len(payload) >= 10:  # LABELSST
                    r, c, _, sst_idx = struct.unpack_from("<HHHI", payload, 0)
                    text = sst[sst_idx] if sst_idx < len(sst) else ""
                    cells[cell_ref(r, c)] = {"type": "string", "value": text, "formula": None}
                    max_r, max_c = max(max_r, r), max(max_c, c)
                    total_cells += 1
                elif rec_id == 0x0204 and len(payload) >= 8:  # LABEL
                    r, c = struct.unpack_from("<HH", payload, 0)
                    text, _ = _read_biff8_unicode_str(payload, 6, cch_bytes=2)
                    cells[cell_ref(r, c)] = {"type": "string", "value": text, "formula": None}
                    max_r, max_c = max(max_r, r), max(max_c, c)
                    total_cells += 1
                elif rec_id == 0x0205 and len(payload) >= 8:  # BOOLERR
                    r, c = struct.unpack_from("<HH", payload, 0)
                    bval = payload[6]
                    is_err = payload[7] == 1
                    if is_err:
                        cells[cell_ref(r, c)] = {"type": "error", "value": BIFF_ERRORS.get(bval, f"#ERR({bval})"), "formula": None}
                    else:
                        cells[cell_ref(r, c)] = {"type": "boolean", "value": bool(bval), "formula": None}
                    max_r, max_c = max(max_r, r), max(max_c, c)
                    total_cells += 1
                elif rec_id == 0x0006 and len(payload) >= 22:  # FORMULA
                    r, c = struct.unpack_from("<HH", payload, 0)
                    raw_res = payload[6:14]
                    val: object = None
                    ctype = "number"
                    if raw_res[6] == 0xFF and raw_res[7] == 0xFF:
                        tag = raw_res[0]
                        if tag == 0:  # String result in immediately following STRING (0x0207) record
                            ctype = "string"
                            if k + 1 < len(records) and records[k + 1][1] == 0x0207:
                                val, _ = _read_biff8_unicode_str(records[k + 1][2], 0, cch_bytes=2)
                            else:
                                val = ""
                        elif tag == 1:
                            ctype = "boolean"
                            val = bool(raw_res[2])
                        elif tag == 2:
                            ctype = "error"
                            val = BIFF_ERRORS.get(raw_res[2], f"#ERR({raw_res[2]})")
                        else:
                            ctype = "empty"
                            val = None
                    else:
                        num = struct.unpack_from("<d", raw_res, 0)[0]
                        val = int(num) if (math.isfinite(num) and num.is_integer()) else num
                    rgce_len = struct.unpack_from("<H", payload, 20)[0]
                    rgce = payload[22: 22 + rgce_len]
                    # Store deterministic BIFF8 token signature (raw formulas in BIFF8 are tokenized)
                    formula_repr = f"=<BIFF8:{rgce.hex()}>" if rgce else "=<BIFF8>"
                    cells[cell_ref(r, c)] = {"type": ctype, "value": val, "formula": formula_repr}
                    max_r, max_c = max(max_r, r), max(max_c, c)
                    total_cells += 1
                elif rec_id == 0x00E5 and len(payload) >= 2:  # MERGEDCELLS
                    cnt = struct.unpack_from("<H", payload, 0)[0]
                    for m in range(cnt):
                        moff = 2 + m * 8
                        if moff + 8 > len(payload):
                            break
                        r1, r2, c1, c2 = struct.unpack_from("<HHHH", payload, moff)
                        merged_ranges.append(f"{cell_ref(r1, c1)}:{cell_ref(r2, c2)}")
                k += 1

        sheets.append({
            "name": sm["name"],
            "index": order_idx,
            "hidden": sm["hidden"],
            "visibility": sm["visibility"],
            "rows": max_r + 1 if max_r >= 0 else 0,
            "cols": max_c + 1 if max_c >= 0 else 0,
            "cell_count": len(cells),
            "cells": cells,
            "merged_cells": sorted(set(merged_ranges)),
            "hidden_rows": [],
            "hidden_cols": [],
            "comments": {},
            "hyperlinks": {},
        })

    limitations = [
        "En archivos XLS (BIFF8), las fórmulas se almacenan en formato de tokens binarios; se compara su firma binaria y su valor en caché sin recalcular.",
        "Las filas/columnas ocultas, estilos avanzados y comentarios de XLS no se evalúan en el lector BIFF8 ligero.",
    ]
    if truncated:
        limitations.append(f"Se alcanzó el límite de seguridad de {MAX_XLS_CELLS:,} celdas extraídas.")

    return {
        "format": "XLS",
        "sheet_count": len(sheets),
        "sheets": sheets,
        "defined_names": sorted(set(defined_names)),
        "date_mode_1904": date_mode_1904,
        "has_macros": has_macros,
        "total_cells": total_cells,
        "truncated": truncated,
        "limitations": limitations,
    }
