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

const UI = {
  btn: {
    primary: "primary inline-flex items-center justify-center gap-[7px] min-h-[36px] px-3 py-[7px] border border-accent rounded-md font-semibold text-[0.86rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-on-accent bg-accent hover:not-disabled:bg-accent-hover hover:not-disabled:border-accent-hover [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    "accent-button": "inline-flex items-center justify-center gap-[7px] min-h-[36px] px-3 py-[7px] border border-tint-line rounded-md font-semibold text-[0.86rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-accent bg-tint hover:not-disabled:border-accent [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    subtle: "inline-flex items-center justify-center gap-[7px] min-h-[36px] px-3 py-[7px] border border-line rounded-md font-semibold text-[0.86rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-ink bg-surface hover:not-disabled:bg-wash hover:not-disabled:border-line-strong [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    quiet: "inline-flex items-center justify-center gap-[7px] min-h-[36px] px-3 py-[7px] border border-transparent rounded-md font-semibold text-[0.86rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-muted bg-transparent hover:not-disabled:bg-wash hover:not-disabled:border-line-strong [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    danger: "inline-flex items-center justify-center gap-[7px] min-h-[36px] px-3 py-[7px] border border-danger rounded-md font-semibold text-[0.86rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-on-danger bg-danger hover:not-disabled:bg-danger-hover [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    "icon-button": "inline-flex items-center justify-center gap-[7px] min-h-[35px] min-w-[35px] p-[7px] border border-line rounded-md font-semibold text-[0.86rem] leading-[1.25] text-center whitespace-nowrap flex-none no-underline text-ink bg-surface hover:not-disabled:bg-wash hover:not-disabled:border-line-strong [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    "primary small-btn": "primary inline-flex items-center justify-center gap-[7px] min-h-[30px] px-[9px] py-1 border border-accent rounded-md font-semibold text-[0.8rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-on-accent bg-accent hover:not-disabled:bg-accent-hover hover:not-disabled:border-accent-hover [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    "accent-button small-btn": "inline-flex items-center justify-center gap-[7px] min-h-[30px] px-[9px] py-1 border border-tint-line rounded-md font-semibold text-[0.8rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-accent bg-tint hover:not-disabled:border-accent [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    "subtle small-btn": "inline-flex items-center justify-center gap-[7px] min-h-[30px] px-[9px] py-1 border border-line rounded-md font-semibold text-[0.8rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-ink bg-surface hover:not-disabled:bg-wash hover:not-disabled:border-line-strong [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    "quiet small-btn": "inline-flex items-center justify-center gap-[7px] min-h-[30px] px-[9px] py-1 border border-transparent rounded-md font-semibold text-[0.8rem] leading-[1.25] text-center whitespace-nowrap shrink-0 break-normal no-underline text-muted bg-transparent hover:not-disabled:bg-wash hover:not-disabled:border-line-strong [&_svg]:size-4 disabled:text-muted disabled:bg-wash disabled:border-line disabled:opacity-78",
    "file-link": "inline-flex items-center gap-2 min-w-0 max-w-full text-left font-semibold text-ink no-underline hover:text-accent hover:underline [&_svg]:size-4 [&_svg]:text-accent [&_svg]:flex-none",
    "sidebar-tool": "flex items-center justify-start gap-3 w-full min-h-[40px] px-3 border border-transparent rounded-[7px] bg-transparent text-muted text-[0.88rem] font-semibold text-left whitespace-nowrap overflow-hidden no-underline hover:bg-wash hover:text-ink aria-[current=page]:text-accent aria-[current=page]:bg-tint aria-[current=page]:border-tint-line aria-[pressed=true]:text-accent aria-[pressed=true]:bg-tint aria-[pressed=true]:border-tint-line",
    "subnav-tab": "min-h-[34px] px-[13px] py-1.5 border border-transparent rounded-md text-muted text-[0.86rem] font-semibold bg-transparent whitespace-nowrap hover:text-ink aria-[selected=true]:text-accent aria-[selected=true]:bg-surface aria-[selected=true]:border-tint-line max-[540px]:flex-auto max-[540px]:text-center max-[540px]:px-[9px] max-[540px]:text-[0.82rem]",
    "matrix-btn": "inline-flex items-center justify-center w-full min-h-[31px] py-1 px-2 text-[0.78rem] font-semibold tabular-nums rounded-md border border-line bg-surface text-ink whitespace-nowrap data-[matrix-state=identical]:bg-success-bg data-[matrix-state=identical]:border-success-line data-[matrix-state=identical]:text-success data-[matrix-state=high]:bg-tint data-[matrix-state=high]:border-tint-line data-[matrix-state=high]:text-accent data-[matrix-state=mid]:bg-warning-bg data-[matrix-state=mid]:border-warning-line data-[matrix-state=mid]:text-warning data-[matrix-state=pending]:bg-wash data-[matrix-state=pending]:text-muted data-[matrix-state=binary]:bg-surface data-[matrix-state=binary]:text-muted disabled:opacity-60",
  },
  sidebarLabel: "opacity-0 invisible pointer-events-none whitespace-nowrap transition-[opacity,visibility] duration-150 ease-out group-hover/sidebar:opacity-100 group-hover/sidebar:visible group-hover/sidebar:pointer-events-auto group-hover/sidebar:delay-[40ms] group-focus-within/sidebar:opacity-100 group-focus-within/sidebar:visible group-focus-within/sidebar:pointer-events-auto group-has-[[data-pinned=true]]/sidebar:opacity-100 group-has-[[data-pinned=true]]/sidebar:visible group-has-[[data-pinned=true]]/sidebar:pointer-events-auto",
  panel: "border border-line rounded-[10px] bg-surface min-w-0",
  labPanel: "border border-line rounded-[10px] bg-surface min-w-0 p-[18px_20px] max-[540px]:p-[13px] grid grid-cols-[minmax(0,1fr)] gap-3.5",
  duelSection: "border border-line rounded-[10px] bg-surface min-w-0 p-[16px_18px] max-[540px]:p-[13px] grid grid-cols-[minmax(0,1fr)] gap-3",
  notice: "px-[13px] py-2.5 border border-warning-line rounded-[7px] bg-warning-bg text-warning break-words text-[0.88rem] leading-[1.45] data-[error=true]:text-error data-[error=true]:bg-error-bg data-[error=true]:border-error-line",
  callout: "py-3 px-3.5 border border-tint-line border-s-[3px] border-s-accent rounded-[7px] bg-tint grid grid-cols-[minmax(0,1fr)] gap-1.5",
  banner: "px-3.5 py-3 border border-line rounded-[7px] bg-wash grid grid-cols-[minmax(0,1fr)] gap-[5px] min-w-0 data-[state=verified]:bg-success-bg data-[state=verified]:border-success-line data-[state=verified]:text-success data-[state=changed]:bg-error-bg data-[state=changed]:border-error-line data-[state=changed]:text-error data-[state=error]:bg-error-bg data-[state=error]:border-error-line data-[state=error]:text-error data-[state=warning]:bg-warning-bg data-[state=warning]:border-warning-line data-[state=warning]:text-warning",
  status: "inline-flex items-center gap-[5px] w-fit max-w-full py-[3px] px-2 rounded-[5px] border border-line bg-wash text-muted text-[0.78rem] font-semibold leading-[1.25] whitespace-nowrap shrink-0 break-normal data-[status=ok]:text-success data-[status=ok]:border-success-line data-[status=ok]:bg-success-bg data-[diff=matching]:text-success data-[diff=matching]:border-success-line data-[diff=matching]:bg-success-bg data-[status=unsupported]:text-warning data-[status=unsupported]:border-warning-line data-[status=unsupported]:bg-warning-bg data-[diff=different]:text-warning data-[diff=different]:border-warning-line data-[diff=different]:bg-warning-bg data-[level=potential]:text-warning data-[level=potential]:border-warning-line data-[level=potential]:bg-warning-bg data-[status=error]:text-error data-[status=error]:border-error-line data-[status=error]:bg-error-bg",
  stats: "flex flex-wrap gap-2 items-center min-w-0",
  stat: "inline-flex items-baseline gap-[7px] py-1.5 px-3 max-[540px]:py-[5px] max-[540px]:px-[9px] border border-line rounded-[7px] bg-wash text-[0.85rem] max-[540px]:text-[0.8rem] text-muted whitespace-nowrap [&_strong]:text-ink [&_strong]:text-[0.95rem] [&_strong]:font-bold [&_strong]:tabular-nums",
  select: "min-h-[36px] px-[9px] py-[5px] border border-line-strong rounded-md bg-surface text-ink text-[0.86rem] max-w-full",
  input: "min-h-[36px] w-full px-2.5 py-1.5 border border-line-strong rounded-md bg-surface text-ink text-[0.88rem] focus:border-accent",
  textarea: "w-full min-h-[62px] px-2.5 py-2 border border-line-strong rounded-md bg-surface text-ink text-[0.85rem] resize-y wrap-anywhere focus:border-accent",
  check: "inline-flex items-center gap-2 text-muted text-[0.875rem] min-w-0 [&_input]:flex-none [&_input]:size-[17px]",
  fieldGroup: "grid grid-cols-[minmax(0,1fr)] gap-[5px] min-w-0",
  compareGrid: "grid grid-cols-2 max-md:grid-cols-1 gap-3.5",
  labActions: "flex items-center justify-between flex-wrap gap-x-4 gap-y-2.5 pt-2.5 border-t border-line",
  scrollTable: "w-full max-w-full overflow-x-auto",
  table: "border-collapse w-full text-[0.865rem] text-left",
  th: "px-3 py-2.5 border-t border-line bg-wash text-muted font-semibold text-[0.82rem] whitespace-nowrap align-middle",
  td: "px-3 py-2.5 border-t border-line align-middle break-words",
  tdNum: "px-3 py-2.5 border-t border-line align-middle text-right tabular-nums whitespace-nowrap",
  tdStatus: "px-3 py-2.5 border-t border-line align-middle whitespace-nowrap w-[1%]",
  tdActions: "px-3 py-2.5 border-t border-line align-middle whitespace-nowrap w-[1%] text-right",
  tdName: "px-3 py-2.5 border-t border-line align-middle min-w-[200px] max-w-[310px]",
  tdData: "px-3 py-2.5 border-t border-line align-middle min-w-[100px] max-w-[185px] break-words",
  tdDate: "px-3 py-2.5 border-t border-line align-middle min-w-[130px] max-w-[185px] break-words",
  tr: "hover:bg-wash data-[selected=true]:bg-wash",
  truncate: "block min-w-0 overflow-hidden text-ellipsis whitespace-nowrap",
  details: "border border-line rounded-[7px] py-[9px] px-3 min-w-0 open:[&>*+*]:mt-2",
  summary: "font-semibold break-words",
  pre: "max-h-[540px] whitespace-pre-wrap overflow-auto wrap-anywhere bg-wash border border-line p-3 rounded-md text-[0.83rem] font-mono",
  code: "font-mono text-muted text-[0.82rem] wrap-anywhere",
  hashCode: "block w-full max-w-full py-2 px-2.5 border border-line rounded-md bg-surface text-ink text-[0.81rem] font-mono break-all",
  hashInline: "block max-w-[220px] text-[0.78rem] font-mono overflow-hidden text-ellipsis whitespace-nowrap",
  dlRow: "grid grid-cols-[minmax(110px,0.75fr)_minmax(0,2fr)] max-[540px]:grid-cols-1 gap-x-3 gap-y-1 py-2 border-b border-line last:border-b-0 text-[0.885rem]",
  dt: "text-muted wrap-anywhere",
  dd: "min-w-0 wrap-anywhere",
  fileLink: "inline-flex items-center gap-2 min-w-0 max-w-full text-left font-semibold text-ink no-underline hover:text-accent hover:underline [&_svg]:size-4 [&_svg]:text-accent [&_svg]:flex-none",
  fileMark: "grid place-items-center flex-none size-[46px] max-[540px]:size-10 text-accent bg-tint border border-tint-line rounded-[9px] [&_svg]:size-[22px]",
  h2: "text-[1.24rem] font-semibold leading-[1.25] tracking-[-0.02em]",
  h3: "text-base font-semibold leading-[1.3]",
  small: "text-[0.875rem]",
  smallMuted: "text-[0.875rem] text-muted",
  muted: "text-muted",
  errorText: "text-[0.875rem] text-error",
  empty: "min-h-[155px] grid grid-cols-[minmax(0,1fr)] content-center justify-items-center gap-[7px] p-[18px] text-center",
  emptyIcon: "grid place-items-center w-10 h-[38px] rounded-lg border border-line bg-wash text-muted",
  tableEmpty: "py-[26px] px-3.5 text-center text-muted text-[0.875rem]",
  duelHead: "flex items-center justify-between flex-wrap gap-x-3 gap-y-2",
  duelFilters: "flex flex-wrap items-center gap-2",
  duelNotes: "list-disc ps-[18px] grid gap-[3px]",
  finding: "py-2.5 border-b border-line last:border-b-0 break-words",
  metaVal: "block whitespace-pre-wrap break-words",
  metaItem: "min-w-0 space-y-1",
  longValue: "bg-wash py-1 px-2 rounded-[5px] [&>summary]:text-accent [&>summary]:text-[0.84rem] [&>summary]:font-medium",
  timeEvents: "list-none border-l border-tint-line pl-4 my-2.5 ml-1.5 mb-0.5",
  timeEvent: "time-event relative pb-3.5 min-w-0",
  timeEventTitle: "flex flex-wrap gap-x-2.5 gap-y-1 items-baseline",
  subnav: "inline-flex flex-wrap gap-1 p-1 mb-4 border border-line rounded-lg bg-wash max-w-full max-[540px]:w-full",
  cardRow: "py-3 px-3.5 border border-line rounded-lg bg-wash grid grid-cols-[minmax(0,1fr)] gap-2 min-w-0",
  cardTop: "flex items-center justify-between flex-wrap gap-x-3 gap-y-2 min-w-0",
  verifyStep: "grid grid-cols-[minmax(0,1fr)] gap-2 py-3 px-3.5 border border-line rounded-[7px] bg-wash min-w-0",
  slideDiffBody: "grid grid-cols-[minmax(0,1fr)] gap-2 pt-1.5",
  copyFeedback: "text-[0.875rem] text-muted empty:hidden",
};

function element(tag, className, ...children) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  result.append(...children);
  return result;
}

function button(text, handler, style = "subtle") {
  const result = element("button", UI.btn[style] ?? style, text);
  result.type = "button";
  result.addEventListener("click", handler);
  return result;
}

function link(text, href, style) {
  const mapped = style?.startsWith("button ") ? UI.btn[style.slice(7)] : (UI.btn[style] ?? style);
  const result = element("a", mapped, text);
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

function truncatedCell(value, className = UI.tdData) {
  const text = display(value);
  const span = element("span", UI.truncate, text);
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
  body.replaceChildren(element("p", UI.muted, "Leyendo los metadatos guardados…"));
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
      const notice = element("p", UI.notice, error.message);
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
    const bar = element("progress", "block w-full my-2");
    bar.setAttribute("aria-label", "Progreso real del análisis");
    if (typeof info.processed === "number" && typeof info.total === "number" && info.total > 0) {
      bar.max = info.total;
      bar.value = info.processed;
    }
    const countText = (typeof info.processed === "number" && typeof info.total === "number")
      ? numbers.format(info.processed) + " / " + numbers.format(info.total) + " archivo(s)"
      : "Preparando lote…";
    const card = element("div", "grid grid-cols-[minmax(0,1fr)] gap-2 p-3 border border-line rounded-lg bg-wash",
      element("div", "flex items-center justify-between gap-2 flex-wrap",
        element("strong", UI.small, info.title),
        element("span", "font-mono text-[0.875rem] text-muted", countText)),
      bar,
      element("p", "text-[0.875rem] text-muted block min-w-0 overflow-hidden text-ellipsis whitespace-nowrap",
        info.current || "Esperando respuesta del motor local…"));
    body.replaceChildren(
      card,
      element("p", UI.smallMuted, "El procesamiento ocurre de forma local con ExifTool. Los archivos originales permanecen en tu equipo."));
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
    const stats = element("div", UI.stats,
      element("div", UI.stat, element("strong", "", numbers.format(state.files.length)), element("span", "", state.files.length === 1 ? "Archivo" : "Archivos")),
      element("div", UI.stat, element("strong", "", formatBytes(totalBytes)), element("span", "", "Tamaño total")));

    const formatChips = element("div", "flex flex-wrap gap-1.5",
      ...Array.from(counts.entries()).map(([fmt, count]) =>
        element("span", UI.status, fmt + " (" + numbers.format(count) + ")")));

    const saveHint = $("keep").checked
      ? "Los resultados se guardarán en el historial SQLite local (sin copias permanentes de los originales)."
      : "Sesión temporal: los archivos y copias caducan automáticamente al cerrar o vencer la retención.";

    body.replaceChildren(
      element("div", UI.slideDiffBody,
        stats,
        element("div", UI.fieldGroup,
          element("span", UI.smallMuted, "Formatos por extensión (se confirmarán por firma binaria):"),
          formatChips)),
      element("p", UI.smallMuted, saveHint));
    return;
  }

  // 3. Completed session summary + contextual shortcuts
  if (state.session) {
    titleNode.textContent = "Última sesión analizada";
    const s = state.session.summary;
    const stats = element("div", UI.stats,
      element("div", UI.stat, element("strong", "", numbers.format(s.count)), element("span", "", "Archivos")),
      element("div", UI.stat, element("strong", "", numbers.format(s.properties)), element("span", "", "Propiedades")),
      element("div", UI.stat, element("strong", "", numbers.format(s.findings)), element("span", "", "Hallazgos")),
      element("div", UI.stat, element("strong", "", numbers.format(s.statuses.error)), element("span", "", "Errores")));
    const actions = element("div", "flex flex-wrap gap-2 pt-3 border-t border-line");
    if (state.session.files.length >= 2) {
      actions.append(link("Abrir Duelo", "#duel", "button accent-button small-btn"));
    }
    actions.append(
      link("Investigaciones", "#investigations", "button subtle small-btn"),
      link("Laboratorio de hashes", "#hashes", "button subtle small-btn"),
      link("Manifiestos", "#manifests", "button subtle small-btn"));
    body.replaceChildren(
      element("div", UI.slideDiffBody,
        stats,
        element("p", UI.smallMuted,
          "Consulta los resultados en la tabla inferior o selecciona nuevos archivos a la izquierda para iniciar otro análisis.")),
      actions);
    return;
  }

  // 4. Initial empty state: concise 3-step quick guide
  titleNode.textContent = "Cómo empezar";
  const stepNumCls = "grid place-items-center size-[26px] rounded-full bg-tint border border-tint-line text-accent text-[0.8rem] font-bold tabular-nums";
  const stepItemCls = "grid grid-cols-[26px_minmax(0,1fr)] gap-2.5 items-start";
  const stepTitleCls = "block text-[0.9rem] font-semibold text-ink";
  const stepDescCls = "mt-0.5 text-[0.84rem] text-muted leading-[1.45]";
  const steps = element("ol", "list-none m-0 p-0 grid grid-cols-[minmax(0,1fr)] gap-[11px]",
    element("li", stepItemCls, element("span", stepNumCls, "1"),
      element("div", "", element("strong", stepTitleCls, "Selecciona o arrastra archivos"),
        element("p", stepDescCls, "Admite PDF, Office, imágenes y paquetes ZIP directamente desde tu equipo."))),
    element("li", stepItemCls, element("span", stepNumCls, "2"),
      element("div", "", element("strong", stepTitleCls, "Ejecuta el análisis local"),
        element("p", stepDescCls, "Extrae metadatos con ExifTool, calcula SHA-256 y detecta exposiciones sin conexión."))),
    element("li", stepItemCls, element("span", stepNumCls, "3"),
      element("div", "", element("strong", stepTitleCls, "Inspecciona, compara o limpia"),
        element("p", stepDescCls, "Revisa detalles, cronología, compara dos documentos o genera una copia saneada verificada."))));
  const links = element("div", "flex flex-wrap gap-2 pt-3 border-t border-line",
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
    const name = element("span", UI.truncate, file.name);
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
    return element("li", "py-[7px] flex items-center gap-2 justify-between text-[0.875rem]",
      element("div", "flex items-center gap-2 min-w-0", icon("file"), name),
      element("div", "flex items-center gap-2", element("span", "text-muted font-mono whitespace-nowrap", formatBytes(file.size)), remove));
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
  $("stats").replaceChildren(...metrics.map(([label, value]) => element("div", UI.stat,
    element("strong", "", numbers.format(value)), element("span", "", label))));
  const breakdown = [
    ["Formatos", Object.entries(summary.formats).map(([key, value]) => key + " (" + value + ")")],
    ["Autores (" + summary.authors.length + ")", summary.authors],
    ["Software (" + summary.software.length + ")", summary.software],
    ["Estados", Object.entries(summary.statuses).map(([key, value]) => statusNames[key] + " (" + value + ")")],
  ];
  $("breakdown").replaceChildren(...breakdown.map(([label, values]) => element("div", "",
    element("h3", UI.h3, label), values.length ? element("ul", "mt-[5px] text-muted text-[0.87rem] break-words", ...values.map((value) => element("li", "", value)))
      : element("p", UI.smallMuted, "Sin datos detectados"))));
  $("search").value = "";
  setOptions("format-filter", "Todos los formatos", Object.keys(summary.formats).map((item) => [item, item]));
  setOptions("status-filter", "Todos los estados", Object.entries(statusNames));
  renderTable();
  $("correlations").replaceChildren(...(summary.correlations.length
    ? summary.correlations.map((match) => {
        const names = match.files.map((file) => {
          const action = button(file.name, () => enterWorkbench("detail", [session.id, file.id]), "subtle");
          return element("li", "text-[0.875rem] mt-[5px] first:mt-0", action, " ", element("code", UI.code, file.source.join(" / ")));
        });
        return element("details", "border-t border-line py-3",
          element("summary", "break-words text-[0.9rem]", fields[match.field] + " · " + display(match.value) + " (" + match.files.length + ")"),
          element("ul", "pl-[19px] mt-1.5 list-disc break-words", ...names));
      })
    : [element("p", UI.smallMuted, "No se encontraron campos y valores compartidos.")]));
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
    const status = element("span", UI.status, statusNames[row.status]);
    status.dataset.status = row.status;
    const target = "#" + ["detail", session.id, row.id].join("/");
    const nameText = element("span", UI.truncate, row.name);
    const fileLink = link("", target, UI.fileLink);
    fileLink.title = row.name;
    fileLink.append(icon("file"), nameText);
    fileLink.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      enterWorkbench("detail", [session.id, row.id]);
    });
    const action = button("Detalles", () => enterWorkbench("detail", [session.id, row.id]), "subtle small-btn");
    action.setAttribute("aria-label", "Detalles de " + row.name);
    const selection = element("td", "px-3 py-2.5 border-t border-line align-middle w-10 text-center [&_input]:align-middle [&_input]:cursor-pointer", comparisonCheckbox(row));
    selection.hidden = session.files.length < 2;
    return element("tr", UI.tr, selection,
      element("td", UI.tdName, fileLink),
      element("td", UI.tdStatus, element("span", UI.status, display(row.format))),
      element("td", UI.tdNum, formatBytes(row.size)),
      truncatedCell(row.author, UI.tdData),
      truncatedCell(row.software, UI.tdData),
      truncatedCell(relevantDate(row), UI.tdDate),
      element("td", UI.tdNum, numbers.format(row.field_count)),
      element("td", UI.tdStatus, status),
      element("td", UI.tdActions, action));
  });
  if (!output.length) {
    const empty = element("td", UI.tableEmpty + " border-t border-line", "Ningún archivo coincide con los filtros.");
    empty.colSpan = session.files.length < 2 ? 9 : 10;
    output.push(element("tr", "", empty));
  }
  $("files").replaceChildren(...output);
  updateSelection();
}

function infoRows(items) {
  return element("dl", "", ...items.map(([label, value]) =>
    element("div", UI.dlRow, element("dt", UI.dt, label), element("dd", UI.dd, value))));
}

function structured(view) {
  const normalized = view.record.normalized;
  if (!Object.keys(normalized).length) return element("p", UI.muted, "Sin campos descriptivos detectados.");
  const indices = new Map(Object.values(normalized).flat().map((item, index) => [item, index]));
  return element("div", "grid gap-2", ...Object.entries(groupNames)
    .filter(([group]) => normalized[group]?.length).map(([group, label]) => {
      const details = element("details", UI.details, element("summary", UI.summary, label + " (" + normalized[group].length + ")"),
        infoRows(normalized[group].map((item) => [
          metadataLabel(item),
          element("div", UI.metaItem, metadataValue(item.value, true),
            element("code", UI.code, item.source.join(" / ")), evidenceControl(view, "normalized", indices.get(item))),
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
    groups.get(label).push([pieces.at(-1), element("div", UI.metaItem,
      metadataValue(value, true), evidenceControl(view, "raw", key))]);
  }
  return groups.size ? element("div", "grid gap-2", ...Array.from(groups, ([name, values]) =>
    element("details", UI.details, element("summary", UI.summary, name + " (" + values.length + ")"), infoRows(values))))
    : element("p", UI.muted, "El motor no devolvió propiedades para este archivo.");
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
  const status = element("p", UI.smallMuted);
  status.setAttribute("role", "status");
  const copy = button("Copiar JSON", async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(record, null, 2));
      status.textContent = "JSON copiado.";
    } catch {
      status.textContent = "El navegador bloqueó el portapapeles. Puedes seleccionar el texto o descargarlo.";
    }
  });
  return element("section", "space-y-3", element("div", "flex gap-2 flex-wrap", copy,
    button("Descargar JSON", () => downloadJson(record))), status, element("pre", UI.pre, JSON.stringify(record, null, 2)));
}

function findingsView(view) {
  const items = view.record.findings;
  if (!items.length) return element("p", UI.muted, "Sin hallazgos potencialmente sensibles.");
  return element("ul", "", ...items.map((item, index) => element("li", UI.finding,
    element("strong", "", metadataLabel(item)),
    metadataValue(item.value, true), element("p", UI.smallMuted, item.description),
    element("code", UI.code, item.source.join(" / ")), evidenceControl(view, "finding", index))));
}

function diffTable(rows) {
  const body = rows.map((row) => element("tr", UI.tr, element("td", UI.td + " whitespace-pre-wrap align-top wrap-anywhere",
    element("code", UI.code, row.source), row.technical ? element("div", UI.smallMuted, "Técnico o estructural") : ""),
  element("td", UI.td + " whitespace-pre-wrap align-top wrap-anywhere text-error", display(row.before)),
  element("td", UI.td + " whitespace-pre-wrap align-top wrap-anywhere text-success", display(row.after))));
  return element("div", UI.scrollTable, element("table", UI.table + " table-fixed min-w-[640px]",
    element("thead", "", element("tr", "", ...["Campo", "Antes", "Después"].map((name, idx) => {
      const th = element("th", UI.th + (idx === 0 ? " w-[32%]" : ""), name);
      th.scope = "col";
      return th;
    }))), element("tbody", "", ...body)));
}

function cleanupView(view) {
  const box = element("section", "space-y-3", element("h2", UI.h2, "Limpieza de metadatos"));
  if (!view.session.temporary || !cleanupKinds.has(view.record.format) || !["ok", "empty"].includes(view.record.status)) {
    box.append(element("p", UI.muted, view.session.temporary
      ? "Este archivo está disponible para inspección. Su formato o estado no admite limpieza verificada."
      : "La sesión viene del historial. Vuelve a cargar el archivo para crear una copia."));
    return box;
  }
  box.append(element("p", UI.muted, "Crea una copia, verifica su validez y compara los metadatos detectados."));
  if (!view.cleaned) {
    const action = button(view.cleaning ? "Verificando copia…" : "Crear copia saneada", () => cleanFile(view), "primary");
    action.disabled = view.cleaning;
    box.append(action);
    if (view.cleaning) {
      const progress = element("p", UI.smallMuted, "Verificando la copia y sus metadatos…");
      progress.setAttribute("role", "status");
      box.append(progress);
    }
  }
  if (view.error) {
    const warning = element("p", UI.notice, view.error);
    warning.dataset.error = "true";
    warning.setAttribute("role", "alert");
    box.append(warning);
  }
  if (view.cleaned) {
    const {after, comparison, policy, limits, exposure_comparison: exposure, image_verification: imgVer} = view.cleaned;
    box.append(element("p", "", policy), element("p", UI.smallMuted, limits),
      link("Descargar copia saneada", filePath(view.session.id, view.record.id) + "/copy", "button primary"),
      element("p", UI.small, "SHA-256 de la copia"),
      element("code", UI.code, after.sha256));
    if (imgVer) {
      const badge = element("span", UI.status,
        imgVer.pixels_preserved === true ? "Píxeles conservados"
          : imgVer.pixels_preserved === false ? "Píxeles modificados" : "No verificable");
      badge.dataset.status = imgVer.pixels_preserved === true ? "ok" : imgVer.pixels_preserved === false ? "error" : "unsupported";
      const verBox = element("section", UI.callout,
        element("div", "flex items-center gap-2 flex-wrap",
          element("strong", "", "Verificación visual tras limpieza"), badge),
        element("p", UI.small, imgVer.verdict));
      if (typeof imgVer.different_pixels === "number") {
        verBox.append(element("p", UI.smallMuted,
          "Píxeles diferentes: " + numbers.format(imgVer.different_pixels)
          + (typeof imgVer.ssim === "number" ? " · SSIM: " + imgVer.ssim.toFixed(4) : "")));
      }
      if (imgVer.structural_differences?.length) {
        verBox.append(element("ul", UI.duelNotes + " text-[0.875rem] text-muted",
          ...imgVer.structural_differences.map((d) => element("li", "", element("strong", "", d.label + ": "), d.detail))));
      }
      box.append(verBox);
    }
    if (exposure) box.append(exposureComparisonView(exposure));
    for (const [key, name] of Object.entries({removed: "Eliminados", changed: "Modificados",
      retained: "Persistentes", added: "Añadidos"})) {
      const rows = comparison[key];
      const details = element("details", UI.details, element("summary", UI.summary, name + " (" + rows.length + ")"),
        rows.length ? diffTable(rows) : element("p", UI.smallMuted, "Sin campos."));
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
  if (!hash) return element("span", UI.muted, "—");
  const feedback = element("p", UI.copyFeedback + " basis-full");
  feedback.setAttribute("role", "status");
  const holder = element("div", "flex items-center flex-wrap gap-x-2 gap-y-1.5 min-w-0 max-w-full");
  const copy = button(icon("copy"), () => copyText(hash, holder, feedback, "SHA-256 copiado."), "icon-button");
  copy.classList.add("min-h-[30px]", "min-w-[30px]", "p-[5px]");
  copy.setAttribute("aria-label", "Copiar SHA-256");
  copy.title = "Copiar SHA-256";
  holder.append(element("code", "flex-[1_1_14rem] min-w-0 font-mono text-[0.8rem] break-all", hash), copy, feedback);
  return holder;
}

function renderDetailSummary(view) {
  const record = view.record;
  const status = element("span", UI.status, statusNames[record.status]);
  status.dataset.status = record.status;
  const size = formatBytes(record.size) + (record.size >= 1000 ? " (" + numbers.format(record.size) + " B)" : "");
  const factItem = (label, value) => element("div", "min-w-0",
    element("dt", "text-muted text-[0.78rem]", label),
    element("dd", "mt-[3px] text-[0.88rem] font-medium", value));
  const general = element("dl", UI.panel + " grid grid-cols-[repeat(3,max-content)_minmax(0,1fr)] max-[1100px]:grid-cols-2 max-[540px]:grid-cols-1 items-start gap-x-6 max-[1100px]:gap-x-[18px] gap-y-2.5 py-3 px-[15px] mb-3.5",
    factItem("Formato", display(record.format)),
    factItem("Tamaño", size),
    factItem("Estado", status),
    factItem("SHA-256", hashControl(record.sha256)));
  const notices = [...record.errors, ...record.warnings].map((text) => element("li", "", text));
  $("detail-summary").replaceChildren(general,
    ...(notices.length ? [element("ul", UI.notice + " mb-3", ...notices)] : []));
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
    view.outlet = element("div", "inspection-content " + UI.panel + " min-w-0 py-4 px-[18px] max-[540px]:p-3");
    for (const [key, [text, , glyph]] of Object.entries(choices)) {
      const item = button(element("span", UI.sidebarLabel, text), () => {
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
  view.outlet.replaceChildren(...(heading ? [element("h2", UI.h2 + " mb-3", heading)] : []), content());
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
      return element("div", "border-t border-line py-3 flex items-center justify-between flex-wrap gap-2",
        element("div", "", element("strong", "font-semibold", timestamp(entry.created)),
          element("p", "text-muted text-[0.86rem]", entry.count + " archivos / " + entry.findings + " hallazgos")),
        element("div", "flex gap-2", open, remove));
    }) : [element("p", UI.muted, "Todavía no hay sesiones guardadas.")]));
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
