"""Universal capability-driven Duel orchestrator, N-file matrix/grouping engine,
and cancellable batch comparison runner for La Orca."""

from __future__ import annotations

import hmac
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from .image_analysis import (
    IMAGE_ENGINE_VERSION,
    compare_images,
    extract_image_profile,
    hamming_distance_hex,
)
from .similarity import ALGORITHM_VERSION as SIMILARITY_VERSION

CONTENT_VERSION = SIMILARITY_VERSION

ORCHESTRATOR_VERSION = "1.1"
IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
SPREADSHEET_FORMATS = {"XLS", "XLSX"}
DOCUMENT_FORMATS = {"PPTX", "PDF", "DOCX", "XLS", "XLSX"}
PERCEPTUAL_HASH_THRESHOLD = 8  # Hamming distance <= 8/64 bits marks perceptual candidate


def detect_file_capabilities(record: dict) -> list[str]:
    """Determine real capabilities based on validated binary format (never extension alone)."""
    caps = ["binary_identity", "metadata"]
    fmt = record.get("format")
    status = record.get("status")
    if status == "error" and not fmt:
        return caps
    if fmt == "PPTX":
        caps.append("pptx_content")
    elif fmt == "PDF":
        caps.append("pdf_content")
    elif fmt == "DOCX":
        caps.append("docx_content")
    elif fmt in SPREADSHEET_FORMATS:
        caps.append("spreadsheet_content")
    if fmt in IMAGE_FORMATS:
        caps.extend(["image_pixels", "image_perceptual"])
    if fmt == "JPEG":
        caps.append("jpeg_structure")
    elif fmt == "PNG":
        caps.append("png_structure")
    return caps


def detect_pair_capabilities(record_a: dict, record_b: dict) -> dict:
    """Determine which specialized comparators apply to a pair of files."""
    caps_a = set(detect_file_capabilities(record_a))
    caps_b = set(detect_file_capabilities(record_b))
    shared = caps_a & caps_b
    fmt_a = record_a.get("format")
    fmt_b = record_b.get("format")
    is_spreadsheet_pair = fmt_a in SPREADSHEET_FORMATS and fmt_b in SPREADSHEET_FORMATS
    is_doc_pair = (
        (fmt_a == "PPTX" and fmt_b == "PPTX")
        or (fmt_a == "PDF" and fmt_b == "PDF")
        or (fmt_a == "DOCX" and fmt_b == "DOCX")
        or is_spreadsheet_pair
    )

    return {
        "format_a": fmt_a,
        "format_b": fmt_b,
        "file_a": sorted(caps_a),
        "file_b": sorted(caps_b),
        "shared": sorted(shared),
        "is_image_pair": fmt_a in IMAGE_FORMATS and fmt_b in IMAGE_FORMATS,
        "is_pptx_pair": fmt_a == "PPTX" and fmt_b == "PPTX",
        "is_pdf_pair": fmt_a == "PDF" and fmt_b == "PDF",
        "is_docx_pair": fmt_a == "DOCX" and fmt_b == "DOCX",
        "is_spreadsheet_pair": is_spreadsheet_pair,
        "is_document_pair": is_doc_pair,
        "is_mixed_formats": fmt_a != fmt_b,
        "applicable_dimensions": {
            "binary_identity": True,
            "metadata": True,
            "pptx_content": "pptx_content" in shared,
            "pdf_content": "pdf_content" in shared,
            "docx_content": "docx_content" in shared,
            "spreadsheet_content": "spreadsheet_content" in shared,
            "image_pixels": "image_pixels" in shared,
            "image_perceptual": "image_perceptual" in shared,
            "jpeg_structure": "jpeg_structure" in shared,
            "png_structure": "png_structure" in shared,
        },
    }


def _dimension_entry(
    dim_id: str,
    label: str,
    status: str,
    algorithm: str,
    version: str,
    *,
    parameters: dict | None = None,
    metrics: dict | None = None,
    evidence: Any = None,
    warnings: list[str] | None = None,
    limitations: list[str] | None = None,
    summary: str = "",
) -> dict:
    return {
        "id": dim_id,
        "label": label,
        "status": status,  # matched | different | not_applicable | unsupported | pending | failed | partial
        "algorithm": algorithm,
        "version": version,
        "parameters": parameters or {},
        "summary": summary,
        "metrics": metrics or {},
        "evidence": evidence,
        "warnings": warnings or [],
        "limitations": limitations or [],
    }


def orchestrate_universal_dimensions(
    first: dict,
    second: dict,
    binary_identity: dict,
    content_comparison: dict,
    metadata_comparison: dict,
    similarity: dict,
    *,
    path_a: Path | None = None,
    path_b: Path | None = None,
    profile_a: dict | None = None,
    profile_b: dict | None = None,
) -> dict:
    """Execute applicable specialized comparators and build typed dimension results."""
    caps = detect_pair_capabilities(first, second)
    applicable = caps["applicable_dimensions"]
    executed: list[str] = ["binary_identity", "metadata"]
    dimensions: list[dict] = []

    # 1. Binary identity dimension
    bin_status = (
        "matched" if binary_identity.get("status") == "identical"
        else "different" if binary_identity.get("status") == "different"
        else "pending"
    )
    dimensions.append(_dimension_entry(
        "binary_identity",
        "Identidad binaria",
        bin_status,
        "SHA-256",
        "1.0",
        metrics={
            "identical_bytes": binary_identity.get("identical_bytes"),
            "size_a": binary_identity.get("size_a"),
            "size_b": binary_identity.get("size_b"),
            "size_delta": binary_identity.get("size_delta"),
        },
        summary=binary_identity.get("verdict", ""),
        limitations=binary_identity.get("notes", []),
    ))

    # 2. Metadata dimension
    desc_summary = metadata_comparison.get("descriptive_summary", {})
    meta_diffs = desc_summary.get("different", 0) + desc_summary.get("only_a", 0) + desc_summary.get("only_b", 0)
    meta_total = metadata_comparison.get("descriptive_count", 0)
    meta_status = "matched" if meta_diffs == 0 else "different"
    dimensions.append(_dimension_entry(
        "metadata",
        "Metadatos",
        meta_status,
        "ExifTool-semantic",
        "1.0",
        metrics={
            "descriptive_count": meta_total,
            "technical_count": metadata_comparison.get("technical_count", 0),
            "descriptive_summary": desc_summary,
        },
        summary=(
            f"Coinciden los metadatos descriptivos examinados ({desc_summary.get('matching', 0)})."
            if meta_status == "matched"
            else f"Se detectaron {meta_diffs} diferencia(s) en metadatos descriptivos."
        ),
        limitations=[metadata_comparison.get("note", "")],
    ))

    # 3. Document content dimension (PPTX, PDF, DOCX, XLS/XLSX)
    doc_dim_map = [
        ("pptx_content", "Contenido documental PPTX", "pptx-deep-compare"),
        ("pdf_content", "Contenido documental PDF", "pdf-deep-compare"),
        ("docx_content", "Contenido documental DOCX", "docx-deep-compare"),
        ("spreadsheet_content", "Contenido de hojas de cálculo (XLS/XLSX)", "spreadsheet-deep-compare"),
    ]
    matched_doc_dim = next((entry for entry in doc_dim_map if applicable.get(entry[0])), None)
    if matched_doc_dim:
        dim_id, dim_label, algo_name = matched_doc_dim
        executed.append(dim_id)
        c_status = content_comparison.get("status")
        if c_status == "analyzed":
            dim_status = "matched" if content_comparison["summary"]["substantive_identical"] else "different"
            dimensions.append(_dimension_entry(
                dim_id,
                dim_label,
                dim_status,
                algo_name,
                CONTENT_VERSION,
                metrics={
                    "similarity_overall": similarity.get("overall"),
                    **{k: v for k, v in content_comparison["summary"].items() if not isinstance(v, (list, dict))},
                },
                summary=similarity.get("label", ""),
                warnings=similarity.get("warnings", []),
                limitations=content_comparison.get("limitations", []),
            ))
        else:
            dimensions.append(_dimension_entry(
                dim_id,
                dim_label,
                "failed" if c_status == "error" else "pending",
                algo_name,
                CONTENT_VERSION,
                summary=content_comparison.get("reason", f"No se pudo analizar {dim_label}."),
                limitations=content_comparison.get("limitations", []),
            ))
    else:
        dimensions.append(_dimension_entry(
            "document_content",
            "Contenido documental (PDF/DOCX/PPTX/XLS/XLSX)",
            "not_applicable",
            "document-deep-compare",
            CONTENT_VERSION,
            summary="No aplicable: requiere dos documentos de formato compatible (PPTX, PDF, DOCX o XLS/XLSX).",
        ))

    # 4. Image comparison dimensions (pixels, visual normalized, perceptual, JPEG/PNG structure)
    image_comparison = None
    structural_comparison = None
    if applicable["image_pixels"]:
        executed.extend(["image_pixels", "image_perceptual"])
        if applicable["jpeg_structure"]:
            executed.append("jpeg_structure")
        if applicable["png_structure"]:
            executed.append("png_structure")
        try:
            image_comparison = compare_images(
                path_a, path_b, profile_a=profile_a, profile_b=profile_b
            )
        except Exception as exc:
            image_comparison = {
                "engine_version": IMAGE_ENGINE_VERSION,
                "status": "failed",
                "reason": f"Error controlado durante el análisis de imágenes: {exc}",
            }

        img_status = image_comparison.get("status")
        if img_status in {"analyzed", "partial"}:
            ds = image_comparison.get("decoded_samples", {})
            vn = image_comparison.get("visual_normalized", {})
            pc = image_comparison.get("perceptual", {})
            structural_comparison = image_comparison.get("structural_comparison")

            dimensions.append(_dimension_entry(
                "image_decoded_samples",
                "Equivalencia de muestras decodificadas",
                ds.get("status", "partial"),
                "exact-sample-compare",
                IMAGE_ENGINE_VERSION,
                metrics={k: v for k, v in ds.items() if k not in {"status", "reason"}},
                summary=ds.get("reason", ""),
                limitations=[
                    "Compara muestras decodificadas tal cual, sin rotación EXIF ni conversión ICC.",
                    "Nunca redimensiona imágenes para declarar equivalencia exacta.",
                ],
            ))
            dimensions.append(_dimension_entry(
                "image_visual_normalized",
                "Equivalencia visual normalizada",
                vn.get("status", "partial"),
                "srgb-rgba-normalized",
                IMAGE_ENGINE_VERSION,
                parameters=vn.get("policy", {}),
                metrics={
                    "exact_match": vn.get("exact_match"),
                    "different_pixels": vn.get("different_pixels"),
                    "different_percent": vn.get("different_percent"),
                    "max_channel_diff": vn.get("max_channel_diff"),
                    "alpha_only_diff": vn.get("alpha_only_diff"),
                },
                summary=vn.get("reason", ""),
                warnings=[vn["policy"]["lossy_note"]] if vn.get("policy", {}).get("lossy_note") else [],
                limitations=[vn.get("policy", {}).get("caveat", "")],
            ))
            dimensions.append(_dimension_entry(
                "image_perceptual",
                "Similitud perceptual",
                pc.get("status", "partial"),
                "ssim-phash-dhash",
                IMAGE_ENGINE_VERSION,
                parameters=pc.get("methodology", {}),
                metrics={
                    "band": pc.get("band"),
                    "ssim": pc.get("ssim"),
                    "phash_distance": pc.get("phash_distance"),
                    "dhash_distance": pc.get("dhash_distance"),
                    "mae": pc.get("mae"),
                    "mse": pc.get("mse"),
                    "psnr": pc.get("psnr"),
                },
                summary=pc.get("band") or pc.get("note", ""),
                limitations=pc.get("methodology", {}).get("limitations", []),
            ))
            if structural_comparison:
                struct_fmt = structural_comparison.get("format") or "IMG"
                dimensions.append(_dimension_entry(
                    "image_structure",
                    f"Inspección estructural {struct_fmt}" if struct_fmt in {"JPEG", "PNG"} else "Inspección estructural de imagen",
                    structural_comparison.get("status", "not_applicable"),
                    f"{struct_fmt.lower()}-structure-parser",
                    IMAGE_ENGINE_VERSION,
                    metrics={
                        "identical_structure": structural_comparison.get("identical_structure"),
                        "differences_count": len(structural_comparison.get("differences", [])),
                    },
                    evidence=structural_comparison.get("differences"),
                    warnings=structural_comparison.get("warnings", []),
                    limitations=structural_comparison.get("limitations", []),
                    summary=(
                        structural_comparison.get("reason")
                        or ("Estructura idéntica." if structural_comparison.get("identical_structure")
                            else f"{len(structural_comparison.get('differences', []))} diferencia(s) estructural(es) detectada(s).")
                    ),
                ))
        else:
            # failed, unsupported (e.g. APNG), or pending
            dim_st = img_status if img_status in {"failed", "unsupported", "pending"} else "failed"
            structural_comparison = image_comparison.get("structural_comparison")
            dimensions.append(_dimension_entry(
                "image_pixels",
                "Comparación de píxeles e imagen",
                dim_st,
                "image-analysis",
                IMAGE_ENGINE_VERSION,
                summary=image_comparison.get("reason", "No se pudo completar el análisis de imagen."),
            ))
            if structural_comparison:
                dimensions.append(_dimension_entry(
                    "image_structure",
                    "Inspección estructural de imagen",
                    structural_comparison.get("status", "partial"),
                    "image-structure-parser",
                    IMAGE_ENGINE_VERSION,
                    warnings=structural_comparison.get("warnings", []),
                    summary=structural_comparison.get("reason", "Inspección estructural completada con limitaciones."),
                ))
    else:
        dimensions.append(_dimension_entry(
            "image_pixels",
            "Comparación de píxeles y similitud perceptual",
            "not_applicable",
            "image-analysis",
            IMAGE_ENGINE_VERSION,
            summary="No aplicable: requiere dos imágenes compatibles (JPEG, PNG o WebP).",
        ))

    # Determine overall completeness without ever hiding partial/failed states
    statuses = {d["status"] for d in dimensions}
    if "failed" in statuses:
        completeness = "partial"
    elif "pending" in statuses or "partial" in statuses:
        completeness = "partial"
    else:
        completeness = "complete"

    return {
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "capabilities": caps,
        "executed_comparators": executed,
        "completeness": completeness,
        "dimensions": dimensions,
        "image_comparison": image_comparison,
        "structural_comparison": structural_comparison,
    }


def verify_cleaned_image(before_record: dict, after_record: dict, source_path: Path, target_path: Path) -> dict:
    """Run real post-cleanup verification for JPEG/PNG images (metadata, structure, pixels, perceptual)."""
    img_cmp = compare_images(source_path, target_path)
    bin_same = (
        isinstance(before_record.get("sha256"), str)
        and isinstance(after_record.get("sha256"), str)
        and hmac.compare_digest(before_record["sha256"].lower(), after_record["sha256"].lower())
    )
    if img_cmp.get("status") != "analyzed":
        return {
            "verdict_code": "unverifiable",
            "verdict": "No fue posible comprobar la equivalencia visual tras la limpieza.",
            "binary_changed": not bin_same,
            "pixels_preserved": None,
            "reason": img_cmp.get("reason"),
            "image_comparison": img_cmp,
        }

    ds = img_cmp.get("decoded_samples", {})
    vn = img_cmp.get("visual_normalized", {})
    st = img_cmp.get("structural_comparison") or {}
    pixels_exact = bool(ds.get("exact_match") is True or vn.get("exact_match") is True)

    if pixels_exact:
        only_meta_struct = (
            not st.get("coding_parameters_changed")
            and not st.get("scan_data_changed")
            and not st.get("idat_stream_changed")
        )
        if only_meta_struct:
            code = "metadata_and_structure_only"
            verdict = "Solo cambiaron metadatos y estructuras asociadas; los píxeles se conservaron intactos."
        else:
            code = "binary_changed_pixels_preserved"
            verdict = "Cambió la representación binaria, pero se verificó la conservación exacta de los píxeles."
    else:
        code = "pixels_modified"
        diff_px = vn.get("different_pixels")
        verdict = (
            f"Se modificaron {diff_px:,} píxeles durante la operación de limpieza."
            if isinstance(diff_px, int)
            else "Se detectaron modificaciones en los píxeles tras la limpieza."
        )

    return {
        "verdict_code": code,
        "verdict": verdict,
        "binary_changed": not bin_same,
        "pixels_preserved": pixels_exact,
        "decoded_samples_match": ds.get("exact_match"),
        "visual_normalized_match": vn.get("exact_match"),
        "different_pixels": vn.get("different_pixels"),
        "ssim": img_cmp.get("perceptual", {}).get("ssim"),
        "structural_differences": st.get("differences", []),
        "image_comparison": img_cmp,
    }


# ---------------------------------------------------------------------------
# N-File Duel Set Analysis, Matrix, Grouping & Cancellable Batch Runner
# ---------------------------------------------------------------------------

def build_duel_matrix(
    records: list[dict],
    source_paths: dict[str, Path],
    image_profiles: dict[str, dict],
    deep_cache: dict[tuple[str, str], dict],
    *,
    reference_id: str | None = None,
) -> dict:
    """Build staged N-file analysis: inventory, binary duplicates, equivalent content,
    non-transitive perceptual candidates, and N×N (or 1×N reference) comparison matrix."""

    # Stage 1: Reusable individual image profiles (decode each image at most once!)
    for rec in records:
        fid = rec["id"]
        if rec.get("format") in IMAGE_FORMATS and fid not in image_profiles:
            p = source_paths.get(fid)
            if p and p.is_file():
                image_profiles[fid] = extract_image_profile(p, expected_format=rec.get("format"))

    # Inventory summary
    format_counts: dict[str, int] = defaultdict(int)
    inventory = []
    for rec in records:
        fid = rec["id"]
        fmt = rec.get("format") or "Sin confirmar"
        format_counts[fmt] += 1
        prof = image_profiles.get(fid)
        inventory.append({
            "id": fid,
            "name": rec["name"],
            "format": rec.get("format"),
            "size": rec.get("size"),
            "sha256": rec.get("sha256"),
            "status": rec.get("status"),
            "capabilities": detect_file_capabilities(rec),
            "image_summary": {
                "width": prof.get("width"),
                "height": prof.get("height"),
                "mode": prof.get("mode"),
                "bit_depth": prof.get("bit_depth"),
                "orientation": prof.get("orientation"),
                "has_transparency": prof.get("has_transparency"),
                "icc_present": prof.get("icc", {}).get("present"),
                "is_apng": prof.get("is_apng"),
                "dhash": prof.get("dhash"),
                "phash": prof.get("phash"),
                "error": prof.get("error"),
            } if prof else None,
        })

    # Stage 2: Binary duplicates (exact SHA-256 groups, no redundant comparisons needed)
    by_sha: dict[str, list[str]] = defaultdict(list)
    for rec in records:
        if rec.get("sha256"):
            by_sha[rec["sha256"].lower()].append(rec["id"])
    binary_duplicate_groups = [
        {"sha256": sha, "file_ids": ids, "count": len(ids)}
        for sha, ids in by_sha.items() if len(ids) >= 2
    ]

    # Stage 3: Equivalent visual content groups (same visual_fingerprint, different SHA-256)
    by_visual: dict[str, list[str]] = defaultdict(list)
    for rec in records:
        prof = image_profiles.get(rec["id"])
        if prof and prof.get("status") == "ok" and prof.get("visual_fingerprint") and not prof.get("is_apng"):
            key = f"{prof['normalized_width']}x{prof['normalized_height']}:{prof['visual_fingerprint']}"
            by_visual[key].append(rec["id"])
    equivalent_content_groups = [
        {"fingerprint": key.split(":", 1)[1][:16], "dimensions": key.split(":", 1)[0], "file_ids": ids, "count": len(ids)}
        for key, ids in by_visual.items()
        if len(ids) >= 2 and len({next(r["sha256"] for r in records if r["id"] == fid) for fid in ids}) > 1
    ]

    # Stage 4: Pairwise matrix (full N×N or 1×(N-1) if reference_id is provided)
    id_set = {r["id"] for r in records}
    if reference_id and reference_id not in id_set:
        reference_id = None

    pairs_to_emit: list[tuple[dict, dict]] = []
    if reference_id:
        ref_rec = next(r for r in records if r["id"] == reference_id)
        for other in records:
            if other["id"] != reference_id:
                pairs_to_emit.append((ref_rec, other))
    else:
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                pairs_to_emit.append((records[i], records[j]))

    cells = []
    perceptual_candidate_pairs = []
    related_document_pairs = []
    counts = {
        "total_pairs": len(pairs_to_emit),
        "identical": 0,
        "equivalent": 0,
        "similar": 0,
        "different": 0,
        "pending": 0,
        "not_applicable": 0,
        "error": 0,
    }

    for rec_a, rec_b in pairs_to_emit:
        fa, fb = rec_a["id"], rec_b["id"]
        pair_key = (fa, fb) if fa < fb else (fb, fa)
        caps = detect_pair_capabilities(rec_a, rec_b)
        cached = deep_cache.get(pair_key)

        same_sha = (
            isinstance(rec_a.get("sha256"), str)
            and isinstance(rec_b.get("sha256"), str)
            and hmac.compare_digest(rec_a["sha256"].lower(), rec_b["sha256"].lower())
        )
        prof_a = image_profiles.get(fa)
        prof_b = image_profiles.get(fb)

        cell_state = "pending"
        label = "Pendiente"
        detail = ""
        metrics: dict[str, Any] = {}

        if rec_a.get("status") == "error" or rec_b.get("status") == "error":
            cell_state = "error"
            label = "Error"
            detail = "Uno o ambos archivos presentaron error de análisis."
        elif same_sha:
            cell_state = "identical"
            label = "SHA ="
            detail = "Idénticos a nivel de bytes (SHA-256 coincidente)."
        elif caps["is_image_pair"]:
            if (prof_a and prof_a.get("status") == "error") or (prof_b and prof_b.get("status") == "error"):
                cell_state = "error"
                label = "Error img"
                detail = (prof_a.get("error") if prof_a and prof_a.get("error") else "") or (
                    prof_b.get("error") if prof_b and prof_b.get("error") else "Error al decodificar imagen."
                )
            elif cached and cached.get("image_comparison", {}).get("status") == "analyzed":
                ic = cached["image_comparison"]
                vn = ic["visual_normalized"]
                pc = ic["perceptual"]
                metrics = {
                    "ssim": pc.get("ssim"),
                    "phash_distance": pc.get("phash_distance"),
                    "dhash_distance": pc.get("dhash_distance"),
                    "different_pixels": vn.get("different_pixels"),
                }
                if vn.get("exact_match"):
                    cell_state = "equivalent"
                    label = "Píxeles ="
                    detail = "Bytes distintos, píxeles equivalentes bajo política normalizada."
                elif pc.get("ssim") is not None and pc["ssim"] >= 0.90:
                    cell_state = "similar"
                    label = f"SSIM {pc['ssim']:.3f}"
                    detail = pc.get("band", "Similitud perceptual")
                else:
                    cell_state = "different"
                    label = f"SSIM {pc['ssim']:.2f}" if pc.get("ssim") is not None else "Diferente"
                    detail = vn.get("reason", "Imágenes diferentes.")
            elif prof_a and prof_b and prof_a.get("status") == "ok" and prof_b.get("status") == "ok":
                if prof_a.get("is_apng") or prof_b.get("is_apng"):
                    cell_state = "not_applicable"
                    label = "APNG"
                    detail = "APNG animado no soportado para equivalencia completa."
                elif (
                    prof_a.get("visual_fingerprint")
                    and prof_a.get("visual_fingerprint") == prof_b.get("visual_fingerprint")
                    and prof_a.get("normalized_width") == prof_b.get("normalized_width")
                    and prof_a.get("normalized_height") == prof_b.get("normalized_height")
                ):
                    cell_state = "equivalent"
                    label = "Píxeles ="
                    detail = "Bytes distintos, misma huella visual normalizada."
                else:
                    p_dist = hamming_distance_hex(prof_a.get("phash"), prof_b.get("phash"))
                    d_dist = hamming_distance_hex(prof_a.get("dhash"), prof_b.get("dhash"))
                    metrics = {"phash_distance": p_dist, "dhash_distance": d_dist}
                    if (p_dist is not None and p_dist <= PERCEPTUAL_HASH_THRESHOLD) or (
                        d_dist is not None and d_dist <= PERCEPTUAL_HASH_THRESHOLD
                    ):
                        cell_state = "similar"
                        label = f"Candidato (pH {p_dist})"
                        detail = "Candidato perceptual por pHash/dHash; abre el duelo para métricas exactas."
                        perceptual_candidate_pairs.append({
                            "file_a_id": fa, "file_b_id": fb,
                            "phash_distance": p_dist, "dhash_distance": d_dist,
                        })
                    else:
                        cell_state = "pending"
                        label = "Pendiente"
                        detail = "Comparación profunda de píxeles bajo demanda."
            else:
                cell_state = "pending"
                label = "Pendiente"
                detail = "Aporta los archivos originales para comparar píxeles."
        elif caps["is_document_pair"]:
            doc_fmt_label = (
                "XLS/XLSX" if caps["is_spreadsheet_pair"]
                else (rec_a.get("format") or "documento")
            )
            if cached and cached.get("content_comparison", {}).get("status") == "error":
                cell_state = "error"
                label = "Error doc"
                detail = cached["content_comparison"].get("reason", "Error al analizar documento.")
            elif cached and cached.get("similarity", {}).get("overall_available"):
                pct = cached["similarity"]["overall"]
                metrics = {"similarity_overall": pct}
                if cached["content_comparison"]["summary"]["substantive_identical"]:
                    cell_state = "equivalent"
                    label = "Contenido ="
                    detail = f"Contenido {doc_fmt_label} equivalente (solo difieren metadatos/empaquetado)."
                elif pct >= 75.0:
                    cell_state = "similar"
                    label = f"{pct:g}%"
                    detail = cached["similarity"]["label"]
                    related_document_pairs.append({"file_a_id": fa, "file_b_id": fb, "similarity": pct})
                else:
                    cell_state = "different"
                    label = f"{pct:g}%"
                    detail = cached["similarity"]["label"]
            else:
                cell_state = "pending"
                label = "Pendiente"
                detail = f"Comparación documental {doc_fmt_label} bajo demanda."
        else:
            # Mixed or unsupported formats for deep content
            cell_state = "not_applicable"
            label = "Meta/Bin"
            detail = (
                f"Formatos distintos ({rec_a.get('format') or '?'} vs {rec_b.get('format') or '?'}); "
                "solo aplican identidad binaria y metadatos."
            )

        counts[cell_state] += 1
        cells.append({
            "file_a_id": fa,
            "file_b_id": fb,
            "state": cell_state,
            "label": label,
            "detail": detail,
            "deep_evaluated": cached is not None or same_sha,
            "capabilities": caps["shared"],
            "metrics": metrics,
        })

    # Non-transitive perceptual neighborhoods (explicitly per-file neighbors, never transitive closure)
    perceptual_neighbors: dict[str, list[dict]] = defaultdict(list)
    for pair in perceptual_candidate_pairs:
        perceptual_neighbors[pair["file_a_id"]].append({
            "file_id": pair["file_b_id"],
            "phash_distance": pair["phash_distance"],
            "dhash_distance": pair["dhash_distance"],
        })
        perceptual_neighbors[pair["file_b_id"]].append({
            "file_id": pair["file_a_id"],
            "phash_distance": pair["phash_distance"],
            "dhash_distance": pair["dhash_distance"],
        })

    return {
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "mode": "reference" if reference_id else "matrix",
        "reference_id": reference_id,
        "file_count": len(records),
        "format_counts": dict(format_counts),
        "inventory": inventory,
        "counts": counts,
        "groups": {
            "binary_duplicates": binary_duplicate_groups,
            "equivalent_content": equivalent_content_groups,
            "perceptual_candidates": perceptual_candidate_pairs,
            "perceptual_neighbors": dict(perceptual_neighbors),
            "related_documents": related_document_pairs,
            "non_transitivity_note": (
                "Las similitudes perceptuales se reportan como pares y vecindades explícitas sin asumir transitividad: "
                "que A sea similar a B y B a C no implica que A sea similar a C."
            ),
        },
        "cells": cells,
        "methodology": {
            "strategy": "Estrategia escalonada: análisis individual reutilizable, hashes SHA-256, perfiles estructurales/perceptuales y comparación profunda bajo demanda.",
            "possible_pairs": len(records) * (len(records) - 1) // 2,
            "evaluated_deep_pairs": sum(1 for c in cells if c["deep_evaluated"]),
            "pending_pairs": counts["pending"],
            "not_applicable_pairs": counts["not_applicable"],
        },
    }


class DuelBatchJob:
    """Tracks progress and safe cancellation for N-file batch deep comparisons."""

    def __init__(self, total: int):
        self.total = total
        self.processed = 0
        self.state = "running"  # running | complete | cancelled | failed
        self.current: str | None = None
        self.error: str | None = None
        self.cancel_event = threading.Event()
        self.lock = threading.Lock()

    def cancel(self):
        self.cancel_event.set()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "processed": self.processed,
                "total": self.total,
                "current": self.current,
                "cancelled": self.cancel_event.is_set(),
                "error": self.error,
            }
