"""Extensible deep content extraction and comparison engine."""
import hashlib
import posixpath
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol
from xml.etree.ElementTree import ParseError

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException

from .config import MAX_BYTES, Settings
from .uploads import (
    OOXML,
    ZIP_ERRORS,
    ExpansionBudget,
    UploadError,
    check_budget,
    checked_zip,
    member_chunks,
    office_format,
    safe_members,
    signatures,
)

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

MEDIA_REL_TYPES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/video",
    "http://schemas.microsoft.com/office/2007/relationships/media",
}
NOTES_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
LAYOUT_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
CHART_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
SLIDE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"

MAX_XML_BYTES = 5_000_000
MAX_SLIDES = 500
WORD_TOKEN_RE = re.compile(r"\S+|\s+")
NORM_SPACE_RE = re.compile(r"\s+")


class ContentError(Exception):
    pass


class ContentAnalyzer(Protocol):
    format_name: str
    limitations: tuple[str, ...]

    def extract(self, path: Path, settings: Settings) -> dict: ...
    def compare(self, extracted_a: dict, extracted_b: dict) -> dict: ...


def _normalize_text(text: str) -> str:
    return NORM_SPACE_RE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_rel_target(base_part: str, target: str) -> str | None:
    if not target or target.startswith(("http://", "https://", "mailto:", "file:")):
        return None
    base_dir = posixpath.dirname(base_part)
    joined = target.lstrip("/") if target.startswith("/") else posixpath.join(base_dir, target)
    normalized = posixpath.normpath(joined)
    if normalized.startswith("..") or normalized == ".":
        return None
    return normalized


def _rels_path_for(part_path: str) -> str:
    directory, filename = posixpath.split(part_path)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _read_xml_part(archive, by_name: dict, part_path: str, budget: ExpansionBudget):
    item = by_name.get(part_path)
    if not item:
        return None
    if item.file_size > MAX_XML_BYTES:
        raise ContentError(f"La parte XML interna «{part_path}» excede el tamaño máximo de inspección.")
    raw = bytearray()
    for chunk in member_chunks(archive, item):
        budget.account(len(chunk))
        raw.extend(chunk)
    return fromstring(bytes(raw), forbid_dtd=True)


def _read_rels(archive, by_name: dict, part_path: str, budget: ExpansionBudget) -> dict[str, dict]:
    rels_xml = _read_xml_part(archive, by_name, _rels_path_for(part_path), budget)
    if rels_xml is None:
        return {}
    rels = {}
    for child in rels_xml:
        if child.tag != f"{{{REL_NS}}}Relationship":
            continue
        rid = child.get("Id")
        target = child.get("Target", "")
        rel_type = child.get("Type", "")
        target_mode = child.get("TargetMode", "")
        if not rid or target_mode.lower() == "external":
            continue
        resolved = _resolve_rel_target(part_path, target)
        if resolved:
            rels[rid] = {"type": rel_type, "target": resolved}
    return rels


def _extract_paragraphs_from_txbody(txbody) -> list[str]:
    paragraphs = []
    for para in txbody.findall(f"{{{A_NS}}}p"):
        parts = []
        for elem in para.iter():
            if elem.tag == f"{{{A_NS}}}t" and elem.text:
                parts.append(elem.text)
            elif elem.tag == f"{{{A_NS}}}br":
                parts.append("\n")
            elif elem.tag == f"{{{A_NS}}}tab":
                parts.append("\t")
        joined = "".join(parts).strip()
        if joined:
            paragraphs.append(unicodedata.normalize("NFC", joined))
    return paragraphs


def _hash_member(archive, item, budget: ExpansionBudget) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in member_chunks(archive, item):
        budget.account(len(chunk))
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _word_similarity(text_a: str, text_b: str) -> float:
    norm_a = _normalize_text(text_a).casefold()
    norm_b = _normalize_text(text_b).casefold()
    if not norm_a and not norm_b:
        return 1.0
    if not norm_a or not norm_b:
        return 0.0
    return SequenceMatcher(None, norm_a.split(" "), norm_b.split(" "), autojunk=False).ratio()


def _text_similarity(text_a: str, text_b: str) -> float:
    norm_a = _normalize_text(text_a)
    norm_b = _normalize_text(text_b)
    if not norm_a and not norm_b:
        return 1.0
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    words_a = norm_a.split(" ")
    words_b = norm_b.split(" ")
    word_ratio = SequenceMatcher(None, words_a, words_b, autojunk=False).ratio()
    char_ratio = SequenceMatcher(None, norm_a, norm_b, autojunk=False).ratio()
    return round(0.7 * word_ratio + 0.3 * char_ratio, 6)


def _token_diff(text_a: str, text_b: str) -> dict:
    tokens_a = WORD_TOKEN_RE.findall(unicodedata.normalize("NFC", text_a))
    tokens_b = WORD_TOKEN_RE.findall(unicodedata.normalize("NFC", text_b))
    matcher = SequenceMatcher(None, tokens_a, tokens_b, autojunk=False)
    segments = []
    removed = []
    added = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            chunk = "".join(tokens_a[i1:i2])
            if chunk:
                segments.append({"op": "equal", "text": chunk})
        elif tag == "delete":
            chunk = "".join(tokens_a[i1:i2])
            if chunk:
                segments.append({"op": "delete", "text": chunk})
                if chunk.strip():
                    removed.append(chunk.strip())
        elif tag == "insert":
            chunk = "".join(tokens_b[j1:j2])
            if chunk:
                segments.append({"op": "insert", "text": chunk})
                if chunk.strip():
                    added.append(chunk.strip())
        elif tag == "replace":
            del_chunk = "".join(tokens_a[i1:i2])
            ins_chunk = "".join(tokens_b[j1:j2])
            if del_chunk:
                segments.append({"op": "delete", "text": del_chunk})
                if del_chunk.strip():
                    removed.append(del_chunk.strip())
            if ins_chunk:
                segments.append({"op": "insert", "text": ins_chunk})
                if ins_chunk.strip():
                    added.append(ins_chunk.strip())
    return {
        "text_a": text_a,
        "text_b": text_b,
        "segments": segments,
        "removed_fragments": removed,
        "added_fragments": added,
    }


def _multiset_jaccard(list_a: list[str], list_b: list[str]) -> float | None:
    if not list_a and not list_b:
        return None
    ca, cb = Counter(list_a), Counter(list_b)
    intersection = sum((ca & cb).values())
    union = sum((ca | cb).values())
    return intersection / union if union else 1.0


def _structure_similarity(struct_a: dict, struct_b: dict) -> float:
    scores = []
    # Layout match
    layout_a = struct_a.get("layout")
    layout_b = struct_b.get("layout")
    if layout_a or layout_b:
        scores.append(1.0 if layout_a == layout_b else 0.0)
    # Count-based structural features
    for key in ("shapes", "pictures", "tables", "charts", "groups"):
        va, vb = struct_a.get(key, 0), struct_b.get(key, 0)
        if va == 0 and vb == 0:
            scores.append(1.0)
        else:
            scores.append(min(va, vb) / max(va, vb))
    # Table dimensions match
    ta = struct_a.get("table_dimensions", [])
    tb = struct_b.get("table_dimensions", [])
    if ta or tb:
        ca, cb = Counter(tuple(x) for x in ta), Counter(tuple(x) for x in tb)
        inter = sum((ca & cb).values())
        uni = sum((ca | cb).values())
        scores.append(inter / uni if uni else 1.0)
    return sum(scores) / len(scores) if scores else 1.0


class PptxAnalyzer:
    format_name = "PPTX"
    limitations = (
        "No se renderizan las diapositivas; no se declara equivalencia visual aunque el texto y los recursos coincidan.",
        "Cambios de tipografía, colores, temas, posición de cuadros, animaciones, transiciones u objetos OLE incrustados pueden no reflejarse en la comparación semántica.",
        "Las imágenes y recursos multimedia se comparan por huella SHA-256 de sus bytes internos: una recompresión con pérdida genera un hash distinto aunque luzca similar.",
    )

    def extract(self, path: Path, settings: Settings) -> dict:
        budget = ExpansionBudget(settings.expanded_limit)
        try:
            with checked_zip(path, 4096) as archive:
                entries = safe_members(archive)
                check_budget(entries, budget.remaining)
                by_name = {item.filename: item for item in entries}
                pres_xml = _read_xml_part(archive, by_name, "ppt/presentation.xml", budget)
                if pres_xml is None:
                    raise ContentError("El archivo PPTX no contiene ppt/presentation.xml.")
                pres_rels = _read_rels(archive, by_name, "ppt/presentation.xml", budget)

                # Resolve slides in exact presentation order
                slide_targets = []
                sld_id_lst = pres_xml.find(f"{{{P_NS}}}sldIdLst")
                if sld_id_lst is not None:
                    for sld_id in sld_id_lst.findall(f"{{{P_NS}}}sldId"):
                        rid = sld_id.get(f"{{{R_NS}}}id")
                        rel = pres_rels.get(rid)
                        if rel and rel["target"] in by_name:
                            slide_targets.append(rel["target"])
                if not slide_targets:
                    # Fallback if sldIdLst is absent/empty: check slide relationships sorted naturally
                    slide_targets = sorted(
                        (rel["target"] for rel in pres_rels.values()
                         if rel["type"] == SLIDE_REL_TYPE and rel["target"] in by_name),
                    )
                if len(slide_targets) > MAX_SLIDES:
                    raise ContentError(f"La presentación supera el máximo de {MAX_SLIDES} diapositivas soportadas.")

                # Pre-hash all media assets in ppt/media/
                media_catalog: dict[str, dict] = {}
                for name, item in sorted(by_name.items()):
                    if name.startswith("ppt/media/"):
                        digest, size = _hash_member(archive, item, budget)
                        media_catalog[name] = {
                            "part": name,
                            "name": posixpath.basename(name),
                            "sha256": digest,
                            "size": size,
                            "slides": [],
                        }

                layout_name_cache: dict[str, str] = {}

                def resolve_layout_name(layout_part: str) -> str:
                    if layout_part in layout_name_cache:
                        return layout_name_cache[layout_part]
                    label = posixpath.basename(layout_part)
                    layout_xml = _read_xml_part(archive, by_name, layout_part, budget)
                    if layout_xml is not None:
                        csld = layout_xml.find(f"{{{P_NS}}}cSld")
                        if csld is not None and csld.get("name"):
                            label = _normalize_text(csld.get("name"))
                    layout_name_cache[layout_part] = label
                    return label

                slides = []
                for idx, slide_part in enumerate(slide_targets, start=1):
                    slide_xml = _read_xml_part(archive, by_name, slide_part, budget)
                    if slide_xml is None:
                        continue
                    slide_rels = _read_rels(archive, by_name, slide_part, budget)

                    titles = []
                    body_paragraphs = []
                    table_paragraphs = []
                    table_dimensions = []
                    shape_count = 0
                    picture_count = 0
                    group_count = 0

                    sp_tree = slide_xml.find(f".//{{{P_NS}}}cSld/{{{P_NS}}}spTree")
                    search_root = sp_tree if sp_tree is not None else slide_xml

                    for sp in search_root.iter(f"{{{P_NS}}}sp"):
                        shape_count += 1
                        ph = sp.find(f".//{{{P_NS}}}ph")
                        ph_type = (ph.get("type") or "").lower() if ph is not None else ""
                        txbody = sp.find(f"{{{P_NS}}}txBody")
                        if txbody is not None:
                            paras = _extract_paragraphs_from_txbody(txbody)
                            if ph_type in {"title", "ctrtitle"}:
                                titles.extend(paras)
                            else:
                                body_paragraphs.extend(paras)

                    for _pic in search_root.iter(f"{{{P_NS}}}pic"):
                        picture_count += 1

                    for grp in search_root.iter(f"{{{P_NS}}}grpSp"):
                        if grp is not sp_tree:
                            group_count += 1

                    for tbl in search_root.iter(f"{{{A_NS}}}tbl"):
                        rows = tbl.findall(f"{{{A_NS}}}tr")
                        max_cols = 0
                        for row in rows:
                            cells = row.findall(f"{{{A_NS}}}tc")
                            max_cols = max(max_cols, len(cells))
                            row_texts = []
                            for cell in cells:
                                tx = cell.find(f"{{{A_NS}}}txBody")
                                cell_text = " ".join(_extract_paragraphs_from_txbody(tx)) if tx is not None else ""
                                if cell_text:
                                    row_texts.append(cell_text)
                            if row_texts:
                                table_paragraphs.append(" | ".join(row_texts))
                        table_dimensions.append([len(rows), max_cols])

                    # Media, notes, charts, and layout from slide relationships
                    slide_media = []
                    notes_paragraphs = []
                    chart_hashes = []
                    layout_name = None

                    for rel in slide_rels.values():
                        rel_type = rel["type"]
                        target = rel["target"]
                        if rel_type in MEDIA_REL_TYPES or target.startswith("ppt/media/"):
                            if target in media_catalog:
                                asset = media_catalog[target]
                                if idx not in asset["slides"]:
                                    asset["slides"].append(idx)
                                slide_media.append({
                                    "part": target,
                                    "name": asset["name"],
                                    "sha256": asset["sha256"],
                                    "size": asset["size"],
                                })
                        elif rel_type == NOTES_REL_TYPE and target in by_name:
                            notes_xml = _read_xml_part(archive, by_name, target, budget)
                            if notes_xml is not None:
                                for sp in notes_xml.iter(f"{{{P_NS}}}sp"):
                                    ph = sp.find(f".//{{{P_NS}}}ph")
                                    if ph is not None and ph.get("type") == "sldNum":
                                        continue
                                    txbody = sp.find(f"{{{P_NS}}}txBody")
                                    if txbody is not None:
                                        notes_paragraphs.extend(_extract_paragraphs_from_txbody(txbody))
                        elif rel_type == LAYOUT_REL_TYPE and target in by_name:
                            layout_name = resolve_layout_name(target)
                        elif rel_type == CHART_REL_TYPE and target in by_name:
                            chart_xml = _read_xml_part(archive, by_name, target, budget)
                            if chart_xml is not None:
                                chart_texts = [
                                    _normalize_text(el.text)
                                    for el in chart_xml.iter()
                                    if el.text and el.text.strip()
                                ]
                                chart_hashes.append(_sha256_text("|".join(chart_texts)))

                    all_paragraphs = [*titles, *body_paragraphs, *table_paragraphs]
                    full_text = "\n".join(all_paragraphs)
                    norm_text = _normalize_text(full_text)
                    notes_text = "\n".join(notes_paragraphs)
                    norm_notes = _normalize_text(notes_text)
                    combined_norm = f"{norm_text}\n[NOTES]\n{norm_notes}" if norm_notes else norm_text
                    text_hash = _sha256_text(combined_norm)
                    media_hashes = sorted(m["sha256"] for m in slide_media)
                    structure = {
                        "layout": layout_name,
                        "shapes": shape_count,
                        "pictures": picture_count,
                        "tables": len(table_dimensions),
                        "table_dimensions": sorted(table_dimensions),
                        "charts": len(chart_hashes),
                        "chart_hashes": sorted(chart_hashes),
                        "groups": group_count,
                        "media_count": len(media_hashes),
                        "has_notes": bool(norm_notes),
                    }
                    struct_hash = _sha256_text(str(sorted(structure.items())))
                    content_hash = _sha256_text(f"{text_hash}|{','.join(media_hashes)}|{struct_hash}")

                    slides.append({
                        "index": idx,
                        "part": slide_part,
                        "title": titles[0] if titles else "",
                        "paragraphs": all_paragraphs,
                        "text": full_text,
                        "normalized_text": norm_text,
                        "notes": notes_text,
                        "normalized_notes": norm_notes,
                        "text_hash": text_hash,
                        "media": sorted(slide_media, key=lambda m: (m["sha256"], m["part"])),
                        "media_hashes": media_hashes,
                        "structure": structure,
                        "structure_hash": struct_hash,
                        "content_hash": content_hash,
                    })

                return {
                    "format": "PPTX",
                    "slide_count": len(slides),
                    "slides": slides,
                    "media": sorted(media_catalog.values(), key=lambda m: (m["sha256"], m["part"])),
                }
        except (UploadError, DefusedXmlException, ParseError, *ZIP_ERRORS) as exc:
            raise ContentError(f"No se pudo leer el contenido interno del PPTX: {exc}") from exc

    def compare(self, a: dict, b: dict) -> dict:
        slides_a = a["slides"]
        slides_b = b["slides"]
        matched_a: set[int] = set()
        matched_b: set[int] = set()
        pairs: list[tuple[int, int, str]] = []  # (idx_a_0based, idx_b_0based, match_reason)

        # Pass 1: exact content_hash match (prefer same index first, then closest index)
        by_content_b: dict[str, list[int]] = defaultdict(list)
        for j, sb in enumerate(slides_b):
            by_content_b[sb["content_hash"]].append(j)
        for i, sa in enumerate(slides_a):
            candidates = [j for j in by_content_b.get(sa["content_hash"], []) if j not in matched_b]
            if candidates:
                best_j = min(candidates, key=lambda j: (abs(i - j), j))
                matched_a.add(i)
                matched_b.add(best_j)
                pairs.append((i, best_j, "exact_content"))

        # Pass 2: exact non-empty text_hash match (same text, different media or structure)
        by_text_b: dict[str, list[int]] = defaultdict(list)
        for j, sb in enumerate(slides_b):
            if j not in matched_b and (sb["normalized_text"] or sb["normalized_notes"]):
                by_text_b[sb["text_hash"]].append(j)
        for i, sa in enumerate(slides_a):
            if i in matched_a or not (sa["normalized_text"] or sa["normalized_notes"]):
                continue
            candidates = [j for j in by_text_b.get(sa["text_hash"], []) if j not in matched_b]
            if candidates:
                best_j = min(candidates, key=lambda j: (abs(i - j), j))
                matched_a.add(i)
                matched_b.add(best_j)
                pairs.append((i, best_j, "exact_text"))

        # Pass 3: deterministic similarity-based pairing for remaining slides
        scored_candidates = []
        for i, sa in enumerate(slides_a):
            if i in matched_a:
                continue
            for j, sb in enumerate(slides_b):
                if j in matched_b:
                    continue
                has_text = bool(sa["normalized_text"] or sb["normalized_text"] or
                                sa["normalized_notes"] or sb["normalized_notes"])
                t_sim = _word_similarity(
                    sa["text"] + ("\n" + sa["notes"] if sa["notes"] else ""),
                    sb["text"] + ("\n" + sb["notes"] if sb["notes"] else ""),
                ) if has_text else None
                m_sim = _multiset_jaccard(sa["media_hashes"], sb["media_hashes"])
                if t_sim is not None and m_sim is not None:
                    combined = 0.75 * t_sim + 0.25 * m_sim
                    threshold = 0.50
                elif t_sim is not None:
                    combined = t_sim
                    threshold = 0.50
                elif m_sim is not None:
                    combined = m_sim
                    threshold = 0.50
                else:
                    # Two empty slides with different layout/structure
                    combined = _structure_similarity(sa["structure"], sb["structure"])
                    threshold = 0.60
                if combined >= threshold:
                    scored_candidates.append((-combined, abs(i - j), i, j))
        scored_candidates.sort()
        for neg_score, _dist, i, j in scored_candidates:
            if i in matched_a or j in matched_b:
                continue
            matched_a.add(i)
            matched_b.add(j)
            pairs.append((i, j, "similar"))

        # Build slide-by-slide diff report
        slide_diffs = []
        reordered_slides = []
        replaced_media = []
        structural_changes = []

        # Determine relative order inversions for matched slides
        pairs.sort(key=lambda item: (item[0], item[1]))
        # Longest increasing subsequence on j determines which slides kept relative order
        lis_indices = _lis_positions([j for _, j, _ in pairs])

        for pair_idx, (i, j, reason) in enumerate(pairs):
            sa, sb = slides_a[i], slides_b[j]
            moved = (sa["index"] != sb["index"]) or (pair_idx not in lis_indices)
            text_same = sa["text_hash"] == sb["text_hash"]
            media_same = sa["media_hashes"] == sb["media_hashes"]
            struct_same = sa["structure_hash"] == sb["structure_hash"]

            if text_same and media_same and struct_same:
                status = "reordered" if moved else "identical"
            else:
                status = "modified"

            if moved:
                reordered_slides.append({
                    "slide_a": sa["index"],
                    "slide_b": sb["index"],
                    "title": sa["title"] or sb["title"] or f"Diapositiva {sa['index']}",
                    "content_identical": text_same and media_same and struct_same,
                })

            text_diff = None
            text_sim = 1.0
            if not text_same:
                combined_a = sa["text"] + ("\n[Notas]\n" + sa["notes"] if sa["notes"] else "")
                combined_b = sb["text"] + ("\n[Notas]\n" + sb["notes"] if sb["notes"] else "")
                text_diff = _token_diff(combined_a, combined_b)
                text_sim = _text_similarity(combined_a, combined_b)

            # Slide media diff
            media_diff = None
            if not media_same:
                ca = Counter(sa["media_hashes"])
                cb = Counter(sb["media_hashes"])
                removed_hashes = list((ca - cb).elements())
                added_hashes = list((cb - ca).elements())
                media_diff = {
                    "removed_hashes": removed_hashes,
                    "added_hashes": added_hashes,
                    "replaced": bool(removed_hashes and added_hashes),
                }
                if removed_hashes and added_hashes:
                    replaced_media.append({
                        "slide_a": sa["index"],
                        "slide_b": sb["index"],
                        "title": sa["title"] or sb["title"] or f"Diapositiva {sa['index']}",
                        "removed_hashes": removed_hashes,
                        "added_hashes": added_hashes,
                    })

            # Slide structural diff
            struct_deltas = []
            if not struct_same:
                for key, label in (
                    ("layout", "Diseño de diapositiva"),
                    ("shapes", "Formas"),
                    ("pictures", "Imágenes"),
                    ("tables", "Tablas"),
                    ("table_dimensions", "Dimensiones de tablas"),
                    ("charts", "Gráficos"),
                    ("chart_hashes", "Contenido de gráficos"),
                    ("groups", "Grupos de formas"),
                ):
                    va, vb = sa["structure"][key], sb["structure"][key]
                    if va != vb:
                        if key == "chart_hashes":
                            struct_deltas.append({"field": key, "label": label,
                                                  "before": f"{len(va)} gráfico(s)",
                                                  "after": f"{len(vb)} gráfico(s) (datos modificados)"})
                        else:
                            struct_deltas.append({"field": key, "label": label, "before": va, "after": vb})
                if struct_deltas:
                    structural_changes.append({
                        "slide_a": sa["index"],
                        "slide_b": sb["index"],
                        "changes": struct_deltas,
                    })

            struct_sim = _structure_similarity(sa["structure"], sb["structure"])
            slide_media_sim = _multiset_jaccard(sa["media_hashes"], sb["media_hashes"])
            change_summary = []
            if not text_same:
                change_summary.append("Texto modificado")
            if not media_same:
                change_summary.append("Recursos multimedia modificados o sustituidos")
            if struct_deltas:
                change_summary.append("Cambios estructurales")
            if moved:
                change_summary.append(f"Posición cambiada ({sa['index']} → {sb['index']})")

            slide_diffs.append({
                "status": status,
                "slide_a": sa["index"],
                "slide_b": sb["index"],
                "title": sa["title"] or sb["title"] or f"Diapositiva {sa['index']}",
                "reordered": moved,
                "text_identical": text_same,
                "media_identical": media_same,
                "structure_identical": struct_same,
                "text_similarity": round(text_sim, 4),
                "media_similarity": round(slide_media_sim, 4) if slide_media_sim is not None else None,
                "structure_similarity": round(struct_sim, 4),
                "change_summary": change_summary,
                "text_diff": text_diff,
                "media_diff": media_diff,
                "structural_changes": struct_deltas,
            })

        for i, sa in enumerate(slides_a):
            if i not in matched_a:
                slide_diffs.append({
                    "status": "removed",
                    "slide_a": sa["index"],
                    "slide_b": None,
                    "title": sa["title"] or f"Diapositiva {sa['index']}",
                    "reordered": False,
                    "text_identical": False,
                    "media_identical": False,
                    "structure_identical": False,
                    "text_similarity": 0.0,
                    "media_similarity": 0.0 if sa["media_hashes"] else None,
                    "structure_similarity": 0.0,
                    "change_summary": ["Diapositiva eliminada en el archivo B"],
                    "text_diff": {"text_a": sa["text"], "text_b": "", "segments": [{"op": "delete", "text": sa["text"]}] if sa["text"] else [],
                                  "removed_fragments": [sa["text"]] if sa["text"] else [], "added_fragments": []},
                    "media_diff": {"removed_hashes": sa["media_hashes"], "added_hashes": [], "replaced": False} if sa["media_hashes"] else None,
                    "structural_changes": [],
                })

        for j, sb in enumerate(slides_b):
            if j not in matched_b:
                slide_diffs.append({
                    "status": "added",
                    "slide_a": None,
                    "slide_b": sb["index"],
                    "title": sb["title"] or f"Diapositiva {sb['index']}",
                    "reordered": False,
                    "text_identical": False,
                    "media_identical": False,
                    "structure_identical": False,
                    "text_similarity": 0.0,
                    "media_similarity": 0.0 if sb["media_hashes"] else None,
                    "structure_similarity": 0.0,
                    "change_summary": ["Diapositiva agregada en el archivo B"],
                    "text_diff": {"text_a": "", "text_b": sb["text"], "segments": [{"op": "insert", "text": sb["text"]}] if sb["text"] else [],
                                  "removed_fragments": [], "added_fragments": [sb["text"]] if sb["text"] else []},
                    "media_diff": {"removed_hashes": [], "added_hashes": sb["media_hashes"], "replaced": False} if sb["media_hashes"] else None,
                    "structural_changes": [],
                })

        # Sort slide_diffs cleanly in presentation reading order
        slide_diffs.sort(key=lambda item: (
            item["slide_a"] if item["slide_a"] is not None else 10_000 + (item["slide_b"] or 0),
            item["slide_b"] if item["slide_b"] is not None else 10_000,
        ))

        # Deck-level media comparison by SHA-256
        media_by_hash_a: dict[str, list[dict]] = defaultdict(list)
        media_by_hash_b: dict[str, list[dict]] = defaultdict(list)
        for m in a["media"]:
            media_by_hash_a[m["sha256"]].append(m)
        for m in b["media"]:
            media_by_hash_b[m["sha256"]].append(m)

        media_identical = []
        media_removed = []
        media_added = []
        for digest in sorted(media_by_hash_a.keys() | media_by_hash_b.keys()):
            in_a = media_by_hash_a.get(digest, [])
            in_b = media_by_hash_b.get(digest, [])
            if in_a and in_b:
                media_identical.append({
                    "sha256": digest,
                    "size": in_a[0]["size"],
                    "parts_a": [m["part"] for m in in_a],
                    "parts_b": [m["part"] for m in in_b],
                    "slides_a": sorted({s for m in in_a for s in m["slides"]}),
                    "slides_b": sorted({s for m in in_b for s in m["slides"]}),
                })
            elif in_a:
                for m in in_a:
                    media_removed.append({"sha256": digest, "size": m["size"], "part": m["part"], "slides": m["slides"]})
            else:
                for m in in_b:
                    media_added.append({"sha256": digest, "size": m["size"], "part": m["part"], "slides": m["slides"]})

        # Aggregate dimension metrics
        counts = Counter(item["status"] for item in slide_diffs)
        any_text = any(s["normalized_text"] or s["normalized_notes"] for s in (*slides_a, *slides_b))
        any_media = bool(a["media"] or b["media"])
        total_aligned = len(slide_diffs)

        if any_text and total_aligned:
            text_score = sum(item["text_similarity"] for item in slide_diffs) / total_aligned
        elif not any_text:
            text_score = None
        else:
            text_score = 1.0

        if any_media:
            all_hashes_a = [m["sha256"] for m in a["media"]]
            all_hashes_b = [m["sha256"] for m in b["media"]]
            deck_media_sim = _multiset_jaccard(all_hashes_a, all_hashes_b)
            slide_media_sims = [item["media_similarity"] for item in slide_diffs if item["media_similarity"] is not None]
            placement_sim = sum(slide_media_sims) / len(slide_media_sims) if slide_media_sims else deck_media_sim
            media_score = 0.6 * deck_media_sim + 0.4 * placement_sim
        else:
            media_score = None

        max_slides = max(a["slide_count"], b["slide_count"])
        if max_slides:
            count_ratio = min(a["slide_count"], b["slide_count"]) / max_slides
            order_ratio = len(lis_indices) / len(pairs) if pairs else (1.0 if not max_slides else 0.0)
            avg_slide_struct = sum(item["structure_similarity"] for item in slide_diffs) / total_aligned
            structure_score = 0.35 * count_ratio + 0.30 * order_ratio + 0.35 * avg_slide_struct
        else:
            structure_score = 1.0

        substantive_identical = (
            a["slide_count"] == b["slide_count"]
            and counts["modified"] == 0
            and counts["added"] == 0
            and counts["removed"] == 0
            and counts["reordered"] == 0
            and not media_added
            and not media_removed
            and not structural_changes
        )

        same_text_diff_media_or_struct = (
            any_text
            and counts["added"] == 0
            and counts["removed"] == 0
            and all(item["text_identical"] for item in slide_diffs)
            and (bool(media_added or media_removed or replaced_media) or bool(structural_changes))
        )

        return {
            "status": "analyzed",
            "format": "PPTX",
            "summary": {
                "slide_count_a": a["slide_count"],
                "slide_count_b": b["slide_count"],
                "identical_slides": counts["identical"],
                "reordered_slides": counts["reordered"],
                "modified_slides": counts["modified"],
                "added_slides": counts["added"],
                "removed_slides": counts["removed"],
                "media_count_a": len(a["media"]),
                "media_count_b": len(b["media"]),
                "media_identical": len(media_identical),
                "media_added": len(media_added),
                "media_removed": len(media_removed),
                "media_replaced_on_slides": len(replaced_media),
                "structural_changes_count": len(structural_changes),
                "substantive_identical": substantive_identical,
                "same_text_different_visual_assets": same_text_diff_media_or_struct,
            },
            "metrics": {
                "text": {"available": text_score is not None, "score": round(text_score, 6) if text_score is not None else None},
                "media": {"available": media_score is not None, "score": round(media_score, 6) if media_score is not None else None},
                "structure": {"available": True, "score": round(structure_score, 6)},
            },
            "slides": slide_diffs,
            "reordered": reordered_slides,
            "media": {
                "identical": media_identical,
                "replaced": replaced_media,
                "added": media_added,
                "removed": media_removed,
            },
            "structural_changes": structural_changes,
            "limitations": list(self.limitations),
        }


def _lis_positions(sequence: list[int]) -> set[int]:
    """Return the indices in `sequence` forming a deterministic Longest Increasing Subsequence."""
    n = len(sequence)
    if not n:
        return set()
    dp = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if sequence[j] < sequence[i] and dp[j] + 1 > dp[i]:
                dp[i] = dp[j] + 1
                prev[i] = j
    best_end = max(range(n), key=lambda idx: (dp[idx], -idx))
    result = set()
    cursor = best_end
    while cursor != -1:
        result.add(cursor)
        cursor = prev[cursor]
    return result


CONTENT_ANALYZERS: dict[str, ContentAnalyzer] = {
    "PPTX": PptxAnalyzer(),
}


def detect_verified_format(path: Path, declared_format: str | None, settings: Settings) -> str | None:
    """Confirm actual binary format without trusting file extension alone."""
    sig = signatures(path)
    if sig == "ZIP":
        try:
            return office_format(path, ExpansionBudget(settings.expanded_limit))
        except (UploadError, DefusedXmlException, ParseError, *ZIP_ERRORS):
            return None
    if sig:
        return sig
    return declared_format


def compare_content(record_a: dict, record_b: dict, path_a: Path | None = None,
                    path_b: Path | None = None, settings: Settings | None = None) -> dict:
    """Run extensible format-aware deep content comparison when files and analyzer are available."""
    settings = settings or Settings()
    fmt_a = record_a.get("format")
    fmt_b = record_b.get("format")

    if record_a.get("status") in {"unsupported", "error"} or record_b.get("status") in {"unsupported", "error"}:
        return {
            "status": "unavailable",
            "format": fmt_a if fmt_a == fmt_b else None,
            "reason": "Al menos uno de los archivos tiene una extracción incompleta o un formato no confirmado.",
            "supported_formats": sorted(CONTENT_ANALYZERS),
            "metrics": {
                "text": {"available": False, "score": None},
                "media": {"available": False, "score": None},
                "structure": {"available": False, "score": None},
            },
            "limitations": ["No se realizó comparación profunda porque uno de los archivos presentó errores."],
        }

    if fmt_a != fmt_b or not fmt_a:
        return {
            "status": "unsupported",
            "format": None,
            "reason": f"Los archivos tienen formatos distintos ({fmt_a or 'desconocido'} frente a {fmt_b or 'desconocido'}). La comparación profunda requiere formatos compatibles.",
            "supported_formats": sorted(CONTENT_ANALYZERS),
            "metrics": {
                "text": {"available": False, "score": None},
                "media": {"available": False, "score": None},
                "structure": {"available": False, "score": None},
            },
            "limitations": ["No es válido calcular un porcentaje de similitud de contenido entre formatos incompatibles."],
        }

    analyzer = CONTENT_ANALYZERS.get(fmt_a)
    if analyzer is None:
        return {
            "status": "unsupported",
            "format": fmt_a,
            "reason": f"La comparación profunda de contenido aún no está implementada para el formato {fmt_a}. Solo se compararon identidad binaria y metadatos.",
            "supported_formats": sorted(CONTENT_ANALYZERS),
            "metrics": {
                "text": {"available": False, "score": None},
                "media": {"available": False, "score": None},
                "structure": {"available": False, "score": None},
            },
            "limitations": [
                f"Actualmente el análisis profundo de contenido está habilitado para: {', '.join(sorted(CONTENT_ANALYZERS))}.",
                "No se calcula una puntuación global de similitud documental para formatos sin analizador de contenido.",
            ],
        }

    if not path_a or not path_b or not path_a.is_file() or not path_b.is_file():
        return {
            "status": "unavailable",
            "format": fmt_a,
            "reason": "Los archivos temporales de esta sesión ya no están disponibles (sesión del historial). Vuelve a cargar los archivos para analizar su contenido profundo.",
            "supported_formats": sorted(CONTENT_ANALYZERS),
            "metrics": {
                "text": {"available": False, "score": None},
                "media": {"available": False, "score": None},
                "structure": {"available": False, "score": None},
            },
            "limitations": ["El historial conserva metadatos y huellas SHA-256, pero no guarda copias de los archivos originales."],
        }

    # Re-verify actual binary format (never trust extension alone)
    verified_a = detect_verified_format(path_a, fmt_a, settings)
    verified_b = detect_verified_format(path_b, fmt_b, settings)
    if verified_a != fmt_a or verified_b != fmt_b:
        return {
            "status": "error",
            "format": fmt_a,
            "reason": "La firma binaria o estructura interna de al menos un archivo no coincide con el formato esperado.",
            "supported_formats": sorted(CONTENT_ANALYZERS),
            "metrics": {
                "text": {"available": False, "score": None},
                "media": {"available": False, "score": None},
                "structure": {"available": False, "score": None},
            },
            "limitations": ["La comparación de contenido se detuvo para evitar resultados engañosos."],
        }

    try:
        extracted_a = analyzer.extract(path_a, settings)
        extracted_b = analyzer.extract(path_b, settings)
        result = analyzer.compare(extracted_a, extracted_b)
        result["supported_formats"] = sorted(CONTENT_ANALYZERS)
        return result
    except ContentError as exc:
        return {
            "status": "error",
            "format": fmt_a,
            "reason": str(exc),
            "supported_formats": sorted(CONTENT_ANALYZERS),
            "metrics": {
                "text": {"available": False, "score": None},
                "media": {"available": False, "score": None},
                "structure": {"available": False, "score": None},
            },
            "limitations": ["Un archivo dañado o fuera de límites impidió completar la comparación profunda."],
        }
