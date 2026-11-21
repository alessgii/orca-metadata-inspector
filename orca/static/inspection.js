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
  if (!compact || text.length <= 72) return element("span", "metadata-value", text);
  return element("details", "long-value", element("summary", "", text.slice(0, 62) + "…"),
    element("div", "metadata-value", text));
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
    const fallback = element("textarea", "evidence-fallback");
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
  const summary = element("summary", "small", icon("copy"), "Evidencia");
  const panel = element("div", "evidence-panel small");
  const control = element("details", "evidence-control", summary, panel);
  let loaded = false;
  control.addEventListener("toggle", async () => {
    if (!control.open || loaded) return;
    loaded = true;
    panel.textContent = "Preparando evidencia…";
    try {
      const options = new URLSearchParams({kind, ...(kind === "raw" ? {tag: selector} : {index: selector})});
      const evidence = await api(filePath(view.session.id, view.record.id) + "/evidence?" + options);
      const status = element("p", "muted evidence-status");
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
      if (Object.keys(evidence.commands).length) panel.append(element("p", "muted",
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
  const box = element("div", "timeline-view timeline-advanced");
  const hasDates = Object.values(timeline.groups).some((items) => items.length);
  if (!hasDates) {
    box.append(element("p", "muted", "No se encontraron fechas en los campos examinados."));
    return box;
  }
  box.append(element("p", "small muted", "Las marcas registradas provienen del archivo y pueden describir eventos distintos."));
  if (timeline.interval) {
    const {seconds, created, modified, created_sources, modified_sources} = timeline.interval;
    box.append(element("div", "inspection-callout", element("strong", "", "Intervalo entre creación y modificación"),
      element("p", "", intervalText(seconds)),
      element("p", "small mono muted", created + " / " + modified),
      element("details", "", element("summary", "small", "Fuentes del intervalo"),
        element("p", "small", "Creación · " + created_sources.map((source) => source.join(" / ")).join(" · ")),
        element("p", "small", "Modificación · " + modified_sources.map((source) => source.join(" / ")).join(" · ")))));
  }
  for (const warning of timeline.warnings) box.append(element("p", "notice", warning));
  const names = {instant: "Instantes ordenados en UTC", local: "Horas sin conversión a un instante",
    partial: "Fechas con precisión de calendario", invalid: "Valores sin interpretar"};
  for (const [group, events] of Object.entries(timeline.groups)) {
    if (!events.length) continue;
    const list = element("ul", "time-events");
    for (const event of events) {
      const details = element("details", "event-source", element("summary", "small", "Valor original y procedencia"),
        element("div", "small metadata-value", recordedValue(event.original)),
        element("code", "", event.source.join(" / ")));
      if (event.timezone_source) details.append(element("p", "small muted", "Zona de " + event.timezone_source.join(" / ")));
      details.append(element("p", "small muted", "Precisión: " + ({
        year: "año", month: "mes", day: "día", hour: "hora", minute: "minuto", second: "segundo",
        fraction: "fracción de segundo",
      }[event.precision] ?? "indeterminada")));
      if (event.utc) details.append(element("p", "small muted mono", event.utc + " (UTC)"));
      const warnings = event.warnings.map((warning) => element("p", "small muted", warning));
      list.append(element("li", "time-event", element("div", "time-event-title",
        element("strong", "", event.label), element("span", "small muted",
          event.origin === "filesystem" ? "Sistema de archivos" : "Metadato interno")),
      element("p", "metadata-value", dateText(event)), element("code", "", event.source.join(" / ")),
      ...warnings, details));
    }
    box.append(element("div", "time-group", element("h3", "small muted", names[group]), list));
  }
  return box;
}

function exposureComparisonView(difference) {
  const box = element("section", "inspection-callout", element("h3", "", "Exposición antes y después"),
    element("p", "small", "Antes " + difference.before + " / Después " + difference.after));
  for (const [key, label] of Object.entries({removed: "Eliminadas", retained: "Persistentes", added: "Añadidas"})) {
    box.append(element("details", "", element("summary", "", label + " (" + difference[key].length + ")"),
      difference[key].length ? element("ul", "exposure-delta", ...difference[key].map((item) =>
        element("li", "", element("strong", "", item.label), " · ", recordedValue(item.value),
          element("div", "small mono muted", item.source.join(" / "))))) : element("p", "small muted", "Sin registros.")));
  }
  return box;
}

function exposureView(view) {
  const {exposure, exposure_comparison: previous} = view.context;
  const box = element("section", "exposure-view", element("h2", "", "¿Qué estoy filtrando?"));
  box.append(element("p", "small muted",
    "Los niveles son orientativos y describen los campos examinados. Pueden existir datos sensibles en el contenido visible o en estructuras fuera de esta revisión."));
  if (!exposure.total) box.append(element("p", "", "No se detectaron exposiciones en las categorías examinadas."));
  if (view.session.temporary && cleanupKinds.has(view.record.format) && ["ok", "empty"].includes(view.record.status)) {
    box.append(button("Abrir Antes / Después", () => {
      renderDetail(view, "clean");
      $("file-tools").querySelector("[aria-pressed=true]").focus();
    }, "accent-button"));
  } else {
    box.append(element("p", "small muted", view.session.temporary
      ? "Este formato o estado admite únicamente inspección en Orca."
      : "Para limpiar un archivo del historial, vuelve a cargar el original."));
  }
  const comparison = view.cleaned?.exposure_comparison ?? previous;
  if (comparison) box.append(exposureComparisonView(comparison));
  let index = 0;
  for (const category of exposure.categories) {
    const list = element("ul", "exposure-list");
    for (const item of category.items) {
      const level = element("span", "status exposure-level", item.level === "potential" ? "Exposición potencial" : "Informativo");
      level.dataset.level = item.level;
      list.append(element("li", "finding",
        element("div", "exposure-head", element("strong", "", item.label), level),
        metadataValue(item.value, true),
        element("p", "small muted", item.description + " " + item.reason),
        element("p", "small mono muted", item.sources.map((source) => source.join(" / ")).join(" · ")),
        element("p", "small mono muted", item.rule),
        evidenceControl(view, "exposure", index++)));
    }
    const section = element("details", "", element("summary", "", category.label + " (" + category.items.length + ")"), list);
    section.open = category.id === "identity" || category.id === "location";
    box.append(section);
  }
  return box;
}

function inspectionContextView(view, tab) {
  if (view.context) return tab === "timeline" ? timelineView(view) : exposureView(view);
  if (view.contextError) return element("p", "notice", view.contextError);
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
  return element("p", "muted", "Leyendo los metadatos guardados…");
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
  return element("section", "duel-file",
    element("strong", "", label), element("h3", "metadata-value", file.name),
    element("p", "small muted", display(file.format) + " · " + formatBytes(file.size) + " · " + statusNames[file.status]),
    element("p", "small muted", "SHA-256"),
    hashControl(file.sha256));
}

function comparisonCell(entries, label) {
  const cell = element("div", "duel-cell");
  cell.setAttribute("role", "cell");
  cell.append(element("span", "duel-mobile-label small muted", label));
  if (!entries.length) cell.append(element("span", "muted small", "Ausente"));
  for (const item of entries) {
    const value = element("div", "duel-value", metadataValue(item.original, true));
    if (Object.hasOwn(item, "interpreted")) value.append(element("p", "small muted", "Interpretado: " + display(item.interpreted)));
    if (item.source) value.append(element("code", "small muted", item.source.join(" / ")));
    if (item.interpretation_source) value.append(element("p", "small muted",
      "Hemisferio · " + item.interpretation_source.join(" / ")));
    for (const date of item.dates ?? []) {
      if (date.utc) value.append(element("p", "small muted", "UTC " + date.utc));
    }
    cell.append(value);
  }
  return cell;
}

function compareTable(rows) {
  const table = element("div", "duel-table");
  table.setAttribute("role", "table");
  table.setAttribute("aria-label", "Comparación de metadatos");
  const head = element("div", "duel-row duel-table-head");
  head.setAttribute("role", "row");
  for (const text of ["Propiedad", "Archivo A", "Archivo B"]) {
    const cell = element("span", "small", text);
    cell.setAttribute("role", "columnheader");
    head.append(cell);
  }
  table.append(head);
  for (const row of rows) {
    const [symbol, label] = comparisonLabels[row.status];
    const badge = element("span", "status duel-badge", symbol + " " + label);
    badge.dataset.diff = row.status;
    const heading = element("div", "duel-property", element("strong", "metadata-value", row.label), badge);
    heading.setAttribute("role", "rowheader");
    if (row.explanation) heading.append(element("p", "small muted", row.explanation));
    const node = element("div", "duel-row", heading,
      comparisonCell(row.a, "Archivo A"), comparisonCell(row.b, "Archivo B"));
    node.setAttribute("role", "row");
    table.append(node);
  }
  return table;
}

function renderDuel(result, url, presentation) {
  const body = $("duel-body");
  const filter = element("select", "");
  filter.setAttribute("aria-label", "Filtrar diferencias");
  filter.append(new Option("Todos los resultados", ""),
    ...Object.entries(comparisonLabels).map(([key, [, label]]) => new Option(label, key)));
  const technical = element("input", "");
  technical.type = "checkbox";
  technical.checked = presentation.technical ?? false;
  filter.value = presentation.filter ?? "";
  const table = element("div", "");
  const summary = element("ul", "duel-counts");
  const update = () => {
    const scope = result.rows.filter((row) => technical.checked || !row.technical);
    const counts = Object.fromEntries(Object.keys(comparisonLabels).map((key) => [key, 0]));
    for (const row of scope) counts[row.status]++;
    summary.replaceChildren(...Object.entries(counts).map(([key, count]) =>
      element("li", "status", comparisonLabels[key][1] + " " + numbers.format(count))));
    const rows = scope.filter((row) => !filter.value || row.status === filter.value);
    table.replaceChildren(rows.length ? compareTable(rows) : element("p", "muted", "Ningún campo coincide con este filtro."));
  };
  filter.addEventListener("change", update);
  technical.addEventListener("change", update);
  const swap = button("Intercambiar A y B", () => enterWorkbench("duel",
    [result.session_id, result.b.id, result.a.id], {replace: true, focusSwap: true, filter: filter.value,
      technical: technical.checked}));
  body.replaceChildren(element("div", "duel-pair", compareSide("Archivo A", result.a), compareSide("Archivo B", result.b)),
    element("div", "flex flex-wrap gap-2", swap,
      link("Descargar comparación JSON", url + "&download=true", "button subtle"),
      link("Descargar informe HTML", url + "&download=true&format=html", "button subtle")),
    summary, ...(result.notes.length ? [element("ul", "duel-notes small muted",
      ...result.notes.map((note) => element("li", "", note)))] : []),
    element("div", "duel-filters", filter, element("label", "check flex items-center gap-2", technical, "Incluir técnicos")),
    table);
  update();
  return swap;
}

async function loadDuel(sessionId, first, second, route, presentation) {
  const url = sessionPath(sessionId) + "/duel?" + new URLSearchParams({file_a: first, file_b: second});
  const result = await api(url);
  if (state.route !== route) return;
  const swap = renderDuel(result, url, presentation);
  if (presentation.focusSwap) swap.focus();
}
