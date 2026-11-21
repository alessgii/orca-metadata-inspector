"use strict";

const comparisonLabels = {
  matching: ["=", "Coincidente"], different: ["≠", "Diferente"], only_a: ["A", "Solo en A"],
  only_b: ["B", "Solo en B"], not_comparable: ["?", "No comparable"],
};

function recordedValue(value) {
  if (value === "") return "Cadena vacía";
  if (value === null) return "Nulo";
  return display(value);
}

function metadataLabel(item) {
  const tag = item.source.findLast((part) => typeof part === "string").split(":").at(-1).toLowerCase();
  if (["author", "software", "identifier", "property"].includes(item.field)) {
    if (tag === "creator" && item.source[0].startsWith("PDF:")) return "Herramienta de creación";
    const specific = {
      creator: "Creador declarado", creators: "Creadores declarados", originator: "Creador declarado",
      lastmodifiedby: "Último editor registrado", lastauthor: "Último editor registrado",
      lasteditor: "Último editor registrado", producer: "Productor", creatortool: "Herramienta de creación",
      creatorsw: "Herramienta de creación", documentid: "Identificador documental",
      instanceid: "Identificador de instancia", originaldocumentid: "Identificador documental original",
      uuid: "UUID", computername: "Nombre de equipo", hostname: "Nombre de equipo",
    };
    if (specific[tag]) return specific[tag];
  }
  return fields[item.field] ?? item.field;
}

function metadataValue(value, compact = false) {
  const text = recordedValue(value);
  if (!compact || text.length <= 72) return element("span", UI.metaVal, text);
  return element("details", UI.longValue, element("summary", "", text.slice(0, 62) + "…"),
    element("div", UI.metaVal, text));
}

async function copyText(text, holder, status, success = "Copiado al portapapeles.") {
  holder.querySelector("textarea")?.remove();
  status.textContent = "Copiando…";
  const trigger = document.activeElement;
  let timer;
  try {
    await Promise.race([
      navigator.clipboard.writeText(text),
      new Promise((_, reject) => { timer = setTimeout(reject, 1600); }),
    ]);
    status.textContent = success;
  } catch {
    const fallback = element("textarea", "basis-full w-full min-h-[75px] p-1.5 border border-line-strong bg-surface font-mono text-[0.82rem]");
    fallback.readOnly = true;
    fallback.value = text;
    fallback.setAttribute("aria-label", "Texto para copiar manualmente");
    holder.append(fallback);
    if (holder.contains(trigger) && document.activeElement === trigger) {
      fallback.focus();
      fallback.select();
    }
    status.textContent = "Selecciona el texto y cópialo con el portapapeles de tu dispositivo.";
  } finally {
    clearTimeout(timer);
  }
}

function evidenceControl(view, kind, selector) {
  const summary = element("summary", "inline-flex items-center gap-[5px] py-[3px] px-1.5 min-h-[28px] text-accent text-[0.875rem] font-semibold hover:bg-tint hover:rounded focus-visible:bg-tint focus-visible:rounded [&_svg]:size-3.5", icon("copy"), "Evidencia");
  const panel = element("div", "p-[9px] bg-wash border border-line rounded-md text-[0.875rem] space-y-1.5 mt-1.5");
  const control = element("details", "mt-1", summary, panel);
  let loaded = false;
  control.addEventListener("toggle", async () => {
    if (!control.open || loaded) return;
    loaded = true;
    panel.textContent = "Preparando evidencia…";
    try {
      const options = new URLSearchParams({kind, ...(kind === "raw" ? {tag: selector} : {index: selector})});
      const evidence = await api(filePath(view.session.id, view.record.id) + "/evidence?" + options);
      const status = element("p", UI.copyFeedback);
      status.setAttribute("role", "status");
      const actions = element("div", "flex flex-wrap gap-2");
      const add = (label, text) => actions.append(button(label,
        () => copyText(text, panel, status)));
      add("Copiar valor original", evidence.value);
      add("Copiar evidencia JSON", JSON.stringify(evidence.structured, null, 2));
      add("Copiar Markdown", evidence.markdown);
      if (evidence.commands.posix) add("Copiar comando Linux", evidence.commands.posix);
      if (evidence.commands.powershell) add("Copiar comando PowerShell", evidence.commands.powershell);
      panel.replaceChildren(actions, status);
      if (Object.keys(evidence.commands).length) panel.append(element("p", UI.muted,
        "Comando de referencia. Ejecútalo en la carpeta del original con ExifTool disponible."));
    } catch (error) {
      loaded = false;
      panel.textContent = error.message;
    }
  });
  return control;
}

function dateText(event) {
  if (!event.normalized) return "Fecha sin interpretar";
  const raw = event.normalized;
  const day = raw.slice(0, event.precision === "year" ? 4 : event.precision === "month" ? 7 : 10);
  const calendar = new Date(day.padEnd(10, "-01") + "T00:00:00Z");
  const format = {year: "numeric", timeZone: "UTC"};
  if (event.precision !== "year") format.month = "long";
  if (!["year", "month"].includes(event.precision)) format.day = "numeric";
  const time = raw.includes("T") ? raw.split("T")[1].replace(/[+-].*$/, "") : "";
  const date = Number.isNaN(calendar.valueOf()) ? day : new Intl.DateTimeFormat("es", format).format(calendar);
  const zone = event.timezone ? " UTC" + event.timezone : "";
  return date + (time ? " · " + time : "") + zone;
}

function intervalText(seconds) {
  const absolute = Math.abs(seconds);
  const days = Math.floor(absolute / 86400);
  const time = absolute - days * 86400;
  const parts = [days && days + (days === 1 ? " día" : " días")];
  if (time || !days) parts.push(new Intl.NumberFormat("es", {maximumFractionDigits: 6}).format(time) + " s");
  return (seconds < 0 ? "−" : "") + parts.filter(Boolean).join(" y ");
}

function advancedTimeline(timeline) {
  const box = element("div", "mt-3.5 space-y-3");
  const hasDates = Object.values(timeline.groups).some((items) => items.length);
  if (!hasDates) {
    box.append(element("p", UI.muted, "No se encontraron fechas en los campos examinados."));
    return box;
  }
  box.append(element("p", UI.smallMuted, "Las marcas registradas provienen del archivo y pueden describir eventos distintos."));
  if (timeline.interval) {
    const {seconds, created, modified, created_sources, modified_sources} = timeline.interval;
    box.append(element("div", UI.callout, element("strong", "", "Intervalo entre creación y modificación"),
      element("p", "", intervalText(seconds)),
      element("p", "text-[0.875rem] font-mono text-muted", created + " / " + modified),
      element("details", UI.details, element("summary", UI.summary + " text-[0.875rem]", "Fuentes del intervalo"),
        element("p", UI.small, "Creación · " + created_sources.map((source) => source.join(" / ")).join(" · ")),
        element("p", UI.small, "Modificación · " + modified_sources.map((source) => source.join(" / ")).join(" · ")))));
  }
  for (const warning of timeline.warnings) box.append(element("p", UI.notice, warning));
  const names = {instant: "Instantes ordenados en UTC", local: "Horas sin conversión a un instante",
    partial: "Fechas con precisión de calendario", invalid: "Valores sin interpretar"};
  for (const [group, events] of Object.entries(timeline.groups)) {
    if (!events.length) continue;
    const list = element("ul", UI.timeEvents);
    for (const event of events) {
      const details = element("details", "text-muted py-[3px] text-[0.86rem]", element("summary", "text-[0.875rem] font-semibold", "Valor original y procedencia"),
        element("div", "text-[0.875rem] " + UI.metaVal, recordedValue(event.original)),
        element("code", UI.code, event.source.join(" / ")));
      if (event.timezone_source) details.append(element("p", UI.smallMuted, "Zona de " + event.timezone_source.join(" / ")));
      details.append(element("p", UI.smallMuted, "Precisión: " + ({
        year: "año", month: "mes", day: "día", hour: "hora", minute: "minuto", second: "segundo",
        fraction: "fracción de segundo",
      }[event.precision] ?? "indeterminada")));
      if (event.utc) details.append(element("p", "text-[0.875rem] text-muted font-mono", event.utc + " (UTC)"));
      const warnings = event.warnings.map((warning) => element("p", UI.smallMuted, warning));
      list.append(element("li", UI.timeEvent, element("div", UI.timeEventTitle,
        element("strong", "", event.label), element("span", UI.smallMuted,
          event.origin === "filesystem" ? "Sistema de archivos" : "Metadato interno")),
      element("p", UI.metaVal, dateText(event)), element("code", UI.code, event.source.join(" / ")),
      ...warnings, details));
    }
    box.append(element("div", "", element("h3", UI.smallMuted + " font-semibold", names[group]), list));
  }
  return box;
}

function exposureComparisonView(difference) {
  const box = element("section", UI.callout, element("h3", UI.h3, "Exposición antes y después"),
    element("p", UI.small, "Antes " + difference.before + " / Después " + difference.after));
  for (const [key, label] of Object.entries({removed: "Eliminadas", retained: "Persistentes", added: "Añadidas"})) {
    box.append(element("details", UI.details, element("summary", UI.summary, label + " (" + difference[key].length + ")"),
      difference[key].length ? element("ul", "", ...difference[key].map((item) =>
        element("li", "py-[3px] break-words", element("strong", "", item.label), " · ", recordedValue(item.value),
          element("div", "text-[0.875rem] font-mono text-muted", item.source.join(" / "))))) : element("p", UI.smallMuted, "Sin registros.")));
  }
  return box;
}

function exposureView(view) {
  const {exposure, exposure_comparison: previous} = view.context;
  const box = element("section", "space-y-3", element("h2", UI.h2, "¿Qué estoy filtrando?"));
  box.append(element("p", UI.smallMuted,
    "Los niveles son orientativos y describen los campos examinados. Pueden existir datos sensibles en el contenido visible o en estructuras fuera de esta revisión."));
  if (!exposure.total) box.append(element("p", "", "No se detectaron exposiciones en las categorías examinadas."));
  if (view.session.temporary && cleanupKinds.has(view.record.format) && ["ok", "empty"].includes(view.record.status)) {
    box.append(button("Abrir Antes / Después", () => {
      renderDetail(view, "clean");
      $("file-tools").querySelector("[aria-pressed=true]").focus();
    }, "accent-button"));
  } else {
    box.append(element("p", UI.smallMuted, view.session.temporary
      ? "Este formato o estado admite únicamente inspección en Orca."
      : "Para limpiar un archivo del historial, vuelve a cargar el original."));
  }
  const comparison = view.cleaned?.exposure_comparison ?? previous;
  if (comparison) box.append(exposureComparisonView(comparison));
  let index = 0;
  for (const category of exposure.categories) {
    const list = element("ul", "");
    for (const item of category.items) {
      const level = element("span", UI.status, item.level === "potential" ? "Exposición potencial" : "Informativo");
      level.dataset.level = item.level;
      list.append(element("li", UI.finding,
        element("div", UI.timeEventTitle, element("strong", "", item.label), level),
        metadataValue(item.value, true),
        element("p", UI.smallMuted, item.description + " " + item.reason),
        element("p", "text-[0.875rem] font-mono text-muted", item.sources.map((source) => source.join(" / ")).join(" · ")),
        element("p", "text-[0.875rem] font-mono text-muted", item.rule),
        evidenceControl(view, "exposure", index++)));
    }
    const section = element("details", UI.details, element("summary", UI.summary, category.label + " (" + category.items.length + ")"), list);
    section.open = category.id === "identity" || category.id === "location";
    box.append(section);
  }
  return box;
}

function inspectionContextView(view, tab) {
  if (view.context) return tab === "timeline" ? timelineView(view) : exposureView(view);
  if (view.contextError) return element("p", UI.notice, view.contextError);
  if (!view.contextRequest) {
    view.contextRequest = api(filePath(view.session.id, view.record.id) + "/context").then((result) => {
      view.context = result;
    }).catch((error) => {
      view.contextError = error.message;
    }).finally(() => {
      if (state.inspection === view && ["timeline", "exposure"].includes(view.tab)) {
        renderDetail(view, view.tab);
      }
    });
  }
  return element("p", UI.muted, "Leyendo los metadatos guardados…");
}

function compareSelection() {
  const files = state.session?.files ?? [];
  return state.selected.map((id) => files.find((file) => file.id === id)).filter(Boolean);
}

function updateSelection() {
  const selected = compareSelection();
  $("duel-open").disabled = selected.length !== 2;
  $("duel-clear").hidden = !selected.length;
  $("duel-selection").textContent = selected.length ? selected.map((file, index) =>
    String.fromCharCode(65 + index) + " · " + file.name).join(" / ") : "Selecciona dos archivos para compararlos.";
  for (const checkbox of $("files").querySelectorAll("[data-compare-id]")) {
    checkbox.checked = state.selected.includes(checkbox.dataset.compareId);
    checkbox.disabled = selected.length === 2 && !checkbox.checked;
    checkbox.closest("tr").dataset.selected = String(checkbox.checked);
  }
}

function comparisonCheckbox(row) {
  const checkbox = element("input", "");
  checkbox.type = "checkbox";
  checkbox.dataset.compareId = row.id;
  checkbox.setAttribute("aria-label", "Seleccionar " + row.name + " para el duelo");
  checkbox.addEventListener("change", () => {
    if (checkbox.checked && state.selected.length < 2) state.selected.push(row.id);
    else state.selected = state.selected.filter((id) => id !== row.id);
    updateSelection();
  });
  return checkbox;
}

function compareSide(label, file) {
  return element("section", "min-w-0 bg-surface border border-line border-t-2 border-t-tint-line rounded-lg py-[13px] px-[15px] max-[540px]:p-[9px] grid grid-cols-[minmax(0,1fr)] gap-1 content-start",
    element("strong", "text-accent text-[0.84rem]", label), element("h3", UI.h3 + " " + UI.metaVal, file.name),
    element("p", UI.smallMuted, display(file.format) + " · " + formatBytes(file.size) + " · " + statusNames[file.status]),
    element("p", UI.smallMuted, "SHA-256"),
    hashControl(file.sha256));
}

function comparisonCell(entries, label, isFirst = false) {
  const cell = element("div", "py-[9px] px-3 max-[540px]:p-[9px] min-w-0 border-l border-line" + (isFirst ? " max-md:border-l-0" : ""));
  cell.setAttribute("role", "cell");
  cell.append(element("span", "hidden max-md:block mb-[3px] text-[0.875rem] text-muted", label));
  if (!entries.length) cell.append(element("span", UI.smallMuted, "Ausente"));
  for (const item of entries) {
    const value = element("div", "first-of-type:pt-0 first-of-type:mt-0 first-of-type:border-t-0 pt-1.5 mt-1.5 border-t border-line", metadataValue(item.original, true));
    if (Object.hasOwn(item, "interpreted")) value.append(element("p", UI.smallMuted, "Interpretado: " + display(item.interpreted)));
    if (item.source) value.append(element("code", UI.code + " max-[540px]:text-[0.75rem]", item.source.join(" / ")));
    if (item.interpretation_source) value.append(element("p", UI.smallMuted,
      "Hemisferio · " + item.interpretation_source.join(" / ")));
    for (const date of item.dates ?? []) {
      if (date.utc) value.append(element("p", UI.smallMuted, "UTC " + date.utc));
    }
    cell.append(value);
  }
  return cell;
}

function compareTable(rows) {
  const table = element("div", "border border-line rounded-lg overflow-hidden bg-surface min-w-0 divide-y divide-line");
  table.setAttribute("role", "table");
  table.setAttribute("aria-label", "Comparación de metadatos");
  const head = element("div", "grid grid-cols-[minmax(150px,0.75fr)_repeat(2,minmax(0,1.4fr))] max-md:hidden bg-wash text-muted font-semibold");
  head.setAttribute("role", "row");
  for (const [idx, text] of ["Propiedad", "Archivo A", "Archivo B"].entries()) {
    const cell = element("span", "py-[9px] px-3 min-w-0 text-[0.875rem]" + (idx > 0 ? " border-l border-line" : ""), text);
    cell.setAttribute("role", "columnheader");
    head.append(cell);
  }
  table.append(head);
  for (const row of rows) {
    const [symbol, label] = comparisonLabels[row.status];
    const badge = element("span", UI.status + " mt-[5px]", symbol + " " + label);
    badge.dataset.diff = row.status;
    const heading = element("div", "py-[9px] px-3 max-[540px]:p-[9px] min-w-0 bg-wash max-md:col-span-full", element("strong", UI.metaVal, row.label), badge);
    heading.setAttribute("role", "rowheader");
    if (row.explanation) heading.append(element("p", UI.smallMuted, row.explanation));
    const node = element("div", "grid grid-cols-[minmax(150px,0.75fr)_repeat(2,minmax(0,1.4fr))] max-md:grid-cols-2", heading,
      comparisonCell(row.a, "Archivo A", true), comparisonCell(row.b, "Archivo B", false));
    node.setAttribute("role", "row");
    table.append(node);
  }
  return table;
}

const slideStatusLabels = {
  identical: "Idéntica",
  modified: "Contenido modificado",
  reordered: "Reordenada",
  added: "Agregada",
  removed: "Eliminada",
};

function formatPercent(val) {
  return new Intl.NumberFormat("es", {maximumFractionDigits: 1}).format(val) + "%";
}

function similaritySection(similarity) {
  if (!similarity) return null;
  const card = element("section", UI.duelSection);
  const head = element("div", "flex items-baseline justify-between flex-wrap gap-x-4 gap-y-2");
  if (similarity.overall_available) {
    const pct = similarity.overall;
    const scoreBox = element("div", "",
      element("p", UI.smallMuted, "Similitud documental · Algoritmo v" + similarity.algorithm_version),
      element("h2", "text-[1.2rem] font-semibold mt-0.5 tracking-[-0.02em]", similarity.label));
    const big = element("span", "text-[clamp(1.5rem,2.8vw,2.05rem)] font-bold text-accent tabular-nums tracking-[-0.03em] whitespace-nowrap font-mono", formatPercent(pct));
    head.append(scoreBox, big);
    card.append(head);
    const bar = element("div", "h-[7px] rounded-full bg-wash border border-line overflow-hidden",
      element("div", "h-full bg-accent rounded-full"));
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", "Similitud documental");
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-valuenow", String(pct));
    bar.firstChild.style.width = Math.max(0, Math.min(100, pct)) + "%";
    card.append(bar);
  } else {
    head.append(element("div", "",
      element("p", UI.smallMuted, "Similitud documental"),
      element("h2", "text-[1.2rem] font-semibold mt-0.5 tracking-[-0.02em]", similarity.label)));
    card.append(head);
  }

  const dimGrid = element("div", "grid grid-cols-4 max-[1100px]:grid-cols-2 max-[540px]:grid-cols-1 gap-2.5");
  for (const id of ["text", "media", "structure", "metadata"]) {
    const dim = similarity.dimensions?.[id];
    if (!dim) continue;
    const item = element("div", "py-[11px] px-3 border border-line rounded-[7px] bg-wash grid grid-cols-[minmax(0,1fr)] gap-1.5 content-start min-w-0",
      element("div", "flex items-baseline justify-between gap-2 flex-wrap",
        element("span", UI.small, dim.label),
        element("strong", "font-mono", dim.available ? formatPercent(dim.percentage) : "No analizado")),
      element("p", UI.smallMuted, dim.reason));
    if (dim.available) {
      const mini = element("div", "h-1 rounded-full bg-line overflow-hidden", element("div", "h-full bg-accent rounded-full"));
      mini.firstChild.style.width = Math.max(0, Math.min(100, dim.percentage)) + "%";
      item.insertBefore(mini, item.lastChild);
    }
    dimGrid.append(item);
  }
  card.append(dimGrid);

  if (similarity.significant_changes?.length) {
    card.append(element("div", UI.callout,
      element("strong", UI.small, "Elementos relevantes modificados"),
      element("ul", UI.duelNotes + " text-[0.875rem]", ...similarity.significant_changes.map((c) => element("li", "", c)))));
  }

  if (similarity.not_analyzed?.length) {
    card.append(element("p", UI.smallMuted, "Dimensiones no analizadas: " + similarity.not_analyzed.join(", ") + "."));
  }

  for (const warn of similarity.warnings || []) {
    card.append(element("p", UI.notice, warn));
  }

  const methodBody = element("div", "grid grid-cols-[minmax(0,1fr)] gap-[7px] pt-1",
    element("p", UI.small, similarity.methodology.summary));
  if (similarity.methodology.effective_weights && Object.keys(similarity.methodology.effective_weights).length) {
    const labels = {text: "Contenido textual", media: "Recursos multimedia", structure: "Estructura", metadata: "Metadatos descriptivos"};
    methodBody.append(element("ul", UI.duelNotes + " text-[0.875rem] text-muted",
      ...Object.entries(similarity.methodology.effective_weights).map(([k, w]) =>
        element("li", "", (labels[k] || k) + ": peso efectivo " + formatPercent(w * 100)))));
  }
  if (similarity.methodology.caveats?.length) {
    methodBody.append(element("ul", UI.duelNotes + " text-[0.875rem] text-muted",
      ...similarity.methodology.caveats.map((c) => element("li", "", c))));
  }
  card.append(element("details", UI.details,
    element("summary", UI.summary + " text-[0.875rem]", "Metodología, ponderaciones y limitaciones"), methodBody));
  return card;
}

function binaryIdentitySection(binary, result) {
  if (!binary) return null;
  const box = element("section", UI.duelSection);
  const badge = element("span", UI.status,
    binary.status === "identical" ? "Bytes idénticos" : binary.status === "different" ? "Bytes distintos" : "Sin confirmar");
  if (binary.status === "identical") badge.dataset.status = "ok";
  else if (binary.status === "different") badge.dataset.status = "unsupported";

  const delta = binary.size_delta === null || binary.size_delta === undefined
    ? "—"
    : binary.size_delta === 0
      ? "Sin diferencia (mismo tamaño)"
      : (binary.size_delta > 0 ? "+" : "") + numbers.format(binary.size_delta) + " B";

  const head = element("div", UI.duelHead,
    element("div", "flex items-center gap-2 flex-wrap", element("h2", UI.h2, "A. Identidad binaria"), badge));
  if (result.a.sha256 || result.b.sha256) {
    head.append(button("Comparar hashes en Laboratorio", () => {
      openHashComparator(result.a.sha256 || "", result.b.sha256 || "", "SHA-256");
    }, "subtle"));
  }
  box.append(
    head,
    element("p", "", binary.verdict),
    element("p", UI.smallMuted,
      "Tamaño A: " + (binary.size_a !== null ? formatBytes(binary.size_a) + " (" + numbers.format(binary.size_a) + " B)" : "—")
      + " · Tamaño B: " + (binary.size_b !== null ? formatBytes(binary.size_b) + " (" + numbers.format(binary.size_b) + " B)" : "—")
      + " · Diferencia: " + delta),
    element("ul", UI.duelNotes + " text-[0.875rem] text-muted", ...(binary.notes || []).map((n) => element("li", "", n))),
  );
  return box;
}

function renderDiffSegments(segments) {
  const wrap = element("p", "text-[0.88rem] leading-[1.5] whitespace-pre-wrap break-words");
  for (const seg of segments || []) {
    if (seg.op === "delete") wrap.append(element("del", "bg-error-bg text-error line-through px-[3px] rounded-[3px]", seg.text));
    else if (seg.op === "insert") wrap.append(element("ins", "bg-success-bg text-success no-underline font-semibold px-[3px] rounded-[3px]", seg.text));
    else wrap.append(document.createTextNode(seg.text));
  }
  return wrap;
}

function slideTitleLabel(slide) {
  let num;
  if (slide.slide_a && slide.slide_b && slide.slide_a !== slide.slide_b) {
    num = "Diapositiva " + slide.slide_a + " → " + slide.slide_b;
  } else if (slide.slide_a) {
    num = "Diapositiva " + slide.slide_a;
  } else {
    num = "Diapositiva " + slide.slide_b + " (B)";
  }
  return num + " — " + (slideStatusLabels[slide.status] || slide.status);
}

function deepDiffSection(content, similarity) {
  if (!content) return null;
  const box = element("section", UI.duelSection);
  const head = element("div", UI.duelHead,
    element("h2", UI.h2, "B. Diferencias profundas de contenido"));
  box.append(head);

  if (content.status !== "analyzed") {
    box.append(element("p", UI.muted, content.reason || "No se pudo realizar el análisis profundo de contenido."));
    if (content.limitations?.length) {
      box.append(element("ul", UI.duelNotes + " text-[0.875rem] text-muted", ...content.limitations.map((l) => element("li", "", l))));
    }
    return box;
  }

  const cs = content.summary;
  const stats = element("div", UI.stats,
    ...[
      ["Diapositivas A / B", cs.slide_count_a + " / " + cs.slide_count_b],
      ["Idénticas", numbers.format(cs.identical_slides)],
      ["Modificadas", numbers.format(cs.modified_slides)],
      ["Reordenadas", numbers.format(cs.reordered_slides)],
      ["Agregadas", numbers.format(cs.added_slides)],
      ["Eliminadas", numbers.format(cs.removed_slides)],
    ].map(([label, val]) => element("div", UI.stat, element("strong", "", String(val)), element("span", "", label))));
  box.append(stats);

  if (similarity?.metadata_only_change) {
    box.append(element("p", UI.callout + " text-[0.875rem]",
      "Diferencias exclusivamente relacionadas con metadatos: todas las diapositivas y recursos multimedia extraídos coinciden."));
  } else if (similarity?.non_substantive_only) {
    box.append(element("p", UI.callout + " text-[0.875rem]",
      "Modificaciones sin impacto sustancial en el contenido extraído (p. ej. serialización XML, IDs internos o recompresión)."));
  }

  const filter = element("select", UI.select);
  filter.setAttribute("aria-label", "Filtrar diferencias por diapositiva");
  const hasNonIdentical = content.slides.some((s) => s.status !== "identical");
  filter.append(
    new Option("Solo cambios (" + content.slides.filter((s) => s.status !== "identical").length + ")", "changes"),
    new Option("Todas las diapositivas (" + content.slides.length + ")", ""),
    ...Object.entries(slideStatusLabels).map(([k, label]) =>
      new Option(label + " (" + content.slides.filter((s) => s.status === k).length + ")", k)),
  );
  filter.value = hasNonIdentical ? "changes" : "";

  const listHost = element("div", "grid grid-cols-[minmax(0,1fr)] gap-2");
  let showAll = false;

  const renderSlides = () => {
    const filtered = content.slides.filter((s) => {
      if (!filter.value) return true;
      if (filter.value === "changes") return s.status !== "identical";
      return s.status === filter.value;
    });
    if (!filtered.length) {
      listHost.replaceChildren(element("p", UI.smallMuted, "No hay diapositivas que coincidan con este filtro."));
      return;
    }
    const limit = showAll ? filtered.length : 12;
    const visible = filtered.slice(0, limit);
    const items = visible.map((slide, idx) => {
      const badge = element("span", UI.status, slideStatusLabels[slide.status] || slide.status);
      if (slide.status === "identical") badge.dataset.status = "ok";
      else if (slide.status === "modified" || slide.status === "reordered") badge.dataset.status = "unsupported";
      else if (slide.status === "removed" || slide.status === "added") badge.dataset.status = "error";

      const summary = element("summary", "flex items-center justify-between flex-wrap gap-x-2.5 gap-y-1.5 font-semibold",
        element("span", "font-semibold", slideTitleLabel(slide) + (slide.title ? " · " + slide.title : "")),
        badge);
      const body = element("div", UI.slideDiffBody);
      if (slide.change_summary?.length) {
        body.append(element("p", UI.smallMuted, slide.change_summary.join(" · ")));
      }
      if (slide.text_diff && (slide.text_diff.text_a || slide.text_diff.text_b)) {
        const pair = element("div", "grid grid-cols-2 max-md:grid-cols-1 gap-2.5",
          element("div", "py-[9px] px-[11px] border border-line rounded-md bg-wash grid grid-cols-[minmax(0,1fr)] gap-1 content-start min-w-0", element("strong", UI.smallMuted, "Archivo A"),
            element("p", UI.metaVal + " text-[0.875rem]", slide.text_diff.text_a || "—")),
          element("div", "py-[9px] px-[11px] border border-line rounded-md bg-wash grid grid-cols-[minmax(0,1fr)] gap-1 content-start min-w-0", element("strong", UI.smallMuted, "Archivo B"),
            element("p", UI.metaVal + " text-[0.875rem]", slide.text_diff.text_b || "—")));
        body.append(pair);
        if (slide.text_diff.segments?.length && slide.status === "modified") {
          body.append(element("div", "py-[9px] px-[11px] border border-line rounded-md bg-surface grid grid-cols-[minmax(0,1fr)] gap-1 min-w-0",
            element("span", UI.smallMuted, "Texto eliminado y agregado:"),
            renderDiffSegments(slide.text_diff.segments)));
        }
      } else if (slide.status === "identical") {
        body.append(element("p", UI.smallMuted, "El texto normalizado, los recursos multimedia y la estructura de esta diapositiva coinciden."));
      } else if (slide.text_identical) {
        body.append(element("p", UI.smallMuted, "El texto de esta diapositiva coincide exactamente."));
      }

      if (slide.media_diff) {
        const m = slide.media_diff;
        const msg = m.replaced
          ? "Imágenes o recursos sustituidos en esta diapositiva (" + m.removed_hashes.length + " retirado(s), " + m.added_hashes.length + " nuevo(s))."
          : "Recursos multimedia modificados (" + m.removed_hashes.length + " eliminado(s), " + m.added_hashes.length + " agregado(s)).";
        body.append(element("p", UI.small, msg));
      }

      if (slide.structural_changes?.length) {
        body.append(element("ul", UI.duelNotes + " text-[0.875rem] text-muted",
          ...slide.structural_changes.map((ch) =>
            element("li", "", ch.label + ": " + display(ch.before) + " → " + display(ch.after)))));
      }

      const details = element("details", UI.details, summary, body);
      details.open = slide.status !== "identical" && idx < 4;
      return details;
    });
    if (filtered.length > limit) {
      items.push(button("Mostrar todas las diapositivas (" + filtered.length + ")", () => {
        showAll = true;
        renderSlides();
      }, "subtle"));
    }
    listHost.replaceChildren(...items);
  };
  filter.addEventListener("change", () => { showAll = false; renderSlides(); });
  renderSlides();

  box.append(element("div", UI.duelFilters, filter), listHost);

  // Media assets summary
  const media = content.media;
  if (media && (media.identical.length || media.replaced.length || media.added.length || media.removed.length)) {
    const mediaBox = element("details", UI.details,
      element("summary", UI.summary + " text-[0.875rem]",
        "Recursos multimedia e imágenes (idénticos " + media.identical.length
        + " · sustituidos " + media.replaced.length
        + " · agregados " + media.added.length
        + " · eliminados " + media.removed.length + ")"));
    mediaBox.open = Boolean(media.replaced.length || media.added.length || media.removed.length);
    const list = element("ul", UI.duelNotes + " text-[0.875rem]");
    for (const item of media.replaced) {
      list.append(element("li", "",
        "Diapositiva " + item.slide_a + " → " + item.slide_b + ": imagen/recurso sustituido (" + item.removed_hashes.length + " retirado(s), " + item.added_hashes.length + " nuevo(s))."));
    }
    for (const item of media.added) {
      list.append(element("li", "",
        "Agregado en B: " + item.part + " (" + formatBytes(item.size) + ", SHA-256 " + item.sha256.slice(0, 12) + "…)"));
    }
    for (const item of media.removed) {
      list.append(element("li", "",
        "Eliminado de A: " + item.part + " (" + formatBytes(item.size) + ", SHA-256 " + item.sha256.slice(0, 12) + "…)"));
    }
    for (const item of media.identical) {
      list.append(element("li", UI.muted,
        "Recurso idéntico (" + formatBytes(item.size) + ", SHA-256 " + item.sha256.slice(0, 12) + "…)"));
    }
    mediaBox.append(list);
    box.append(mediaBox);
  }

  return box;
}

function renderDuel(result, url, presentation) {
  const body = $("duel-body");
  const filter = element("select", UI.select);
  filter.setAttribute("aria-label", "Filtrar diferencias de metadatos");
  filter.append(new Option("Todos los resultados", ""),
    ...Object.entries(comparisonLabels).map(([key, [, label]]) => new Option(label, key)));
  const technical = element("input", "");
  technical.type = "checkbox";
  technical.checked = presentation.technical ?? false;
  filter.value = presentation.filter ?? "";
  const table = element("div", "");
  const summary = element("ul", UI.duelFilters);
  const update = () => {
    const scope = result.rows.filter((row) => technical.checked || !row.technical);
    const counts = Object.fromEntries(Object.keys(comparisonLabels).map((key) => [key, 0]));
    for (const row of scope) counts[row.status]++;
    summary.replaceChildren(...Object.entries(counts).map(([key, count]) =>
      element("li", UI.status, comparisonLabels[key][1] + " " + numbers.format(count))));
    const rows = scope.filter((row) => !filter.value || row.status === filter.value);
    table.replaceChildren(rows.length ? compareTable(rows) : element("p", UI.muted, "Ningún campo coincide con este filtro."));
  };
  filter.addEventListener("change", update);
  technical.addEventListener("change", update);
  const swap = button("Intercambiar A y B", () => enterWorkbench("duel",
    [result.session_id, result.b.id, result.a.id], {replace: true, focusSwap: true, filter: filter.value,
      technical: technical.checked}));

  const metadataSection = element("section", UI.duelSection,
    element("div", UI.duelHead, element("h2", UI.h2, "C. Comparación de metadatos")),
    ...(result.metadata_comparison?.note ? [element("p", UI.smallMuted, result.metadata_comparison.note)] : []),
    summary,
    ...(result.notes.length ? [element("ul", UI.duelNotes + " text-[0.875rem] text-muted",
      ...result.notes.map((note) => element("li", "", note)))] : []),
    element("div", UI.duelFilters, filter, element("label", UI.check, technical, "Incluir técnicos")),
    table);

  const blocks = [
    element("div", "grid grid-cols-2 max-md:grid-cols-1 gap-3.5", compareSide("Archivo A", result.a), compareSide("Archivo B", result.b)),
    element("div", "flex flex-wrap gap-2", swap,
      link("Descargar comparación JSON", url + "&download=true", "button subtle"),
      link("Descargar informe HTML", url + "&download=true&format=html", "button subtle")),
  ];
  const simCard = similaritySection(result.similarity);
  if (simCard) blocks.push(simCard);
  const binCard = binaryIdentitySection(result.binary_identity, result);
  if (binCard) blocks.push(binCard);
  const diffCard = deepDiffSection(result.content_comparison, result.similarity);
  if (diffCard) blocks.push(diffCard);
  blocks.push(metadataSection);

  body.replaceChildren(...blocks);
  update();
  return swap;
}

function renderDuelHub() {
  const body = $("duel-body");
  const panel = element("section", UI.labPanel);
  panel.append(
    element("h2", UI.h2, "Iniciar un duelo de documentos"),
    element("p", UI.smallMuted, "Compara identidad binaria, contenido profundo (PPTX) y metadatos de dos archivos."));

  if (state.session && state.session.files.length >= 2) {
    const selA = element("select", UI.select);
    const selB = element("select", UI.select);
    selA.setAttribute("aria-label", "Archivo A");
    selB.setAttribute("aria-label", "Archivo B");
    for (const file of state.session.files) {
      selA.append(new Option(file.name + " (" + display(file.format) + ")", file.id));
      selB.append(new Option(file.name + " (" + display(file.format) + ")", file.id));
    }
    selB.selectedIndex = 1;
    const err = element("p", UI.errorText);
    err.hidden = true;
    const go = button("Comparar archivos seleccionados", () => {
      if (selA.value === selB.value) {
        err.textContent = "Selecciona dos archivos distintos de la sesión.";
        err.hidden = false;
        return;
      }
      enterWorkbench("duel", [state.session.id, selA.value, selB.value]);
    }, "primary");
    panel.append(
      element("div", UI.compareGrid,
        element("label", UI.fieldGroup, element("span", UI.smallMuted, "Archivo A (sesión actual)"), selA),
        element("label", UI.fieldGroup, element("span", UI.smallMuted, "Archivo B (sesión actual)"), selB)),
      err,
      element("div", "flex flex-wrap gap-2", go, link("Ir a la tabla de archivos", "#workspace", "button subtle")));
  } else {
    panel.append(
      element("p", UI.muted, "Carga al menos dos archivos en el Analizador para compararlos en un duelo, o abre una sesión guardada en Historial."),
      element("div", "flex flex-wrap gap-2",
        link("Ir al Analizador", "#workspace", "button primary"),
        link("Abrir Historial", "#history", "button subtle")));
  }
  body.replaceChildren(panel);
}

async function loadDuel(sessionId, first, second, route, presentation) {
  const url = sessionPath(sessionId) + "/duel?" + new URLSearchParams({file_a: first, file_b: second});
  const result = await api(url);
  if (state.route !== route) return;
  const swap = renderDuel(result, url, presentation);
  if (presentation.focusSwap) swap.focus();
}
