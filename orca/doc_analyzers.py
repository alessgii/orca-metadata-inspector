"""Deep semantic and structural document extractors & comparators for PDF, DOCX, XLS, and XLSX.

All parsers are strictly local, read-only, bounded against resource exhaustion,
and NEVER execute JavaScript, macros, formulas, or external links.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

import openpyxl
import pikepdf
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException

from .config import MAX_BYTES, Settings
from .content import (
    ContentError,
    _normalize_text as normalize_text,
    _text_similarity as text_similarity,
    _token_diff,
)


def inline_diff_segments(text_a: str, text_b: str) -> list[dict]:
    return _token_diff(text_a, text_b).get("segments", [])
from .uploads import (
    ZIP_ERRORS,
    ExpansionBudget,
    UploadError,
    check_budget,
    checked_zip,
    member_chunks,
    safe_members,
)
from .xls_reader import MAX_XLS_CELLS, XlsError, cell_ref, extract_xls_workbook

DOC_ENGINE_VERSION = "1.0"
MAX_PDF_PAGES = 500
MAX_DOCX_PARAGRAPHS = 20_000
MAX_WORKBOOK_CELLS = MAX_XLS_CELLS  # 50,000 populated cells safety ceiling
MAX_DIFF_CELLS_RETURNED = 1000

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _read_zip_part(archive, item, budget: ExpansionBudget, max_size: int = 8_000_000) -> bytes:
    if item.file_size > max_size:
        raise ContentError(f"La parte interna {item.filename} supera el límite de lectura segura.")
    buf = bytearray()
    for chunk in member_chunks(archive, item):
        budget.account(len(chunk))
        buf.extend(chunk)
    return bytes(buf)


def _norm_cell_val(val: Any) -> tuple[str, Any]:
    """Normalize cell values into JSON-safe (type_name, value) tuples."""
    if val is None:
        return "empty", None
    if isinstance(val, bool):
        return "boolean", val
    if isinstance(val, int):
        return "number", val
    if isinstance(val, float):
        return "number", int(val) if val.is_integer() else round(val, 10)
    if isinstance(val, (datetime, date)):
        return "date", val.isoformat()
    s = str(val)
    if s.startswith("#") and s in {"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A"}:
        return "error", s
    return "string", s


# ===========================================================================
# 1. PDF Deep Extractor & Comparator (pikepdf)
# ===========================================================================

def _decode_pdf_str(obj: Any) -> str:
    try:
        raw = bytes(obj)
    except Exception:
        return str(obj)
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin1", errors="replace")


def _extract_pdf_page_text(page: pikepdf.Page) -> tuple[str, list[str]]:
    """Extract text operators (Tj, TJ, ', \") from a PDF page content stream safely."""
    blocks: list[str] = []
    current: list[str] = []
    try:
        instructions = pikepdf.parse_content_stream(page)
    except Exception:
        return "", []

    for operands, operator in instructions:
        op = str(operator)
        if op == "BT":
            current = []
        elif op == "ET":
            line = normalize_text("".join(current))
            if line:
                blocks.append(line)
            current = []
        elif op in {"Tj", "'", '"'}:
            if operands:
                text = _decode_pdf_str(operands[-1])
                if op in {"'", '"'}:
                    current.append(" ")
                current.append(text)
        elif op == "TJ":
            if operands and isinstance(operands[0], pikepdf.Array):
                parts = []
                for item in operands[0]:
                    if isinstance(item, (pikepdf.String, bytes)):
                        parts.append(_decode_pdf_str(item))
                    elif isinstance(item, (int, float)) and float(item) < -180:
                        parts.append(" ")
                current.append("".join(parts))
        elif op in {"Td", "TD", "T*"}:
            if current and not current[-1].endswith(" "):
                current.append(" ")

    if current:
        line = normalize_text("".join(current))
        if line:
            blocks.append(line)

    full = normalize_text("\n".join(blocks))
    return full, blocks


def _scan_pdf_js_indicators(pdf: pikepdf.Pdf) -> list[str]:
    """Detect JavaScript / active action keys without executing anything."""
    indicators: list[str] = []
    root = pdf.Root
    if "/OpenAction" in root:
        oa = root["/OpenAction"]
        indicators.append("OpenAction en catálogo")
        try:
            if isinstance(oa, pikepdf.Dictionary) and (str(oa.get("/S", "")) == "/JavaScript" or "/JS" in oa):
                indicators.append("JavaScript en OpenAction")
        except Exception:
            pass
    if "/AA" in root:
        indicators.append("Acciones adicionales (/AA) en catálogo")
    try:
        names = root.get("/Names")
        if isinstance(names, pikepdf.Dictionary) and "/JavaScript" in names:
            indicators.append("Árbol /Names/JavaScript")
    except Exception:
        pass
    # Scan bounded objects for /JS or /JavaScript keys
    checked = 0
    for obj in pdf.objects:
        checked += 1
        if checked > 4000:
            break
        if isinstance(obj, pikepdf.Dictionary):
            if "/JS" in obj or str(obj.get("/S", "")) == "/JavaScript":
                indicators.append("Objeto con acción /JavaScript o /JS")
                break
    return sorted(set(indicators))


class PdfAnalyzer:
    format_name = "PDF"

    def extract(self, path: Path, settings: Settings) -> dict:
        try:
            pdf = pikepdf.open(path)
        except pikepdf.PasswordError as exc:
            raise ContentError("El PDF está cifrado o protegido con contraseña. No se intenta descifrar sin contraseña.") from exc
        except (pikepdf.PdfError, OSError, ValueError) as exc:
            raise ContentError(f"No se pudo abrir o analizar la estructura del PDF: {exc}") from exc

        with pdf:
            if pdf.is_encrypted:
                raise ContentError("El PDF está cifrado. No se extrae contenido sin contraseña explícita.")

            total_pages = len(pdf.pages)
            truncated = total_pages > MAX_PDF_PAGES
            pages_to_scan = min(total_pages, MAX_PDF_PAGES)

            pages: list[dict] = []
            all_fonts: set[str] = set()
            all_links: set[str] = set()
            all_images: dict[str, dict] = {}
            total_annots = 0
            pages_with_text = 0
            pages_with_images_only = 0

            for idx in range(pages_to_scan):
                page = pdf.pages[idx]
                mbox = page.get("/MediaBox")
                dims = [round(float(x), 2) for x in mbox] if mbox and len(mbox) == 4 else None
                width = round(abs(dims[2] - dims[0]), 2) if dims else None
                height = round(abs(dims[3] - dims[1]), 2) if dims else None
                rot = int(page.get("/Rotate", 0) or 0) % 360

                text, blocks = _extract_pdf_page_text(page)
                if text:
                    pages_with_text += 1

                # Fonts & XObject images on page
                page_fonts: list[str] = []
                page_image_hashes: list[str] = []
                resources = page.get("/Resources")
                if isinstance(resources, pikepdf.Dictionary):
                    fonts_dict = resources.get("/Font")
                    if isinstance(fonts_dict, pikepdf.Dictionary):
                        for _, fobj in fonts_dict.items():
                            try:
                                if isinstance(fobj, pikepdf.Dictionary) and "/BaseFont" in fobj:
                                    fname = str(fobj["/BaseFont"]).lstrip("/")
                                    page_fonts.append(fname)
                                    all_fonts.add(fname)
                            except Exception:
                                pass
                    xobjs = resources.get("/XObject")
                    if isinstance(xobjs, pikepdf.Dictionary):
                        for xname, xobj in xobjs.items():
                            try:
                                if isinstance(xobj, pikepdf.Stream) and str(xobj.get("/Subtype", "")) == "/Image":
                                    raw_img = xobj.read_raw_bytes()
                                    h = _sha256_bytes(raw_img)
                                    page_image_hashes.append(h)
                                    all_images.setdefault(h, {
                                        "sha256": h,
                                        "size": len(raw_img),
                                        "width": int(xobj.get("/Width", 0) or 0),
                                        "height": int(xobj.get("/Height", 0) or 0),
                                        "name": str(xname),
                                    })
                            except Exception:
                                pass

                if not text and page_image_hashes:
                    pages_with_images_only += 1

                # Annotations & links
                page_annots = 0
                page_links: list[str] = []
                annots = page.get("/Annots")
                if isinstance(annots, pikepdf.Array):
                    for an in annots:
                        try:
                            if not isinstance(an, pikepdf.Dictionary):
                                continue
                            page_annots += 1
                            total_annots += 1
                            act = an.get("/A")
                            if isinstance(act, pikepdf.Dictionary) and "/URI" in act:
                                uri = _decode_pdf_str(act["/URI"])
                                page_links.append(uri)
                                all_links.add(uri)
                        except Exception:
                            pass

                pages.append({
                    "number": idx + 1,
                    "width": width,
                    "height": height,
                    "rotation": rot,
                    "text": text,
                    "blocks": blocks[:200],
                    "text_sha256": _sha256_bytes(text.encode("utf-8")) if text else None,
                    "image_hashes": sorted(page_image_hashes),
                    "font_count": len(page_fonts),
                    "annotation_count": page_annots,
                    "links": sorted(set(page_links)),
                })

            # Bookmarks / Outlines
            bookmarks: list[str] = []
            try:
                with pdf.open_outline() as outline:
                    for item in outline.root[:200]:
                        if item.title:
                            bookmarks.append(str(item.title))
            except Exception:
                pass

            # Embedded files / Attachments
            attachments = sorted(str(k) for k in pdf.attachments.keys())

            # Forms & Digital Signatures
            acroform = pdf.Root.get("/AcroForm")
            has_forms = isinstance(acroform, pikepdf.Dictionary)
            has_signatures = False
            if has_forms:
                fields = acroform.get("/Fields")
                if isinstance(fields, pikepdf.Array):
                    for fld in fields:
                        try:
                            if isinstance(fld, pikepdf.Dictionary) and str(fld.get("/FT", "")) == "/Sig":
                                has_signatures = True
                                break
                        except Exception:
                            pass

            js_indicators = _scan_pdf_js_indicators(pdf)
            scanned_likely = total_pages > 0 and pages_with_text == 0

            limitations = [
                "La comparación textual de PDF evalúa texto extraíble de los flujos de contenido y no equivale a igualdad visual renderizada.",
                "No se ejecuta JavaScript ni acciones incrustadas; los indicadores activos se reportan únicamente de forma descriptiva.",
            ]
            if scanned_likely:
                limitations.append(
                    "No se detectó texto nativo extraíble (posible PDF escaneado o basado en imágenes). No se aplicó OCR silencioso."
                )
            if truncated:
                limitations.append(f"Se alcanzó el límite de seguridad de {MAX_PDF_PAGES} páginas.")

            return {
                "format": "PDF",
                "pdf_version": str(pdf.pdf_version),
                "page_count": total_pages,
                "pages": pages,
                "pages_with_text": pages_with_text,
                "scanned_likely": scanned_likely,
                "images": list(all_images.values()),
                "fonts": sorted(all_fonts)[:200],
                "annotation_count": total_annots,
                "links": sorted(all_links)[:200],
                "bookmarks": bookmarks,
                "attachments": attachments,
                "has_forms": has_forms,
                "has_signatures": has_signatures,
                "javascript_indicators": js_indicators,
                "limitations": limitations,
            }

    def compare(self, a: dict, b: dict) -> dict:
        pages_a = a["pages"]
        pages_b = b["pages"]
        n_a, n_b = len(pages_a), len(pages_b)

        # Strip repeated headers/footers (lines appearing identically on >= 60% of pages when >= 3 pages)
        def _repeated_lines(pages_list: list[dict]) -> set[str]:
            if len(pages_list) < 3:
                return set()
            cnt: Counter[str] = Counter()
            for p in pages_list:
                for blk in set(p.get("blocks", [])):
                    if len(blk) <= 120:
                        cnt[blk] += 1
            thresh = max(2, int(len(pages_list) * 0.6))
            return {line for line, c in cnt.items() if c >= thresh}

        rep_a = _repeated_lines(pages_a)
        rep_b = _repeated_lines(pages_b)

        def _core_text(page: dict, rep: set[str]) -> str:
            if not rep:
                return page["text"]
            kept = [blk for blk in page.get("blocks", []) if blk not in rep]
            return normalize_text("\n".join(kept))

        # Match pages by exact hash first, then by position/similarity
        used_b: set[int] = set()
        matches: dict[int, int] = {}
        by_hash_b: dict[str, list[int]] = {}
        for j, pb in enumerate(pages_b):
            key = pb["text_sha256"] or f"empty:{','.join(pb['image_hashes'])}"
            by_hash_b.setdefault(key, []).append(j)

        for i, pa in enumerate(pages_a):
            key = pa["text_sha256"] or f"empty:{','.join(pa['image_hashes'])}"
            cands = [j for j in by_hash_b.get(key, []) if j not in used_b]
            if cands:
                # Prefer same index if available
                pick = i if i in cands else cands[0]
                matches[i] = pick
                used_b.add(pick)

        # Align remaining unmatched pages by index / best similarity
        rem_a = [i for i in range(n_a) if i not in matches]
        rem_b = [j for j in range(n_b) if j not in used_b]
        for i, j in zip(rem_a, rem_b):
            matches[i] = j
            used_b.add(j)

        page_diffs: list[dict] = []
        identical_pages = 0
        modified_pages = 0
        reordered_pages = 0
        added_pages = 0
        removed_pages = 0
        text_scores: list[float] = []

        for i in range(n_a):
            pa = pages_a[i]
            if i not in matches:
                removed_pages += 1
                page_diffs.append({
                    "page_a": pa["number"],
                    "page_b": None,
                    "status": "removed",
                    "text_a": pa["text"][:1000],
                    "text_b": "",
                    "segments": [],
                    "changes": ["Página eliminada en B."],
                })
                text_scores.append(0.0)
                continue
            j = matches[i]
            pb = pages_b[j]
            ta = _core_text(pa, rep_a)
            tb = _core_text(pb, rep_b)
            same_text = pa["text"] == pb["text"]
            same_imgs = pa["image_hashes"] == pb["image_hashes"]
            same_geom = (pa["width"] == pb["width"] and pa["height"] == pb["height"] and pa["rotation"] == pb["rotation"])
            reordered = i != j

            changes = []
            if not same_text:
                changes.append("Texto modificado")
            if not same_imgs:
                changes.append("Imágenes incrustadas modificadas")
            if not same_geom:
                changes.append("Dimensiones o rotación modificadas")
            if pa["annotation_count"] != pb["annotation_count"]:
                changes.append(f"Anotaciones: {pa['annotation_count']} → {pb['annotation_count']}")
            if reordered:
                changes.append(f"Reordenada (pág. {pa['number']} → {pb['number']})")

            if not changes:
                st = "identical"
                identical_pages += 1
            elif reordered and same_text and same_imgs and same_geom:
                st = "reordered"
                reordered_pages += 1
            else:
                st = "modified"
                modified_pages += 1

            sim = 1.0 if same_text else text_similarity(ta, tb)
            if pa["text"] or pb["text"]:
                text_scores.append(sim)

            page_diffs.append({
                "page_a": pa["number"],
                "page_b": pb["number"],
                "status": st,
                "text_identical": same_text,
                "text_a": pa["text"][:1500] if not same_text else "",
                "text_b": pb["text"][:1500] if not same_text else "",
                "segments": inline_diff_segments(pa["text"][:1500], pb["text"][:1500]) if not same_text else [],
                "changes": changes,
            })

        for j in range(n_b):
            if j not in used_b:
                pb = pages_b[j]
                added_pages += 1
                page_diffs.append({
                    "page_a": None,
                    "page_b": pb["number"],
                    "status": "added",
                    "text_a": "",
                    "text_b": pb["text"][:1000],
                    "segments": [],
                    "changes": ["Página agregada en B."],
                })
                text_scores.append(0.0)

        # Structural & security summary
        structural_changes: list[str] = []
        if a["pdf_version"] != b["pdf_version"]:
            structural_changes.append(f"Versión PDF: {a['pdf_version']} → {b['pdf_version']}")
        if a["attachments"] != b["attachments"]:
            structural_changes.append(f"Adjuntos: {len(a['attachments'])} → {len(b['attachments'])}")
        if a["bookmarks"] != b["bookmarks"]:
            structural_changes.append(f"Marcadores: {len(a['bookmarks'])} → {len(b['bookmarks'])}")
        if a["annotation_count"] != b["annotation_count"]:
            structural_changes.append(f"Anotaciones totales: {a['annotation_count']} → {b['annotation_count']}")
        if a["has_forms"] != b["has_forms"]:
            structural_changes.append(f"Formularios AcroForm: {a['has_forms']} → {b['has_forms']}")
        if a["has_signatures"] != b["has_signatures"]:
            structural_changes.append(f"Indicadores de firma digital: {a['has_signatures']} → {b['has_signatures']}")
        if a["javascript_indicators"] != b["javascript_indicators"]:
            structural_changes.append("Indicadores de JavaScript / acciones activas difieren.")

        # Image inventory score
        img_a = {im["sha256"] for im in a["images"]}
        img_b = {im["sha256"] for im in b["images"]}
        media_avail = bool(img_a or img_b)
        media_score = (len(img_a & img_b) / len(img_a | img_b)) if media_avail else None

        # Text & structure scores
        text_avail = bool(text_scores) and not (a["scanned_likely"] and b["scanned_likely"])
        text_score = round(sum(text_scores) / len(text_scores), 6) if text_avail else None
        max_p = max(n_a, n_b, 1)
        page_count_score = min(n_a, n_b) / max_p
        order_score = 1.0 - (reordered_pages / max_p)
        struct_penalty = min(0.4, len(structural_changes) * 0.08)
        struct_score = round(max(0.0, 0.55 * page_count_score + 0.30 * order_score + 0.15 * (1.0 - struct_penalty)), 6)

        substantive_identical = (
            modified_pages == 0 and added_pages == 0 and removed_pages == 0
            and reordered_pages == 0 and not structural_changes
            and img_a == img_b
        )

        warnings = []
        if a["scanned_likely"] or b["scanned_likely"]:
            warnings.append("Al menos un PDF parece escaneado (sin texto extraíble). La comparación de texto es limitada o no aplicable.")
        if a["javascript_indicators"] or b["javascript_indicators"]:
            warnings.append(
                "Se detectaron indicadores de JavaScript/acciones activas en el PDF (no ejecutados): "
                + ", ".join(sorted(set(a["javascript_indicators"] + b["javascript_indicators"])))
            )

        return {
            "status": "analyzed",
            "format": "PDF",
            "engine_version": DOC_ENGINE_VERSION,
            "summary": {
                "page_count_a": n_a,
                "page_count_b": n_b,
                "identical_pages": identical_pages,
                "modified_pages": modified_pages,
                "reordered_pages": reordered_pages,
                "added_pages": added_pages,
                "removed_pages": removed_pages,
                "images_a": len(img_a),
                "images_b": len(img_b),
                "images_shared": len(img_a & img_b),
                "structural_changes_count": len(structural_changes),
                "scanned_a": a["scanned_likely"],
                "scanned_b": b["scanned_likely"],
                "javascript_a": a["javascript_indicators"],
                "javascript_b": b["javascript_indicators"],
                "substantive_identical": substantive_identical,
            },
            "pages": page_diffs,
            "structural_changes": structural_changes,
            "warnings": warnings,
            "metrics": {
                "text": {"available": text_avail, "score": text_score},
                "media": {"available": media_avail, "score": round(media_score, 6) if media_score is not None else None},
                "structure": {"available": True, "score": struct_score},
            },
            "limitations": list(dict.fromkeys([*a["limitations"], *b["limitations"]])),
        }


# ===========================================================================
# 2. DOCX Deep Extractor & Comparator (multi-part OOXML via defusedxml)
# ===========================================================================

def _extract_docx_paragraphs_and_tables(xml_bytes: bytes) -> dict:
    """Parse a WordprocessingML part (document, header, footer, footnote, comment)."""
    root = fromstring(xml_bytes, forbid_dtd=True)
    paragraphs: list[dict] = []
    tables: list[dict] = []
    tracked_insertions: list[str] = []
    tracked_deletions: list[str] = []

    def _p_text_and_meta(p_elem) -> dict:
        texts: list[str] = []
        style = ""
        is_list = False
        has_bold = False
        has_italic = False
        for child in p_elem.iter():
            ln = _local(child.tag)
            if ln == "pStyle":
                style = child.get(f"{{{W_NS}}}val", "") or child.get("val", "")
            elif ln == "numPr":
                is_list = True
            elif ln == "b":
                has_bold = True
            elif ln == "i":
                has_italic = True
            elif ln == "t" and child.text:
                texts.append(child.text)
            elif ln == "delText" and child.text:
                tracked_deletions.append(normalize_text(child.text))
            elif ln == "ins":
                ins_txt = "".join(t.text for t in child.iter() if _local(t.tag) == "t" and t.text)
                if ins_txt.strip():
                    tracked_insertions.append(normalize_text(ins_txt))
        raw_text = normalize_text("".join(texts))
        is_heading = style.lower().startswith(("heading", "título", "titulo", "title"))
        return {
            "text": raw_text,
            "style": style,
            "is_heading": is_heading,
            "is_list": is_list,
            "formatting": f"s={style}|b={int(has_bold)}|i={int(has_italic)}|l={int(is_list)}",
        }

    # Walk direct body children when present to preserve top-level paragraph vs table structure
    body = next((el for el in root if _local(el.tag) == "body"), root)
    for child in body:
        ln = _local(child.tag)
        if ln == "p":
            if len(paragraphs) < MAX_DOCX_PARAGRAPHS:
                p_info = _p_text_and_meta(child)
                if p_info["text"]:
                    paragraphs.append(p_info)
        elif ln == "tbl":
            rows_data: list[list[str]] = []
            for tr in child:
                if _local(tr.tag) != "tr":
                    continue
                row_cells: list[str] = []
                for tc in tr:
                    if _local(tc.tag) != "tc":
                        continue
                    cell_parts = [
                        _p_text_and_meta(p)["text"]
                        for p in tc if _local(p.tag) == "p"
                    ]
                    row_cells.append(normalize_text(" ".join(x for x in cell_parts if x)))
                if row_cells:
                    rows_data.append(row_cells)
            if rows_data:
                tables.append({"rows": rows_data, "row_count": len(rows_data), "col_count": max(len(r) for r in rows_data)})

    return {
        "paragraphs": paragraphs,
        "tables": tables,
        "tracked_insertions": tracked_insertions,
        "tracked_deletions": tracked_deletions,
    }


class DocxAnalyzer:
    format_name = "DOCX"

    def extract(self, path: Path, settings: Settings) -> dict:
        budget = ExpansionBudget(settings.expanded_limit)
        try:
            with checked_zip(path, 4096) as archive:
                entries = safe_members(archive)
                check_budget(entries, budget.remaining)
                by_name = {item.filename: item for item in entries}

                if "word/document.xml" not in by_name:
                    raise ContentError("El archivo DOCX no contiene word/document.xml.")

                main = _extract_docx_paragraphs_and_tables(_read_zip_part(archive, by_name["word/document.xml"], budget))

                # Multi-part extraction: headers, footers, footnotes, endnotes, comments
                headers: list[str] = []
                footers: list[str] = []
                footnotes: list[str] = []
                endnotes: list[str] = []
                comments: list[dict] = []
                hyperlinks: set[str] = set()
                external_relations: set[str] = set()
                media: list[dict] = []
                embedded_objects: list[str] = []
                has_macros = "word/vbaProject.bin" in by_name

                for name, item in sorted(by_name.items()):
                    low = name.lower()
                    if re.fullmatch(r"word/header\d+\.xml", low):
                        part = _extract_docx_paragraphs_and_tables(_read_zip_part(archive, item, budget))
                        txt = normalize_text(" ".join(p["text"] for p in part["paragraphs"]))
                        if txt:
                            headers.append(txt)
                    elif re.fullmatch(r"word/footer\d+\.xml", low):
                        part = _extract_docx_paragraphs_and_tables(_read_zip_part(archive, item, budget))
                        txt = normalize_text(" ".join(p["text"] for p in part["paragraphs"]))
                        if txt:
                            footers.append(txt)
                    elif low == "word/footnotes.xml":
                        part = _extract_docx_paragraphs_and_tables(_read_zip_part(archive, item, budget))
                        footnotes.extend(p["text"] for p in part["paragraphs"])
                    elif low == "word/endnotes.xml":
                        part = _extract_docx_paragraphs_and_tables(_read_zip_part(archive, item, budget))
                        endnotes.extend(p["text"] for p in part["paragraphs"])
                    elif low == "word/comments.xml":
                        croot = fromstring(_read_zip_part(archive, item, budget), forbid_dtd=True)
                        for cel in croot:
                            if _local(cel.tag) == "comment":
                                author = cel.get(f"{{{W_NS}}}author", "") or cel.get("author", "")
                                ctxt = normalize_text(" ".join(t.text for t in cel.iter() if _local(t.tag) == "t" and t.text))
                                if ctxt:
                                    comments.append({"author": author, "text": ctxt})
                    elif low.startswith("word/_rels/") and low.endswith(".rels"):
                        rroot = fromstring(_read_zip_part(archive, item, budget), forbid_dtd=True)
                        for rel in rroot:
                            if _local(rel.tag) != "Relationship":
                                continue
                            mode = rel.get("TargetMode", "")
                            target = rel.get("Target", "")
                            rtype = rel.get("Type", "")
                            if mode.lower() == "external" and target:
                                external_relations.add(target)
                                if "hyperlink" in rtype.lower():
                                    hyperlinks.add(target)
                    elif low.startswith("word/media/"):
                        raw_m = _read_zip_part(archive, item, budget, max_size=MAX_BYTES)
                        media.append({"part": name, "size": len(raw_m), "sha256": _sha256_bytes(raw_m)})
                    elif low.startswith("word/embeddings/"):
                        embedded_objects.append(name)

                return {
                    "format": "DOCX",
                    "paragraphs": main["paragraphs"],
                    "tables": main["tables"],
                    "headers": headers,
                    "footers": footers,
                    "footnotes": footnotes,
                    "endnotes": endnotes,
                    "comments": comments,
                    "hyperlinks": sorted(hyperlinks),
                    "external_relations": sorted(external_relations),
                    "media": media,
                    "embedded_objects": sorted(embedded_objects),
                    "tracked_insertions": main["tracked_insertions"],
                    "tracked_deletions": main["tracked_deletions"],
                    "has_macros": has_macros,
                    "limitations": [
                        "La normalización semántica ignora identificadores internos de revisión XML (rsid/paraId) y diferencias de serialización.",
                        "Dos DOCX con el mismo texto pueden renderizarse de forma distinta según fuentes instaladas, márgenes o motor de maquetación.",
                    ],
                }
        except (UploadError, DefusedXmlException, ParseError, *ZIP_ERRORS) as exc:
            raise ContentError(f"Error al analizar la estructura OOXML del DOCX: {exc}") from exc

    def compare(self, a: dict, b: dict) -> dict:
        pa_list = a["paragraphs"]
        pb_list = b["paragraphs"]
        texts_a = [p["text"] for p in pa_list]
        texts_b = [p["text"] for p in pb_list]

        matcher = SequenceMatcher(None, texts_a, texts_b, autojunk=False)
        paragraph_diffs: list[dict] = []
        added_p = 0
        removed_p = 0
        modified_p = 0
        formatting_only_p = 0
        identical_p = 0

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for idx_a, idx_b in zip(range(i1, i2), range(j1, j2)):
                    if pa_list[idx_a]["formatting"] != pb_list[idx_b]["formatting"]:
                        formatting_only_p += 1
                    else:
                        identical_p += 1
            elif tag == "delete":
                for idx_a in range(i1, i2):
                    removed_p += 1
                    if len(paragraph_diffs) < 200:
                        paragraph_diffs.append({
                            "index_a": idx_a + 1, "index_b": None, "status": "removed",
                            "text_a": texts_a[idx_a], "text_b": "", "segments": [],
                        })
            elif tag == "insert":
                for idx_b in range(j1, j2):
                    added_p += 1
                    if len(paragraph_diffs) < 200:
                        paragraph_diffs.append({
                            "index_a": None, "index_b": idx_b + 1, "status": "added",
                            "text_a": "", "text_b": texts_b[idx_b], "segments": [],
                        })
            elif tag == "replace":
                paired = min(i2 - i1, j2 - j1)
                for k in range(paired):
                    modified_p += 1
                    ta, tb = texts_a[i1 + k], texts_b[j1 + k]
                    if len(paragraph_diffs) < 200:
                        paragraph_diffs.append({
                            "index_a": i1 + k + 1, "index_b": j1 + k + 1, "status": "modified",
                            "text_a": ta, "text_b": tb, "segments": inline_diff_segments(ta, tb),
                        })
                for idx_a in range(i1 + paired, i2):
                    removed_p += 1
                    if len(paragraph_diffs) < 200:
                        paragraph_diffs.append({
                            "index_a": idx_a + 1, "index_b": None, "status": "removed",
                            "text_a": texts_a[idx_a], "text_b": "", "segments": [],
                        })
                for idx_b in range(j1 + paired, j2):
                    added_p += 1
                    if len(paragraph_diffs) < 200:
                        paragraph_diffs.append({
                            "index_a": None, "index_b": idx_b + 1, "status": "added",
                            "text_a": "", "text_b": texts_b[idx_b], "segments": [],
                        })

        # Table diffs
        tables_a, tables_b = a["tables"], b["tables"]
        table_diffs: list[dict] = []
        for idx in range(max(len(tables_a), len(tables_b))):
            ta = tables_a[idx] if idx < len(tables_a) else None
            tb = tables_b[idx] if idx < len(tables_b) else None
            if ta is None:
                table_diffs.append({"table": idx + 1, "status": "added", "changes": ["Tabla agregada en B."]})
            elif tb is None:
                table_diffs.append({"table": idx + 1, "status": "removed", "changes": ["Tabla eliminada en B."]})
            elif ta["rows"] != tb["rows"]:
                cell_changes = []
                max_r = max(len(ta["rows"]), len(tb["rows"]))
                for r in range(max_r):
                    ra = ta["rows"][r] if r < len(ta["rows"]) else []
                    rb = tb["rows"][r] if r < len(tb["rows"]) else []
                    for c in range(max(len(ra), len(rb))):
                        va = ra[c] if c < len(ra) else ""
                        vb = rb[c] if c < len(rb) else ""
                        if va != vb and len(cell_changes) < 50:
                            cell_changes.append(f"F{r + 1}C{c + 1}: «{va}» → «{vb}»")
                table_diffs.append({"table": idx + 1, "status": "modified", "changes": cell_changes})

        # Media comparison by SHA-256
        hashes_a = {m["sha256"] for m in a["media"]}
        hashes_b = {m["sha256"] for m in b["media"]}
        media_added = sorted(hashes_b - hashes_a)
        media_removed = sorted(hashes_a - hashes_b)
        media_shared = sorted(hashes_a & hashes_b)
        media_avail = bool(hashes_a or hashes_b)
        media_score = (len(media_shared) / len(hashes_a | hashes_b)) if media_avail else None

        structural_changes: list[str] = []
        if a["headers"] != b["headers"]:
            structural_changes.append("Encabezados de página modificados.")
        if a["footers"] != b["footers"]:
            structural_changes.append("Pies de página modificados.")
        if a["footnotes"] != b["footnotes"] or a["endnotes"] != b["endnotes"]:
            structural_changes.append("Notas al pie o notas finales modificadas.")
        if a["comments"] != b["comments"]:
            structural_changes.append(f"Comentarios: {len(a['comments'])} → {len(b['comments'])}")
        if a["hyperlinks"] != b["hyperlinks"]:
            structural_changes.append(f"Hipervínculos externos: {len(a['hyperlinks'])} → {len(b['hyperlinks'])}")
        if a["embedded_objects"] != b["embedded_objects"]:
            structural_changes.append(f"Objetos incrustados: {len(a['embedded_objects'])} → {len(b['embedded_objects'])}")
        if formatting_only_p > 0:
            structural_changes.append(f"{formatting_only_p} párrafo(s) con cambios exclusivamente de formato/estilo.")

        full_a = normalize_text("\n".join([*texts_a, *a["headers"], *a["footers"], *a["footnotes"], *a["endnotes"]]))
        full_b = normalize_text("\n".join([*texts_b, *b["headers"], *b["footers"], *b["footnotes"], *b["endnotes"]]))
        text_avail = bool(full_a or full_b)
        text_score = (1.0 if full_a == full_b else text_similarity(full_a, full_b)) if text_avail else None

        # Table + structure score
        max_p = max(len(pa_list), len(pb_list), 1)
        p_struct = (identical_p + formatting_only_p) / max_p
        tbl_score = 1.0 if not table_diffs else max(0.0, 1.0 - (len(table_diffs) / max(len(tables_a), len(tables_b), 1)))
        struct_score = round(0.65 * p_struct + 0.25 * tbl_score + 0.10 * max(0.0, 1.0 - len(structural_changes) * 0.15), 6)

        substantive_identical = (
            added_p == 0 and removed_p == 0 and modified_p == 0
            and not table_diffs and not media_added and not media_removed
            and a["headers"] == b["headers"] and a["footers"] == b["footers"]
            and a["footnotes"] == b["footnotes"] and a["endnotes"] == b["endnotes"]
            and a["comments"] == b["comments"] and a["hyperlinks"] == b["hyperlinks"]
        )

        warnings = []
        if formatting_only_p > 0 and substantive_identical:
            warnings.append("El contenido textual coincide por completo; solo se detectaron diferencias de estilo o formato.")
        if a["has_macros"] or b["has_macros"]:
            warnings.append("Se detectó proyecto de macros VBA (word/vbaProject.bin). Las macros nunca se ejecutan.")

        return {
            "status": "analyzed",
            "format": "DOCX",
            "engine_version": DOC_ENGINE_VERSION,
            "summary": {
                "paragraphs_a": len(pa_list),
                "paragraphs_b": len(pb_list),
                "identical_paragraphs": identical_p,
                "formatting_only_paragraphs": formatting_only_p,
                "modified_paragraphs": modified_p,
                "added_paragraphs": added_p,
                "removed_paragraphs": removed_p,
                "tables_a": len(tables_a),
                "tables_b": len(tables_b),
                "modified_tables": len(table_diffs),
                "media_added": len(media_added),
                "media_removed": len(media_removed),
                "media_shared": len(media_shared),
                "structural_changes_count": len(structural_changes),
                "substantive_identical": substantive_identical,
            },
            "paragraph_diffs": paragraph_diffs,
            "table_diffs": table_diffs,
            "structural_changes": structural_changes,
            "warnings": warnings,
            "metrics": {
                "text": {"available": text_avail, "score": text_score},
                "media": {"available": media_avail, "score": round(media_score, 6) if media_score is not None else None},
                "structure": {"available": True, "score": struct_score},
            },
            "limitations": list(dict.fromkeys([*a["limitations"], *b["limitations"]])),
        }


# ===========================================================================
# 3. XLS & XLSX Unified Semantic Spreadsheet Extractor & Comparator
# ===========================================================================

def _extract_xlsx_workbook(path: Path, settings: Settings) -> dict:
    """Safely extract XLSX workbook using dual-read (formulas + cached values)."""
    budget = ExpansionBudget(settings.expanded_limit)
    has_macros = False
    try:
        with checked_zip(path, 4096) as archive:
            entries = safe_members(archive)
            check_budget(entries, budget.remaining)
            has_macros = any(e.filename.lower().endswith("vbaproject.bin") for e in entries)
    except (UploadError, *ZIP_ERRORS) as exc:
        raise ContentError(f"Contenedor XLSX corrupto o inseguro: {exc}") from exc

    try:
        wb_formula = openpyxl.load_workbook(path, data_only=False, read_only=False)
        wb_cached = openpyxl.load_workbook(path, data_only=True, read_only=False)
    except Exception as exc:
        raise ContentError(f"No se pudo leer el libro XLSX: {exc}") from exc

    try:
        sheets: list[dict] = []
        total_cells = 0
        truncated = False

        for idx, name in enumerate(wb_formula.sheetnames):
            ws_f = wb_formula[name]
            ws_c = wb_cached[name]
            cells: dict[str, dict] = {}
            comments: dict[str, str] = {}
            hyperlinks: dict[str, str] = {}

            for row in ws_f.iter_rows():
                if total_cells >= MAX_WORKBOOK_CELLS:
                    truncated = True
                    break
                for cell in row:
                    if total_cells >= MAX_WORKBOOK_CELLS:
                        truncated = True
                        break
                    raw_f = cell.value
                    coord = cell.coordinate
                    if raw_f is None and cell.comment is None and cell.hyperlink is None:
                        continue
                    cached_cell = ws_c[coord]
                    if isinstance(raw_f, str) and raw_f.startswith("="):
                        ctype, cval = _norm_cell_val(cached_cell.value)
                        entry = {
                            "type": ctype,
                            "value": cval,
                            "formula": raw_f,
                            "number_format": cell.number_format if cell.number_format != "General" else None,
                        }
                    else:
                        ctype, cval = _norm_cell_val(raw_f)
                        if ctype == "empty" and cell.comment is None and cell.hyperlink is None:
                            continue
                        entry = {
                            "type": ctype,
                            "value": cval,
                            "formula": None,
                            "number_format": cell.number_format if cell.number_format != "General" else None,
                        }
                    cells[coord] = entry
                    total_cells += 1
                    if cell.comment and cell.comment.text:
                        comments[coord] = str(cell.comment.text)
                    if cell.hyperlink and cell.hyperlink.target:
                        hyperlinks[coord] = str(cell.hyperlink.target)

            hidden_rows = sorted(
                r for r, dim in ws_f.row_dimensions.items() if getattr(dim, "hidden", False)
            )[:500]
            hidden_cols = sorted(
                c for c, dim in ws_f.column_dimensions.items() if getattr(dim, "hidden", False)
            )[:500]
            merged = sorted(str(rng) for rng in ws_f.merged_cells.ranges)

            sheets.append({
                "name": name,
                "index": idx,
                "hidden": ws_f.sheet_state != "visible",
                "visibility": ws_f.sheet_state,
                "rows": ws_f.max_row or 0,
                "cols": ws_f.max_column or 0,
                "cell_count": len(cells),
                "cells": cells,
                "merged_cells": merged,
                "hidden_rows": hidden_rows,
                "hidden_cols": hidden_cols,
                "comments": comments,
                "hyperlinks": hyperlinks,
            })

        defined_names = sorted(str(n) for n in wb_formula.defined_names.keys())
        limitations = [
            "Las fórmulas no se ejecutan ni recalculan; se compara el texto de la fórmula por separado del valor en caché almacenado.",
            "Un valor almacenado en caché puede estar desactualizado respecto a su fórmula si el archivo fue generado o editado sin recalcular.",
        ]
        if truncated:
            limitations.append(f"Se alcanzó el límite de seguridad de {MAX_WORKBOOK_CELLS:,} celdas pobladas.")

        return {
            "format": "XLSX",
            "sheet_count": len(sheets),
            "sheets": sheets,
            "defined_names": defined_names,
            "date_mode_1904": bool(getattr(wb_formula, "epoch", None) and wb_formula.epoch.year == 1904),
            "has_macros": has_macros,
            "total_cells": total_cells,
            "truncated": truncated,
            "limitations": limitations,
        }
    finally:
        wb_formula.close()
        wb_cached.close()


class SpreadsheetAnalyzer:
    """Unified semantic analyzer supporting XLSX, XLS, and cross-format XLS↔XLSX comparisons."""

    def __init__(self, format_name: str = "XLSX"):
        self.format_name = format_name

    def extract(self, path: Path, settings: Settings) -> dict:
        if self.format_name == "XLS":
            try:
                return extract_xls_workbook(path)
            except XlsError as exc:
                raise ContentError(str(exc)) from exc
        return _extract_xlsx_workbook(path, settings)

    def compare(self, a: dict, b: dict) -> dict:
        fmt_a, fmt_b = a["format"], b["format"]
        cross_format = fmt_a != fmt_b
        sheets_a = a["sheets"]
        sheets_b = b["sheets"]
        by_name_a = {s["name"]: s for s in sheets_a}
        by_name_b = {s["name"]: s for s in sheets_b}

        # Match sheets by exact name first
        matched_pairs: list[tuple[dict, dict, str]] = []  # (sa, sb, match_kind)
        matched_a: set[str] = set()
        matched_b: set[str] = set()
        for sa in sheets_a:
            if sa["name"] in by_name_b:
                matched_pairs.append((sa, by_name_b[sa["name"]], "same_name"))
                matched_a.add(sa["name"])
                matched_b.add(sa["name"])

        # Detect probable renamed sheets among unmatched sheets by cell-content overlap (>= 60%)
        unmatched_a = [s for s in sheets_a if s["name"] not in matched_a]
        unmatched_b = [s for s in sheets_b if s["name"] not in matched_b]
        renamed_sheets: list[dict] = []
        used_b_rename: set[str] = set()

        for sa in unmatched_a:
            best_sb = None
            best_ratio = 0.0
            keys_a = set(sa["cells"].keys())
            for sb in unmatched_b:
                if sb["name"] in used_b_rename:
                    continue
                keys_b = set(sb["cells"].keys())
                union = keys_a | keys_b
                if not union:
                    continue
                same_vals = sum(
                    1 for k in (keys_a & keys_b)
                    if sa["cells"][k].get("value") == sb["cells"][k].get("value")
                )
                ratio = same_vals / len(union)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_sb = sb
            if best_sb is not None and best_ratio >= 0.60:
                matched_pairs.append((sa, best_sb, "renamed"))
                matched_a.add(sa["name"])
                matched_b.add(best_sb["name"])
                used_b_rename.add(best_sb["name"])
                renamed_sheets.append({
                    "name_a": sa["name"],
                    "name_b": best_sb["name"],
                    "overlap": round(best_ratio * 100.0, 1),
                })

        added_sheets = [s["name"] for s in sheets_b if s["name"] not in matched_b]
        removed_sheets = [s["name"] for s in sheets_a if s["name"] not in matched_a]
        reordered_sheets = [
            {"name": sa["name"], "index_a": sa["index"] + 1, "index_b": sb["index"] + 1}
            for sa, sb, kind in matched_pairs
            if kind == "same_name" and sa["index"] != sb["index"]
        ]

        # Per-sheet cell comparison (strictly over populated cells — NEVER inflating via empty cells!)
        sheet_diffs: list[dict] = []
        total_populated_union = 0
        total_matching_cells = 0
        total_modified_cells = 0
        total_formula_diffs = 0
        total_cache_only_diffs = 0

        for sa, sb, kind in matched_pairs:
            cells_a = sa["cells"]
            cells_b = sb["cells"]
            all_coords = sorted(set(cells_a.keys()) | set(cells_b.keys()))
            total_populated_union += len(all_coords)
            diff_rows: list[dict] = []
            sheet_matching = 0

            for coord in all_coords:
                ca = cells_a.get(coord)
                cb = cells_b.get(coord)
                if ca is None:
                    total_modified_cells += 1
                    if len(diff_rows) < MAX_DIFF_CELLS_RETURNED:
                        diff_rows.append({
                            "cell": coord, "change_type": "added",
                            "value_a": None, "value_b": cb["value"],
                            "formula_a": None, "formula_b": cb.get("formula"),
                            "type_a": None, "type_b": cb["type"],
                        })
                    continue
                if cb is None:
                    total_modified_cells += 1
                    if len(diff_rows) < MAX_DIFF_CELLS_RETURNED:
                        diff_rows.append({
                            "cell": coord, "change_type": "removed",
                            "value_a": ca["value"], "value_b": None,
                            "formula_a": ca.get("formula"), "formula_b": None,
                            "type_a": ca["type"], "type_b": None,
                        })
                    continue

                fa, fb = ca.get("formula"), cb.get("formula")
                va, vb = ca.get("value"), cb.get("value")
                ta, tb = ca.get("type"), cb.get("type")

                # When comparing XLS (BIFF8 token formula) vs XLSX (text formula), compare cached values cleanly
                if cross_format and ((fa and fa.startswith("=<BIFF8")) or (fb and fb.startswith("=<BIFF8"))):
                    formula_diff = False
                else:
                    formula_diff = fa != fb

                val_diff = va != vb
                type_diff = ta != tb

                if not formula_diff and not val_diff and not type_diff:
                    sheet_matching += 1
                    total_matching_cells += 1
                    continue

                total_modified_cells += 1
                if formula_diff and val_diff:
                    ctype = "formula_and_value_modified"
                    total_formula_diffs += 1
                elif formula_diff:
                    ctype = "formula_modified"
                    total_formula_diffs += 1
                elif fa and fb and not formula_diff and val_diff:
                    ctype = "cache_only_diff"
                    total_cache_only_diffs += 1
                elif type_diff and not val_diff:
                    ctype = "type_changed"
                else:
                    ctype = "value_modified"

                if len(diff_rows) < MAX_DIFF_CELLS_RETURNED:
                    diff_rows.append({
                        "cell": coord, "change_type": ctype,
                        "value_a": va, "value_b": vb,
                        "formula_a": fa, "formula_b": fb,
                        "type_a": ta, "type_b": tb,
                    })

            sheet_struct_changes = []
            if sa["hidden"] != sb["hidden"]:
                sheet_struct_changes.append(f"Visibilidad: {sa['visibility']} → {sb['visibility']}")
            if sa["merged_cells"] != sb["merged_cells"]:
                sheet_struct_changes.append(f"Celdas combinadas: {len(sa['merged_cells'])} → {len(sb['merged_cells'])}")
            if not cross_format and (sa["hidden_rows"] != sb["hidden_rows"] or sa["hidden_cols"] != sb["hidden_cols"]):
                sheet_struct_changes.append("Filas o columnas ocultas modificadas.")
            if not cross_format and sa["comments"] != sb["comments"]:
                sheet_struct_changes.append(f"Comentarios: {len(sa['comments'])} → {len(sb['comments'])}")
            if not cross_format and sa["hyperlinks"] != sb["hyperlinks"]:
                sheet_struct_changes.append(f"Hipervínculos: {len(sa['hyperlinks'])} → {len(sb['hyperlinks'])}")

            sheet_diffs.append({
                "name_a": sa["name"],
                "name_b": sb["name"],
                "match_kind": kind,
                "status": "identical" if (not diff_rows and not sheet_struct_changes and kind == "same_name") else "modified",
                "cells_a": sa["cell_count"],
                "cells_b": sb["cell_count"],
                "matching_cells": sheet_matching,
                "modified_cells": len(all_coords) - sheet_matching,
                "diffs": diff_rows,
                "diffs_truncated": (len(all_coords) - sheet_matching) > MAX_DIFF_CELLS_RETURNED,
                "structural_changes": sheet_struct_changes,
            })

        # Account for populated cells in added/removed sheets so they reduce data similarity
        for s_name in added_sheets:
            total_populated_union += by_name_b[s_name]["cell_count"]
            total_modified_cells += by_name_b[s_name]["cell_count"]
        for s_name in removed_sheets:
            total_populated_union += by_name_a[s_name]["cell_count"]
            total_modified_cells += by_name_a[s_name]["cell_count"]

        structural_changes = []
        if added_sheets:
            structural_changes.append(f"Hojas agregadas en B: {', '.join(added_sheets)}")
        if removed_sheets:
            structural_changes.append(f"Hojas eliminadas respecto a A: {', '.join(removed_sheets)}")
        if renamed_sheets:
            structural_changes.append(
                "Hojas renombradas (relación probable): "
                + ", ".join(f"«{r['name_a']}» → «{r['name_b']}» ({r['overlap']}%)" for r in renamed_sheets)
            )
        if reordered_sheets:
            structural_changes.append(f"{len(reordered_sheets)} hoja(s) reordenada(s).")
        if a["defined_names"] != b["defined_names"]:
            structural_changes.append(f"Nombres definidos: {len(a['defined_names'])} → {len(b['defined_names'])}")

        data_avail = total_populated_union > 0
        data_score = round(total_matching_cells / total_populated_union, 6) if data_avail else None
        max_s = max(len(sheets_a), len(sheets_b), 1)
        sheet_score = len(matched_pairs) / max_s
        order_pen = len(reordered_sheets) / max_s
        struct_score = round(max(0.0, 0.70 * sheet_score + 0.30 * (1.0 - order_pen)), 6)

        substantive_identical = (
            total_modified_cells == 0
            and not added_sheets and not removed_sheets and not renamed_sheets and not reordered_sheets
            and all(not sd["structural_changes"] for sd in sheet_diffs)
        )

        limitations = list(dict.fromkeys([*a["limitations"], *b["limitations"]]))
        if cross_format:
            limitations.append(
                f"Comparación entre formatos distintos ({fmt_a} vs {fmt_b}): se comparan hojas, celdas y valores comunes; "
                "las estructuras binarias internas y fórmulas tokenizadas BIFF8 no son directamente equivalentes."
            )

        warnings = []
        if total_cache_only_diffs > 0:
            warnings.append(
                f"Se detectaron {total_cache_only_diffs} celda(s) con la misma fórmula pero distinto valor almacenado en caché (posible caché desactualizado)."
            )
        if a["has_macros"] or b["has_macros"]:
            warnings.append("Se detectó proyecto de macros VBA en al menos un libro. Las macros nunca se ejecutan.")

        return {
            "status": "analyzed",
            "format": "SPREADSHEET" if cross_format else fmt_a,
            "format_a": fmt_a,
            "format_b": fmt_b,
            "cross_format": cross_format,
            "engine_version": DOC_ENGINE_VERSION,
            "summary": {
                "sheets_a": len(sheets_a),
                "sheets_b": len(sheets_b),
                "added_sheets": added_sheets,
                "removed_sheets": removed_sheets,
                "renamed_sheets": renamed_sheets,
                "reordered_sheets": reordered_sheets,
                "populated_cells_union": total_populated_union,
                "matching_cells": total_matching_cells,
                "modified_cells": total_modified_cells,
                "formula_diffs": total_formula_diffs,
                "cache_only_diffs": total_cache_only_diffs,
                "structural_changes_count": len(structural_changes),
                "substantive_identical": substantive_identical,
            },
            "sheets": sheet_diffs,
            "structural_changes": structural_changes,
            "warnings": warnings,
            "metrics": {
                "text": {"available": data_avail, "score": data_score},
                "media": {"available": False, "score": None},
                "structure": {"available": True, "score": struct_score},
            },
            "limitations": limitations,
        }
