"use strict";

const invState = {
  listVersion: 0,
  current: null,
  tab: "summary",
  timelineMode: "activity", // activity | documents | graph
  docSort: "added",
  selectedDocIds: [],
  graphSelectedNode: null,
  graphFilters: new Set([
    "identical_hash", "equivalent_content", "structural_similarity",
    "shared_media", "manual_confirmed", "manual_hypothesis",
  ]),
  activeFileDetail: null,
  activeComparison: null,
  importFiles: [],
  importPreview: null,
  verifyingFileUuid: null,
};

const invStatusBadge = {
  open: "ok",
  paused: "unsupported",
  closed: "",
};

const invEventLabels = {
  investigation_created: "Investigación creada",
  investigation_edited: "Información editada",
  file_added: "Archivo incorporado",
  analysis_started: "Análisis iniciado",
  analysis_completed: "Análisis completado",
  analysis_error: "Error de análisis",
  duplicate_detected: "Duplicado detectado",
  comparison_executed: "Comparación ejecutada",
  hash_verified: "Hash verificado",
  manifest_generated: "Manifiesto generado",
  note_created: "Observación creada",
  note_updated: "Observación modificada",
  file_removed: "Archivo retirado",
  investigation_paused: "Investigación pausada",
  investigation_resumed: "Investigación reanudada",
  investigation_closed: "Investigación cerrada",
  investigation_reopened: "Investigación reabierta",
  relation_created: "Relación registrada",
  relation_removed: "Relación retirada",
  investigation_imported: "Investigación importada",
};

const invRelationLabels = {
  identical_hash: "Hash SHA-256 idéntico",
  equivalent_content: "Contenido equivalente",
  structural_similarity: "Similitud estructural (hipótesis)",
  shared_media: "Recursos multimedia compartidos",
  manual_confirmed: "Manual confirmada",
  manual_hypothesis: "Hipótesis manual",
};

const invConflictLabels = {
  none: {label: "Listo para importar", badge: "ok"},
  identical_existing: {label: "Ya importada (idéntica)", badge: ""},
  uuid_conflict: {label: "Conflicto de UUID", badge: "unsupported"},
  duplicate_in_batch: {label: "Duplicado en lote", badge: ""},
  uuid_conflict_in_batch: {label: "Conflicto en lote", badge: "unsupported"},
  invalid: {label: "Paquete inválido", badge: "error"},
};

function invApi(uuid, suffix = "") {
  return "/api/investigations/" + encodeURIComponent(uuid) + suffix;
}

// --- Panel Principal: Listado, Creación e Importación Individual/Masiva ---

async function loadInvestigationsList() {
  const version = ++invState.listVersion;
  const params = new URLSearchParams();
  const q = $("inv-search").value.trim();
  const status = $("inv-status-filter").value;
  const sort = $("inv-sort").value;
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (sort) params.set("sort", sort);
  const query = params.toString() ? "?" + params.toString() : "";
  message("inv-list-message", "Cargando investigaciones…");
  try {
    const items = await api("/api/investigations" + query);
    if (version !== invState.listVersion) return;
    message("inv-list-message");
    $("inv-list-count").textContent = "(" + numbers.format(items.length) + ")";
    const host = $("inv-list");
    if (!items.length) {
      host.replaceChildren(element("div", "empty",
        element("span", "empty-icon", icon("case")),
        element("div", "",
          element("strong", "", q || status ? "Sin expedientes coincidentes" : "Todavía no hay investigaciones"),
          element("p", "", q || status
            ? "Ajusta la búsqueda o el filtro de estado."
            : "Crea una investigación nueva o importa paquetes .zip desde otra computadora."))));
      return;
    }
    const cards = items.map((item) => {
      const badge = element("span", "status", item.status_label);
      if (invStatusBadge[item.status]) badge.dataset.status = invStatusBadge[item.status];
      const openLink = link(item.name, "#investigation/" + item.uuid, "file-link inv-card-title");
      openLink.prepend(icon("case"));

      const actions = element("div", "flex flex-wrap gap-2");
      actions.append(link("Abrir", "#investigation/" + item.uuid, "button accent-button small-btn"));
      if (item.status === "open") {
        actions.append(button("Pausar", () => quickStateTransition(item.uuid, "pause"), "subtle small-btn"));
      } else if (item.status === "paused") {
        actions.append(button("Reanudar", () => quickStateTransition(item.uuid, "resume"), "subtle small-btn"));
      }
      actions.append(link("Exportar .zip", invApi(item.uuid, "/export"), "button quiet small-btn"));

      return element("article", "inv-card",
        element("div", "inv-card-top",
          element("div", "min-w-0",
            element("div", "flex items-center gap-2 flex-wrap", openLink, badge),
            element("p", "small muted",
              "Responsable: " + item.responsible + " · UUID " + item.short_uuid + " · " + item.kind_label),
            ...(item.description ? [element("p", "small muted inv-card-desc", item.description)] : [])),
          actions),
        element("div", "inv-card-meta small muted",
          element("span", "", "Apertura: " + timestamp(item.created_at)),
          element("span", "", "Última actividad: " + timestamp(item.updated_at)),
          element("span", "", numbers.format(item.file_count) + " archivo(s)"),
          element("span", "", numbers.format(item.comparison_count) + " comparación(es)")));
    });
    host.replaceChildren(...cards);
  } catch (error) {
    if (version === invState.listVersion) message("inv-list-message", error.message, true);
  }
}

async function quickStateTransition(uuid, action) {
  try {
    await api(invApi(uuid, "/state"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action}),
    });
    loadInvestigationsList();
  } catch (error) {
    message("inv-list-message", error.message, true);
  }
}

async function handleCreateInvestigation(event) {
  event.preventDefault();
  const name = $("inv-new-name").value.trim();
  const responsible = $("inv-new-responsible").value.trim();
  const description = $("inv-new-description").value.trim();
  const kind = $("inv-new-kind").value;
  const classification = $("inv-new-classification").value;
  if (!name || !responsible) {
    message("inv-create-error", "El nombre de la investigación y el nombre completo del responsable son obligatorios.", true);
    return;
  }
  $("inv-create-submit").disabled = true;
  message("inv-create-error");
  try {
    const created = await api("/api/investigations", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, responsible, description, kind, classification}),
    });
    $("inv-create-form").reset();
    $("inv-create-panel").hidden = true;
    location.hash = "investigation/" + created.uuid;
  } catch (error) {
    message("inv-create-error", error.message, true);
  } finally {
    $("inv-create-submit").disabled = false;
  }
}

// --- Importación Individual y Masiva (Profesores) ---

async function previewSelectedPackages(files) {
  invState.importFiles = Array.from(files);
  invState.importPreview = null;
  $("inv-import-results").hidden = true;
  if (!invState.importFiles.length) {
    $("inv-import-count").textContent = "Ningún paquete seleccionado.";
    $("inv-import-preview").hidden = true;
    return;
  }
  $("inv-import-count").textContent = numbers.format(invState.importFiles.length) + " paquete(s) seleccionado(s).";
  message("inv-import-status", "Validando paquetes y detectando conflictos de UUID…");
  const form = new FormData();
  for (const file of invState.importFiles) form.append("packages", file);
  try {
    const data = await api("/api/investigations/import/preview", {method: "POST", body: form});
    invState.importPreview = data.items;
    message("inv-import-status");
    renderImportPreview(data.items);
  } catch (error) {
    message("inv-import-status", error.message, true);
    $("inv-import-preview").hidden = true;
  }
}

function renderImportPreview(items) {
  const host = $("inv-import-preview");
  host.hidden = false;
  const actionSelects = [];
  const rows = items.map((item) => {
    const cMeta = invConflictLabels[item.conflict] || {label: item.conflict, badge: ""};
    const badge = element("span", "status", cMeta.label);
    if (cMeta.badge) badge.dataset.status = cMeta.badge;

    const sel = element("select", "");
    sel.setAttribute("aria-label", "Acción para " + item.filename);
    const labels = {
      import: "Importar (conservar UUID)",
      copy: "Importar como copia independiente",
      skip: "Omitir",
    };
    for (const act of item.available_actions || ["skip"]) {
      sel.append(new Option(labels[act] || act, act));
    }
    sel.value = item.recommended_action || "skip";
    actionSelects.push(sel);

    return element("tr", "",
      element("td", "", element("strong", "small", item.valid ? item.name : item.filename),
        element("div", "small muted", item.valid ? item.filename : (item.error || "Inválido"))),
      element("td", "small", item.valid ? item.responsible : "—"),
      element("td", "small mono", item.valid ? item.short_uuid : "—"),
      element("td", "small", item.valid ? timestamp(item.created_at) : "—"),
      element("td", "table-number small", item.valid ? numbers.format(item.file_count) : "—"),
      element("td", "", badge,
        ...(item.conflict_detail ? [element("div", "small muted", item.conflict_detail)] : [])),
      element("td", "", sel));
  });

  const runBtn = button("Ejecutar importación seleccionada", async () => {
    runBtn.disabled = true;
    const actions = actionSelects.map((s) => s.value);
    const form = new FormData();
    for (const file of invState.importFiles) form.append("packages", file);
    form.set("actions", JSON.stringify(actions));
    message("inv-import-status", "Importando expedientes de forma transaccional…");
    try {
      const res = await api("/api/investigations/import", {method: "POST", body: form});
      message("inv-import-status");
      renderImportResults(res);
      loadInvestigationsList();
    } catch (error) {
      message("inv-import-status", error.message, true);
    } finally {
      runBtn.disabled = false;
    }
  }, "primary");

  host.replaceChildren(
    element("p", "small muted",
      "Un paquete inválido no impide importar los demás. Cada expediente se importa de manera atómica. No se sobrescriben expedientes locales en conflicto."),
    element("div", "scroll-table",
      element("table", "",
        element("thead", "", element("tr", "",
          element("th", "", "Expediente / Archivo"), element("th", "", "Responsable"),
          element("th", "", "UUID"), element("th", "", "Creación"),
          element("th", "", "Archivos"), element("th", "", "Validación / Conflictos"),
          element("th", "", "Acción"))),
        element("tbody", "", ...rows))),
    element("div", "lab-actions",
      element("span", "small muted", "El manifiesto de hashes verifica integridad interna del paquete, no autenticidad criptográfica."),
      runBtn));
}

function renderImportResults(res) {
  const host = $("inv-import-results");
  host.hidden = false;
  const s = res.summary;
  const stats = element("div", "stats manifest-stats",
    ...[
      ["Importados", s.imported],
      ["Copias independientes", s.imported_copy],
      ["Ya existentes", s.already_exists],
      ["Omitidos", s.skipped],
      ["Conflictos / Errores", s.conflict + s.error],
    ].map(([label, val]) => element("div", "stat", element("strong", "", numbers.format(val)), element("span", "", label))));

  const list = element("ul", "duel-notes small",
    ...res.results.map((r) => {
      const li = element("li", "", element("strong", "", r.filename + ": "), r.message);
      if (r.uuid && (r.outcome === "imported" || r.outcome === "imported_copy" || r.outcome === "already_exists")) {
        li.append(" ", link("Abrir expediente", "#investigation/" + r.uuid, "file-link"));
      }
      return li;
    }));
  host.replaceChildren(element("h3", "", "Resultado de la importación"), stats, list);
}

// --- Vista Individual del Expediente ---

async function openInvestigation(uuid, tab = null) {
  invState.activeFileDetail = null;
  invState.activeComparison = null;
  invState.selectedDocIds = [];
  invState.graphSelectedNode = null;
  $("inv-detail-action-drawer").hidden = true;
  message("inv-detail-notice");
  $("inv-tab-outlet").replaceChildren(element("p", "muted", "Cargando expediente…"));
  try {
    invState.current = await api(invApi(uuid));
    renderInvestigationHeader(invState.current);
    setInvestigationTab(tab || invState.tab || "summary");
  } catch (error) {
    $("inv-detail-header").replaceChildren(element("h1", "", "Expediente no disponible"));
    $("inv-tab-outlet").replaceChildren(element("p", "notice", error.message));
  }
}

function renderInvestigationHeader(inv) {
  setTopbarContext("Investigaciones · " + inv.name);
  const badge = element("span", "status", inv.status_label);
  if (invStatusBadge[inv.status]) badge.dataset.status = invStatusBadge[inv.status];
  const actions = element("div", "flex flex-wrap gap-2");

  if (inv.status !== "closed") {
    actions.append(button("Editar", () => openEditDrawer(inv), "subtle"));
    if (inv.status === "open") {
      actions.append(button("Pausar", () => doStateAction(inv, {action: "pause"}), "subtle"));
    } else if (inv.status === "paused") {
      actions.append(button("Reanudar", () => doStateAction(inv, {action: "resume"}), "subtle"));
    }
    actions.append(button("Cerrar expediente", () => openCloseDrawer(inv), "accent-button"));
  } else {
    actions.append(button("Reabrir expediente", () => openReopenDrawer(inv), "accent-button"));
  }
  actions.append(link("Exportar paquete .zip", invApi(inv.uuid, "/export"), "button subtle"));

  const heading = element("h1", "", inv.name);
  heading.id = "inv-detail-title";
  heading.tabIndex = -1;

  $("inv-detail-header").replaceChildren(
    element("span", "file-mark", icon("case")),
    element("div", "inv-head-main",
      element("div", "flex items-center gap-2 flex-wrap", heading, badge),
      element("p", "small muted",
        "Responsable: " + inv.responsible
        + " · Apertura: " + timestamp(inv.created_at)
        + " · " + numbers.format(inv.file_count) + " archivo(s)"),
      ...(inv.description ? [element("p", "small inv-head-desc", inv.description)] : [])),
    actions);
}

async function doStateAction(inv, payload) {
  message("inv-detail-notice");
  try {
    invState.current = await api(invApi(inv.uuid, "/state"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    $("inv-detail-action-drawer").hidden = true;
    renderInvestigationHeader(invState.current);
    setInvestigationTab(invState.tab);
  } catch (error) {
    message("inv-detail-notice", error.message, true);
  }
}

function openEditDrawer(inv) {
  const drawer = $("inv-detail-action-drawer");
  drawer.hidden = false;
  const nameIn = element("input", "text-input");
  nameIn.value = inv.name;
  const respIn = element("input", "text-input");
  respIn.value = inv.responsible;
  const descIn = element("textarea", "hash-input");
  descIn.rows = 2;
  descIn.value = inv.description;
  const kindSel = element("select", "");
  for (const [k, l] of [["document_analysis", "Análisis documental"], ["integrity_verification", "Verificación de integridad"], ["exploratory_forensic", "Análisis forense exploratorio"]]) {
    kindSel.append(new Option(l, k));
  }
  kindSel.value = inv.kind;
  const classSel = element("select", "");
  classSel.append(new Option("Privada (informativa)", "private"), new Option("Confidencial (informativa)", "confidential"));
  classSel.value = inv.classification;

  const saveBtn = button("Guardar cambios", async () => {
    saveBtn.disabled = true;
    try {
      invState.current = await api(invApi(inv.uuid), {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: nameIn.value, responsible: respIn.value, description: descIn.value,
          kind: kindSel.value, classification: classSel.value,
        }),
      });
      drawer.hidden = true;
      renderInvestigationHeader(invState.current);
      setInvestigationTab(invState.tab);
    } catch (err) {
      message("inv-detail-notice", err.message, true);
    } finally {
      saveBtn.disabled = false;
    }
  }, "primary");

  drawer.replaceChildren(
    element("div", "duel-section-head",
      element("h2", "", "Editar información general"),
      button("Cancelar", () => { drawer.hidden = true; }, "quiet")),
    element("div", "compare-grid",
      element("label", "field-group", element("span", "small muted", "Nombre"), nameIn),
      element("label", "field-group", element("span", "small muted", "Responsable"), respIn)),
    element("label", "field-group", element("span", "small muted", "Descripción"), descIn),
    element("div", "compare-grid",
      element("label", "field-group", element("span", "small muted", "Tipo"), kindSel),
      element("label", "field-group", element("span", "small muted", "Clasificación (informativa)"), classSel)),
    element("p", "small muted", "El UUID del expediente es inmutable y no cambia al editar el nombre ni el responsable."),
    element("div", "flex justify-end gap-2", saveBtn));
}

function openCloseDrawer(inv) {
  const drawer = $("inv-detail-action-drawer");
  drawer.hidden = false;
  const summaryIn = element("textarea", "hash-input");
  summaryIn.rows = 2;
  summaryIn.placeholder = "Resume los hallazgos principales observados";
  const conclusionIn = element("textarea", "hash-input");
  conclusionIn.rows = 3;
  conclusionIn.placeholder = "Conclusión explícita del investigador";
  const finalIn = element("textarea", "hash-input");
  finalIn.rows = 2;
  finalIn.placeholder = "Observaciones finales opcionales";
  const actorIn = element("input", "text-input");
  actorIn.value = inv.responsible;

  const confirmBtn = button("Confirmar cierre", () => doStateAction(inv, {
    action: "close", actor: actorIn.value, summary: summaryIn.value,
    conclusion: conclusionIn.value, final_notes: finalIn.value,
  }), "primary");

  drawer.replaceChildren(
    element("div", "duel-section-head",
      element("h2", "", "Cerrar investigación"),
      button("Cancelar", () => { drawer.hidden = true; }, "quiet")),
    element("p", "small muted", "Un expediente cerrado permanece consultable y exportable, pero bloquea modificaciones ordinarias hasta una reapertura explícita. Las conclusiones no constituyen certificación pericial automática."),
    element("label", "field-group", element("span", "small", "Responsable que cierra"), actorIn),
    element("label", "field-group", element("span", "small", "Resumen de hallazgos *"), summaryIn),
    element("label", "field-group", element("span", "small", "Conclusión *"), conclusionIn),
    element("label", "field-group", element("span", "small muted", "Observaciones finales (opcional)"), finalIn),
    element("div", "flex justify-end gap-2", confirmBtn));
}

function openReopenDrawer(inv) {
  const drawer = $("inv-detail-action-drawer");
  drawer.hidden = false;
  const reasonIn = element("textarea", "hash-input");
  reasonIn.rows = 2;
  reasonIn.placeholder = "Indica el motivo explícito para reabrir este expediente";
  const actorIn = element("input", "text-input");
  actorIn.value = inv.responsible;
  const confirmBtn = button("Confirmar reapertura", () => doStateAction(inv, {
    action: "reopen", actor: actorIn.value, reason: reasonIn.value,
  }), "primary");

  drawer.replaceChildren(
    element("div", "duel-section-head",
      element("h2", "", "Reabrir investigación"),
      button("Cancelar", () => { drawer.hidden = true; }, "quiet")),
    element("label", "field-group", element("span", "small", "Responsable"), actorIn),
    element("label", "field-group", element("span", "small", "Motivo de reapertura *"), reasonIn),
    element("div", "flex justify-end gap-2", confirmBtn));
}

function setInvestigationTab(tab) {
  invState.tab = ["summary", "files", "comparisons", "timeline", "notes", "reports"].includes(tab) ? tab : "summary";
  for (const btn of document.querySelectorAll("[data-inv-tab]")) {
    btn.setAttribute("aria-selected", String(btn.dataset.invTab === invState.tab));
  }
  const inv = invState.current;
  if (!inv) return;
  if (invState.tab === "summary") renderInvSummaryTab(inv);
  else if (invState.tab === "files") renderInvFilesTab(inv);
  else if (invState.tab === "comparisons") renderInvComparisonsTab(inv);
  else if (invState.tab === "timeline") renderInvTimelineTab(inv);
  else if (invState.tab === "notes") renderInvNotesTab(inv);
  else if (invState.tab === "reports") renderInvReportsTab(inv);
}

// --- Tab 1: Resumen ---

function renderInvSummaryTab(inv) {
  const outlet = $("inv-tab-outlet");
  const stats = element("div", "stats",
    ...[
      ["Archivos activos", inv.file_count],
      ["Comparaciones", inv.comparison_count],
      ["Observaciones", inv.note_count],
      ["Eventos de auditoría", inv.event_count],
    ].map(([label, val]) => element("div", "stat", element("strong", "", numbers.format(val)), element("span", "", label))));

  const panel = element("section", "panel lab-panel",
    element("h2", "", "Resumen del expediente"),
    stats,
    element("dl", "detail-facts panel",
      element("div", "", element("dt", "", "Tipo"), element("dd", "", inv.kind_label)),
      element("div", "", element("dt", "", "Clasificación"), element("dd", "", inv.classification_label)),
      element("div", "", element("dt", "", "Última actividad"), element("dd", "", timestamp(inv.updated_at))),
      element("div", "", element("dt", "", "Estado"), element("dd", "", inv.status_label))));

  if (inv.latest_closure) {
    const cl = inv.latest_closure;
    panel.append(element("section", "inspection-callout",
      element("h3", "", "Cierre y conclusión registrados"),
      element("p", "small", element("strong", "", "Resumen: "), cl.summary),
      element("p", "small", element("strong", "", "Conclusión: "), cl.conclusion),
      ...(cl.final_notes ? [element("p", "small", element("strong", "", "Observaciones finales: "), cl.final_notes)] : []),
      element("p", "small muted", "Registrado por " + cl.actor + " el " + timestamp(cl.created_at) + ". " + inv.disclaimers.conclusion)));
  }

  if (inv.origin_uuid) {
    panel.append(element("p", "notice small", inv.origin_note + " (UUID original: " + inv.origin_uuid + ")"));
  }

  const techDetails = element("details", "",
    element("summary", "small", "Detalles técnicos e identificador global (UUID)"),
    element("div", "slide-diff-body",
      element("p", "small muted", "Identificador único global e inmutable para intercambio entre instalaciones:"),
      hashControl(inv.uuid),
      element("p", "small muted", "Apertura UTC: " + inv.created_at + " · Última actividad UTC: " + inv.updated_at),
      element("ul", "duel-notes small muted",
        element("li", "", inv.disclaimers.clock),
        element("li", "", inv.disclaimers.classification),
        element("li", "", inv.disclaimers.actor))));
  panel.append(techDetails);

  const deleteBtn = button("Eliminar expediente local", async () => {
    if (!await confirmDelete("Se eliminará permanentemente esta investigación y todos sus análisis, comparaciones y notas de la base SQLite local.")) return;
    try {
      await api(invApi(inv.uuid), {method: "DELETE"});
      location.hash = "investigations";
    } catch (err) {
      message("inv-detail-notice", err.message, true);
    }
  }, "quiet");
  panel.append(element("div", "lab-actions",
    element("span", "small muted", "Cerrar una investigación no la elimina. Esta acción borra el expediente completo de SQLite."),
    deleteBtn));
  outlet.replaceChildren(panel);
}

// --- Tab 2: Archivos ---

async function renderInvFilesTab(inv) {
  const outlet = $("inv-tab-outlet");
  outlet.replaceChildren(element("p", "muted", "Cargando inventario de archivos…"));
  try {
    const files = await api(invApi(inv.uuid, "/files"));
    const panel = element("section", "panel lab-panel");
    const closed = inv.status === "closed";

    const addInput = element("input", "");
    addInput.type = "file";
    addInput.multiple = true;
    addInput.hidden = true;
    const folderInput = element("input", "");
    folderInput.type = "file";
    folderInput.multiple = true;
    folderInput.webkitdirectory = true;
    folderInput.hidden = true;
    const verifyInput = element("input", "");
    verifyInput.type = "file";
    verifyInput.hidden = true;

    const statusBox = element("div", "notice");
    statusBox.hidden = true;
    statusBox.setAttribute("role", "status");
    const verifyBox = element("div", "verify-banner");
    verifyBox.hidden = true;
    verifyBox.setAttribute("role", "status");

    const uploadFiles = async (fileList) => {
      const arr = Array.from(fileList);
      if (!arr.length) return;
      statusBox.hidden = false;
      statusBox.dataset.error = "false";
      statusBox.textContent = "Analizando e incorporando " + arr.length + " archivo(s)…";
      const form = new FormData();
      for (const f of arr) {
        form.append("files", f);
        form.append("paths", fileRelativePath(f));
      }
      try {
        const res = await api(invApi(inv.uuid, "/files"), {method: "POST", body: form});
        const dupes = res.files.filter((f) => f.duplicate_warning);
        invState.current = await api(invApi(inv.uuid));
        renderInvestigationHeader(invState.current);
        await renderInvFilesTab(invState.current);
        if (dupes.length) {
          message("inv-detail-notice",
            dupes.map((d) => d.name + ": " + d.duplicate_warning).join(" | "));
        }
      } catch (err) {
        statusBox.hidden = false;
        statusBox.dataset.error = "true";
        statusBox.textContent = err.message;
      }
    };

    addInput.addEventListener("change", () => { uploadFiles(addInput.files); addInput.value = ""; });
    folderInput.addEventListener("change", () => { uploadFiles(folderInput.files); folderInput.value = ""; });
    verifyInput.addEventListener("change", async () => {
      const f = verifyInput.files[0];
      verifyInput.value = "";
      if (!f || !invState.verifyingFileUuid) return;
      verifyBox.hidden = false;
      verifyBox.dataset.state = "loading";
      verifyBox.replaceChildren(element("p", "small", "Verificando integridad SHA-256 de «" + f.name + "»…"));
      try {
        const resp = await fetch(invApi(inv.uuid, "/files/" + encodeURIComponent(invState.verifyingFileUuid) + "/verify-hash"), {
          method: "POST",
          headers: {"Content-Type": "application/octet-stream", "X-Orca-Filename": encodeURIComponent(f.name)},
          body: f,
          cache: "no-store",
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || "Error de verificación.");
        const v = await resp.json();
        verifyBox.dataset.state = v.status;
        verifyBox.replaceChildren(
          element("strong", "", v.message),
          element("p", "small mono muted", "Esperado: " + v.expected.sha256),
          element("p", "small mono muted", "Calculado: " + v.actual.sha256));
      } catch (err) {
        verifyBox.dataset.state = "error";
        verifyBox.replaceChildren(element("strong", "", err.message));
      } finally {
        invState.verifyingFileUuid = null;
      }
    });

    const head = element("div", "duel-section-head",
      element("div", "",
        element("h2", "", "Archivos del expediente (" + numbers.format(files.length) + ")"),
        element("p", "small muted", "Se persisten hashes, metadatos y estructura extraída; los archivos originales no se almacenan permanentemente.")));
    if (!closed) {
      head.append(element("div", "flex flex-wrap gap-2",
        button("Incorporar archivos", () => addInput.click(), "primary"),
        button("Incorporar carpeta", () => folderInput.click(), "subtle")));
    }

    const detailHost = element("div", "");
    const openDetail = async (fileUuid) => {
      detailHost.replaceChildren(element("p", "small muted", "Cargando ficha del archivo…"));
      try {
        const d = await api(invApi(inv.uuid, "/files/" + encodeURIComponent(fileUuid)));
        invState.activeFileDetail = d;
        detailHost.replaceChildren(renderInvFileDetail(d, () => detailHost.replaceChildren()));
      } catch (err) {
        detailHost.replaceChildren(element("p", "notice", err.message));
      }
    };

    let listNode;
    if (!files.length) {
      listNode = element("p", "muted small table-empty", "Todavía no hay archivos registrados en esta investigación.");
    } else {
      const rows = files.map((f) => {
        const st = element("span", "status", statusNames[f.status] || f.status);
        st.dataset.status = f.status;
        const nameBtn = button(element("span", "truncate-cell", f.name), () => openDetail(f.uuid), "file-link");
        nameBtn.title = f.name;
        const nameCell = element("td", "files-name", nameBtn);
        if (f.duplicate_of_uuid) {
          const dup = element("span", "status", "Duplicado SHA-256");
          dup.dataset.status = "unsupported";
          nameCell.append(element("div", "", dup));
        }
        if (f.relative_path && f.relative_path !== f.name) {
          const relSpan = element("div", "small mono muted truncate-cell", f.relative_path);
          relSpan.title = f.relative_path;
          nameCell.append(relSpan);
        }
        const acts = element("div", "flex items-center justify-end gap-1",
          button("Ficha", () => openDetail(f.uuid), "subtle small-btn"),
          button("Verificar hash", () => { invState.verifyingFileUuid = f.uuid; verifyInput.click(); }, "quiet small-btn"));
        if (f.sha256) {
          acts.append(button("Lab", () => openHashComparator(f.sha256, "", "SHA-256"), "quiet small-btn"));
        }
        if (!closed) {
          acts.append(button("Retirar", async () => {
            if (!await confirmDelete("Se retirará «" + f.name + "» del inventario activo (el evento quedará registrado en la auditoría).")) return;
            await api(invApi(inv.uuid, "/files/" + encodeURIComponent(f.uuid)), {method: "DELETE"});
            invState.current = await api(invApi(inv.uuid));
            renderInvestigationHeader(invState.current);
            renderInvFilesTab(invState.current);
          }, "quiet small-btn"));
        }
        const shaCell = element("td", "mono small whitespace-nowrap", f.sha256 ? f.sha256.slice(0, 12) + "…" : "—");
        if (f.sha256) shaCell.title = f.sha256;
        return element("tr", "",
          nameCell,
          element("td", "table-status", element("span", "status", display(f.format))),
          element("td", "table-number", formatBytes(f.size)),
          shaCell,
          element("td", "table-number", numbers.format(f.field_count)),
          element("td", "table-status", st),
          element("td", "table-actions", acts));
      });
      listNode = element("div", "scroll-table",
        element("table", "inv-files-table",
          element("thead", "", element("tr", "",
            element("th", "", "Archivo"), element("th", "", "Formato"),
            element("th", "", "Tamaño"), element("th", "", "SHA-256"),
            element("th", "", "Propiedades"), element("th", "", "Estado"),
            element("th", "", "Acciones"))),
          element("tbody", "", ...rows)));
    }

    panel.replaceChildren(head, addInput, folderInput, verifyInput, statusBox, verifyBox, detailHost, listNode);
    outlet.replaceChildren(panel);
  } catch (error) {
    outlet.replaceChildren(element("p", "notice", error.message));
  }
}

function renderInvFileDetail(file, onClose) {
  const card = element("section", "panel duel-section");
  const head = element("div", "duel-section-head",
    element("div", "",
      element("h3", "", "Ficha de archivo: " + file.name),
      element("p", "small muted", "Incorporado: " + timestamp(file.added_at) + " · Formato: " + display(file.format) + " · " + formatBytes(file.size))),
    button("Cerrar ficha", onClose, "quiet"));

  const metaGroups = Object.entries(groupNames)
    .filter(([g]) => file.normalized?.[g]?.length)
    .map(([g, label]) => element("details", "",
      element("summary", "small", label + " (" + file.normalized[g].length + ")"),
      infoRows(file.normalized[g].map((it) => [metadataLabel(it), element("div", "", metadataValue(it.value, true), element("code", "muted", it.source.join(" / ")))]))));

  const structInfo = file.structure_summary
    ? element("p", "small muted",
        "Estructura persistida (" + file.structure_summary.format + "): "
        + (file.structure_summary.slide_count ?? 0) + " diapositiva(s), "
        + (file.structure_summary.media_count ?? 0) + " recurso(s) multimedia.")
    : element("p", "small muted", "Sin extracción estructural profunda para este formato.");

  card.append(
    head,
    hashControl(file.sha256),
    structInfo,
    element("p", "small muted", file.storage_note),
    ...(file.findings?.length
      ? [element("details", "", element("summary", "small", "Hallazgos potencialmente sensibles (" + file.findings.length + ")"),
          element("ul", "duel-notes small", ...file.findings.map((f) => element("li", "", element("strong", "", metadataLabel(f) + ": "), display(f.value)))))]
      : []),
    ...(metaGroups.length ? metaGroups : [element("p", "small muted", "Sin metadatos descriptivos normalizados.")]),
    element("details", "", element("summary", "small", "JSON técnico completo"), element("pre", "", JSON.stringify(file.raw, null, 2))));
  return card;
}

// --- Tab 3: Comparaciones & Matriz de Similitud ---

async function renderInvComparisonsTab(inv) {
  const outlet = $("inv-tab-outlet");
  outlet.replaceChildren(element("p", "muted", "Cargando matriz de similitud y comparaciones…"));
  try {
    const [matrix, saved] = await Promise.all([
      api(invApi(inv.uuid, "/matrix")),
      api(invApi(inv.uuid, "/comparisons")),
    ]);
    const closed = inv.status === "closed";
    const panel = element("section", "panel lab-panel");
    const duelHost = element("div", "");

    const openPairComparison = async (fileA, fileB, compUuid = null) => {
      duelHost.replaceChildren(element("p", "small muted", "Ejecutando y cargando comparación…"));
      try {
        const comp = compUuid
          ? await api(invApi(inv.uuid, "/comparisons/" + encodeURIComponent(compUuid)))
          : await api(invApi(inv.uuid, "/comparisons"), {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({file_a_uuid: fileA, file_b_uuid: fileB}),
            });
        invState.activeComparison = comp;
        renderEmbeddedComparison(duelHost, comp, inv, () => {
          duelHost.replaceChildren();
          renderInvComparisonsTab(invState.current);
        });
      } catch (err) {
        duelHost.replaceChildren(element("p", "notice", err.message));
      }
    };

    const head = element("div", "duel-section-head",
      element("div", "",
        element("h2", "", "Matriz de similitud y Duelo múltiple"),
        element("p", "small muted", matrix.note)));

    if (!closed && matrix.pending_deep_pairs > 0) {
      const batchBtn = button("Calcular lote pendiente (hasta 10 de " + matrix.pending_deep_pairs + ")", async () => {
        batchBtn.disabled = true;
        batchBtn.textContent = "Calculando lote…";
        try {
          await api(invApi(inv.uuid, "/matrix/compute"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({max_pairs: 10}),
          });
          invState.current = await api(invApi(inv.uuid));
          renderInvestigationHeader(invState.current);
          renderInvComparisonsTab(invState.current);
        } catch (err) {
          message("inv-detail-notice", err.message, true);
        }
      }, "accent-button");
      head.append(batchBtn);
    }

    panel.append(head);

    if (matrix.files.length >= 2) {
      // Quick pair runner
      if (!closed) {
        const selA = element("select", "");
        const selB = element("select", "");
        for (const f of matrix.files) {
          selA.append(new Option(f.name + " (" + display(f.format) + ")", f.uuid));
          selB.append(new Option(f.name + " (" + display(f.format) + ")", f.uuid));
        }
        selB.selectedIndex = 1;
        panel.append(element("div", "duel-filters",
          selA, selB,
          button("Comparar pareja", () => {
            if (selA.value === selB.value) {
              message("inv-detail-notice", "Selecciona dos archivos distintos.", true);
              return;
            }
            openPairComparison(selA.value, selB.value);
          }, "primary")));
      }

      // Interactive Similarity Matrix
      const cellMap = new Map();
      for (const c of matrix.cells) {
        cellMap.set(c.file_a_uuid + "|" + c.file_b_uuid, c);
        cellMap.set(c.file_b_uuid + "|" + c.file_a_uuid, c);
      }
      const mHead = element("tr", "", element("th", "", "Archivo"),
        ...matrix.files.map((f, idx) => {
          const th = element("th", "matrix-col-head", "F" + (idx + 1));
          th.title = f.name;
          return th;
        }));
      const mRows = matrix.files.map((fa, i) => {
        const rowHeader = element("th", "matrix-row-head", "F" + (i + 1) + " · " + fa.name);
        const tds = matrix.files.map((fb, j) => {
          if (i === j) return element("td", "matrix-diag", "—");
          const cell = cellMap.get(fa.uuid + "|" + fb.uuid);
          const td = element("td", "matrix-cell");
          let label = "Meta/Bin";
          let stateAttr = "binary";
          if (cell.identical_bytes) {
            label = "SHA =";
            stateAttr = "identical";
          } else if (cell.status === "computed" && cell.similarity_available) {
            label = formatPercent(cell.similarity_overall);
            stateAttr = cell.similarity_overall >= 80 ? "high" : "mid";
          } else if (cell.status === "pending") {
            label = "Calcular";
            stateAttr = "pending";
          }
          const btn = button(label, () => openPairComparison(fa.uuid, fb.uuid, cell.comparison_uuid), "matrix-btn");
          btn.dataset.matrixState = stateAttr;
          btn.title = fa.name + " ↔ " + fb.name + " (" + (cell.identical_bytes ? "SHA-256 idéntico" : label) + ")";
          if (closed && !cell.comparison_uuid) btn.disabled = true;
          td.append(btn);
          return td;
        });
        return element("tr", "", rowHeader, ...tds);
      });
      panel.append(
        element("p", "small muted",
          "Leyenda: «SHA =» indica identidad binaria exacta; «X%» indica similitud documental calculada; «Calcular» ejecuta el análisis profundo bajo demanda; «Meta/Bin» solo permite identidad binaria y metadatos."),
        element("div", "scroll-table",
          element("table", "matrix-table", element("thead", "", mHead), element("tbody", "", ...mRows))));
    } else {
      panel.append(element("p", "muted small", "Incorpora al menos dos archivos en la pestaña Archivos para construir la matriz de similitud."));
    }

    panel.append(duelHost);

    // Saved comparisons history
    if (saved.length) {
      const list = element("ul", "duel-notes small",
        ...saved.map((c) => element("li", "",
          button(c.file_a_name + " ↔ " + c.file_b_name, () => openPairComparison(c.file_a_uuid, c.file_b_uuid, c.uuid), "file-link"),
          " · ",
          c.identical_bytes ? "Bytes idénticos" : (c.similarity_available ? "Similitud " + formatPercent(c.similarity_overall) : "Solo metadatos/binario"),
          " · ", element("span", "muted", timestamp(c.created_at) + " · " + c.analyzer_version))));
      panel.append(element("div", "", element("h3", "", "Comparaciones guardadas (" + saved.length + ")"), list));
    }

    outlet.replaceChildren(panel);
  } catch (error) {
    outlet.replaceChildren(element("p", "notice", error.message));
  }
}

function renderEmbeddedComparison(host, comp, inv, onClose) {
  const result = comp.result;
  const box = element("section", "panel duel-section embedded-duel");
  const head = element("div", "duel-section-head",
    element("div", "",
      element("h3", "", "Duelo: " + result.a.name + " ↔ " + result.b.name),
      element("p", "small muted", "UUID comparación: " + comp.uuid.slice(0, 8) + "… · " + timestamp(comp.created_at) + " · Algoritmos: " + comp.algorithms.join(", "))),
    button("Cerrar duelo", onClose, "subtle"));
  box.append(head);
  const sim = similaritySection(result.similarity);
  if (sim) box.append(sim);
  const bin = binaryIdentitySection(result.binary_identity, result);
  if (bin) box.append(bin);
  const diff = deepDiffSection(result.content_comparison, result.similarity);
  if (diff) box.append(diff);

  const techCheck = element("input", "");
  techCheck.type = "checkbox";
  const metaTable = element("div", "");
  const drawMeta = () => {
    const rows = result.rows.filter((r) => techCheck.checked || !r.technical);
    metaTable.replaceChildren(rows.length ? compareTable(rows) : element("p", "small muted", "Sin propiedades."));
  };
  techCheck.addEventListener("change", drawMeta);
  drawMeta();
  box.append(element("details", "",
    element("summary", "small", "C. Comparación de metadatos (" + result.rows.length + " campos)"),
    element("div", "slide-diff-body",
      element("label", "check", techCheck, "Incluir propiedades técnicas"), metaTable)));
  host.replaceChildren(box);
}

// --- Tab 4: Línea de Tiempo (Actividades, Documental y Mapa de Relaciones) ---

async function renderInvTimelineTab(inv) {
  const outlet = $("inv-tab-outlet");
  const sub = element("div", "subnav");
  for (const [key, label] of [["activity", "Actividades (auditoría)"], ["documents", "Línea de tiempo documental"], ["graph", "Mapa de relaciones"]]) {
    const b = button(label, () => {
      invState.timelineMode = key;
      renderInvTimelineTab(inv);
    }, "subnav-tab");
    b.setAttribute("aria-selected", String(invState.timelineMode === key));
    sub.append(b);
  }
  const body = element("div", "");
  outlet.replaceChildren(sub, body);

  if (invState.timelineMode === "activity") await renderActivityTimeline(inv, body);
  else if (invState.timelineMode === "documents") await renderDocumentTimeline(inv, body);
  else await renderRelationGraph(inv, body);
}

async function renderActivityTimeline(inv, host) {
  host.replaceChildren(element("p", "muted", "Cargando línea de tiempo de actividades…"));
  try {
    const data = await api(invApi(inv.uuid, "/timeline/activity"));
    const filter = element("select", "");
    filter.append(new Option("Todos los eventos (" + data.events.length + ")", ""),
      ...Object.entries(invEventLabels).map(([k, l]) => new Option(l, k)));
    const listHost = element("ul", "time-events");
    const draw = () => {
      const items = data.events.filter((e) => !filter.value || e.event_type === filter.value);
      listHost.replaceChildren(...(items.length ? items.map((ev) => element("li", "time-event",
        element("div", "time-event-title",
          element("strong", "", invEventLabels[ev.event_type] || ev.event_type),
          element("span", "small muted", timestamp(ev.created_at) + " · " + ev.actor)),
        element("p", "small", ev.description),
        element("code", "small muted", "UUID " + ev.uuid.slice(0, 8) + "… · UTC " + ev.created_at)))
        : [element("li", "small muted", "Sin eventos para este filtro.")]));
    };
    filter.addEventListener("change", draw);
    draw();

    host.replaceChildren(element("section", "panel lab-panel",
      element("h2", "", "Línea de tiempo de actividades (solo anexado)"),
      element("p", "small muted", data.disclaimers.clock + " " + data.disclaimers.actor),
      element("div", "duel-filters", filter),
      listHost));
  } catch (err) {
    host.replaceChildren(element("p", "notice", err.message));
  }
}

async function renderDocumentTimeline(inv, host) {
  host.replaceChildren(element("p", "muted", "Cargando línea de tiempo documental…"));
  try {
    const data = await api(invApi(inv.uuid, "/timeline/documents?sort=" + encodeURIComponent(invState.docSort)));
    const closed = inv.status === "closed";
    const sortSel = element("select", "");
    for (const [k, l] of [["added", "Orden de incorporación"], ["created", "Fecha interna de creación (declarada)"], ["modified", "Fecha interna de modificación (declarada)"], ["manual", "Orden manual del investigador"]]) {
      sortSel.append(new Option(l, k));
    }
    sortSel.value = invState.docSort;
    sortSel.addEventListener("change", () => {
      invState.docSort = sortSel.value;
      renderDocumentTimeline(inv, host);
    });

    const duelBtn = button("Ejecutar Duelo con 2 seleccionados", () => {
      if (invState.selectedDocIds.length !== 2) return;
      invState.tab = "comparisons";
      setInvestigationTab("comparisons");
      api(invApi(inv.uuid, "/comparisons"), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({file_a_uuid: invState.selectedDocIds[0], file_b_uuid: invState.selectedDocIds[1]}),
      }).then(() => renderInvComparisonsTab(inv));
    }, "accent-button");
    duelBtn.disabled = invState.selectedDocIds.length !== 2;

    const moveNode = async (idx, dir) => {
      const ids = data.nodes.map((n) => n.uuid);
      const target = idx + dir;
      if (target < 0 || target >= ids.length) return;
      [ids[idx], ids[target]] = [ids[target], ids[idx]];
      await api(invApi(inv.uuid, "/files/order"), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({file_uuids: ids}),
      });
      invState.docSort = "manual";
      renderDocumentTimeline(inv, host);
    };

    const list = element("ol", "time-events");
    data.nodes.forEach((node, idx) => {
      const cb = element("input", "");
      cb.type = "checkbox";
      cb.checked = invState.selectedDocIds.includes(node.uuid);
      cb.addEventListener("change", () => {
        if (cb.checked && invState.selectedDocIds.length < 2) invState.selectedDocIds.push(node.uuid);
        else invState.selectedDocIds = invState.selectedDocIds.filter((id) => id !== node.uuid);
        duelBtn.disabled = invState.selectedDocIds.length !== 2;
      });
      const rels = data.relations.filter((r) => r.source === node.uuid || r.target === node.uuid);
      const controls = element("div", "flex flex-wrap items-center gap-2",
        element("label", "check", cb, element("strong", "", node.name)),
        element("span", "status", display(node.format)));
      if (!closed) {
        controls.append(
          button("↑", () => moveNode(idx, -1), "quiet small-btn"),
          button("↓", () => moveNode(idx, 1), "quiet small-btn"));
      }
      list.append(element("li", "time-event",
        controls,
        element("p", "small muted",
          "Incorporado: " + timestamp(node.added_at)
          + " · Creación declarada: " + (node.internal_created || "Sin dato")
          + " · Modificación declarada: " + (node.internal_modified || "Sin dato")),
        ...(rels.length ? [element("p", "small muted", "Relaciones: " + rels.map((r) => r.label + " [" + r.confidence_label + "]").join(" · "))] : [])));
    });

    const panel = element("section", "panel lab-panel",
      element("h2", "", "Línea de tiempo documental"),
      ...data.disclaimers.map((d) => element("p", "notice small", d)),
      element("div", "duel-filters", sortSel, duelBtn),
      data.nodes.length ? list : element("p", "muted small", "No hay documentos en el expediente."));

    // Manual relation annotation form
    if (!closed && data.nodes.length >= 2) {
      const sA = element("select", "");
      const sB = element("select", "");
      for (const n of data.nodes) {
        sA.append(new Option(n.name, n.uuid));
        sB.append(new Option(n.name, n.uuid));
      }
      sB.selectedIndex = 1;
      const rType = element("select", "");
      rType.append(new Option("Relación confirmada manualmente", "manual_confirmed"), new Option("Hipótesis de relación", "manual_hypothesis"));
      const rLabel = element("input", "text-input");
      rLabel.placeholder = "Ej. Revisión entregada tras corrección";
      const addRelBtn = button("Anotar relación", async () => {
        try {
          await api(invApi(inv.uuid, "/relations"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({source_file_uuid: sA.value, target_file_uuid: sB.value, relation_type: rType.value, label: rLabel.value}),
          });
          renderDocumentTimeline(inv, host);
        } catch (err) {
          message("inv-detail-notice", err.message, true);
        }
      }, "subtle");
      panel.append(element("details", "",
        element("summary", "small", "Anotar relación manual entre documentos"),
        element("div", "slide-diff-body",
          element("div", "compare-grid", sA, sB),
          element("div", "compare-grid", rType, rLabel),
          addRelBtn)));
    }
    host.replaceChildren(panel);
  } catch (err) {
    host.replaceChildren(element("p", "notice", err.message));
  }
}

async function renderRelationGraph(inv, host) {
  host.replaceChildren(element("p", "muted", "Construyendo mapa de relaciones…"));
  try {
    const graph = await api(invApi(inv.uuid, "/graph"));
    const panel = element("section", "panel lab-panel",
      element("h2", "", "Mapa de relaciones entre documentos"),
      element("p", "notice small", graph.disclaimer));

    if (!graph.nodes.length) {
      panel.append(element("p", "small muted", "Incorpora documentos para visualizar sus relaciones."));
      host.replaceChildren(panel);
      return;
    }

    const filterBar = element("div", "algo-checks");
    for (const [type, label] of Object.entries(invRelationLabels)) {
      const cb = element("input", "");
      cb.type = "checkbox";
      cb.checked = invState.graphFilters.has(type);
      cb.addEventListener("change", () => {
        if (cb.checked) invState.graphFilters.add(type);
        else invState.graphFilters.delete(type);
        renderRelationGraph(inv, host);
      });
      filterBar.append(element("label", "check", cb, label));
    }
    panel.append(filterBar);

    const visibleEdges = graph.edges.filter((e) => invState.graphFilters.has(e.relation_type));
    const width = 680;
    const height = Math.max(260, Math.min(460, 180 + graph.nodes.length * 24));
    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(width, height) * 0.36;
    const coords = new Map();
    graph.nodes.forEach((n, idx) => {
      const angle = (2 * Math.PI * idx) / Math.max(1, graph.nodes.length) - Math.PI / 2;
      coords.set(n.uuid, {
        x: graph.nodes.length === 1 ? cx : cx + radius * Math.cos(angle),
        y: graph.nodes.length === 1 ? cy : cy + radius * Math.sin(angle),
      });
    });

    const svg = timelineSvg("svg", {viewBox: `0 0 ${width} ${height}`, class: "relation-graph-svg", role: "img", "aria-label": "Grafo de relaciones documentales"});
    for (const edge of visibleEdges) {
      const a = coords.get(edge.source);
      const b = coords.get(edge.target);
      if (!a || !b) continue;
      const line = timelineSvg("line", {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        class: "graph-edge graph-edge-" + edge.relation_type,
      });
      svg.append(line);
    }

    const inspector = element("div", "graph-inspector");
    const showNodeInfo = (node) => {
      invState.graphSelectedNode = node.uuid;
      const related = visibleEdges.filter((e) => e.source === node.uuid || e.target === node.uuid);
      const byId = new Map(graph.nodes.map((n) => [n.uuid, n]));
      inspector.replaceChildren(
        element("strong", "", node.name + " (" + display(node.format) + ")"),
        element("p", "small muted mono", "SHA-256: " + (node.sha256 || "—")),
        related.length
          ? element("ul", "duel-notes small", ...related.map((r) => {
              const other = byId.get(r.source === node.uuid ? r.target : r.source);
              return element("li", "",
                element("strong", "", other ? other.name : "Archivo"),
                " — ", r.label, " (", r.confidence_label, ") ",
                button("Ver Duelo", async () => {
                  await api(invApi(inv.uuid, "/comparisons"), {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({file_a_uuid: node.uuid, file_b_uuid: other.uuid}),
                  });
                  setInvestigationTab("comparisons");
                }, "quiet small-btn"));
            }))
          : element("p", "small muted", "Sin conexiones visibles con los filtros actuales."));
    };

    graph.nodes.forEach((node, idx) => {
      const pos = coords.get(node.uuid);
      const g = timelineSvg("g", {class: "graph-node", tabindex: "0", role: "button", "aria-label": node.name});
      g.append(
        timelineSvg("circle", {cx: pos.x, cy: pos.y, r: 16, class: "graph-node-circle"}),
        (() => {
          const t = timelineSvg("text", {x: pos.x, y: pos.y + 4, "text-anchor": "middle", class: "graph-node-id"});
          t.textContent = "F" + (idx + 1);
          return t;
        })(),
        (() => {
          const lbl = timelineSvg("text", {x: pos.x, y: pos.y + 30, "text-anchor": "middle", class: "graph-node-label"});
          lbl.textContent = node.name.length > 20 ? node.name.slice(0, 18) + "…" : node.name;
          return lbl;
        })(),
      );
      g.addEventListener("click", () => showNodeInfo(node));
      g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); showNodeInfo(node); } });
      svg.append(g);
    });

    panel.append(svg, inspector);
    if (graph.clusters.length) {
      const byId = new Map(graph.nodes.map((n) => [n.uuid, n.name]));
      panel.append(element("div", "",
        element("h3", "small", "Agrupaciones detectadas (" + graph.clusters.length + ")"),
        element("ul", "duel-notes small muted",
          ...graph.clusters.map((c, i) => element("li", "", "Grupo " + (i + 1) + ": " + c.map((u) => byId.get(u) || u).join(" · "))))));
    }
    showNodeInfo(graph.nodes[0]);
    host.replaceChildren(panel);
  } catch (err) {
    host.replaceChildren(element("p", "notice", err.message));
  }
}

// --- Tab 5: Observaciones del Investigador (con historial de versiones) ---

async function renderInvNotesTab(inv) {
  const outlet = $("inv-tab-outlet");
  outlet.replaceChildren(element("p", "muted", "Cargando observaciones…"));
  try {
    const [notes, files, comps] = await Promise.all([
      api(invApi(inv.uuid, "/notes")),
      api(invApi(inv.uuid, "/files")),
      api(invApi(inv.uuid, "/comparisons")),
    ]);
    const closed = inv.status === "closed";
    const panel = element("section", "panel lab-panel",
      element("h2", "", "Observaciones del investigador (" + notes.length + ")"),
      element("p", "small muted", "Las observaciones humanas se distinguen explícitamente de los resultados automáticos del inspector y conservan historial de versiones al editarse."));

    if (!closed) {
      const targetType = element("select", "");
      targetType.append(
        new Option("Investigación completa", "investigation"),
        new Option("Archivo específico", "file"),
        new Option("Comparación específica", "comparison"));
      const targetSelect = element("select", "");
      targetSelect.hidden = true;
      const syncTargets = () => {
        targetSelect.replaceChildren();
        if (targetType.value === "file") {
          targetSelect.hidden = false;
          for (const f of files) targetSelect.append(new Option(f.name, f.uuid));
        } else if (targetType.value === "comparison") {
          targetSelect.hidden = false;
          for (const c of comps) targetSelect.append(new Option(c.file_a_name + " ↔ " + c.file_b_name, c.uuid));
        } else {
          targetSelect.hidden = true;
        }
      };
      targetType.addEventListener("change", syncTargets);
      const authorIn = element("input", "text-input");
      authorIn.value = inv.responsible;
      const textIn = element("textarea", "hash-input");
      textIn.rows = 3;
      textIn.placeholder = "Escribe una observación o hallazgo redactado por el investigador…";
      const createBtn = button("Agregar observación", async () => {
        try {
          await api(invApi(inv.uuid, "/notes"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              text: textIn.value,
              author: authorIn.value,
              target_type: targetType.value,
              target_uuid: targetType.value === "investigation" ? null : targetSelect.value,
            }),
          });
          invState.current = await api(invApi(inv.uuid));
          renderInvNotesTab(invState.current);
        } catch (err) {
          message("inv-detail-notice", err.message, true);
        }
      }, "primary");

      panel.append(element("div", "verify-step",
        element("div", "compare-grid",
          element("label", "field-group", element("span", "small muted", "Vincular a"), targetType),
          element("label", "field-group", element("span", "small muted", "Autor declarado"), authorIn)),
        targetSelect,
        textIn,
        element("div", "flex justify-end", createBtn)));
    }

    if (!notes.length) {
      panel.append(element("p", "small muted", "Todavía no hay observaciones registradas."));
    } else {
      for (const n of notes) {
        const card = element("article", "hash-history-row");
        const badge = element("span", "status", "Observación humana · v" + n.version);
        const editBox = element("div", "slide-diff-body");
        editBox.hidden = true;
        const editArea = element("textarea", "hash-input");
        editArea.value = n.text;
        const saveEdit = button("Guardar nueva versión", async () => {
          await api(invApi(inv.uuid, "/notes/" + encodeURIComponent(n.uuid)), {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({text: editArea.value, author: inv.responsible}),
          });
          renderInvNotesTab(inv);
        }, "primary small-btn");
        editBox.append(editArea, element("div", "flex gap-2", saveEdit, button("Cancelar", () => { editBox.hidden = true; }, "quiet small-btn")));

        const head = element("div", "hash-history-top",
          element("div", "flex items-center gap-2 flex-wrap",
            element("strong", "small", n.author), badge,
            element("span", "small muted", "Vínculo: " + n.target_type + " · " + timestamp(n.updated_at))),
          ...(!closed ? [button("Editar", () => { editBox.hidden = !editBox.hidden; }, "subtle small-btn")] : []));
        card.append(head, element("p", "metadata-value", n.text), editBox);
        if (n.versions?.length > 1) {
          card.append(element("details", "",
            element("summary", "small", "Historial de versiones (" + n.versions.length + ")"),
            element("ul", "duel-notes small muted",
              ...n.versions.map((v) => element("li", "", "v" + v.version + " (" + timestamp(v.created_at) + " · " + v.author + "): " + v.text)))));
        }
        panel.append(card);
      }
    }
    outlet.replaceChildren(panel);
  } catch (err) {
    outlet.replaceChildren(element("p", "notice", err.message));
  }
}

// --- Tab 6: Informes y Portabilidad ---

function renderInvReportsTab(inv) {
  const outlet = $("inv-tab-outlet");
  const status = element("p", "small muted");
  status.setAttribute("role", "status");

  const manifestBtn = button("Generar manifiesto de integridad (JSON)", async () => {
    manifestBtn.disabled = true;
    try {
      const m = await api(invApi(inv.uuid, "/manifest"), {method: "POST"});
      downloadBlobFile(JSON.stringify(m, null, 2), "application/json", "orca-manifiesto-" + inv.short_uuid + ".json");
      status.textContent = "Manifiesto SHA-256 generado y evento registrado en la auditoría.";
    } catch (err) {
      status.textContent = err.message;
    } finally {
      manifestBtn.disabled = false;
    }
  }, "subtle");

  outlet.replaceChildren(element("section", "panel lab-panel",
    element("h2", "", "Informes y exportación portable"),
    element("p", "small muted",
      "El paquete portable (.zip) incluye todos los resultados, metadatos, comparaciones, cronologías, observaciones, relaciones y un manifiesto de integridad interno, sin incluir archivos originales por defecto."),
    element("div", "flex flex-wrap gap-2",
      link("Exportar paquete portable (.zip)", invApi(inv.uuid, "/export"), "button primary"),
      link("Descargar informe HTML", invApi(inv.uuid, "/report/html"), "button subtle"),
      link("Descargar informe JSON (compatible con IA)", invApi(inv.uuid, "/report/json"), "button subtle"),
      manifestBtn),
    status,
    element("p", "notice small",
      "El manifiesto de hashes incluido en el paquete verifica integridad frente a alteraciones accidentales, pero no garantiza autenticidad criptográfica al no incluir firma digital.")));
}

// --- Inicialización de eventos ---

function initInvestigations() {
  $("inv-open-create").addEventListener("click", () => {
    $("inv-import-panel").hidden = true;
    $("inv-create-panel").hidden = false;
    $("inv-new-name").focus();
  });
  for (const id of ["inv-create-cancel", "inv-create-close"]) {
    $(id).addEventListener("click", () => { $("inv-create-panel").hidden = true; });
  }
  $("inv-create-form").addEventListener("submit", handleCreateInvestigation);

  $("inv-open-import").addEventListener("click", () => {
    $("inv-create-panel").hidden = true;
    $("inv-import-panel").hidden = false;
  });
  $("inv-import-close").addEventListener("click", () => { $("inv-import-panel").hidden = true; });
  $("inv-import-pick").addEventListener("click", () => $("inv-import-input").click());
  $("inv-import-input").addEventListener("change", () => {
    previewSelectedPackages($("inv-import-input").files);
    $("inv-import-input").value = "";
  });

  let searchTimer = null;
  $("inv-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadInvestigationsList, 180);
  });
  $("inv-status-filter").addEventListener("change", loadInvestigationsList);
  $("inv-sort").addEventListener("change", loadInvestigationsList);

  for (const btn of document.querySelectorAll("[data-inv-tab]")) {
    btn.addEventListener("click", () => setInvestigationTab(btn.dataset.invTab));
  }
}
