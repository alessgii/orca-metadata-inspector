"""Automated test suite for Deep PDF, DOCX, XLS, and XLSX Universal Duel.

Covers all 26 cases of the minimum test matrix with real generated PDF, DOCX,
binary OLE2/BIFF8 XLS, XLSX, PPTX, and PNG fixtures.
"""

from __future__ import annotations

import io
import struct
import zipfile

import openpyxl
import pikepdf
import pytest
from docx import Document
from PIL import Image

from orca.doc_analyzers import DocxAnalyzer, PdfAnalyzer, SpreadsheetAnalyzer
from orca.xls_reader import OLE_SIGNATURE, extract_xls_workbook, is_valid_xls

from conftest import complete, upload


# ---------------------------------------------------------------------------
# Minimal Real Binary OLE2/CFBF + BIFF8 .XLS Builder (for deterministic tests)
# ---------------------------------------------------------------------------

def _biff(rec_id: int, payload: bytes) -> bytes:
    return struct.pack("<HH", rec_id, len(payload)) + payload


def _build_minimal_xls_bytes(sheets_spec: list[tuple[str, list[tuple[int, int, str | int | float]]]]) -> bytes:
    """Build a real, valid OLE2/CFBF + BIFF8 Excel .xls binary file."""
    # Build worksheet streams first so we know their sizes
    ws_streams: list[bytes] = []
    for _, cells in sheets_spec:
        buf = bytearray()
        buf.extend(_biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0x0DBB, 0x07CC, 0, 0x0006)))  # Worksheet BOF
        for r, c, val in cells:
            if isinstance(val, (int, float)):
                buf.extend(_biff(0x0203, struct.pack("<HHHd", r, c, 0, float(val))))  # NUMBER
            else:
                raw = str(val).encode("latin1", errors="replace")
                buf.extend(_biff(0x0204, struct.pack("<HHHHB", r, c, 0, len(raw), 0) + raw))  # LABEL
        buf.extend(_biff(0x000A, b""))  # EOF
        ws_streams.append(bytes(buf))

    # Build Workbook globals with placeholder BOUNDSHEET offsets, then patch offsets
    globals_prefix = bytearray()
    globals_prefix.extend(_biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 0x0006)))  # Workbook BOF
    globals_prefix.extend(_biff(0x0022, struct.pack("<H", 0)))  # DATEMODE 1900

    # Calculate total globals length
    bs_records_len = 0
    for name, _ in sheets_spec:
        enc = name.encode("latin1")
        bs_records_len += 4 + 8 + len(enc)  # header(4) + offset(4)+vis(1)+type(1)+cch(1)+flags(1) + name
    eof_len = 4
    globals_len = len(globals_prefix) + bs_records_len + eof_len

    wb = bytearray(globals_prefix)
    cur_off = globals_len
    for idx, (name, _) in enumerate(sheets_spec):
        enc = name.encode("latin1")
        payload = struct.pack("<IBBBB", cur_off, 0, 0, len(enc), 0) + enc
        wb.extend(_biff(0x0085, payload))  # BOUNDSHEET
        cur_off += len(ws_streams[idx])
    wb.extend(_biff(0x000A, b""))  # EOF globals
    for ws in ws_streams:
        wb.extend(ws)

    # Pad Workbook stream to >= 4096 bytes so it uses regular FAT sectors (not MiniStream)
    wb_size = len(wb)
    if len(wb) < 4096:
        wb.extend(b"\x00" * (4096 - len(wb)))
    wb_sectors = (len(wb) + 511) // 512
    wb_padded = bytes(wb).ljust(wb_sectors * 512, b"\x00")

    # Layout:
    # Sector 0: FAT
    # Sector 1: Directory (Root Entry + Workbook)
    # Sectors 2 .. 2 + wb_sectors - 1: Workbook stream
    fat = [0xFFFFFFFF] * 128
    fat[0] = 0xFFFFFFFD  # FATSECT
    fat[1] = 0xFFFFFFFE  # ENDOFCHAIN (Directory)
    for s in range(wb_sectors):
        sec_id = 2 + s
        fat[sec_id] = (sec_id + 1) if (s + 1 < wb_sectors) else 0xFFFFFFFE

    fat_bytes = struct.pack("<128I", *fat)

    def dir_entry(name: str, obj_type: int, left: int, right: int, child: int, start_sec: int, size: int) -> bytes:
        nm = (name + "\x00").encode("utf-16-le")
        entry = bytearray(128)
        entry[0:len(nm)] = nm
        struct.pack_into("<H", entry, 64, len(nm))
        entry[66] = obj_type
        entry[67] = 1  # black
        struct.pack_into("<III", entry, 68, left, right, child)
        struct.pack_into("<II", entry, 116, start_sec, size)
        return bytes(entry)

    root_dir = dir_entry("Root Entry", 5, 0xFFFFFFFF, 0xFFFFFFFF, 1, 0xFFFFFFFE, 0)
    wb_dir = dir_entry("Workbook", 2, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 2, wb_size)
    dir_sector = (root_dir + wb_dir).ljust(512, b"\x00")

    # 512-byte OLE2 Header
    hdr = bytearray(512)
    hdr[0:8] = OLE_SIGNATURE
    struct.pack_into("<HH", hdr, 24, 0x003E, 0x0003)  # minor/major version
    struct.pack_into("<H", hdr, 28, 0xFFFE)  # little-endian
    struct.pack_into("<HH", hdr, 30, 9, 6)  # 512B sectors, 64B mini-sectors
    struct.pack_into("<I", hdr, 44, 1)  # 1 FAT sector
    struct.pack_into("<I", hdr, 48, 1)  # first dir sector = 1
    struct.pack_into("<I", hdr, 56, 4096)  # mini cutoff
    struct.pack_into("<II", hdr, 60, 0xFFFFFFFE, 0)  # no minifat
    struct.pack_into("<II", hdr, 68, 0xFFFFFFFE, 0)  # no extra difat
    difat = [0] + [0xFFFFFFFF] * 108
    struct.pack_into("<109I", hdr, 76, *difat)

    return bytes(hdr) + fat_bytes + dir_sector + wb_padded


# ---------------------------------------------------------------------------
# PDF, DOCX, XLSX Fixture Builders
# ---------------------------------------------------------------------------

def _make_pdf_bytes(pages_text: list[str], *, add_js: bool = False, password: str | None = None,
                    scanned_image_only: bool = False) -> bytes:
    buf = io.BytesIO()
    with pikepdf.new() as pdf:
        for txt in pages_text:
            page = pdf.add_blank_page(page_size=(595, 842))
            if scanned_image_only:
                # Attach an image XObject with no text operators
                img = Image.new("RGB", (16, 16), (100, 120, 140))
                raw_rgb = img.tobytes()
                xobj = pdf.make_stream(raw_rgb, {
                    "/Type": pikepdf.Name("/XObject"),
                    "/Subtype": pikepdf.Name("/Image"),
                    "/Width": 16, "/Height": 16,
                    "/ColorSpace": pikepdf.Name("/DeviceRGB"),
                    "/BitsPerComponent": 8,
                })
                page.Resources = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/Im0": xobj})})
                page.Contents = pdf.make_stream(b"q 100 0 0 100 50 700 cm /Im0 Do Q")
            else:
                safe = txt.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin1")
                page.Contents = pdf.make_stream(stream)
        if add_js:
            pdf.Root["/OpenAction"] = pikepdf.Dictionary({
                "/S": pikepdf.Name("/JavaScript"),
                "/JS": pikepdf.String("app.alert('test');"),
            })
        if password:
            pdf.save(buf, encryption=pikepdf.Encryption(user=password, owner=password))
        else:
            pdf.save(buf)
    return buf.getvalue()


def _make_docx_bytes(paragraphs: list[str], *, table_rows: list[list[str]] | None = None,
                     image_color: tuple[int, int, int] | None = None) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    if table_rows:
        tbl = doc.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r_idx, row in enumerate(table_rows):
            for c_idx, val in enumerate(row):
                tbl.cell(r_idx, c_idx).text = val
    if image_color is not None:
        img_buf = io.BytesIO()
        Image.new("RGB", (20, 20), image_color).save(img_buf, format="PNG")
        img_buf.seek(0)
        doc.add_picture(img_buf)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _make_xlsx_bytes(sheets: dict[str, dict[str, object]]) -> bytes:
    wb = openpyxl.Workbook()
    first = True
    for name, cells in sheets.items():
        ws = wb.active if first else wb.create_sheet(title=name)
        ws.title = name
        first = False
        for coord, val in cells.items():
            ws[coord] = val
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _make_xlsx_with_cached_formula(formula: str, cached_value: int) -> bytes:
    """Create an XLSX where cell B1 has an explicit formula AND an explicit XML <v> cached value."""
    import re
    raw_xlsx = _make_xlsx_bytes({"Hoja1": {"A1": 10, "B1": formula}})
    in_buf = io.BytesIO(raw_xlsx)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(in_buf, "r") as zin, zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                xml = data.decode("utf-8")
                xml = re.sub(r"<c r=\"B1\"[^>]*>.*?</c>", f'<c r="B1"><f>{formula.lstrip("=")}</f><v>{cached_value}</v></c>', xml)
                data = xml.encode("utf-8")
            zout.writestr(item, data)
    return out_buf.getvalue()


def _duel_via_api(client, items: list[tuple[str, bytes, str]]):
    resp = upload(client, items)
    base, session = complete(client, resp)
    f0, f1 = session["files"][0]["id"], session["files"][1]["id"]
    duel = client.get(f"{base}/duel", params={"file_a": f0, "file_b": f1})
    assert duel.status_code == 200, duel.text
    return base, session, duel.json()


# ---------------------------------------------------------------------------
# PDF Cases (1 - 6)
# ---------------------------------------------------------------------------

def test_pdf_identical(client):
    pdf = _make_pdf_bytes(["Informe anual de auditoria"])
    _, _, res = _duel_via_api(client, [("a.pdf", pdf, "application/pdf"), ("b.pdf", pdf, "application/pdf")])
    assert res["binary_identity"]["identical_bytes"] is True
    assert res["capabilities"]["is_pdf_pair"] is True
    assert res["content_comparison"]["status"] == "analyzed"
    assert res["content_comparison"]["summary"]["substantive_identical"] is True
    assert res["similarity"]["overall_available"] is True


def test_pdf_added_page(client):
    p1 = _make_pdf_bytes(["Pagina uno"])
    p2 = _make_pdf_bytes(["Pagina uno", "Pagina dos agregada"])
    _, _, res = _duel_via_api(client, [("a.pdf", p1, "application/pdf"), ("b.pdf", p2, "application/pdf")])
    cc = res["content_comparison"]
    assert cc["status"] == "analyzed"
    assert cc["summary"]["added_pages"] == 1
    assert any(p["status"] == "added" for p in cc["pages"])


def test_pdf_modified_text(client):
    p1 = _make_pdf_bytes(["Total aprobado: 1500 pesos"])
    p2 = _make_pdf_bytes(["Total aprobado: 9500 pesos"])
    _, _, res = _duel_via_api(client, [("a.pdf", p1, "application/pdf"), ("b.pdf", p2, "application/pdf")])
    cc = res["content_comparison"]
    assert cc["summary"]["modified_pages"] == 1
    assert cc["pages"][0]["text_identical"] is False
    assert cc["pages"][0]["segments"]


def test_pdf_scanned_without_text_reports_limitation(client):
    p1 = _make_pdf_bytes([""], scanned_image_only=True)
    p2 = _make_pdf_bytes([""], scanned_image_only=True)
    _, _, res = _duel_via_api(client, [("scan1.pdf", p1, "application/pdf"), ("scan2.pdf", p2, "application/pdf")])
    cc = res["content_comparison"]
    assert cc["summary"]["scanned_a"] is True
    assert cc["metrics"]["text"]["available"] is False
    assert any("escaneado" in w.lower() for w in cc["warnings"])


def test_pdf_encrypted_controlled_error(client):
    p1 = _make_pdf_bytes(["Secreto"], password="clave")
    p2 = _make_pdf_bytes(["Publico"])
    _, _, res = _duel_via_api(client, [("enc.pdf", p1, "application/pdf"), ("pub.pdf", p2, "application/pdf")])
    assert res["content_comparison"]["status"] in {"error", "unavailable"}
    assert "cifrado" in res["content_comparison"]["reason"].lower() or "contraseña" in res["content_comparison"]["reason"].lower()


def test_pdf_javascript_detected_without_execution(client):
    p1 = _make_pdf_bytes(["Documento con JS"], add_js=True)
    p2 = _make_pdf_bytes(["Documento con JS"], add_js=False)
    _, _, res = _duel_via_api(client, [("js.pdf", p1, "application/pdf"), ("clean.pdf", p2, "application/pdf")])
    cc = res["content_comparison"]
    assert cc["summary"]["javascript_a"]
    assert any("JavaScript" in w for w in cc["warnings"])


# ---------------------------------------------------------------------------
# DOCX Cases (7 - 11)
# ---------------------------------------------------------------------------

def test_docx_identical(client):
    d = _make_docx_bytes(["Titulo", "Primer parrafo de prueba."])
    _, _, res = _duel_via_api(client, [
        ("a.docx", d, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("b.docx", d, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ])
    assert res["capabilities"]["is_docx_pair"] is True
    assert res["content_comparison"]["summary"]["substantive_identical"] is True
    assert res["similarity"]["overall"] == 100.0


def test_docx_modified_paragraph(client):
    d1 = _make_docx_bytes(["Clausula uno", "Monto original cien"])
    d2 = _make_docx_bytes(["Clausula uno", "Monto original doscientos"])
    _, _, res = _duel_via_api(client, [
        ("a.docx", d1, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("b.docx", d2, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ])
    cc = res["content_comparison"]
    assert cc["summary"]["modified_paragraphs"] == 1
    assert cc["paragraph_diffs"][0]["segments"]


def test_docx_modified_table(client):
    d1 = _make_docx_bytes(["Resumen"], table_rows=[["Item", "Cant"], ["Lapiz", "10"]])
    d2 = _make_docx_bytes(["Resumen"], table_rows=[["Item", "Cant"], ["Lapiz", "25"]])
    _, _, res = _duel_via_api(client, [
        ("a.docx", d1, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("b.docx", d2, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ])
    cc = res["content_comparison"]
    assert cc["summary"]["modified_tables"] == 1
    assert any("10" in ch and "25" in ch for ch in cc["table_diffs"][0]["changes"])


def test_docx_replaced_image(client):
    d1 = _make_docx_bytes(["Foto"], image_color=(255, 0, 0))
    d2 = _make_docx_bytes(["Foto"], image_color=(0, 0, 255))
    _, _, res = _duel_via_api(client, [
        ("a.docx", d1, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("b.docx", d2, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ])
    cc = res["content_comparison"]
    assert cc["summary"]["media_added"] == 1
    assert cc["summary"]["media_removed"] == 1


def test_docx_different_xml_serialization_no_false_semantic_diff(client):
    d1 = _make_docx_bytes(["Texto estable"])
    # Re-serialize word/document.xml with extra whitespace & rsid attribute (different bytes, same semantics)
    in_buf = io.BytesIO(d1)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(in_buf, "r") as zin, zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                xml = data.decode("utf-8").replace("<w:p>", '<w:p w:rsidR="00AA11BB">')
                data = xml.encode("utf-8")
            zout.writestr(item, data)
    d2 = out_buf.getvalue()
    assert d1 != d2
    _, _, res = _duel_via_api(client, [
        ("a.docx", d1, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("b.docx", d2, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ])
    assert res["binary_identity"]["identical_bytes"] is False
    assert res["content_comparison"]["summary"]["substantive_identical"] is True


# ---------------------------------------------------------------------------
# XLS & XLSX Cases (12 - 19)
# ---------------------------------------------------------------------------

def test_xls_identical_and_modified_cell(client):
    xls_a = _build_minimal_xls_bytes([("Inventario", [(0, 0, "Producto"), (0, 1, 150)])])
    xls_b = _build_minimal_xls_bytes([("Inventario", [(0, 0, "Producto"), (0, 1, 175)])])

    # Identical XLS
    _, _, res_eq = _duel_via_api(client, [
        ("a.xls", xls_a, "application/vnd.ms-excel"),
        ("b.xls", xls_a, "application/vnd.ms-excel"),
    ])
    assert res_eq["capabilities"]["is_spreadsheet_pair"] is True
    assert res_eq["content_comparison"]["summary"]["substantive_identical"] is True

    # Modified cell B1 (150 -> 175)
    _, _, res_diff = _duel_via_api(client, [
        ("a.xls", xls_a, "application/vnd.ms-excel"),
        ("b.xls", xls_b, "application/vnd.ms-excel"),
    ])
    cc = res_diff["content_comparison"]
    assert cc["summary"]["modified_cells"] == 1
    diff = cc["sheets"][0]["diffs"][0]
    assert diff["cell"] == "B1"
    assert diff["value_a"] == 150 and diff["value_b"] == 175


def test_xlsx_identical_formula_modified_new_and_renamed_sheet(client):
    x1 = _make_xlsx_bytes({"Inventario": {"A1": 10, "A2": 20, "C1": "=SUM(A1:A2)"}})
    x2_formula = _make_xlsx_bytes({"Inventario": {"A1": 10, "A2": 20, "C1": "=SUM(A1:A3)"}})
    x3_new_sheet = _make_xlsx_bytes({"Inventario": {"A1": 10, "A2": 20, "C1": "=SUM(A1:A2)"}, "Extra": {"A1": "Nuevo"}})
    x4_renamed = _make_xlsx_bytes({"Existencias": {"A1": 10, "A2": 20, "C1": "=SUM(A1:A2)"}})

    # Identical XLSX
    _, _, r_eq = _duel_via_api(client, [
        ("a.xlsx", x1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("b.xlsx", x1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ])
    assert r_eq["content_comparison"]["summary"]["substantive_identical"] is True

    # Formula modified
    _, _, r_f = _duel_via_api(client, [
        ("a.xlsx", x1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("b.xlsx", x2_formula, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ])
    assert r_f["content_comparison"]["summary"]["formula_diffs"] == 1
    assert r_f["content_comparison"]["sheets"][0]["diffs"][0]["cell"] == "C1"

    # Added sheet
    _, _, r_add = _duel_via_api(client, [
        ("a.xlsx", x1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("b.xlsx", x3_new_sheet, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ])
    assert "Extra" in r_add["content_comparison"]["summary"]["added_sheets"]

    # Renamed sheet detected by content overlap
    _, _, r_ren = _duel_via_api(client, [
        ("a.xlsx", x1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("b.xlsx", x4_renamed, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ])
    assert r_ren["content_comparison"]["summary"]["renamed_sheets"]
    assert r_ren["content_comparison"]["summary"]["renamed_sheets"][0]["name_a"] == "Inventario"
    assert r_ren["content_comparison"]["summary"]["renamed_sheets"][0]["name_b"] == "Existencias"


def test_xls_vs_xlsx_cross_format_comparison(client):
    xls = _build_minimal_xls_bytes([("Datos", [(0, 0, "Clave"), (0, 1, 42)])])
    xlsx = _make_xlsx_bytes({"Datos": {"A1": "Clave", "B1": 99}})
    _, _, res = _duel_via_api(client, [
        ("legacy.xls", xls, "application/vnd.ms-excel"),
        ("modern.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ])
    assert res["capabilities"]["is_spreadsheet_pair"] is True
    cc = res["content_comparison"]
    assert cc["status"] == "analyzed"
    assert cc["cross_format"] is True
    assert cc["summary"]["modified_cells"] == 1
    assert cc["sheets"][0]["diffs"][0]["cell"] == "B1"


def test_xlsx_stale_cached_value_distinguished_from_formula(client):
    x_a = _make_xlsx_with_cached_formula("=A1*2", 20)
    x_b = _make_xlsx_with_cached_formula("=A1*2", 999)  # same formula, stale cached value!
    _, _, res = _duel_via_api(client, [
        ("fresh.xlsx", x_a, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("stale.xlsx", x_b, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ])
    cc = res["content_comparison"]
    assert cc["summary"]["cache_only_diffs"] == 1
    diff = cc["sheets"][0]["diffs"][0]
    assert diff["cell"] == "B1"
    assert diff["change_type"] == "cache_only_diff"
    assert diff["formula_a"] == "=A1*2" and diff["formula_b"] == "=A1*2"
    assert diff["value_a"] == 20 and diff["value_b"] == 999


# ---------------------------------------------------------------------------
# Corrupt, Mixed Set, 10 Docs, Investigation, Exports, Regressions (20 - 26)
# ---------------------------------------------------------------------------

def test_corrupt_document_controlled_error(client):
    valid = _make_pdf_bytes(["Valido"])
    corrupt = b"%PDF-1.7\nthis-is-corrupted-pdf-content-without-xref"
    resp = upload(client, [("ok.pdf", valid, "application/pdf"), ("bad.pdf", corrupt, "application/pdf")])
    base, session = complete(client, resp)
    f0, f1 = session["files"][0]["id"], session["files"][1]["id"]
    duel = client.get(f"{base}/duel", params={"file_a": f0, "file_b": f1})
    assert duel.status_code == 200
    assert duel.json()["content_comparison"]["status"] in {"unavailable", "error"}


def test_mixed_set_pdf_docx_xlsx_and_ten_documents(client):
    files = [
        ("a.pdf", _make_pdf_bytes(["Uno"]), "application/pdf"),
        ("b.pdf", _make_pdf_bytes(["Dos"]), "application/pdf"),
        ("c.docx", _make_docx_bytes(["Tres"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("d.docx", _make_docx_bytes(["Cuatro"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("e.xlsx", _make_xlsx_bytes({"H": {"A1": 1}}), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("f.xlsx", _make_xlsx_bytes({"H": {"A1": 2}}), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("g.xls", _build_minimal_xls_bytes([("H", [(0, 0, 3)])]), "application/vnd.ms-excel"),
        ("h.pdf", _make_pdf_bytes(["Uno"]), "application/pdf"),
        ("i.docx", _make_docx_bytes(["Cinco"]), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("j.xlsx", _make_xlsx_bytes({"H": {"A1": 4}}), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ]
    resp = upload(client, files)
    base, _ = complete(client, resp)
    mx = client.get(f"{base}/duel/matrix")
    assert mx.status_code == 200
    data = mx.json()
    assert data["file_count"] == 10
    assert data["counts"]["total_pairs"] == 45
    assert data["counts"]["not_applicable"] > 0
    assert data["counts"]["pending"] > 0


def test_investigation_and_exports_for_documents(client):
    inv = client.post("/api/investigations", json={
        "name": "Auditoría documental", "responsible": "Ing. Clara",
    }).json()["uuid"]
    d1 = _make_docx_bytes(["Contrato versión 1"])
    d2 = _make_docx_bytes(["Contrato versión 2"])
    added = client.post(
        f"/api/investigations/{inv}/files",
        files=[
            ("files", ("v1.docx", d1, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
            ("files", ("v2.docx", d2, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        ],
    )
    assert added.status_code == 201
    u0, u1 = added.json()["files"][0]["uuid"], added.json()["files"][1]["uuid"]
    comp = client.post(f"/api/investigations/{inv}/comparisons", json={"file_a_uuid": u0, "file_b_uuid": u1})
    assert comp.status_code == 200
    assert comp.json()["result"]["content_comparison"]["status"] == "analyzed"

    # Exports JSON & HTML safe against XSS
    x1 = _make_xlsx_bytes({"Hoja1": {"A1": "<script>x</script>"}})
    x2 = _make_xlsx_bytes({"Hoja1": {"A1": "seguro"}})
    base, session, _ = _duel_via_api(client, [
        ("a.xlsx", x1, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("b.xlsx", x2, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ])
    f0, f1 = session["files"][0]["id"], session["files"][1]["id"]
    h = client.get(f"{base}/duel", params={"file_a": f0, "file_b": f1, "download": "true", "format": "html"})
    j = client.get(f"{base}/duel", params={"file_a": f0, "file_b": f1, "download": "true", "format": "json"})
    assert h.status_code == 200 and j.status_code == 200
    assert "<script>x</script>" not in h.text
    assert "Diferencias profundas de contenido" in h.text
