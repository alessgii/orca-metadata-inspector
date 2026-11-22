"""Deterministic, explainable document similarity scoring engine."""

ALGORITHM_VERSION = "1.0"
GENERAL_FIELDS = {"sha256", "size", "format"}

# Documented base weights per supported format.
WEIGHT_PROFILES = {
    "PPTX": {
        "text": 0.50,
        "media": 0.25,
        "structure": 0.15,
        "metadata": 0.10,
    },
    "PDF": {
        "text": 0.55,
        "media": 0.15,
        "structure": 0.20,
        "metadata": 0.10,
    },
    "DOCX": {
        "text": 0.55,
        "media": 0.15,
        "structure": 0.20,
        "metadata": 0.10,
    },
    "XLSX": {
        "text": 0.65,  # Populated cells & formulas (never inflated by empty cells)
        "media": 0.0,
        "structure": 0.25,
        "metadata": 0.10,
    },
    "XLS": {
        "text": 0.65,
        "media": 0.0,
        "structure": 0.25,
        "metadata": 0.10,
    },
    "SPREADSHEET": {
        "text": 0.65,
        "media": 0.0,
        "structure": 0.25,
        "metadata": 0.10,
    },
}

DIMENSION_LABELS = {
    "text": "Contenido textual",
    "media": "Recursos multimedia",
    "structure": "Estructura",
    "metadata": "Metadatos descriptivos",
}


def _pct(score: float, has_difference: bool = False) -> float:
    raw = round(max(0.0, min(1.0, score)) * 100.0, 1)
    if has_difference and raw >= 100.0:
        return 99.9
    return raw


def _metadata_dimension(rows: list[dict]) -> dict:
    """Compute similarity strictly over non-technical, comparable descriptive metadata."""
    descriptive = [
        row for row in rows
        if row["field"] not in GENERAL_FIELDS and not row.get("technical")
    ]
    comparable = [row for row in descriptive if row["status"] != "not_comparable"]
    matching = sum(1 for row in comparable if row["status"] == "matching")
    different = sum(1 for row in comparable if row["status"] == "different")
    only_a = sum(1 for row in comparable if row["status"] == "only_a")
    only_b = sum(1 for row in comparable if row["status"] == "only_b")
    not_comparable = len(descriptive) - len(comparable)
    total = len(comparable)
    if total == 0:
        return {
            "id": "metadata",
            "label": DIMENSION_LABELS["metadata"],
            "available": False,
            "score": None,
            "percentage": None,
            "reason": "No se detectaron campos descriptivos comparables en ninguno de los dos archivos.",
            "counts": {
                "matching": 0, "different": 0, "only_a": 0, "only_b": 0,
                "not_comparable": not_comparable, "total_comparable": 0,
            },
        }
    score = matching / total
    has_diff = (different + only_a + only_b) > 0
    return {
        "id": "metadata",
        "label": DIMENSION_LABELS["metadata"],
        "available": True,
        "score": round(score, 6),
        "percentage": _pct(score, has_difference=has_diff),
        "reason": f"{matching} de {total} propiedades descriptivas comparables coinciden (se excluyen etiquetas técnicas).",
        "counts": {
            "matching": matching, "different": different, "only_a": only_a, "only_b": only_b,
            "not_comparable": not_comparable, "total_comparable": total,
        },
    }


def compute_similarity(binary_identity: dict, content: dict, metadata_rows: list[dict]) -> dict:
    meta_dim = _metadata_dimension(metadata_rows)
    dimensions: dict[str, dict] = {}
    not_analyzed: list[str] = []
    significant_changes: list[str] = []
    warnings: list[str] = []

    content_status = content.get("status")
    fmt = content.get("format")

    if content_status != "analyzed" or fmt not in WEIGHT_PROFILES:
        for dim_id in ("text", "media", "structure"):
            dimensions[dim_id] = {
                "id": dim_id,
                "label": DIMENSION_LABELS[dim_id],
                "available": False,
                "score": None,
                "percentage": None,
                "reason": content.get("reason") or "Dimensión no analizada para este formato.",
            }
            not_analyzed.append(DIMENSION_LABELS[dim_id])
        dimensions["metadata"] = meta_dim
        if not meta_dim["available"]:
            not_analyzed.append(DIMENSION_LABELS["metadata"])

        if binary_identity.get("identical_bytes"):
            summary_note = (
                "Los archivos son idénticos a nivel de bytes según SHA-256. "
                "No se calcula puntuación ponderada de contenido porque el formato no tiene analizador profundo activo."
            )
        else:
            summary_note = (
                "No se emite una puntuación global de similitud documental porque el contenido interno "
                "no pudo analizarse de forma profunda. Evita inferir similitud de contenido únicamente a partir de metadatos."
            )
        warnings.append(summary_note)
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "format": fmt,
            "overall_available": False,
            "overall": None,
            "label": "Similitud global no disponible",
            "dimensions": dimensions,
            "not_analyzed": not_analyzed,
            "significant_changes": significant_changes,
            "warnings": warnings,
            "methodology": {
                "summary": summary_note,
                "base_weights": WEIGHT_PROFILES.get(fmt),
                "effective_weights": {},
                "caveats": [
                    "Una coincidencia parcial de metadatos no equivale a similitud del contenido visible.",
                    "Los hashes SHA-256 distintos no permiten estimar grado de parecido; nunca se comparan por caracteres.",
                ],
            },
        }

    # Content was analyzed for a supported format (PPTX, PDF, DOCX, XLS, XLSX, SPREADSHEET)
    summary = content["summary"]
    metrics = content["metrics"]
    is_pptx = fmt == "PPTX"
    is_spreadsheet = fmt in {"XLS", "XLSX", "SPREADSHEET"}

    # Text / Data dimension
    text_m = metrics["text"]
    text_label = "Datos y fórmulas (celdas pobladas)" if is_spreadsheet else DIMENSION_LABELS["text"]
    if text_m["available"]:
        if is_pptx:
            text_has_diff = (
                summary["modified_slides"] > 0
                and any(not s["text_identical"] for s in content["slides"])
            ) or summary["added_slides"] > 0 or summary["removed_slides"] > 0
            text_reason = "Promedio de similitud léxica y de caracteres sobre diapositivas alineadas (incluye tablas y notas)."
        elif fmt == "PDF":
            text_has_diff = (
                summary["modified_pages"] > 0 or summary["added_pages"] > 0 or summary["removed_pages"] > 0
            )
            text_reason = "Similitud sobre texto extraíble por página (excluyendo encabezados/pies repetidos)."
        elif fmt == "DOCX":
            text_has_diff = (
                summary["modified_paragraphs"] > 0 or summary["added_paragraphs"] > 0 or summary["removed_paragraphs"] > 0
            )
            text_reason = "Similitud textual sobre párrafos, encabezados, pies y notas del paquete OOXML."
        else:
            text_has_diff = summary["modified_cells"] > 0
            text_reason = (
                f"{summary['matching_cells']} de {summary['populated_cells_union']} celdas pobladas coinciden "
                "(las celdas vacías nunca inflan el porcentaje)."
            )
        dimensions["text"] = {
            "id": "text",
            "label": text_label,
            "available": True,
            "score": text_m["score"],
            "percentage": _pct(text_m["score"], has_difference=text_has_diff),
            "reason": text_reason,
        }
    else:
        dimensions["text"] = {
            "id": "text",
            "label": text_label,
            "available": False,
            "score": None,
            "percentage": None,
            "reason": (
                "No se detectó texto extraíble (posible PDF escaneado)."
                if fmt == "PDF" else
                "No se detectaron celdas pobladas comparables."
                if is_spreadsheet else
                "Ninguno de los documentos contiene texto extraíble."
            ),
        }
        not_analyzed.append(text_label)

    # Media dimension
    media_m = metrics["media"]
    if media_m["available"] and WEIGHT_PROFILES[fmt].get("media", 0) > 0:
        if is_pptx:
            media_has_diff = (
                summary["media_added"] > 0 or summary["media_removed"] > 0 or summary["media_replaced_on_slides"] > 0
            )
            media_reason = "Comparación de recursos multimedia por huella SHA-256 (60% inventario global, 40% ubicación por diapositiva)."
        elif fmt == "PDF":
            media_has_diff = summary["images_shared"] != max(summary["images_a"], summary["images_b"])
            media_reason = "Comparación de imágenes XObject incrustadas mediante huella SHA-256."
        else:
            media_has_diff = summary.get("media_added", 0) > 0 or summary.get("media_removed", 0) > 0
            media_reason = "Comparación de recursos multimedia incrustados en word/media mediante SHA-256."
        dimensions["media"] = {
            "id": "media",
            "label": DIMENSION_LABELS["media"],
            "available": True,
            "score": media_m["score"],
            "percentage": _pct(media_m["score"], has_difference=media_has_diff),
            "reason": media_reason,
        }
    else:
        dimensions["media"] = {
            "id": "media",
            "label": DIMENSION_LABELS["media"],
            "available": False,
            "score": None,
            "percentage": None,
            "reason": (
                "No aplicable a libros de cálculo." if is_spreadsheet
                else "Ninguno de los documentos contiene imágenes ni recursos multimedia incrustados."
            ),
        }
        not_analyzed.append(DIMENSION_LABELS["media"])

    # Structure dimension
    struct_m = metrics["structure"]
    if is_pptx:
        struct_has_diff = (
            summary["slide_count_a"] != summary["slide_count_b"]
            or summary["reordered_slides"] > 0
            or summary["structural_changes_count"] > 0
        )
        struct_reason = "Combina cantidad de diapositivas (35%), preservación de orden (30%) y elementos estructurales por diapositiva (35%)."
    elif fmt == "PDF":
        struct_has_diff = (
            summary["page_count_a"] != summary["page_count_b"]
            or summary["reordered_pages"] > 0
            or summary["structural_changes_count"] > 0
        )
        struct_reason = "Evalúa cantidad de páginas, orden, anotaciones, marcadores, adjuntos y versión PDF."
    elif fmt == "DOCX":
        struct_has_diff = (
            summary["paragraphs_a"] != summary["paragraphs_b"]
            or summary["modified_tables"] > 0
            or summary["structural_changes_count"] > 0
        )
        struct_reason = "Evalúa estructura de párrafos, tablas, encabezados/pies, notas, enlaces y formato."
    else:
        struct_has_diff = (
            summary["sheets_a"] != summary["sheets_b"]
            or bool(summary["added_sheets"])
            or bool(summary["removed_sheets"])
            or bool(summary["renamed_sheets"])
            or bool(summary["reordered_sheets"])
            or summary["structural_changes_count"] > 0
        )
        struct_reason = "Evalúa cantidad, nombres, orden, visibilidad y celdas combinadas de las hojas."

    dimensions["structure"] = {
        "id": "structure",
        "label": DIMENSION_LABELS["structure"],
        "available": True,
        "score": struct_m["score"],
        "percentage": _pct(struct_m["score"], has_difference=struct_has_diff),
        "reason": struct_reason,
    }

    # Metadata dimension
    dimensions["metadata"] = meta_dim
    if not meta_dim["available"]:
        not_analyzed.append(DIMENSION_LABELS["metadata"])

    # Compute effective weights (redistributing inapplicable dimensions proportionally)
    base_weights = WEIGHT_PROFILES[fmt]
    active = {k: w for k, w in base_weights.items() if w > 0 and dimensions[k]["available"]}
    total_w = sum(active.values())
    effective_weights = {k: round(w / total_w, 6) for k, w in active.items()} if total_w > 0 else {}

    weighted_score = sum(dimensions[k]["score"] * effective_weights[k] for k in effective_weights)

    # Identify significant changes explicitly so high percentages never hide material edits
    if is_pptx:
        text_modified_slides = [s for s in content["slides"] if s["status"] == "modified" and not s["text_identical"]]
        if text_modified_slides:
            significant_changes.append(f"{len(text_modified_slides)} diapositiva(s) con texto modificado.")
        if summary["added_slides"]:
            significant_changes.append(f"{summary['added_slides']} diapositiva(s) agregada(s) en B.")
        if summary["removed_slides"]:
            significant_changes.append(f"{summary['removed_slides']} diapositiva(s) eliminada(s) respecto a A.")
        if summary["reordered_slides"]:
            significant_changes.append(f"{summary['reordered_slides']} diapositiva(s) con orden modificado.")
        if summary["media_replaced_on_slides"]:
            significant_changes.append(f"{summary['media_replaced_on_slides']} diapositiva(s) con imágenes o recursos sustituidos.")
        elif summary["media_added"] or summary["media_removed"]:
            significant_changes.append(f"Cambios en recursos multimedia (+{summary['media_added']} / −{summary['media_removed']}).")
        if summary["structural_changes_count"]:
            significant_changes.append(f"{summary['structural_changes_count']} diapositiva(s) con cambios estructurales (formas, tablas, gráficos o diseño).")
    elif fmt == "PDF":
        if summary["modified_pages"]:
            significant_changes.append(f"{summary['modified_pages']} página(s) con contenido modificado.")
        if summary["added_pages"]:
            significant_changes.append(f"{summary['added_pages']} página(s) agregada(s) en B.")
        if summary["removed_pages"]:
            significant_changes.append(f"{summary['removed_pages']} página(s) eliminada(s) respecto a A.")
        if summary["reordered_pages"]:
            significant_changes.append(f"{summary['reordered_pages']} página(s) reordenada(s).")
        if summary["structural_changes_count"]:
            significant_changes.append(f"{summary['structural_changes_count']} cambio(s) estructural(es) en el PDF.")
    elif fmt == "DOCX":
        if summary["modified_paragraphs"]:
            significant_changes.append(f"{summary['modified_paragraphs']} párrafo(s) modificado(s).")
        if summary["added_paragraphs"]:
            significant_changes.append(f"{summary['added_paragraphs']} párrafo(s) agregado(s) en B.")
        if summary["removed_paragraphs"]:
            significant_changes.append(f"{summary['removed_paragraphs']} párrafo(s) eliminado(s) respecto a A.")
        if summary["modified_tables"]:
            significant_changes.append(f"{summary['modified_tables']} tabla(s) modificada(s).")
        if summary["media_added"] or summary["media_removed"]:
            significant_changes.append(f"Recursos multimedia modificados (+{summary['media_added']} / −{summary['media_removed']}).")
    elif is_spreadsheet:
        if summary["modified_cells"]:
            significant_changes.append(f"{summary['modified_cells']} celda(s) modificada(s) en el libro.")
        if summary["formula_diffs"]:
            significant_changes.append(f"{summary['formula_diffs']} fórmula(s) modificada(s).")
        if summary["cache_only_diffs"]:
            significant_changes.append(f"{summary['cache_only_diffs']} celda(s) con valor en caché distinto bajo la misma fórmula.")
        if summary["added_sheets"]:
            significant_changes.append(f"Hojas agregadas: {', '.join(summary['added_sheets'])}.")
        if summary["removed_sheets"]:
            significant_changes.append(f"Hojas eliminadas: {', '.join(summary['removed_sheets'])}.")
        if summary["renamed_sheets"]:
            significant_changes.append(f"{len(summary['renamed_sheets'])} hoja(s) con posible renombrado.")

    warnings.extend(content.get("warnings", []))

    meta_counts = meta_dim["counts"]
    meta_diff_count = meta_counts["different"] + meta_counts["only_a"] + meta_counts["only_b"]
    metadata_only_change = summary["substantive_identical"] and meta_diff_count > 0
    non_substantive_only = (
        not binary_identity.get("identical_bytes")
        and summary["substantive_identical"]
    )

    if metadata_only_change:
        warnings.append(
            "El contenido extraído de las diapositivas coincide por completo; las diferencias detectadas se limitan a metadatos."
        )
    elif non_substantive_only:
        warnings.append(
            "Los bytes difieren pero el contenido y la estructura extraídos coinciden; las variaciones corresponden a detalles internos de empaquetado, XML o metadatos no descriptivos."
        )

    if summary.get("same_text_different_visual_assets"):
        warnings.append(
            "Ambos documentos conservan el mismo texto, pero difieren en imágenes, recursos multimedia o estructura."
        )

    any_detected_diff = bool(significant_changes) or meta_diff_count > 0 or not binary_identity.get("identical_bytes")
    # If all analyzed dimensions are 100% AND binary differs only in non-substantive XML/ZIP, overall is still < 100 or 100?
    # Wait: if metadata also matched 100% and content matched 100%, analyzed similarity is 100% (while binary identity notes bytes differ).
    # If ANY analyzed dimension has a difference, cap at 99.9%.
    analyzed_has_diff = bool(significant_changes) or meta_diff_count > 0
    overall_pct = _pct(weighted_score, has_difference=analyzed_has_diff)

    if overall_pct >= 95.0 and significant_changes:
        warnings.append(
            "Una similitud elevada (incluso superior al 95%) no implica equivalencia funcional: revisa las diferencias relevantes listadas."
        )

    weight_parts = [
        f"{DIMENSION_LABELS[k]} ({round(effective_weights[k] * 100, 1)}%)"
        for k in ("text", "media", "structure", "metadata")
        if k in effective_weights
    ]
    explanation = (
        f"Ponderación determinista v{ALGORITHM_VERSION} para {fmt}: "
        + ", ".join(weight_parts)
        + ". Las propiedades técnicas de ExifTool se excluyen del porcentaje de metadatos."
    )

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "format": fmt,
        "overall_available": True,
        "overall": overall_pct,
        "label": f"Los documentos presentan una similitud del {overall_pct:g}%",
        "dimensions": dimensions,
        "not_analyzed": not_analyzed,
        "significant_changes": significant_changes,
        "metadata_only_change": metadata_only_change,
        "non_substantive_only": non_substantive_only,
        "warnings": warnings,
        "methodology": {
            "summary": explanation,
            "base_weights": base_weights,
            "effective_weights": effective_weights,
            "caveats": [
                "Una similitud alta (incluso 99%) no garantiza equivalencia funcional ni visual.",
                "El porcentaje refleja únicamente las dimensiones analizadas y no sustituye la revisión visual humana.",
                *content.get("limitations", []),
            ],
        },
    }
