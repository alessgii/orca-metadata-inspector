"use strict";

const $ = (id) => document.getElementById(id);
const statusNames = {ok: "Analizado", empty: "Sin descriptivos", unsupported: "No compatible", error: "Error"};
const pages = ["workspace", "history", "guide", "detail", "duel", "hashes", "manifests", "investigations", "investigation"];
const storedId = /^[a-f0-9]{32}$/;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const fields = {
  author: "Autor declarado", organization: "Organización", username: "Usuario", created: "Creación",
  modified: "Modificación", software: "Software", version: "Versión", operating_system: "Sistema operativo",
  make: "Fabricante", model: "Modelo", latitude: "Latitud", longitude: "Longitud", gps: "Coordenadas",
  title: "Título", subject: "Asunto", comments: "Comentarios", keywords: "Palabras clave",
  path: "Ruta", identifier: "Identificador", property: "Propiedad",
};
const groupNames = {identity: "Identidad", dates: "Fechas", software: "Aplicaciones", device: "Dispositivo",
  location: "Ubicación", document: "Documento", other: "Otros campos"};
const cleanupKinds = new Set(["JPEG", "PNG", "PDF", "DOCX", "XLSX", "PPTX"]);
const numbers = new Intl.NumberFormat("es");
const order = new Intl.Collator("es", {numeric: true, sensitivity: "base"});
const state = {
  files: [], nextId: 0, working: false, engine: false, session: null, sort: "name", ascending: true,
  inspection: null, historyVersion: 0, selected: [], route: 0, page: null, returnFocus: null,
};

function element(tag, className, ...children) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  result.append(...children);
  return result;
}

function button(text, handler, style = "subtle") {
  const result = element("button", "button " + style, text);
  result.type = "button";
  result.addEventListener("click", handler);
  return result;
}

function link(text, href, style) {
  const result = element("a", style, text);
  result.href = href;
  return result;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS(svg.namespaceURI, "use");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  use.setAttribute("href", "/static/icons.svg#" + name);
  svg.append(use);
  return svg;
}

function display(value) {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

function formatBytes(bytes) {
  if (bytes < 1000) return numbers.format(bytes) + " B";
  return new Intl.NumberFormat("es", {maximumFractionDigits: 1})
    .format(bytes / (bytes < 1e6 ? 1000 : 1e6)) + (bytes < 1e6 ? " kB" : " MB");
}

function timestamp(iso) {
  return new Intl.DateTimeFormat("es", {dateStyle: "medium", timeStyle: "short"}).format(new Date(iso));
}

function message(id, text, error = false) {
  const node = $(id);
  node.textContent = text ?? "";
  node.hidden = !text;
  node.dataset.error = String(error);
}

async function api(path, options) {
  const response = await fetch(path, {...options, cache: "no-store"});
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(typeof result.detail === "string" ? result.detail : "No se pudo completar la operación local.");
  }
  return response.status === 204 ? null : response.json();
}

function sessionPath(id) {
  return "/api/sessions/" + encodeURIComponent(id);
}

function filePath(sessionId, fileId) {
  return sessionPath(sessionId) + "/files/" + encodeURIComponent(fileId);
}

async function checkEngine() {
  try {
    const engine = await api("/api/health");
    state.engine = engine.available;
    $("engine").textContent = engine.available ? "ExifTool " + engine.version : "ExifTool no disponible";
    $("engine-dot").dataset.ready = engine.available ? "yes" : "no";
    if (!engine.available) message("upload-message", "ExifTool no está disponible. Consulta la instalación en el README.", true);
  } catch {
    $("engine").textContent = "Servidor no disponible";
    $("engine-dot").dataset.ready = "no";
    message("upload-message", "No se pudo conectar con Orca. Comprueba que el servidor local esté en ejecución.", true);
  }
  renderQueue();
}

const compactSidebar = window.matchMedia("(max-width: 768px)");

const pageTitles = {
  workspace: "Analizador de archivos",
  investigations: "Investigaciones",
  investigation: "Investigación",
  duel: "Duelo de documentos",
  hashes: "Laboratorio de hashes",
  history: "Historial local",
  manifests: "Manifiestos de integridad",
  guide: "Guía de uso",
  detail: "Inspector de archivo",
};

function setTopbarContext(text) {
  const node = $("topbar-context");
  if (!node) return;
  node.textContent = text;
  node.title = text;
}

function truncatedCell(value, className = "table-data") {
  const text = display(value);
  const span = element("span", "truncate-cell", text);
  if (text !== "—") span.title = text;
  return element("td", className, span);
}

function setSidebar(pinned) {
  const sidebar = $("sidebar");
  sidebar.dataset.pinned = String(Boolean(pinned));
  const toggle = $("sidebar-toggle");
  const label = pinned ? "Contraer barra lateral" : "Fijar barra lateral";
  toggle.setAttribute("aria-pressed", String(Boolean(pinned)));
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  const text = $("sidebar-pin-label");
  if (text) text.textContent = pinned ? "Modo compacto" : "Fijar barra";
}

function focusHeading(page) {
  const heading = $("page-" + page).querySelector("h1");
  heading.focus({preventScroll: true});
  window.scrollTo(0, 0);
}

function enterWorkbench(page, ids, {replace = false, ...presentation} = {}) {
  const hash = "#" + [page, ...ids].join("/");
  if (!replace) {
    state.returnFocus = document.activeElement;
    history.pushState({orcaWorkbench: true}, "", hash);
  } else {
    history.replaceState(history.state, "", hash);
  }
  updatePage(presentation);
}

async function updatePage(presentation = {}) {
  const route = ++state.route;
  const [name, ...ids] = location.hash.slice(1).split("/");
  const workbench = (name === "detail" && ids.length === 2 || name === "duel" && ids.length === 3)
    && ids.every((id) => storedId.test(id));
  const invView = name === "investigation" && ids.length >= 1 && uuidPattern.test(ids[0]);
  const current = workbench || invView || ["history", "guide", "hashes", "manifests", "duel", "investigations"].includes(name) ? name : "workspace";
  const previous = state.page;
  state.page = current;
  state.inspection = null;
  $("file-tools").hidden = true;
  $("file-tools").replaceChildren();
  for (const page of pages) $("page-" + page).hidden = page !== current;
  const navTarget = current === "detail" ? "workspace" : (current === "investigation" ? "investigations" : current);
  for (const item of document.querySelectorAll("[data-page]")) {
    if (item.dataset.page === navTarget) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  setTopbarContext(pageTitles[current] || "Analizador");
  if (!workbench) {
    const trigger = state.returnFocus;
    state.returnFocus = null;
    if (current === "workspace" && trigger?.isConnected && !trigger.closest("[hidden]")) trigger.focus();
    else if (previous && previous !== current && !invView) focusHeading(current);
    if (current === "history") loadHistory();
    if (current === "duel") renderDuelHub();
    if (current === "investigations") loadInvestigationsList();
    if (current === "investigation" && invView) openInvestigation(ids[0], ids[1] || null);
    if (current === "hashes") {
      if (ids[0] && ["calc", "compare", "history"].includes(ids[0])) setHashTab(ids[0]);
      else if (hashLabState.tab === "history") loadHashHistory();
    }
    return;
  }

  const [sessionId, ...files] = ids;
  const body = $(current + "-body");
  if (current === "detail") {
    $("detail-title").textContent = "Inspector de archivo";
    $("detail-summary").replaceChildren();
  }
  body.setAttribute("aria-busy", "true");
  body.replaceChildren(element("p", "view-loading muted", "Leyendo los metadatos guardados…"));
  if (!presentation.focusSwap) focusHeading(current);
  try {
    const session = state.session?.id === sessionId ? state.session : await api(sessionPath(sessionId));
    if (state.route !== route) return;
    if (session.state !== "complete") throw new Error("Esta sesión todavía no tiene resultados disponibles.");
    if (state.session?.id !== sessionId) renderResults(session);
    if (current === "detail") {
      const record = await api(filePath(sessionId, files[0]));
      if (state.route !== route) return;
      const view = {session, record, cleaned: null, cleaning: false, error: ""};
      state.inspection = view;
      $("detail-title").textContent = record.name;
      setTopbarContext("Analizador · " + record.name);
      renderDetailSummary(view);
      renderDetail(view);
    } else {
      await loadDuel(sessionId, ...files, route, presentation);
    }
  } catch (error) {
    if (state.route === route) {
      const notice = element("p", "notice", error.message);
      notice.dataset.error = "true";
      notice.setAttribute("role", "alert");
      body.replaceChildren(notice);
    }
  } finally {
    if (state.route === route) body.removeAttribute("aria-busy");
  }
}

function setUploadOpen(open) {
  $("intake").hidden = !open;
  $("reopen-upload").hidden = open;
  $("collapse-upload").hidden = !state.session;
}

function addFiles(files) {
  if (state.working || !files.length) return;
  if (files.length + state.files.length > 100) {
    message("upload-message", "Selecciona hasta 100 elementos por carga.", true);
    return;
  }
  state.files.push(...Array.from(files, (file) => ({file, id: ++state.nextId})));
  message("upload-message");
  renderQueue();
}

const extFormatMap = {
  pdf: "PDF", docx: "DOCX", xlsx: "XLSX", pptx: "PPTX",
  jpg: "JPEG", jpeg: "JPEG", png: "PNG", webp: "WebP", zip: "ZIP",
};

function renderIntakeContext(progressInfo = null) {
  const titleNode = $("context-title");
  const body = $("intake-context-body");
  if (!titleNode || !body) return;

  // 1. Real analysis in progress (no fake percentages)
  if (state.working) {
    titleNode.textContent = "Análisis en curso";
    const info = progressInfo || {title: "Cargando y validando archivos", processed: null, total: null, current: ""};
    const bar = element("progress", "");
    bar.setAttribute("aria-label", "Progreso real del análisis");
    if (typeof info.processed === "number" && typeof info.total === "number" && info.total > 0) {
      bar.max = info.total;
      bar.value = info.processed;
    }
    const countText = (typeof info.processed === "number" && typeof info.total === "number")
      ? numbers.format(info.processed) + " / " + numbers.format(info.total) + " archivo(s)"
      : "Preparando lote…";
    const card = element("div", "context-progress",
      element("div", "flex items-center justify-between gap-2 flex-wrap",
        element("strong", "small", info.title),
        element("span", "mono small muted", countText)),
      bar,
      element("p", "small muted truncate-cell", info.current || "Esperando respuesta del motor local…"));
    body.replaceChildren(
      card,
      element("p", "small muted", "El procesamiento ocurre de forma local con ExifTool. Los archivos originales permanecen en tu equipo."));
    return;
  }

  // 2. Files queued for analysis
  if (state.files.length > 0) {
    titleNode.textContent = "Resumen de selección";
    const totalBytes = state.files.reduce((sum, item) => sum + item.file.size, 0);
    const counts = new Map();
    for (const {file} of state.files) {
      const ext = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
      const fmt = extFormatMap[ext] || "Otro";
      counts.set(fmt, (counts.get(fmt) || 0) + 1);
    }
    const stats = element("div", "stats",
      element("div", "stat", element("strong", "", numbers.format(state.files.length)), element("span", "", state.files.length === 1 ? "Archivo" : "Archivos")),
      element("div", "stat", element("strong", "", formatBytes(totalBytes)), element("span", "", "Tamaño total")));

    const formatChips = element("div", "context-formats",
      ...Array.from(counts.entries()).map(([fmt, count]) =>
        element("span", "status", fmt + " (" + numbers.format(count) + ")")));

    const saveHint = $("keep").checked
      ? "Los resultados se guardarán en el historial SQLite local (sin copias permanentes de los originales)."
      : "Sesión temporal: los archivos y copias caducan automáticamente al cerrar o vencer la retención.";

    body.replaceChildren(
      element("div", "slide-diff-body",
        stats,
        element("div", "field-group",
          element("span", "small muted", "Formatos por extensión (se confirmarán por firma binaria):"),
          formatChips)),
      element("p", "small muted", saveHint));
    return;
  }

  // 3. Completed session summary + contextual shortcuts
  if (state.session) {
    titleNode.textContent = "Última sesión analizada";
    const s = state.session.summary;
    const stats = element("div", "stats",
      element("div", "stat", element("strong", "", numbers.format(s.count)), element("span", "", "Archivos")),
      element("div", "stat", element("strong", "", numbers.format(s.properties)), element("span", "", "Propiedades")),
      element("div", "stat", element("strong", "", numbers.format(s.findings)), element("span", "", "Hallazgos")),
      element("div", "stat", element("strong", "", numbers.format(s.statuses.error)), element("span", "", "Errores")));
    const actions = element("div", "context-links");
    if (state.session.files.length >= 2) {
      actions.append(link("Abrir Duelo", "#duel", "button accent-button small-btn"));
    }
    actions.append(
      link("Investigaciones", "#investigations", "button subtle small-btn"),
      link("Laboratorio de hashes", "#hashes", "button subtle small-btn"),
      link("Manifiestos", "#manifests", "button subtle small-btn"));
    body.replaceChildren(
      element("div", "slide-diff-body",
        stats,
        element("p", "small muted",
          "Consulta los resultados en la tabla inferior o selecciona nuevos archivos a la izquierda para iniciar otro análisis.")),
      actions);
    return;
  }

  // 4. Initial empty state: concise 3-step quick guide
  titleNode.textContent = "Cómo empezar";
  const steps = element("ol", "context-steps",
    element("li", "", element("span", "step-num", "1"),
      element("div", "", element("strong", "", "Selecciona o arrastra archivos"),
        element("p", "", "Admite PDF, Office, imágenes y paquetes ZIP directamente desde tu equipo."))),
    element("li", "", element("span", "step-num", "2"),
      element("div", "", element("strong", "", "Ejecuta el análisis local"),
        element("p", "", "Extrae metadatos con ExifTool, calcula SHA-256 y detecta exposiciones sin conexión."))),
    element("li", "", element("span", "step-num", "3"),
      element("div", "", element("strong", "", "Inspecciona, compara o limpia"),
        element("p", "", "Revisa detalles, cronología, compara dos documentos o genera una copia saneada verificada."))));
  const links = element("div", "context-links",
    link("Investigaciones", "#investigations", "button subtle small-btn"),
    link("Laboratorio de hashes", "#hashes", "button subtle small-btn"),
    link("Manifiestos", "#manifests", "button subtle small-btn"));
  body.replaceChildren(steps, links);
}

function renderQueue() {
  $("queue").hidden = !state.files.length;
  $("reset").hidden = !state.files.length;
  $("pick").disabled = state.working;
  $("reset").disabled = state.working;
  $("keep").disabled = state.working;
  $("collapse-upload").disabled = state.working;
  $("analyze").disabled = !state.files.length || state.working || !state.engine;
  const rows = state.files.map(({file, id}) => {
    const name = element("span", "truncate-cell", file.name);
    name.title = file.name;
    const remove = button(icon("x"), () => {
      state.files = state.files.filter((item) => item.id !== id);
      message("upload-message");
      renderQueue();
      const next = $("queue").querySelector("button");
      (next ?? $("pick")).focus();
    }, "icon-button");
    remove.disabled = state.working;
    remove.setAttribute("aria-label", "Quitar " + file.name);
    return element("li", "flex items-center gap-2 justify-between small",
      element("div", "flex items-center gap-2 min-w-0", icon("file"), name),
      element("div", "flex items-center gap-2", element("span", "muted mono whitespace-nowrap", formatBytes(file.size)), remove));
  });
  $("queue").replaceChildren(...rows);
  renderIntakeContext();
}

function validateSelection() {
  const files = state.files.map((item) => item.file);
  if (files.some((file) => file.size > 100_000_000)) {
    throw new Error("Cada ZIP o archivo individual admite hasta 100 MB (100 000 000 bytes).");
  }
  const direct = files.filter((file) => !file.name.toLowerCase().endsWith(".zip"));
  if (direct.reduce((total, file) => total + file.size, 0) > 100_000_000) {
    throw new Error("El tamaño acumulado de los archivos individuales supera 100 MB.");
  }
}

async function analyze() {
  if (state.working || !state.files.length) return;
  try {
    validateSelection();
  } catch (error) {
    message("upload-message", error.message, true);
    return;
  }
  state.working = true;
  renderQueue();
  message("upload-message");
  $("progress-box").hidden = false;
  $("progress").removeAttribute("value");
  $("progress-title").textContent = "Cargando y validando archivos";
  $("progress-count").textContent = "";
  $("progress-current").textContent = "";
  try {
    const form = new FormData();
    for (const {file} of state.files) form.append("files", file);
    form.set("keep", $("keep").checked ? "1" : "0");
    const {id} = await api("/api/sessions", {method: "POST", body: form});
    let next;
    let failures = 0;
    while (true) {
      try {
        next = await api(sessionPath(id));
        failures = 0;
      } catch (error) {
        if (++failures >= 3) throw error;
        await new Promise((resolve) => setTimeout(resolve, 700));
        continue;
      }
      if (next.state === "complete") break;
      if (next.state === "failed") throw new Error(next.error);
      $("progress-title").textContent = "Extrayendo metadatos";
      $("progress-count").textContent = next.processed + " / " + next.total;
      $("progress").max = next.total;
      $("progress").value = next.processed;
      $("progress-current").textContent = next.current ?? "Sesión en espera";
      renderIntakeContext({
        title: "Extrayendo metadatos",
        processed: next.processed,
        total: next.total,
        current: next.current ?? "Sesión en espera",
      });
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    state.files = [];
    renderResults(next);
    if (next.notice) message("upload-message", next.notice);
    if (state.page === "history") loadHistory();
    if (state.page === "workspace") {
      $("results-title").tabIndex = -1;
      $("results-title").focus();
    }
  } catch (error) {
    message("upload-message", error.message, true);
  } finally {
    state.working = false;
    $("progress-box").hidden = true;
    renderQueue();
  }
}

function renderResults(session) {
  const changed = state.session?.id !== session?.id;
  if (changed) state.selected = [];
  state.session = session;
  if (changed) setUploadOpen(!session || state.files.length > 0);
  const loaded = Boolean(session);
  $("empty-results").hidden = loaded;
  for (const id of ["files", "table-controls", "result-actions", "result-meta", "session-summary", "correlation-panel"]) {
    $(id).hidden = !loaded;
  }
  const canCompare = loaded && session.files.length >= 2;
  $("duel-toolbar").hidden = $("duel-column").hidden = !canCompare;
  for (const sort of document.querySelectorAll("[data-sort]")) {
    sort.disabled = !loaded;
    if (!loaded) sort.parentElement.removeAttribute("aria-sort");
  }
  if (!session) {
    renderIntakeContext();
    return;
  }
  renderIntakeContext();
  const summary = session.summary;
  $("result-meta").textContent = [
    timestamp(session.created), (session.duration_ms / 1000).toLocaleString("es") + " s",
    session.saved ? "Guardado en el historial" : "Sesión temporal",
  ].join(" / ");
  $("downloads").replaceChildren(...["json", "csv", "html"].map((kind) =>
    link(kind.toUpperCase(), sessionPath(session.id) + "/export/" + kind)));
  const metrics = [
    ["Archivos", summary.count], ["Propiedades", summary.properties], ["Hallazgos", summary.findings],
    ["Errores", summary.statuses.error],
  ];
  $("stats").replaceChildren(...metrics.map(([label, value]) => element("div", "stat",
    element("strong", "", numbers.format(value)), element("span", "", label))));
  const breakdown = [
    ["Formatos", Object.entries(summary.formats).map(([key, value]) => key + " (" + value + ")")],
    ["Autores (" + summary.authors.length + ")", summary.authors],
    ["Software (" + summary.software.length + ")", summary.software],
    ["Estados", Object.entries(summary.statuses).map(([key, value]) => statusNames[key] + " (" + value + ")")],
  ];
  $("breakdown").replaceChildren(...breakdown.map(([label, values]) => element("div", "",
    element("h3", "", label), values.length ? element("ul", "", ...values.map((value) => element("li", "", value)))
      : element("p", "small muted", "Sin datos detectados"))));
  $("search").value = "";
  setOptions("format-filter", "Todos los formatos", Object.keys(summary.formats).map((item) => [item, item]));
  setOptions("status-filter", "Todos los estados", Object.entries(statusNames));
  renderTable();
  $("correlations").replaceChildren(...(summary.correlations.length
    ? summary.correlations.map((match) => {
        const names = match.files.map((file) => {
          const action = button(file.name, () => enterWorkbench("detail", [session.id, file.id]), "subtle");
          return element("li", "small", action, " ", element("code", "muted", file.source.join(" / ")));
        });
        return element("details", "correlation",
          element("summary", "", fields[match.field] + " · " + display(match.value) + " (" + match.files.length + ")"),
          element("ul", "", ...names));
      })
    : [element("p", "small muted", "No se encontraron campos y valores compartidos.")]));
}

function setOptions(id, label, options) {
  $(id).replaceChildren(new Option(label, ""), ...options.map(([value, text]) => new Option(text, value)));
}

function renderTable() {
  const session = state.session;
  if (!session) return;
  const query = $("search").value.toLocaleLowerCase("es").trim();
  const format = $("format-filter").value;
  const status = $("status-filter").value;
  const relevantDate = (row) => row.created ?? row.modified;
  const sortValue = (row) => state.sort === "date" ? relevantDate(row) : row[state.sort];
  const rows = session.files.filter((row) =>
    (!format || format === (row.format ?? "Sin confirmar")) && (!status || status === row.status) &&
    (!query || [row.name, row.author, row.software, row.format, ...row.errors].some((value) =>
      display(value).toLocaleLowerCase("es").includes(query))));
  rows.sort((first, second) => {
    const a = sortValue(first) ?? "";
    const b = sortValue(second) ?? "";
    const diff = typeof a === "number" && typeof b === "number" ? a - b : order.compare(String(a), String(b));
    return state.ascending ? diff : -diff;
  });
  for (const item of document.querySelectorAll("[data-sort]")) {
    if (item.dataset.sort === state.sort) item.parentElement.setAttribute("aria-sort", state.ascending ? "ascending" : "descending");
    else item.parentElement.removeAttribute("aria-sort");
  }
  $("match-count").textContent = "(" + rows.length + " de " + session.files.length + ")";
  const output = rows.map((row) => {
    const status = element("span", "status", statusNames[row.status]);
    status.dataset.status = row.status;
    const target = "#" + ["detail", session.id, row.id].join("/");
    const nameText = element("span", "truncate-cell", row.name);
    const fileLink = link("", target, "file-link");
    fileLink.title = row.name;
    fileLink.append(icon("file"), nameText);
    fileLink.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      enterWorkbench("detail", [session.id, row.id]);
    });
    const action = button("Detalles", () => enterWorkbench("detail", [session.id, row.id]), "subtle small-btn");
    action.setAttribute("aria-label", "Detalles de " + row.name);
    const selection = element("td", "duel-check", comparisonCheckbox(row));
    selection.hidden = session.files.length < 2;
    return element("tr", "", selection,
      element("td", "files-name", fileLink),
      element("td", "table-status", element("span", "status", display(row.format))),
      element("td", "table-number", formatBytes(row.size)),
      truncatedCell(row.author, "table-data"),
      truncatedCell(row.software, "table-data"),
      truncatedCell(relevantDate(row), "table-date"),
      element("td", "table-number", numbers.format(row.field_count)),
      element("td", "table-status", status),
      element("td", "table-actions", action));
  });
  if (!output.length) {
    const empty = element("td", "muted table-empty", "Ningún archivo coincide con los filtros.");
    empty.colSpan = session.files.length < 2 ? 9 : 10;
    output.push(element("tr", "", empty));
  }
  $("files").replaceChildren(...output);
  updateSelection();
}

function infoRows(items) {
  return element("dl", "", ...items.map(([label, value]) =>
    element("div", "", element("dt", "", label), element("dd", "", value))));
}

function structured(view) {
  const normalized = view.record.normalized;
  if (!Object.keys(normalized).length) return element("p", "muted", "Sin campos descriptivos detectados.");
  const indices = new Map(Object.values(normalized).flat().map((item, index) => [item, index]));
  return element("div", "grid gap-2", ...Object.entries(groupNames)
    .filter(([group]) => normalized[group]?.length).map(([group, label]) => {
      const details = element("details", "", element("summary", "", label + " (" + normalized[group].length + ")"),
        infoRows(normalized[group].map((item) => [
          metadataLabel(item),
          element("div", "metadata-item", metadataValue(item.value, true),
            element("code", "muted", item.source.join(" / ")), evidenceControl(view, "normalized", indices.get(item))),
        ])));
      details.open = group === "identity";
      return details;
    }));
}

function technical(view) {
  const groups = new Map();
  for (const [key, value] of Object.entries(view.record.raw)) {
    const pieces = key.split(":");
    const label = pieces.length > 1 ? pieces.slice(0, -1).join(" / ") : "Otros";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push([pieces.at(-1), element("div", "metadata-item",
      metadataValue(value, true), evidenceControl(view, "raw", key))]);
  }
  return groups.size ? element("div", "grid gap-2", ...Array.from(groups, ([name, values]) =>
    element("details", "", element("summary", "", name + " (" + values.length + ")"), infoRows(values))))
    : element("p", "muted", "El motor no devolvió propiedades para este archivo.");
}

function downloadJson(record) {
  const blob = new Blob([JSON.stringify(record, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const download = link("", url);
  download.download = "orca-" + record.id + ".json";
  document.body.append(download);
  download.click();
  download.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function jsonView(record) {
  const status = element("p", "small muted");
  status.setAttribute("role", "status");
  const copy = button("Copiar JSON", async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(record, null, 2));
      status.textContent = "JSON copiado.";
    } catch {
      status.textContent = "El navegador bloqueó el portapapeles. Puedes seleccionar el texto o descargarlo.";
    }
  });
  return element("section", "", element("div", "flex gap-2 flex-wrap", copy,
    button("Descargar JSON", () => downloadJson(record))), status, element("pre", "", JSON.stringify(record, null, 2)));
}

function findingsView(view) {
  const items = view.record.findings;
  if (!items.length) return element("p", "muted", "Sin hallazgos potencialmente sensibles.");
  return element("ul", "", ...items.map((item, index) => element("li", "finding",
    element("strong", "", metadataLabel(item)),
    metadataValue(item.value, true), element("p", "muted small", item.description),
    element("code", "small", item.source.join(" / ")), evidenceControl(view, "finding", index))));
}

function diffTable(rows) {
  const body = rows.map((row) => element("tr", "", element("td", "",
    element("code", "", row.source), row.technical ? element("div", "small muted", "Técnico o estructural") : ""),
  element("td", "before", display(row.before)), element("td", "after", display(row.after))));
  return element("div", "scroll-table", element("table", "diff-table",
    element("thead", "", element("tr", "", ...["Campo", "Antes", "Después"].map((name) => {
      const th = element("th", "", name);
      th.scope = "col";
      return th;
    }))), element("tbody", "", ...body)));
}

function cleanupView(view) {
  const box = element("section", "", element("h2", "", "Limpieza de metadatos"));
  if (!view.session.temporary || !cleanupKinds.has(view.record.format) || !["ok", "empty"].includes(view.record.status)) {
    box.append(element("p", "muted", view.session.temporary
      ? "Este archivo está disponible para inspección. Su formato o estado no admite limpieza verificada."
      : "La sesión viene del historial. Vuelve a cargar el archivo para crear una copia."));
    return box;
  }
  box.append(element("p", "muted", "Crea una copia, verifica su validez y compara los metadatos detectados."));
  if (!view.cleaned) {
    const action = button(view.cleaning ? "Verificando copia…" : "Crear copia saneada", () => cleanFile(view), "primary");
    action.disabled = view.cleaning;
    box.append(action);
    if (view.cleaning) {
      const progress = element("p", "small muted", "Verificando la copia y sus metadatos…");
      progress.setAttribute("role", "status");
      box.append(progress);
    }
  }
  if (view.error) {
    const warning = element("p", "notice", view.error);
    warning.dataset.error = "true";
    warning.setAttribute("role", "alert");
    box.append(warning);
  }
  if (view.cleaned) {
    const {after, comparison, policy, limits, exposure_comparison: exposure} = view.cleaned;
    box.append(element("p", "", policy), element("p", "muted small", limits),
      link("Descargar copia saneada", filePath(view.session.id, view.record.id) + "/copy", "button primary"),
      element("p", "small", "SHA-256 de la copia"),
      element("code", "", after.sha256));
    if (exposure) box.append(exposureComparisonView(exposure));
    for (const [key, name] of Object.entries({removed: "Eliminados", changed: "Modificados",
      retained: "Persistentes", added: "Añadidos"})) {
      const rows = comparison[key];
      const details = element("details", "", element("summary", "", name + " (" + rows.length + ")"),
        rows.length ? diffTable(rows) : element("p", "muted small", "Sin campos."));
      details.open = key === "removed" && Boolean(rows.length);
      box.append(details);
    }
  }
  return box;
}

async function cleanFile(view) {
  view.cleaning = true;
  view.error = "";
  renderDetail(view, "clean");
  try {
    view.cleaned = await api(filePath(view.session.id, view.record.id) + "/clean", {method: "POST"});
  } catch (error) {
    view.error = error.message;
  } finally {
    view.cleaning = false;
    if (state.inspection === view && ["clean", "exposure"].includes(view.tab)) {
      const focusWasUnassigned = document.activeElement === document.body;
      renderDetail(view, view.tab);
      if (focusWasUnassigned && view.tab === "clean") {
        $("detail-body").querySelector(".inspection-content .primary")?.focus();
      }
    }
  }
}

function hashControl(hash) {
  if (!hash) return element("span", "muted", "—");
  const feedback = element("p", "small muted copy-feedback");
  feedback.setAttribute("role", "status");
  const holder = element("div", "hash-value");
  const copy = button(icon("copy"), () => copyText(hash, holder, feedback, "SHA-256 copiado."), "icon-button");
  copy.setAttribute("aria-label", "Copiar SHA-256");
  copy.title = "Copiar SHA-256";
  holder.append(element("code", "", hash), copy, feedback);
  return holder;
}

function renderDetailSummary(view) {
  const record = view.record;
  const status = element("span", "status", statusNames[record.status]);
  status.dataset.status = record.status;
  const size = formatBytes(record.size) + (record.size >= 1000 ? " (" + numbers.format(record.size) + " B)" : "");
  const general = infoRows([
    ["Formato", display(record.format)], ["Tamaño", size], ["Estado", status],
    ["SHA-256", hashControl(record.sha256)],
  ]);
  general.className = "detail-facts panel";
  const notices = [...record.errors, ...record.warnings].map((text) => element("li", "", text));
  $("detail-summary").replaceChildren(general,
    ...(notices.length ? [element("ul", "notice inspection-notices", ...notices)] : []));
}

function renderDetail(view, tab = "normalized") {
  view.tab = tab;
  const record = view.record;
  const choices = {
    normalized: ["Normalizados", "Metadatos normalizados", "layers", () => structured(view)],
    technical: ["Técnicos", "Metadatos técnicos", "braces", () => technical(view)],
    timeline: ["Cronología", "", "clock", () => inspectionContextView(view, "timeline")],
    findings: ["Hallazgos (" + record.findings.length + ")", "Hallazgos", "triangle", () => findingsView(view)],
    exposure: ["Exposición", "", "shield", () => inspectionContextView(view, "exposure")],
    json: ["JSON", "Registro JSON", "file", () => jsonView(record)],
    clean: ["Antes / Después", "", "compare", () => cleanupView(view)],
  };
  if (!view.navigation) {
    view.navigation = $("file-tools");
    view.navigation.hidden = false;
    view.outlet = element("div", "inspection-content panel");
    for (const [key, [text, , glyph]] of Object.entries(choices)) {
      const item = button(element("span", "sidebar-label", text), () => {
        renderDetail(view, key);
        if (compactSidebar.matches) setSidebar(false);
      }, "sidebar-tool");
      item.prepend(icon(glyph));
      item.setAttribute("aria-label", text);
      item.title = text;
      item.dataset.view = key;
      view.navigation.append(item);
    }
    $("detail-body").replaceChildren(view.outlet);
  }
  for (const item of view.navigation.children) {
    item.setAttribute("aria-pressed", String(item.dataset.view === tab));
  }
  const [, heading, , content] = choices[tab];
  view.outlet.replaceChildren(...(heading ? [element("h2", "", heading)] : []), content());
}

function confirmDelete(text) {
  const dialog = $("confirm");
  dialog.returnValue = "cancel";
  $("confirm-text").textContent = text;
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "delete"), {once: true}));
}

async function removeSession(id, fromHistory = false) {
  if (!await confirmDelete("Se eliminarán los resultados guardados y los archivos temporales de esta sesión.")) return;
  try {
    await api(sessionPath(id), {method: "DELETE"});
    if (state.session?.id === id) renderResults(null);
    if (fromHistory) loadHistory();
  } catch (error) {
    message(fromHistory ? "history-message" : "upload-message", error.message, true);
  }
}

async function loadHistory() {
  const version = ++state.historyVersion;
  message("history-message", "Cargando historial…");
  try {
    const entries = await api("/api/history");
    if (version !== state.historyVersion) return;
    message("history-message");
    $("clear-history").disabled = entries.length === 0;
    $("history-list").replaceChildren(...(entries.length ? entries.map((entry) => {
      const open = button("Abrir sesión", async () => {
        try {
          renderResults(await api(sessionPath(entry.id)));
          location.hash = "workspace";
        } catch (error) {
          message("history-message", error.message, true);
        }
      });
      const remove = button("Eliminar", () => removeSession(entry.id, true));
      return element("div", "history-row flex items-center justify-between flex-wrap gap-2",
        element("div", "", element("strong", "", timestamp(entry.created)),
          element("p", "muted", entry.count + " archivos / " + entry.findings + " hallazgos")),
        element("div", "flex gap-2", open, remove));
    }) : [element("p", "muted", "Todavía no hay sesiones guardadas.")]));
  } catch (error) {
    if (version === state.historyVersion) message("history-message", error.message, true);
  }
}

$("clear-history").addEventListener("click", async () => {
  if (!await confirmDelete("Se eliminarán todos los resultados guardados en SQLite. Las sesiones temporales siguen su plazo de retención.")) return;
  try {
    await api("/api/history", {method: "DELETE"});
    if (state.session?.saved) {
      if (state.session.temporary) renderResults({...state.session, saved: false});
      else renderResults(null);
    }
    loadHistory();
  } catch (error) {
    message("history-message", error.message, true);
  }
});

$("pick").addEventListener("click", () => $("picker").click());
$("picker").addEventListener("change", () => {
  addFiles($("picker").files);
  $("picker").value = "";
});
$("reset").addEventListener("click", () => {
  state.files = [];
  message("upload-message");
  renderQueue();
  $("pick").focus();
});
$("analyze").addEventListener("click", analyze);
$("keep").addEventListener("change", () => renderIntakeContext());
$("close-session").addEventListener("click", () => removeSession(state.session.id));
$("reopen-upload").addEventListener("click", () => {
  setUploadOpen(true);
  $("pick").focus();
});
$("collapse-upload").addEventListener("click", () => {
  setUploadOpen(false);
  $("reopen-upload").focus();
});
for (const returnLink of document.querySelectorAll("[data-return]")) {
  returnLink.addEventListener("click", (event) => {
    if (!history.state?.orcaWorkbench || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    history.back();
  });
}
$("duel-open").addEventListener("click", () => {
  if (state.selected.length === 2) enterWorkbench("duel", [state.session.id, ...state.selected]);
});
$("duel-clear").addEventListener("click", () => {
  state.selected = [];
  updateSelection();
  $("files").querySelector("[data-compare-id]")?.focus();
});
$("search").addEventListener("input", renderTable);
for (const id of ["format-filter", "status-filter"]) $(id).addEventListener("change", renderTable);
for (const sort of document.querySelectorAll("[data-sort]")) sort.addEventListener("click", () => {
  state.ascending = state.sort === sort.dataset.sort ? !state.ascending : true;
  state.sort = sort.dataset.sort;
  renderTable();
});
for (const eventName of ["dragenter", "dragover", "dragleave", "drop"]) {
  $("drop").addEventListener(eventName, (event) => {
    if (!event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    if (state.working) return;
    const leaving = eventName === "dragleave" && !$("drop").contains(event.relatedTarget);
    if (eventName === "drop" || leaving) $("drop").classList.remove("dragging");
    else $("drop").classList.add("dragging");
    if (eventName === "drop") addFiles(event.dataTransfer.files);
  });
}
for (const eventName of ["dragover", "drop"]) window.addEventListener(eventName, (event) => {
  if (event.dataTransfer.types.includes("Files")) event.preventDefault();
});
$("sidebar-toggle").addEventListener("click", () => {
  setSidebar($("sidebar").dataset.pinned !== "true");
});
$("sidebar").addEventListener("click", (event) => {
  if (compactSidebar.matches && event.target.closest("a")) setSidebar(false);
});
$("sidebar").addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("sidebar").dataset.pinned === "true") {
    setSidebar(false);
    $("sidebar-toggle").focus();
  }
});
setSidebar(false);
document.querySelector(".skip").addEventListener("click", (event) => {
  event.preventDefault();
  $("main").focus();
});
window.addEventListener("hashchange", () => updatePage());
initHashLab();
initManifests();
initInvestigations();
renderResults(null);
updatePage();
checkEngine();
