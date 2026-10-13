// Trust Console — firehose, baseline, calibration, benchmark and ROI.
const pct = (v, d = 1) => (v == null ? "—" : (v * 100).toFixed(d) + "%");
const kpi = (v, l, c = "") => `<div class="kpi ${c}"><div class="v">${v}</div><div class="l">${l}</div></div>`;

// ------------------------------------------------------------------ firehose (SSE over fetch)
async function runFirehose() {
  const btn = $("#fh-run"); btn.disabled = true;
  const body = { count: Number($("#fh-count").value), lane: $("#fh-lane").value || null };
  const tbody = $("#fh-table tbody"); tbody.innerHTML = "";
  const s = { done: 0, total: body.count, decisions: 0, calls: 0, cost: 0, match: 0, auto: 0, lat: [], t0: performance.now() };
  const paint = (final) => {
    const lat = [...s.lat].sort((a, b) => a - b);
    const p50 = lat.length ? lat[Math.floor(lat.length / 2)] : 0;
    $("#fh-kpis").innerHTML = kpi(`${s.done}/${s.total}`, "Events triaged") + kpi(s.decisions.toLocaleString(), `Microsoft-Decision-1 decisions (${s.calls} calls)`, "d1") +
      kpi(fmtMs(p50), "P50 per call", "d1") + kpi(fmtUsd(s.cost, 5), "Microsoft-Decision-1 cost", "d1") + kpi(pct(s.done ? s.auto / s.done : 0), "Automated in customer's favour", "ok") +
      kpi(pct(s.done ? s.match / s.done : 0), "Matches labels") + kpi(fmtMs(final?.wall_s ? final.wall_s * 1000 : performance.now() - s.t0), "Wall clock");
  };
  paint();
  try {
    const r = await fetch("/api/firehose", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.type === "start") { s.total = ev.total; paint(); }
        if (ev.type === "row") {
          s.done++; s.decisions += ev.decisions; s.calls += ev.calls; s.cost += ev.cost_usd; s.match += ev.match ? 1 : 0; s.auto += ev.automatic ? 1 : 0; s.lat.push(...ev.latencies);
          tbody.insertAdjacentHTML("afterbegin", `<tr class="new"><td><b>${esc(ev.id)}</b> ${esc(ev.title)}<div class="hint">${esc(ev.reason)}</div></td><td>${laneTag(ev.lane)}</td>
            <td>${pill(ev.disposition)}</td><td class="hint">${esc(ev.expected.join(" / "))}</td><td>${ev.match ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>'}</td>
            <td>${ev.decisions} <span class="hint">/ ${ev.calls} calls</span></td><td>${fmtMs(ev.critical_path_ms)}</td></tr>`);
          paint();
        }
        if (ev.type === "summary") paint(ev);
      }
    }
  } catch (e) { $("#fh-kpis").insertAdjacentHTML("beforeend", `<div class="bad">${esc(e.message)}</div>`); }
  btn.disabled = false;
}
$("#fh-run").addEventListener("click", runFirehose);

async function renderBaseline() {
  const b = await api("/api/baseline");
  if (!b || !b.events) { $("#baseline").innerHTML = `<p class="muted">Run <code>python tools/evaluate.py</code> to produce the baseline.</p>`; return; }
  state.baseline = b;
  const cfg = await api("/api/config");
  const deployments = cfg.decision_deployments.split(",").length;
  $("#fh-quota").textContent = `This demo is limited to ${deployments} × ${cfg.rpm_per_deployment} requests/minute of Microsoft-Decision-1 quota (${cfg.decision_deployments}). Throughput scales linearly with quota; per-call latency does not change.`;
  $("#baseline").innerHTML = `
    <div class="kpis">${kpi(pct(b.disposition_accuracy), "Disposition accuracy (120 labelled events)", "ok")}${kpi(b.total_decisions.toLocaleString(), `Decisions in ${b.total_calls} calls`, "d1")}
      ${kpi(fmtUsd(b.total_cost_usd, 4), "Total Microsoft-Decision-1 cost", "d1")}${kpi(fmtMs(b.call_latency_ms.p50), "P50 per call", "d1")}${kpi(fmtMs(b.call_latency_ms.p95), "P95 per call", "d1")}
      ${kpi(pct(b.automation_rate), "Automated in customer's favour", "ok")}</div>
    <div class="grid2">
      <div class="card"><h4>Disposition accuracy by lane</h4><table>${Object.entries(b.per_lane_disposition_accuracy).map(([k, v]) => `<tr><td>${laneTag(k)}</td><td><b>${pct(v)}</b></td></tr>`).join("")}</table></div>
      <div class="card"><h4>Accuracy of individual Microsoft-Decision-1 answers</h4><table>${Object.entries(b.decision_accuracy).map(([k, v]) => `<tr><td>${esc(k)}</td><td><b>${pct(v.accuracy)}</b></td><td class="hint">n=${v.n}</td></tr>`).join("")}</table></div>
    </div>
    <p class="hint">Synthetic, labelled demo data. The confidence policy was tuned on this set — validate on your own history with <b>Calibrate on your data</b>.</p>`;
}

// ------------------------------------------------------------------ calibration
let sampleBlob = null;
$("#cal-sample").addEventListener("click", async () => {
  const text = await (await fetch("/api/calibrate/sample")).text();
  sampleBlob = new Blob([text], { type: "text/csv" });
  $("#cal-sample").textContent = "Sample loaded ✓ (30 rows)";
});
$("#cal-run").addEventListener("click", async () => {
  const file = $("#cal-file").files[0] || sampleBlob;
  if (!file) { $("#cal-result").innerHTML = `<p class="bad">Choose a CSV or load the sample first.</p>`; return; }
  const fd = new FormData();
  fd.append("file", file, file.name || "sample.csv");
  fd.append("instructions", $("#cal-instructions").value);
  fd.append("max_rows", $("#cal-max").value);
  fd.append("shuffle_check", "12");
  $("#cal-result").innerHTML = `<div class="loading"><span class="spinner"></span>&nbsp; Microsoft-Decision-1 is labelling every row and re-asking a sample with the options reversed…</div>`;
  $("#cal-run").disabled = true;
  try {
    const r = await fetch("/api/calibrate", { method: "POST", body: fd });
    if (!r.ok) throw new Error(await r.text());
    renderCalibration(await r.json());
  } catch (e) { $("#cal-result").innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
  $("#cal-run").disabled = false;
});

function svgChart(series, { yLabel = "", xLabel = "", diagonal = false, bars = null } = {}) {
  const W = 520, H = 250, L = 44, B = 32, T = 10, R = 12, w = W - L - R, h = H - T - B;
  const x = (v) => L + v * w, y = (v) => T + (1 - v) * h;
  let g = "";
  for (let i = 0; i <= 5; i++) { const v = i / 5; g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#e5e7eb"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${(v * 100).toFixed(0)}%</text><text x="${x(v)}" y="${H - 12}" text-anchor="middle">${v.toFixed(1)}</text>`; }
  if (diagonal) g += `<line x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(1)}" stroke="#94a3b8" stroke-dasharray="5 4"/>`;
  if (bars) bars.forEach((b) => { g += `<rect x="${x(b.lo) + 2}" y="${y(b.v)}" width="${x(b.hi) - x(b.lo) - 4}" height="${y(0) - y(b.v)}" fill="#c4b5fd"><title>${b.title}</title></rect>`; });
  series.forEach((s) => { g += `<polyline fill="none" stroke="${s.color}" stroke-width="2.5" points="${s.points.map(([a, b]) => `${x(a)},${y(b)}`).join(" ")}"/>` + s.points.map(([a, b]) => `<circle cx="${x(a)}" cy="${y(b)}" r="3" fill="${s.color}"/>`).join(""); });
  const legend = series.map((s, i) => `<rect x="${L + 10 + i * 150}" y="${T + 4}" width="12" height="4" fill="${s.color}"/><text x="${L + 26 + i * 150}" y="${T + 10}">${s.name}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}">${g}${legend}<text x="${W / 2}" y="${H - 1}" text-anchor="middle">${xLabel}</text><text x="12" y="${H / 2}" transform="rotate(-90 12 ${H / 2})" text-anchor="middle">${yLabel}</text></svg>`;
}

function renderCalibration(m) {
  const at = (c) => (c ? `${pct(c.coverage, 0)} of volume (threshold ${c.threshold.toFixed(2)})` : "not reached");
  const conf = `<table><tr><th>actual \\ predicted</th>${m.labels.map((l) => `<th>${esc(l)}</th>`).join("")}</tr>${m.labels.map((a) => `<tr><td><b>${esc(a)}</b></td>${m.labels.map((p) => `<td style="${a === p ? "background:#ede9fe;font-weight:700" : m.confusion[a][p] ? "background:#fee2e2" : ""}">${m.confusion[a][p] || ""}</td>`).join("")}</tr>`).join("")}</table>`;
  $("#cal-result").innerHTML = `
    <div class="kpis">${kpi(pct(m.accuracy), `Accuracy (${m.n} rows)`, "ok")}${kpi(m.ece.toFixed(3), "Calibration error (ECE)", "d1")}
      ${kpi(at(m.at_95), "Automatable at ≥95% accuracy", "ok")}${kpi(at(m.at_99), "Automatable at ≥99% accuracy", "ok")}
      ${m.shuffle ? kpi(`${m.shuffle.flips}/${m.shuffle.checked}`, "Answers changed when options were reversed", "d1") : ""}${kpi(fmtMs(m.p50_ms), "P50 per decision", "d1")}</div>
    <div class="grid2">
      <div class="card"><h4>Reliability: does 90% confidence mean 90% right?</h4>${svgChart([{ name: "accuracy per confidence bin", color: "#6d28d9", points: m.reliability.map((b) => [b.avg_conf, b.accuracy]) }], { diagonal: true, xLabel: "average confidence", yLabel: "accuracy", bars: m.reliability.map((b) => ({ lo: b.lo, hi: b.hi, v: b.accuracy, title: `${b.n} rows` })) })}</div>
      <div class="card"><h4>Choose your threshold: coverage vs accuracy</h4>${svgChart([{ name: "accuracy above threshold", color: "#0f9d58", points: m.curve.map((c) => [c.threshold, c.accuracy]) }, { name: "share automated", color: "#ea7a0c", points: m.curve.map((c) => [c.threshold, c.coverage]) }], { xLabel: "confidence threshold", yLabel: "" })}</div>
    </div>
    <div class="card" style="margin-top:12px"><h4>Confusion matrix</h4><div class="table-wrap">${conf}</div></div>
    <details><summary>Row-level results</summary><div class="table-wrap"><table><tr><th>Text</th><th>Label</th><th>Microsoft-Decision-1</th><th>Confidence</th></tr>${m.rows.map((r) => `<tr><td>${esc(r.text)}</td><td>${esc(r.label)}</td><td class="${r.pred === r.label ? "ok" : "bad"}">${esc(r.pred)}</td><td>${r.conf.toFixed(3)}</td></tr>`).join("")}</table></div></details>`;
}

// ------------------------------------------------------------------ benchmark
async function renderBenchmark() {
  const b = await api("/api/benchmark");
  if (!b || !b.models) { $("#bench").innerHTML = `<p class="muted">Run <code>python tools/benchmark.py</code> to compare Microsoft-Decision-1 with LLMs on the labelled decisions.</p>`; return; }
  state.benchmark = b;
  const best = Math.max(...b.models.map((x) => x.p50_ms));
  $("#bench").innerHTML = `
    <p class="muted">${esc(b.description)}</p>
    <table><tr><th>Model</th><th>Accuracy</th><th>P50 latency</th><th>P95 latency</th><th>Cost per 1,000 decisions</th><th>Changed answer when options were reordered</th><th>Calibrated probabilities</th></tr>
      ${b.models.map((x) => `<tr><td><b>${esc(x.name)}</b></td><td><b>${pct(x.accuracy)}</b></td>
        <td><div class="bar ${x.name.includes("Decision") ? "" : "llm"}" style="width:${Math.max(3, (x.p50_ms / best) * 160)}px"></div>${fmtMs(x.p50_ms)}</td><td>${fmtMs(x.p95_ms)}</td>
        <td>${fmtUsd(x.cost_per_1k_usd, 4)}</td><td>${x.flip_rate == null ? "—" : pct(x.flip_rate)}</td><td>${x.calibrated ? "✓" : "—"}</td></tr>`).join("")}</table>
    <p class="hint">${esc(b.notes || "")}</p>`;
}

// ------------------------------------------------------------------ ROI
function renderRoi() {
  const b = state.baseline || {};
  const auto = (lane) => {
    const rows = (b.rows || []).filter((r) => r.gold_lane === lane);
    return rows.length ? rows.filter((r) => ["AUTO_APPROVE", "AUTO_CREDIT", "AUTO_REQUEST_INFO", "APPROVE_WITH_MONITORING", "ALLOW", "AUTO_WARNING", "AUTO_REPLY"].includes(r.disposition)).length / rows.length : 0.5;
  };
  const perCase = b.total_cost_usd && b.events ? b.total_cost_usd / b.events : 0.00006;
  const bm = state.benchmark?.models || [];
  const d1 = bm.find((m) => m.name.includes("Decision")), llm = bm.filter((m) => !m.name.includes("Decision")).sort((a, c) => c.cost_per_1k_usd - a.cost_per_1k_usd)[0];
  const ratio = d1 && llm ? llm.cost_per_1k_usd / Math.max(d1.cost_per_1k_usd, 1e-9) : 25;
  const lanes = [
    { key: "dispute", name: "Disputes", vol: 60000, min: 12 }, { key: "merchant_review", name: "Merchant reviews", vol: 15000, min: 25 },
    { key: "scam_or_account_risk", name: "P2P / scam screening alerts", vol: 120000, min: 6 }, { key: "agent_payment", name: "Agentic checkout decisions", vol: 3000000, min: 0 },
  ];
  $("#roi").innerHTML = `<div class="roi-grid">${lanes.map((l) => `
      <div class="card"><h4>${laneTag(l.key)} ${l.name}</h4>
        <label>Cases per month <input type="number" data-k="${l.key}-vol" value="${l.vol}"></label>
        <label>Manual minutes per case <input type="number" data-k="${l.key}-min" value="${l.min}"></label>
        <label>Automated share (baseline) <input type="number" step="0.01" data-k="${l.key}-auto" value="${auto(l.key).toFixed(2)}"></label></div>`).join("")}
      <div class="card"><h4>Assumptions</h4>
        <label>Loaded analyst cost $/hour <input type="number" data-k="rate" value="55"></label>
        <label>Microsoft-Decision-1 cost per case $ <input type="number" step="0.000001" data-k="d1" value="${perCase.toFixed(6)}"></label>
        <label>LLM-only cost multiple (benchmark) <input type="number" step="0.1" data-k="ratio" value="${ratio.toFixed(1)}"></label></div>
    </div><div id="roi-out"></div>`;
  const calc = () => {
    const v = (k) => Number(document.querySelector(`[data-k="${k}"]`).value || 0);
    let hours = 0, cases = 0;
    lanes.forEach((l) => { hours += (v(`${l.key}-vol`) * v(`${l.key}-auto`) * v(`${l.key}-min`)) / 60; cases += v(`${l.key}-vol`); });
    const d1Year = cases * v("d1") * 12, llmYear = d1Year * v("ratio");
    $("#roi-out").innerHTML = `<div class="kpis">${kpi(Math.round(hours).toLocaleString(), "Analyst hours saved / month", "ok")}${kpi("$" + Math.round(hours * v("rate") * 12).toLocaleString(), "Labour value / year", "ok")}
      ${kpi("$" + Math.round(d1Year).toLocaleString(), "Microsoft-Decision-1 cost / year", "d1")}${kpi("$" + Math.round(llmYear).toLocaleString(), "Same decisions with an LLM / year", "llm")}
      ${kpi((cases / 1e6).toFixed(2) + "M", "Cases decided / month")}</div>
      <p class="hint">Labour value counts only cases the policy automates in the customer's favour; adverse actions stay with people. Losses avoided (fraud, scams, penalties) are not included.</p>`;
  };
  document.querySelectorAll("#roi input").forEach((i) => i.addEventListener("input", calc));
  calc();
}

(async () => {
  try { await renderBaseline(); } catch (e) { $("#baseline").textContent = e.message; }
  try { await renderBenchmark(); } catch (e) { $("#bench").textContent = e.message; }
  renderRoi();
})();
