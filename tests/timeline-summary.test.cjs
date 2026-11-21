const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {test} = require("node:test");
const {runInNewContext} = require("node:vm");

const source = readFileSync(join(__dirname, "../orca/static/timeline-summary.js"), "utf8");
const {timelinePoints} = runInNewContext(source + "\n({timelinePoints})");
const event = (utc, label = "Metadato", origin = "internal") => ({utc, label, origin});

test("positions UTC instants in proportion to the elapsed interval", () => {
  const points = timelinePoints([
    event("2024-01-11T00:00:00+00:00"),
    event("2024-01-03T00:00:00+00:00"),
    event("2024-01-01T00:00:00+00:00"),
  ], "internal");
  assert.deepEqual(Array.from(points, (point) => point.position), [0, 0.2, 1]);
});

test("groups simultaneous records and keeps microseconds distinct", () => {
  const points = timelinePoints([
    event("2024-01-01T00:00:00+00:00", "Creación"),
    event("2024-01-01T00:00:00.000000+00:00", "Modificación"),
    event("2024-01-01T00:00:00.000001+00:00"),
    event("2024-01-01T00:00:00.000004+00:00"),
  ], "internal");
  assert.deepEqual(Array.from(points, (point) => point.position), [0, 0.25, 1]);
  assert.deepEqual(Array.from(points[0].events, (item) => item.label), ["Creación", "Modificación"]);
});

test("keeps system dates separate and leaves dates without UTC outside the axis", () => {
  const events = [
    event("2024-01-11T00:00:00+00:00", "Sistema", "filesystem"),
    event(null, "Sin zona"),
    event("2024-01-01T00:00:00+00:00", "Documento"),
  ];
  assert.equal(timelinePoints(events, "internal")[0].position, 0.5);
  assert.equal(timelinePoints(events, "filesystem")[0].events[0].label, "Sistema");
  assert.equal(timelinePoints([event(null)], "internal").length, 0);
});
