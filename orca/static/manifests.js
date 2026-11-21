"use strict";

const manifestState = {
  tab: "generate",
  genFiles: [],
  genController: null,
  generatedManifest: null,
  verManifest: null,
  verManifestName: "",
  verFiles: [],
  verController: null,
  verReport: null,
};

const manifestStatusMeta = {
  unchanged: {label: "Sin cambios", badge: "ok"},
  modified: {label: "Modificado", badge: "error"},
  missing: {label: "Faltante", badge: "unsupported"},
  extra: {label: "Adicional", badge: ""},
  unreadable: {label: "No analizable", badge: "error"},
};

function setManifestTab(tab) {
  manifestState.tab = tab === "verify" ? "verify" : "generate";
  for (const btn of document.querySelectorAll("[data-manifest-tab]")) {
    btn.setAttribute("aria-selected", String(btn.dataset.manifestTab === manifestState.tab));
  }
  $("manifest-tab-generate").hidden = manifestState.tab !== "generate";
  $("manifest-tab-verify").hidden = manifestState.tab !== "verify";
}

function fileRelativePath(file) {
  const raw = (file.webkitRelativePath && file.webkitRelativePath.trim()) ? file.webkitRelativePath : file.name;
  return raw.replace(/\\/g, "/").replace(/^\.\//, "");
}

function downloadBlobFile(content, mime, filename) {
  const blob = new Blob([content], {type: mime});
  const url = URL.createObjectURL(blob);
  const a = link("", url);
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function updateManifestGenUI() {
  const running = Boolean(manifestState.genController);
  const count = manifestState.genFiles.length;
  $("manifest-gen-run").disabled = running || count === 0;
  $("manifest-gen-clear").hidden = running || count === 0;
  $("manifest-gen-cancel").hidden = !running;
  $("manifest-pick-files").disabled = running;
  $("manifest-pick-folder").disabled = running;
  $("manifest-gen-algo").disabled = running;

  const algo = $("manifest-gen-algo").value;
  const info = hashAlgoMeta[algo];
  const warn = $("manifest-gen-legacy");
  if (info?.legacy) {
    warn.hidden = false;
    warn.dataset.error = "false";
    warn.textContent = info.warning;
  } else {
    warn.hidden = true;
    warn.textContent = "";
  }

  if (!count) {
    $("manifest-gen-selection").textContent = "Ningún archivo seleccionado todavía.";
  } else {
    const totalBytes = manifestState.genFiles.reduce((acc, f) => acc + f.size, 0);
    $("manifest-gen-selection").textContent =
      numbers.format(count) + " archivo(s) seleccionados · " + formatBytes(totalBytes) + " en total.";
  }
}

function updateManifestVerUI() {
  const running = Boolean(manifestState.verController);
  $("manifest-ver-run").disabled = running || !manifestState.verManifest;
  $("manifest-ver-cancel").hidden = !running;
  $("manifest-import-btn").disabled = running;
  $("manifest-ver-files-btn").disabled = running;
  $("manifest-ver-folder-btn").disabled = running;

  if (!manifestState.verFiles.length) {
    $("manifest-ver-selection").textContent = "Ningún archivo seleccionado (puedes verificar incluso con 0 archivos para detectar faltantes).";
  } else {
    const totalBytes = manifestState.verFiles.reduce((acc, f) => acc + f.size, 0);
    $("manifest-ver-selection").textContent =
      numbers.format(manifestState.verFiles.length) + " archivo(s) seleccionados · " + formatBytes(totalBytes);
  }
}

async function runGenerateManifest() {
  if (!manifestState.genFiles.length || manifestState.genController) return;
  const algorithm = $("manifest-gen-algo").value;
  const controller = new AbortController();
  manifestState.genController = controller;
  manifestState.generatedManifest = null;
  $("manifest-gen-output").hidden = true;
  message("manifest-gen-status");
  $("manifest-gen-progress").hidden = false;
  $("manifest-gen-bar").max = manifestState.genFiles.length;
  $("manifest-gen-bar").value = 0;
  updateManifestGenUI();

  const hashedEntries = [];
  const errors = [];
  try {
    for (let i = 0; i < manifestState.genFiles.length; i++) {
      const file = manifestState.genFiles[i];
      const relPath = fileRelativePath(file);
      $("manifest-gen-progress-count").textContent = i + " / " + manifestState.genFiles.length;
      $("manifest-gen-current").textContent = relPath;
      try {
        const res = await streamFileHash(file, [algorithm], {signal: controller.signal});
        hashedEntries.push({path: relPath, size: res.size, hash: res.results[0].hash});
      } catch (err) {
        if (err.name === "AbortError") throw err;
        errors.push(relPath + ": " + err.message);
      }
      $("manifest-gen-bar").value = i + 1;
    }
    $("manifest-gen-progress-count").textContent = manifestState.genFiles.length + " / " + manifestState.genFiles.length;

    if (!hashedEntries.length) {
      throw new Error("No se pudo procesar ningún archivo para el manifiesto.");
    }

    const manifest = await api("/api/manifests/generate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({algorithm, files: hashedEntries}),
      signal: controller.signal,
    });
    manifestState.generatedManifest = manifest;
    renderGeneratedManifest(manifest, errors);
    message("manifest-gen-status", errors.length
      ? "Manifiesto generado con " + manifest.files.length + " archivo(s) y " + errors.length + " error(es) de lectura."
      : "Manifiesto v" + manifest.version + " generado con " + manifest.files.length + " archivo(s).");
  } catch (error) {
    if (error.name === "AbortError") {
      message("manifest-gen-status", "Generación de manifiesto cancelada.");
    } else {
      message("manifest-gen-status", error.message, true);
    }
  } finally {
    manifestState.genController = null;
    $("manifest-gen-progress").hidden = true;
    updateManifestGenUI();
  }
}

function renderGeneratedManifest(manifest, errors) {
  const out = $("manifest-gen-output");
  out.hidden = false;
  const jsonText = JSON.stringify(manifest, null, 2);
  const feedback = element("span", "small muted copy-feedback");
  feedback.setAttribute("role", "status");

  const actions = element("div", "flex flex-wrap items-center gap-2",
    button("Descargar JSON", () => downloadBlobFile(jsonText, "application/json", "orca-manifiesto.json"), "primary"),
    button("Copiar JSON", () => copyText(jsonText, out, feedback, "Manifiesto JSON copiado."), "subtle"),
    button("Enviar a Verificar", () => {
      manifestState.verManifest = manifest;
      manifestState.verManifestName = "Manifiesto recién generado";
      $("manifest-import-meta").textContent =
        "Manifiesto v" + manifest.version + " · " + manifest.algorithm + " · " + numbers.format(manifest.files.length) + " archivo(s)";
      setManifestTab("verify");
      updateManifestVerUI();
    }, "accent-button"),
    feedback);

  const previewRows = manifest.files.slice(0, 50).map((f) => element("tr", "",
    element("td", "", element("code", "", f.path)),
    element("td", "table-number whitespace-nowrap", formatBytes(f.size)),
    element("td", "", element("code", "hash-inline", f.hash))));

  const table = element("div", "scroll-table",
    element("table", "",
      element("thead", "", element("tr", "",
        element("th", "", "Ruta relativa"), element("th", "", "Tamaño"), element("th", "", "Hash (" + manifest.algorithm + ")"))),
      element("tbody", "", ...previewRows)));

  const nodes = [actions];
  if (errors.length) {
    nodes.push(element("div", "notice",
      element("strong", "", "Archivos omitidos por error (" + errors.length + "):"),
      element("ul", "duel-notes small", ...errors.map((e) => element("li", "", e)))));
  }
  nodes.push(table);
  if (manifest.files.length > 50) {
    nodes.push(element("p", "small muted", "Mostrando los primeros 50 de " + numbers.format(manifest.files.length) + " archivos. Descarga el JSON para ver el manifiesto completo."));
  }
  nodes.push(element("details", "", element("summary", "small", "Vista JSON completa"), element("pre", "", jsonText)));
  out.replaceChildren(...nodes);
}

async function importManifestFile(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || parsed.version !== 1 || !Array.isArray(parsed.files)) {
      throw new Error("El JSON no corresponde a un manifiesto Orca versión 1 válido.");
    }
    manifestState.verManifest = parsed;
    manifestState.verManifestName = file.name;
    $("manifest-import-meta").textContent =
      file.name + " · v" + parsed.version + " · " + (parsed.algorithm || "SHA-256") + " · " + numbers.format(parsed.files.length) + " archivo(s)";
    message("manifest-ver-status");
  } catch (error) {
    manifestState.verManifest = null;
    $("manifest-import-meta").textContent = "Error al cargar el manifiesto.";
    message("manifest-ver-status", error.message || "No se pudo leer el archivo JSON.", true);
  }
  updateManifestVerUI();
}

async function runVerifyManifest() {
  if (!manifestState.verManifest || manifestState.verController) return;
  const manifest = manifestState.verManifest;
  const algorithm = manifest.algorithm || "SHA-256";
  const controller = new AbortController();
  manifestState.verController = controller;
  manifestState.verReport = null;
  $("manifest-ver-results").hidden = true;
  message("manifest-ver-status");

  const files = manifestState.verFiles;
  if (files.length) {
    $("manifest-ver-progress").hidden = false;
    $("manifest-ver-bar").max = files.length;
    $("manifest-ver-bar").value = 0;
  }
  updateManifestVerUI();

  const observed = [];
  const unreadable = [];
  try {
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const relPath = fileRelativePath(file);
      $("manifest-ver-progress-count").textContent = i + " / " + files.length;
      $("manifest-ver-current").textContent = relPath;
      try {
        const res = await streamFileHash(file, [algorithm], {signal: controller.signal});
        observed.push({path: relPath, size: res.size, hash: res.results[0].hash});
      } catch (err) {
        if (err.name === "AbortError") throw err;
        unreadable.push({path: relPath, error: err.message || "Error de lectura"});
      }
      $("manifest-ver-bar").value = i + 1;
    }

    const report = await api("/api/manifests/verify", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({manifest, files: observed, unreadable}),
      signal: controller.signal,
    });
    manifestState.verReport = report;
    renderManifestVerification(report);
  } catch (error) {
    if (error.name === "AbortError") {
      message("manifest-ver-status", "Verificación de manifiesto cancelada.");
    } else {
      message("manifest-ver-status", error.message, true);
    }
  } finally {
    manifestState.verController = null;
    $("manifest-ver-progress").hidden = true;
    updateManifestVerUI();
  }
}

async function downloadManifestHtml(report) {
  const response = await fetch("/api/manifests/report", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(report),
    cache: "no-store",
  });
  if (!response.ok) throw new Error("No se pudo generar el informe HTML.");
  const html = await response.text();
  downloadBlobFile(html, "text/html;charset=utf-8", "orca-verificacion-manifiesto.html");
}

function renderManifestVerification(report) {
  const container = $("manifest-ver-results");
  container.hidden = false;

  const verdictTitles = {
    verified: "Integridad verificada sin discrepancias",
    verified_with_extra: "Archivos del manifiesto íntegros (hay archivos adicionales)",
    mismatch: "Se detectaron diferencias de integridad",
  };
  const banner = element("div", "verify-banner",
    element("strong", "", verdictTitles[report.status] || "Resultado de verificación"),
    element("p", "small muted",
      "Algoritmo: " + report.manifest.algorithm + " · Manifiesto del " + timestamp(report.manifest.createdAt)
      + " · Esperados: " + numbers.format(report.summary.expected_total) + " · Observados: " + numbers.format(report.summary.observed_total)));
  banner.dataset.state = report.status === "verified" ? "verified" : report.status === "verified_with_extra" ? "warning" : "changed";

  const stats = element("div", "stats manifest-stats",
    ...[
      ["Sin cambios", report.summary.unchanged],
      ["Modificados", report.summary.modified],
      ["Faltantes", report.summary.missing],
      ["Adicionales", report.summary.extra],
      ["No analizables", report.summary.unreadable],
    ].map(([label, val]) => element("div", "stat", element("strong", "", numbers.format(val)), element("span", "", label))));

  const exportBar = element("div", "flex flex-wrap gap-2",
    button("Exportar resultado JSON", () => downloadBlobFile(
      JSON.stringify(report, null, 2), "application/json", "orca-verificacion-manifiesto.json"), "subtle"),
    button("Exportar informe HTML", async () => {
      try {
        await downloadManifestHtml(report);
      } catch (err) {
        message("manifest-ver-status", err.message, true);
      }
    }, "subtle"));

  const filter = element("select", "");
  filter.setAttribute("aria-label", "Filtrar estado de archivos");
  filter.append(new Option("Todos los estados (" + report.rows.length + ")", ""),
    ...Object.entries(manifestStatusMeta).map(([k, v]) => new Option(v.label + " (" + report.summary[k] + ")", k)));

  const tableHost = element("div", "scroll-table");
  const renderRows = () => {
    const filtered = report.rows.filter((r) => !filter.value || r.status === filter.value);
    if (!filtered.length) {
      tableHost.replaceChildren(element("p", "small muted table-empty", "Ningún archivo coincide con el filtro."));
      return;
    }
    const body = filtered.map((row) => {
      const meta = manifestStatusMeta[row.status] || {label: row.status, badge: ""};
      const badge = element("span", "status", meta.label);
      if (meta.badge) badge.dataset.status = meta.badge;
      return element("tr", "",
        element("td", "", element("code", "", row.path)),
        element("td", "", badge),
        element("td", "", row.expected_hash ? element("code", "hash-inline", row.expected_hash) : element("span", "muted", "—")),
        element("td", "", row.actual_hash ? element("code", "hash-inline", row.actual_hash) : element("span", "muted", "—")),
        element("td", "small muted", row.detail));
    });
    tableHost.replaceChildren(element("table", "",
      element("thead", "", element("tr", "",
        element("th", "", "Ruta"), element("th", "", "Estado"),
        element("th", "", "Hash esperado"), element("th", "", "Hash actual"), element("th", "", "Detalle"))),
      element("tbody", "", ...body)));
  };
  filter.addEventListener("change", renderRows);
  renderRows();

  const warnings = (report.warnings || []).map((w) => element("p", "notice small", w));
  container.replaceChildren(banner, stats, exportBar, ...warnings,
    element("div", "duel-filters", filter), tableHost);
}

function initManifests() {
  for (const btn of document.querySelectorAll("[data-manifest-tab]")) {
    btn.addEventListener("click", () => setManifestTab(btn.dataset.manifestTab));
  }

  $("manifest-gen-algo").addEventListener("change", updateManifestGenUI);
  $("manifest-pick-files").addEventListener("click", () => $("manifest-files-input").click());
  $("manifest-pick-folder").addEventListener("click", () => $("manifest-folder-input").click());
  $("manifest-files-input").addEventListener("change", () => {
    manifestState.genFiles = Array.from($("manifest-files-input").files);
    $("manifest-files-input").value = "";
    message("manifest-gen-status");
    updateManifestGenUI();
  });
  $("manifest-folder-input").addEventListener("change", () => {
    manifestState.genFiles = Array.from($("manifest-folder-input").files);
    $("manifest-folder-input").value = "";
    message("manifest-gen-status");
    updateManifestGenUI();
  });
  $("manifest-gen-clear").addEventListener("click", () => {
    manifestState.genFiles = [];
    manifestState.generatedManifest = null;
    $("manifest-gen-output").hidden = true;
    message("manifest-gen-status");
    updateManifestGenUI();
  });
  $("manifest-gen-run").addEventListener("click", runGenerateManifest);
  $("manifest-gen-cancel").addEventListener("click", () => manifestState.genController?.abort());

  $("manifest-import-btn").addEventListener("click", () => $("manifest-import-input").click());
  $("manifest-import-input").addEventListener("change", () => {
    const file = $("manifest-import-input").files[0];
    $("manifest-import-input").value = "";
    if (file) importManifestFile(file);
  });
  $("manifest-ver-files-btn").addEventListener("click", () => $("manifest-ver-files-input").click());
  $("manifest-ver-folder-btn").addEventListener("click", () => $("manifest-ver-folder-input").click());
  $("manifest-ver-files-input").addEventListener("change", () => {
    manifestState.verFiles = Array.from($("manifest-ver-files-input").files);
    $("manifest-ver-files-input").value = "";
    updateManifestVerUI();
  });
  $("manifest-ver-folder-input").addEventListener("change", () => {
    manifestState.verFiles = Array.from($("manifest-ver-folder-input").files);
    $("manifest-ver-folder-input").value = "";
    updateManifestVerUI();
  });
  $("manifest-ver-run").addEventListener("click", runVerifyManifest);
  $("manifest-ver-cancel").addEventListener("click", () => manifestState.verController?.abort());

  updateManifestGenUI();
  updateManifestVerUI();
}
