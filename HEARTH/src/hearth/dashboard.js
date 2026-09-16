"use strict";
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
let lastReport = null;
function svgNode(name, attributes, text) {
  const element = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}
function render(report) {
  lastReport = report;
  const samples = report.samples;
  if (!Array.isArray(samples) || !samples.length) throw new Error("Empty simulation report");
  const final = samples[samples.length - 1];
  $("final-state").textContent = final.state.toUpperCase();
  $("final-state").className = `state-${final.state}`;
  $("health").textContent = `Sensor health: ${final.health}`;
  $("lifetime").textContent = `${report.constant_temperature_quality_lifetime_h.toFixed(2)} h`;
  $("ratio").textContent = `${report.cooling_lifetime_ratio_25_vs_30.toFixed(2)}×`;
  $("ledger").textContent = report.ledger.valid ? "Verified" : "FAILED";
  $("event-count").textContent = `${report.ledger.checked} local events; no external anchor`;
  const plot = $("plot"); plot.replaceChildren();
  const width = Math.max(220, Math.min(960, plot.getBoundingClientRect().width));
  const compact = width < 500;
  const height = compact ? 240 : 290;
  plot.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const left = 42, right = width - 12, bottom = height - 36;
  const x = (time) => left + (right - left) * time / report.request.hours;
  const y = (quality) => bottom - (bottom - 24) * quality;
  for (const q of [0, 0.25, 0.5, 0.75, 1]) {
    plot.append(svgNode("line", {x1:left, x2:right, y1:y(q), y2:y(q), class:"grid"}));
    plot.append(svgNode("text", {x:6, y:y(q)+5}, q.toFixed(2)));
  }
  const ticks = compact ? 3 : 6;
  for (let i = 0; i <= ticks; i++) {
    const time = report.request.hours * i / ticks;
    plot.append(svgNode("text", {x:x(time), y:height-8, "text-anchor":i === ticks ? "end" : "middle"}, `${time.toFixed(0)} h`));
  }
  for (const [field, className] of [["synthetic_truth_quality", "curve truth"], ["quality_index", "curve"]]) {
    const path = samples.map((s,i) => `${i ? "L" : "M"}${x(s.elapsed_h).toFixed(2)},${y(s[field]).toFixed(2)}`).join(" ");
    plot.append(svgNode("path", {d:path, class:className}));
  }
  $("transitions").replaceChildren(); let previous = "";
  for (const sample of samples) {
    const signature = `${sample.state}/${sample.health}/${sample.reasons.join(",")}`;
    if (signature === previous) continue; previous = signature;
    const row = document.createElement("div"); row.className = "transition";
    const hour = document.createElement("span"); hour.textContent = `${sample.elapsed_h.toFixed(2)} h`;
    const state = document.createElement("span"); state.className = `state-label state-${sample.state}`; state.textContent = sample.state;
    const reason = document.createElement("span"); reason.textContent = `${sample.health} · ${sample.reasons.join(", ").replaceAll("_", " ")}`;
    row.append(hour, state, reason); $("transitions").append(row);
  }
}
$("run").addEventListener("click", async () => {
  const key = $("api-key").value;
  if (!key) { $("message").textContent = "Enter the local operator key first."; return; }
  $("run").disabled = true; $("message").textContent = "Running a synthetic edge experiment…";
  try {
    const response = await fetch("/v1/demo/run", {method:"POST", headers:{"Content-Type":"application/json", "X-API-Key":key},
      body:JSON.stringify({scenario:$("scenario").value, hours:24, interval_minutes:10, seed:20260916})});
    if (!response.ok) throw new Error(`Request failed (${response.status}). Check your key and server mode.`);
    const report = await response.json(); render(report);
    $("message").textContent = `Completed ${report.samples.length} synthetic time points. No live products or external services were changed.`;
  } catch (error) {
    $("message").textContent = error.message;
  } finally { $("run").disabled = false; }
});

window.addEventListener("resize", () => { if (lastReport) render(lastReport); });
