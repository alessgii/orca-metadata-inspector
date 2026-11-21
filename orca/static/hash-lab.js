"use strict";

const hashAlgoMeta = {
  "SHA-256": {length: 64, legacy: false},
  "SHA-512": {length: 128, legacy: false},
  "SHA-1": {length: 40, legacy: true, warning: "SHA-1 es un algoritmo heredado vulnerable a colisiones. Prefiere SHA-256 o SHA-512."},
  "MD5": {length: 32, legacy: true, warning: "MD5 es un algoritmo heredado vulnerable a colisiones. No debe usarse como garantía criptográfica moderna."},
};

const hashLabState = {
  tab: "calc",
  file: null,
  fileKey: "",
  cache: new Map(), // algorithm -> {algorithm, hash, hex_length, legacy, warning, savedId}
  controller: null,
  historyVersion: 0,
  verifyingRecord: null,
};

function setHashTab(tab) {
  hashLabState.tab = ["calc", "compare", "history"].includes(tab) ? tab : "calc";
  for (const btn of document.querySelectorAll("[data-hash-tab]")) {
    btn.setAttribute("aria-selected", String(btn.dataset.hashTab === hashLabState.tab));
  }
  for (const name of ["calc", "compare", "history"]) {
    $("hash-tab-" + name).hidden = name !== hashLabState.tab;
  }
  if (hashLabState.tab === "history") loadHashHistory();
}

function selectedHashAlgorithms() {
  return Array.from(document.querySelectorAll('input[name="hash-algo"]:checked'), (el) => el.value);
}

function updateCalcLegacyWarning() {
  const legacy = selectedHashAlgorithms().filter((algo) => hashAlgoMeta[algo]?.legacy);
  const box = $("hash-legacy-warning");
  if (!legacy.length) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.dataset.error = "false";
  box.textContent = legacy.join(" y ") + " son algoritmos heredados con debilidades conocidas frente a colisiones. No deben usarse como garantía criptográfica moderna.";
}

function updateCalcControls() {
  const running = Boolean(hashLabState.controller);
  const algos = selectedHashAlgorithms();
  $("hash-run").disabled = running || !hashLabState.file || !algos.length;
  $("hash-cancel").hidden = !running;
  $("hash-pick").disabled = running;
  for (const el of document.querySelectorAll('input[name="hash-algo"]')) el.disabled = running;
  $("hash-auto-save").disabled = running;
  updateCalcLegacyWarning();
}

function selectHashFile(file) {
  if (!file || hashLabState.controller) return;
  const key = [file.name, file.size, file.lastModified].join(":");
  if (key !== hashLabState.fileKey) {
    hashLabState.cache.clear();
    hashLabState.fileKey = key;
  }
  hashLabState.file = file;
  const meta = $("hash-file-meta");
  meta.hidden = false;
  meta.replaceChildren(
    element("div", "flex items-center gap-2 min-w-0", icon("file"),
      element("strong", "min-w-0 break-all", file.name),
      element("span", "text-[0.875rem] text-muted font-mono whitespace-nowrap", formatBytes(file.size) + " (" + numbers.format(file.size) + " B)")),
    button("Quitar archivo", () => {
      hashLabState.file = null;
      hashLabState.fileKey = "";
      hashLabState.cache.clear();
      meta.hidden = true;
      $("hash-calc-results").replaceChildren();
      message("hash-calc-status");
      updateCalcControls();
    }, "quiet"),
  );
  message("hash-calc-status");
  renderHashCalcResults();
  updateCalcControls();
}

async function streamFileHash(file, algorithms, {save = false, signal = undefined} = {}) {
  const params = new URLSearchParams({algorithms: algorithms.join(",")});
  if (save) params.set("save", "1");
  const response = await fetch("/api/hashes/compute?" + params, {
    method: "POST",
    headers: {
      "Content-Type": "application/octet-stream",
      "X-Orca-Filename": encodeURIComponent(file.name || "archivo"),
    },
    body: file,
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(typeof err.detail === "string" ? err.detail : "No se pudo calcular el hash del archivo.");
  }
  return response.json();
}

async function runHashCalculation() {
  if (!hashLabState.file || hashLabState.controller) return;
  const selected = selectedHashAlgorithms();
  if (!selected.length) {
    message("hash-calc-status", "Selecciona al menos un algoritmo.", true);
    return;
  }
  const autoSave = $("hash-auto-save").checked;
  const missing = selected.filter((algo) => !hashLabState.cache.has(algo));

  if (!missing.length) {
    if (autoSave) {
      for (const algo of selected) {
        const entry = hashLabState.cache.get(algo);
        if (entry && !entry.savedId) {
          try {
            const saved = await api("/api/hashes/history", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({name: hashLabState.file.name, size: hashLabState.file.size, algorithm: entry.algorithm, hash: entry.hash}),
            });
            entry.savedId = saved.id;
          } catch (err) {
            message("hash-calc-status", err.message, true);
          }
        }
      }
    }
    message("hash-calc-status", "Resultados mostrados desde el cálculo actual del archivo.");
    renderHashCalcResults();
    return;
  }

  const controller = new AbortController();
  hashLabState.controller = controller;
  updateCalcControls();
  message("hash-calc-status", "Calculando " + missing.join(", ") + " en bloques locales…");

  try {
    const data = await streamFileHash(hashLabState.file, missing, {save: autoSave, signal: controller.signal});
    const savedByAlgo = new Map((data.saved_records || []).map((rec) => [rec.algorithm, rec.id]));
    for (const item of data.results) {
      hashLabState.cache.set(item.algorithm, {...item, savedId: savedByAlgo.get(item.algorithm) || null});
    }
    message("hash-calc-status", autoSave
      ? "Cálculo completado y guardado en el historial local."
      : "Cálculo completado correctamente.");
    renderHashCalcResults();
  } catch (error) {
    if (error.name === "AbortError") {
      message("hash-calc-status", "Cálculo cancelado por el usuario.");
    } else {
      message("hash-calc-status", error.message || "Error al leer o procesar el archivo.", true);
    }
  } finally {
    hashLabState.controller = null;
    updateCalcControls();
  }
}

function renderHashCalcResults() {
  const container = $("hash-calc-results");
  if (!hashLabState.file) {
    container.replaceChildren();
    return;
  }
  const items = selectedHashAlgorithms()
    .map((algo) => hashLabState.cache.get(algo))
    .filter(Boolean);
  if (!items.length) {
    container.replaceChildren();
    return;
  }
  const cards = items.map((item) => {
    const feedback = element("span", UI.copyFeedback);
    feedback.setAttribute("role", "status");
    const holder = element("div", UI.cardRow);
    const badge = element("span", UI.status, item.algorithm);
    if (item.legacy) badge.dataset.status = "unsupported";
    else badge.dataset.status = "ok";

    const copyBtn = button("Copiar hash", () => copyText(item.hash, holder, feedback, item.algorithm + " copiado."), "subtle");
    const compareBtn = button("Usar en Comparador", () => {
      $("compare-algo").value = item.algorithm;
      $("compare-hash-a").value = item.hash;
      updateCompareLegacyWarning();
      setHashTab("compare");
      $("compare-hash-b").focus();
    }, "quiet");

    const saveBtn = button(item.savedId ? "Guardado" : "Guardar en historial", async () => {
      if (item.savedId) return;
      saveBtn.disabled = true;
      try {
        const saved = await api("/api/hashes/history", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            name: hashLabState.file.name,
            size: hashLabState.file.size,
            algorithm: item.algorithm,
            hash: item.hash,
          }),
        });
        item.savedId = saved.id;
        saveBtn.textContent = "Guardado";
        feedback.textContent = "Registro guardado en el historial.";
      } catch (err) {
        saveBtn.disabled = false;
        feedback.textContent = err.message;
      }
    }, "subtle");
    if (item.savedId) saveBtn.disabled = true;

    const head = element("div", UI.cardTop,
      element("div", "flex items-center gap-2 flex-wrap", badge,
        element("span", UI.smallMuted, hashLabState.file.name + " · " + formatBytes(hashLabState.file.size))),
      element("div", "flex items-center gap-2 flex-wrap", copyBtn, saveBtn, compareBtn));

    holder.append(head, element("code", UI.hashCode, item.hash));
    if (item.warning) holder.append(element("p", UI.smallMuted, item.warning));
    holder.append(feedback);
    return holder;
  });
  container.replaceChildren(...cards);
}

function updateCompareLegacyWarning() {
  const algo = $("compare-algo").value;
  const info = hashAlgoMeta[algo];
  const box = $("compare-legacy-warning");
  if (info?.legacy) {
    box.hidden = false;
    box.dataset.error = "false";
    box.textContent = info.warning;
  } else {
    box.hidden = true;
    box.textContent = "";
  }
}

function validateCompareInput(raw, algo) {
  const expected = hashAlgoMeta[algo]?.length ?? 64;
  const cleaned = raw.trim();
  if (!cleaned) return "Introduce un hash hexadecimal (" + expected + " caracteres para " + algo + ").";
  if (!/^[0-9a-fA-F]+$/.test(cleaned)) return "Solo se admiten caracteres hexadecimales (0–9, A–F).";
  if (cleaned.length !== expected) {
    return "Longitud inválida para " + algo + ": se esperan " + expected + " caracteres (actual: " + cleaned.length + ").";
  }
  return "";
}

async function runHashComparison() {
  const algorithm = $("compare-algo").value;
  const hashA = $("compare-hash-a").value;
  const hashB = $("compare-hash-b").value;
  const errA = validateCompareInput(hashA, algorithm);
  const errB = validateCompareInput(hashB, algorithm);
  $("compare-err-a").textContent = errA;
  $("compare-err-a").hidden = !errA;
  $("compare-err-b").textContent = errB;
  $("compare-err-b").hidden = !errB;

  const box = $("compare-result");
  if (errA || errB) {
    box.hidden = true;
    return;
  }
  try {
    const res = await api("/api/hashes/compare", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({algorithm, hash_a: hashA, hash_b: hashB}),
    });
    box.hidden = false;
    if (!res.valid) {
      box.dataset.match = "invalid";
      box.replaceChildren(element("strong", "", res.message));
      return;
    }
    box.dataset.match = res.match ? "yes" : "no";
    box.replaceChildren(
      element("strong", "", res.message),
      element("p", UI.smallMuted, "Algoritmo: " + res.algorithm + " · Longitud verificada: " + res.expected_length + " caracteres hexadecimales."),
      element("p", UI.smallMuted, res.disclaimer),
    );
  } catch (error) {
    box.hidden = false;
    box.dataset.match = "invalid";
    box.replaceChildren(element("strong", "", error.message));
  }
}

function openHashComparator(hashA, hashB, algorithm = "SHA-256") {
  $("compare-algo").value = hashAlgoMeta[algorithm] ? algorithm : "SHA-256";
  $("compare-hash-a").value = hashA || "";
  $("compare-hash-b").value = hashB || "";
  updateCompareLegacyWarning();
  location.hash = "hashes/compare";
  setHashTab("compare");
  if (hashA && hashB) runHashComparison();
}

function updateHashExportLinks() {
  const params = new URLSearchParams();
  const q = $("hash-history-search").value.trim();
  const algo = $("hash-history-algo").value;
  if (q) params.set("q", q);
  if (algo) params.set("algorithm", algo);
  const suffix = params.toString() ? "?" + params.toString() : "";
  const container = $("hash-history-exports");
  container.replaceChildren(
    link("JSON", "/api/hashes/history/export/json" + suffix),
    link("CSV", "/api/hashes/history/export/csv" + suffix),
  );
}

async function loadHashHistory() {
  const version = ++hashLabState.historyVersion;
  updateHashExportLinks();
  const params = new URLSearchParams();
  const q = $("hash-history-search").value.trim();
  const algo = $("hash-history-algo").value;
  if (q) params.set("q", q);
  if (algo) params.set("algorithm", algo);
  const query = params.toString() ? "?" + params.toString() : "";
  message("hash-history-status", "Cargando historial de hashes…");
  try {
    const records = await api("/api/hashes/history" + query);
    if (version !== hashLabState.historyVersion) return;
    message("hash-history-status");
    $("hash-history-clear").disabled = records.length === 0 && !q && !algo;
    const list = $("hash-history-list");
    if (!records.length) {
      list.replaceChildren(element("p", UI.tableEmpty,
        q || algo ? "Ningún registro coincide con los filtros actuales." : "Todavía no hay registros de hashes guardados."));
      return;
    }
    const rows = records.map((rec) => {
      const feedback = element("p", UI.copyFeedback);
      feedback.setAttribute("role", "status");
      const card = element("article", UI.cardRow);
      const badge = element("span", UI.status, rec.algorithm);
      if (hashAlgoMeta[rec.algorithm]?.legacy) badge.dataset.status = "unsupported";

      const copyBtn = button("Copiar", () => copyText(rec.hash, card, feedback, "Hash copiado."), "subtle");
      const verifyBtn = button("Verificar nuevamente", () => {
        hashLabState.verifyingRecord = rec;
        $("hash-verify-picker").value = "";
        $("hash-verify-picker").click();
      }, "accent-button");
      const compareBtn = button("Comparar", () => openHashComparator(rec.hash, "", rec.algorithm), "quiet");
      const delBtn = button("Eliminar", async () => {
        if (!await confirmDelete("Se eliminará este registro del historial local de hashes.")) return;
        try {
          await api("/api/hashes/history/" + encodeURIComponent(rec.id), {method: "DELETE"});
          loadHashHistory();
        } catch (err) {
          message("hash-history-status", err.message, true);
        }
      }, "quiet");

      card.append(
        element("div", UI.cardTop,
          element("div", "min-w-0",
            element("div", "flex items-center gap-2 flex-wrap",
              element("strong", "break-all", rec.name), badge),
            element("p", UI.smallMuted,
              formatBytes(rec.size) + " (" + numbers.format(rec.size) + " B) · " + timestamp(rec.created) + " · ID " + rec.id.slice(0, 8))),
          element("div", "flex flex-wrap gap-2", verifyBtn, copyBtn, compareBtn, delBtn)),
        element("code", UI.hashCode, rec.hash),
        feedback,
      );
      return card;
    });
    list.replaceChildren(...rows);
  } catch (error) {
    if (version === hashLabState.historyVersion) message("hash-history-status", error.message, true);
  }
}

async function handleVerifyAgainstHistory(file) {
  const rec = hashLabState.verifyingRecord;
  if (!rec || !file) return;
  const banner = $("hash-verify-banner");
  banner.hidden = false;
  banner.dataset.state = "loading";
  banner.replaceChildren(element("p", UI.small, "Verificando «" + file.name + "» contra el registro " + rec.id.slice(0, 8) + " (" + rec.algorithm + ")…"));
  try {
    const response = await fetch("/api/hashes/history/" + encodeURIComponent(rec.id) + "/verify", {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Orca-Filename": encodeURIComponent(file.name || "archivo"),
      },
      body: file,
      cache: "no-store",
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : "No fue posible realizar la verificación.");
    }
    const res = await response.json();
    banner.dataset.state = res.status;
    const closeBtn = button("Cerrar aviso", () => { banner.hidden = true; }, "quiet");
    banner.replaceChildren(
      element("div", "flex items-center justify-between gap-2 flex-wrap",
        element("strong", "", res.message), closeBtn),
      element("p", UI.smallMuted,
        "Referencia: " + res.record.name + " (" + formatBytes(res.record.size) + ") · Seleccionado: " + res.actual.name + " (" + formatBytes(res.actual.size) + ")"),
      element("p", "text-[0.875rem] text-muted font-mono break-all", "Esperado (" + res.record.algorithm + "): " + res.record.hash),
      element("p", "text-[0.875rem] text-muted font-mono break-all", "Calculado (" + res.actual.algorithm + "): " + res.actual.hash),
      element("p", UI.smallMuted, res.disclaimer),
    );
  } catch (error) {
    banner.dataset.state = "error";
    banner.replaceChildren(
      element("strong", "", "No fue posible realizar la verificación."),
      element("p", UI.small, error.message),
    );
  } finally {
    hashLabState.verifyingRecord = null;
  }
}

function initHashLab() {
  for (const btn of document.querySelectorAll("[data-hash-tab]")) {
    btn.addEventListener("click", () => setHashTab(btn.dataset.hashTab));
  }

  $("hash-pick").addEventListener("click", () => $("hash-picker").click());
  $("hash-picker").addEventListener("change", () => {
    if ($("hash-picker").files[0]) selectHashFile($("hash-picker").files[0]);
    $("hash-picker").value = "";
  });

  for (const eventName of ["dragenter", "dragover", "dragleave", "drop"]) {
    $("hash-drop").addEventListener(eventName, (event) => {
      if (!event.dataTransfer.types.includes("Files")) return;
      event.preventDefault();
      if (hashLabState.controller) return;
      const leaving = eventName === "dragleave" && !$("hash-drop").contains(event.relatedTarget);
      if (eventName === "drop" || leaving) $("hash-drop").classList.remove("dragging");
      else $("hash-drop").classList.add("dragging");
      if (eventName === "drop" && event.dataTransfer.files[0]) selectHashFile(event.dataTransfer.files[0]);
    });
  }

  for (const el of document.querySelectorAll('input[name="hash-algo"]')) {
    el.addEventListener("change", () => {
      updateCalcControls();
      renderHashCalcResults();
    });
  }

  $("hash-run").addEventListener("click", runHashCalculation);
  $("hash-cancel").addEventListener("click", () => hashLabState.controller?.abort());

  $("compare-algo").addEventListener("change", () => {
    updateCompareLegacyWarning();
    if ($("compare-hash-a").value.trim() && $("compare-hash-b").value.trim()) runHashComparison();
  });
  $("compare-swap").addEventListener("click", () => {
    const prevA = $("compare-hash-a").value;
    $("compare-hash-a").value = $("compare-hash-b").value;
    $("compare-hash-b").value = prevA;
    if ($("compare-hash-a").value.trim() && $("compare-hash-b").value.trim()) runHashComparison();
  });
  $("compare-clear").addEventListener("click", () => {
    $("compare-hash-a").value = "";
    $("compare-hash-b").value = "";
    $("compare-err-a").hidden = true;
    $("compare-err-b").hidden = true;
    $("compare-result").hidden = true;
    $("compare-copy-status").textContent = "";
  });
  $("compare-copy-a").addEventListener("click", () => {
    const val = $("compare-hash-a").value.trim();
    if (val) copyText(val, $("hash-tab-compare"), $("compare-copy-status"), "Hash A copiado.");
  });
  $("compare-copy-b").addEventListener("click", () => {
    const val = $("compare-hash-b").value.trim();
    if (val) copyText(val, $("hash-tab-compare"), $("compare-copy-status"), "Hash B copiado.");
  });
  $("compare-run").addEventListener("click", runHashComparison);

  let searchTimer = null;
  $("hash-history-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadHashHistory, 180);
  });
  $("hash-history-algo").addEventListener("change", loadHashHistory);
  $("hash-history-clear").addEventListener("click", async () => {
    if (!await confirmDelete("Se eliminarán todos los registros del historial de hashes en SQLite.")) return;
    try {
      await api("/api/hashes/history", {method: "DELETE"});
      $("hash-verify-banner").hidden = true;
      loadHashHistory();
    } catch (err) {
      message("hash-history-status", err.message, true);
    }
  });
  $("hash-verify-picker").addEventListener("change", () => {
    const file = $("hash-verify-picker").files[0];
    $("hash-verify-picker").value = "";
    if (file) handleVerifyAgainstHistory(file);
  });

  updateCalcControls();
  updateCompareLegacyWarning();
}
