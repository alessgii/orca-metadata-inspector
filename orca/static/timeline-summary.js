"use strict";

const timelineLabels = {
  created: "Creación", modified: "Modificación", captured: "Captura", digitized: "Digitalización",
  metadata_date: "Edición de metadatos", printed: "Impresión", gps_date: "Fecha GPS",
  file_created: "Creación", file_modified: "Modificación", file_accessed: "Acceso", file_changed: "Cambio de atributos",
};

// Keep source precision down to the microsecond, while Date supplies the calendar-to-epoch conversion.
function timelineEpoch(utc) {
  const parts = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,6}))?\+00:00$/.exec(utc ?? "");
  if (!parts) return null;
  const whole = Date.parse(parts[1] + "Z");
  if (!Number.isFinite(whole)) return null;
  return BigInt(whole) * 1000n + BigInt((parts[2] ?? "").padEnd(6, "0"));
}

function timelinePoints(events, origin) {
  const byTime = new Map();
  for (const event of events) {
    if (event.origin !== origin) continue;
    const time = timelineEpoch(event.utc);
    if (time === null) continue;
    if (!byTime.has(time)) byTime.set(time, []);
    byTime.get(time).push(event);
  }
  const times = [...byTime.keys()].sort((a, b) => a < b ? -1 : a > b ? 1 : 0);
  if (!times.length) return [];
  const start = times[0];
  const duration = times.at(-1) - start;
  return times.map((time) => ({
    events: byTime.get(time),
    position: duration ? Number(time - start) / Number(duration) : 0.5,
  }));
}

function timelineSvg(name, attributes) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

const timelineDateFormat = new Intl.DateTimeFormat("es", {
  day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
});

function conciseUtc(point, withTime = false) {
  const [day, time] = point.events[0].utc.split("T");
  const date = timelineDateFormat.format(new Date(day + "T00:00:00Z"));
  return withTime ? date + " · " + time.replace("+00:00", "") + " UTC" : date;
}

function pointLabel(point) {
  const labels = [...new Set(point.events.map(({kind, label}) => timelineLabels[kind] ?? label))];
  // The selector identifies the instant, and its accompanying panel lists every record.
  return labels.length > 2 ? labels.slice(0, 2).join(" · ") + " y " + (labels.length - 2) + " más" : labels.join(" · ");
}

function timelineAxis(points, title) {
  const region = element("section", "min-w-0 pt-[7px] pb-1 border-t border-line first-of-type:border-t-0 space-y-2", element("h3", UI.h3, title));
  const chart = timelineSvg("svg", {"aria-hidden": "true", focusable: "false", class: "block w-full h-[52px] overflow-visible"});
  const relative = timelineSvg("svg", {width: "100%", height: "52", class: "w-full h-[52px] overflow-visible"});
  const position = (point) => (point.position * 100) + "%";
  if (points.length > 1) {
    relative.append(timelineSvg("line", {x1: "0%", x2: "100%", y1: 26, y2: 26, class: "stroke-accent stroke-[4] [stroke-linecap:round]"}));
  }
  const plot = element("div", "px-3");
  plot.append(chart);
  const marks = points.map((point, index) => timelineSvg("circle", {
    cx: position(point), cy: 26, r: index === 0 || index === points.length - 1 ? 7 : 4, class: "fill-accent stroke-surface stroke-[1.5]",
  }));
  const selected = timelineSvg("circle", {cx: position(points[0]), cy: 26, r: 8, class: "fill-surface stroke-accent stroke-[3]", hidden: ""});
  relative.append(...marks, selected);
  chart.append(relative);
  region.append(plot);

  const sameDay = points[0].events[0].utc.slice(0, 10) === points.at(-1).events[0].utc.slice(0, 10);
  const endpoint = (point, right = false) => element("div", "min-w-0 break-words" + (right ? " text-right" : ""),
    element("time", "block text-[0.95rem] font-semibold", conciseUtc(point, sameDay)), element("span", UI.smallMuted, pointLabel(point)));
  const ends = element("div", points.length > 1 ? "grid grid-cols-2 gap-x-3 gap-y-[7px] px-[5px]" : "grid grid-cols-1 text-center gap-x-3 gap-y-[7px] px-[5px]",
    endpoint(points[0]));
  if (points.length > 1) ends.append(endpoint(points.at(-1), true));
  region.append(ends);

  if (points.length === 1 && points[0].events.length === 1) {
    region.append(element("p", UI.smallMuted + " text-center", "Un solo instante registrado."));
    return region;
  }

  const readout = element("div", "border-s-2 border-tint-line py-[5px] px-2.5");
  readout.setAttribute("aria-live", "polite");
  const show = (index) => {
    const point = points[index];
    selected.setAttribute("cx", position(point));
    selected.removeAttribute("hidden");
    readout.replaceChildren(element("strong", "", conciseUtc(point, true)), element("ul", "mt-1",
      ...point.events.map((event) => element("li", "flex flex-wrap gap-x-[11px] gap-y-[3px] py-[3px] break-words text-[0.875rem]", event.label,
        element("code", UI.code, event.source.join(" / "))))));
  };
  if (points.length > 2 || points.some((point) => point.events.length > 1)) {
    if (points.length > 1) {
      const picker = element("select", UI.select + " min-w-0 max-w-full");
      for (const [index, point] of points.entries()) {
        const count = point.events.length;
        picker.append(new Option(conciseUtc(point, true) + " · " + pointLabel(point)
          + (count > 1 ? " (" + count + " registros)" : ""), index));
      }
      picker.addEventListener("change", () => show(Number(picker.value)));
      region.append(element("label", "flex flex-wrap items-center gap-x-2.5 gap-y-1.5", element("span", UI.small, "Explorar eventos"), picker));
    } else {
      region.append(element("p", UI.smallMuted, "Los eventos comparten el mismo instante."));
    }
    show(0);
    region.append(readout);
  }
  return region;
}

function summaryTimeline(timeline) {
  const panel = element("div", "mt-3.5 space-y-3");
  if (!Object.values(timeline.groups).some((events) => events.length)) {
    panel.append(element("p", "py-2.5 text-muted", "No se encontraron fechas en los campos examinados."));
    return panel;
  }
  if (timeline.interval) {
    panel.append(element("div", "pt-2.5 pb-3 border-b border-line",
      element("p", UI.smallMuted, "Tiempo entre creación y modificación"),
      element("strong", "block text-[clamp(1.4rem,2.8vw,1.95rem)] tracking-[-0.03em] font-bold", intervalText(timeline.interval.seconds)),
      element("p", UI.smallMuted, "Según las fechas registradas del documento")));
  } else {
    panel.append(element("p", UI.smallMuted, "El intervalo entre creación y modificación no está disponible."));
  }
  for (const warning of timeline.warnings) panel.append(element("p", UI.notice, warning));

  const internal = timelinePoints(timeline.groups.instant, "internal");
  const filesystem = timelinePoints(timeline.groups.instant, "filesystem");
  if (internal.length || filesystem.length) {
    if (internal.length) panel.append(timelineAxis(internal, "Metadatos del documento"));
    if (filesystem.length) panel.append(timelineAxis(filesystem, "Sistema de archivos"));
  } else {
    panel.append(element("p", "py-2.5 text-muted", "Las fechas disponibles se muestran sin escala proporcional."));
  }
  const unplaced = {local: "Horas sin instante UTC", partial: "Fechas de calendario", invalid: "Valores sin interpretar"};
  for (const [group, label] of Object.entries(unplaced)) {
    const events = timeline.groups[group];
    if (!events.length) continue;
    const list = element("ul", "mt-1");
    for (const event of events) {
      list.append(element("li", "flex flex-wrap gap-x-[11px] gap-y-[3px] py-[3px] break-words text-[0.875rem]",
        element("strong", "", event.label),
        element("span", UI.muted, group === "invalid" ? recordedValue(event.original) : dateText(event)),
        element("span", UI.muted, event.origin === "filesystem" ? "Sistema de archivos" : "Metadato interno")));
    }
    const details = element("details", UI.details, element("summary", UI.summary + " text-[0.875rem]", label + " (" + events.length + ")"), list);
    details.open = !internal.length && !filesystem.length && group !== "invalid";
    panel.append(details);
  }
  return panel;
}

function timelineView(view) {
  if (view.timelineUI) return view.timelineUI;
  const timeline = view.context.timeline;
  const modes = element("fieldset", "inline-flex gap-0.5 m-0 p-[3px] border border-line rounded-lg bg-wash", element("legend", "sr-only", "Vista de cronología"));
  const panels = {
    summary: summaryTimeline(timeline),
    advanced: advancedTimeline(timeline),
  };
  for (const [key, label] of [["summary", "Resumida"], ["advanced", "Avanzada"]]) {
    const radio = element("input", "peer sr-only");
    radio.type = "radio";
    radio.name = "timeline-mode";
    radio.checked = key === "summary";
    radio.value = key;
    radio.setAttribute("aria-controls", "timeline-" + key);
    panels[key].id = "timeline-" + key;
    panels[key].hidden = !radio.checked;
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      for (const [mode, panel] of Object.entries(panels)) panel.hidden = mode !== key;
    });
    modes.append(element("label", "relative cursor-pointer", radio,
      element("span", "block text-muted py-[5px] px-[11px] border border-transparent rounded-[5px] text-[0.85rem] font-semibold whitespace-nowrap peer-checked:text-accent peer-checked:bg-surface peer-checked:border-tint-line peer-focus-visible:outline-2 peer-focus-visible:outline-accent peer-focus-visible:outline-offset-2", label)));
  }
  const heading = element("h2", UI.h2, "Cronología del documento");
  view.timelineUI = element("section", "space-y-3",
    element("div", "flex items-center justify-between flex-wrap gap-x-[15px] gap-y-[9px]", heading, modes), panels.summary, panels.advanced);
  return view.timelineUI;
}
