// Trust Console — core, live cases, review queue, architecture.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtMs = (v) => (v == null ? "" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms");
const fmtUsd = (v, d = 6) => "$" + Number(v || 0).toFixed(d);
const LANES = { agent_payment: "Agentic checkout", dispute: "Disputes", merchant_review: "Merchant integrity", scam_or_account_risk: "Scams & sanctions", general_inquiry: "General" };
const DISP_CLASS = {
  AUTO_APPROVE: "auto", AUTO_CREDIT: "auto", AUTO_REQUEST_INFO: "auto", APPROVE_WITH_MONITORING: "auto", ALLOW: "auto", AUTO_REPLY: "auto",
  AUTO_WARNING: "warn", CONSUMER_STEP_UP: "warn",
  RISK_REVIEW: "human", ANALYST_REVIEW: "human", UNDERWRITER_REVIEW: "human", HUMAN_HOLD: "human", ACCOUNT_SECURITY_REVIEW: "human", HUMAN_TRIAGE: "human",
  COMPLIANCE_REFERRAL: "stop", SANCTIONS_HOLD: "stop", QUARANTINED: "stop",
};
const pill = (d) => `<span class="pill p-${DISP_CLASS[d] || "human"}">${esc((d || "").replaceAll("_", " "))}</span>`;
const laneTag = (l) => `<span class="lane ${esc(l)}">${esc(LANES[l] || l || "—")}</span>`;
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
};
const state = { config: {}, events: [], baseline: null };

// ------------------------------------------------------------------ tabs
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
    if (b.dataset.tab === "review") loadReview();
  })
);

// ------------------------------------------------------------------ live cases
async function initLive() {
  const [heroes, events, config] = await Promise.all([api("/api/heroes"), api("/api/events"), api("/api/config")]);
  state.events = events; state.config = config;
  $("#hero-list").innerHTML = heroes.map((h) => `
    <div class="hero" data-id="${esc(h.id)}">
      <div class="t">${laneTag(h.lane)}${esc(h.title)}</div>
      <div class="s">${esc(h.story)}</div>
    </div>`).join("");
  document.querySelectorAll(".hero").forEach((el) => el.addEventListener("click", () => runCase(el.dataset.id)));
  const groups = {};
  events.forEach((e) => (groups[e.lane] = groups[e.lane] || []).push(e));
  $("#event-select").innerHTML = Object.entries(groups).map(([lane, list]) =>
    `<optgroup label="${esc(LANES[lane] || lane)}">${list.map((e) => `<option value="${esc(e.id)}">${esc(e.id)} · ${esc(e.title)}</option>`).join("")}</optgroup>`).join("");
  $("#run-event").addEventListener("click", () => runCase($("#event-select").value));
  $("#run-text").addEventListener("click", () => { const t = $("#free-text").value.trim(); if (t) runCase(t); });
}

async function runCase(input) {
  const box = $("#result");
  document.querySelectorAll("button").forEach((b) => b.id?.startsWith("run") && (b.disabled = true));
  box.innerHTML = `<div class="loading"><span class="spinner"></span>&nbsp; Running <b>${esc(input.length > 60 ? input.slice(0, 60) + "…" : input)}</b> through the hosted multi-agent workflow…</div>`;
  try {
    const res = await api("/api/agent/run", { method: "POST", body: JSON.stringify({ input }) });
    renderResult(res);
    refreshReviewBadge();
  } catch (err) {
    box.innerHTML = `<div class="loading bad">Run failed: ${esc(err.message).slice(0, 600)}</div>`;
  } finally {
    document.querySelectorAll("button").forEach((b) => b.id?.startsWith("run") && (b.disabled = false));
  }
}

function probRows(ans) {
  if (ans.type === "noul") return `<div class="prob top"><span>probability true</span><div class="track"><div class="fill" style="width:${ans.p * 100}%"></div></div><span>${ans.p.toFixed(3)}</span></div>`;
  const entries = Object.entries(ans.probabilities || {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
  const top = ans.type === "choice" ? ans.choice : ans.label;
  const head = ans.type === "score" ? `<div class="qn">score ${ans.score} of ${ans.levels - 1} → <b>${esc(ans.label)}</b></div>` : "";
  return head + entries.map(([k, v]) => `<div class="prob ${k === top ? "top" : ""}"><span>${esc(k)}</span><div class="track"><div class="fill" style="width:${v * 100}%"></div></div><span>${v.toFixed(3)}</span></div>`).join("");
}

function renderResult(res) {
  const rec = res.record;
  if (!rec) { $("#result").innerHTML = `<div class="card"><pre style="white-space:pre-wrap">${esc(res.markdown)}</pre></div>`; return; }
  const m = rec.metrics || {};
  const cls = { auto: "b-auto", warn: "b-warn", human: "b-human", stop: "b-stop" }[DISP_CLASS[rec.disposition] || "human"];
  const maxMs = Math.max(1, ...rec.timeline.map((s) => s.ms || 0));
  const gold = rec.gold && (rec.gold.acceptable_dispositions || [rec.gold.disposition]);
  const goldLine = gold ? `<span class="${gold.includes(rec.disposition) ? "ok" : "bad"}">${gold.includes(rec.disposition) ? "✓ matches" : "✗ differs from"} demo label (${esc(gold.join(" / "))})</span>` : "";
  const draft = rec.draft ? Object.entries(rec.draft).map(([k, v]) => `<div><b>${esc(k.replaceAll("_", " "))}:</b> ${Array.isArray(v) ? "<ul class='reasons'>" + v.map((x) => `<li>${esc(x)}</li>`).join("") + "</ul>" : esc(v)}</div>`).join("") : "";
  const decisions = (rec.decisions || []).map((d) => `
    <details class="dec"><summary>⚡ ${esc(d.agent)} — ${Object.keys(d.answers).length} decisions · ${fmtMs(d.latency_ms)} · ${d.input_tokens} tokens · ${fmtUsd(d.cost_usd, 7)}</summary>
      ${Object.entries(d.answers).map(([name, a]) => `<div class="dec"><div class="q">${esc(name)} <span class="qn">(${esc(a.type)})</span></div><div class="qn">${esc(d.questions[name]?.instructions || "")}</div>${probRows(a)}</div>`).join("")}
    </details>`).join("");
  $("#result").innerHTML = `
    <div class="banner ${cls}"><div><div class="d">${esc(rec.disposition.replaceAll("_", " "))}</div><div class="q">${esc(rec.automatic ? "Automatic — no human needed" : "Human in the loop: " + (rec.human_queue || ""))}</div></div>
      <div class="q">${laneTag(rec.lane)} ${esc(rec.case_id)}</div></div>
    <div class="case-title">${esc(rec.title)}</div><div>${goldLine}</div>
    <div class="kpis">
      <div class="kpi d1"><div class="v">${m.decisions}</div><div class="l">Microsoft-Decision-1 decisions (${m.decision_calls} calls)</div></div>
      <div class="kpi d1"><div class="v">${fmtMs(m.decision_ms_critical_path)}</div><div class="l">Microsoft-Decision-1 time</div></div>
      <div class="kpi d1"><div class="v">${fmtUsd(m.decision_cost_usd, 5)}</div><div class="l">Microsoft-Decision-1 cost</div></div>
      <div class="kpi llm"><div class="v">${m.llm_calls}</div><div class="l">LLM drafts (${fmtMs(m.llm_ms)})</div></div>
      <div class="kpi"><div class="v">${fmtMs(m.total_ms)}</div><div class="l">Workflow end-to-end</div></div>
    </div>
    <h3>Agent trail</h3>
    <ul class="trail">${rec.timeline.map((s, i) => `
      <li class="step" style="animation-delay:${i * 120}ms"><span class="ic ${esc(s.kind)}">${{ decision: "⚡", llm: "✎", policy: "⚖", info: "↓" }[s.kind] || "•"}</span>
        <span class="ag">${esc(s.agent)}</span><span class="sm">${esc(s.summary)}</span>
        <span>${s.ms ? `<div class="bar ${s.kind === "llm" ? "llm" : ""}" style="width:${Math.max(4, (s.ms / maxMs) * 100)}%"></div><span class="ms">${fmtMs(s.ms)}</span>` : ""}</span></li>`).join("")}
    </ul>
    <div class="grid2" style="margin-top:14px">
      <div class="card"><h4>Why</h4><ul class="reasons">${(rec.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("") || "<li>No issues found</li>"}</ul></div>
      <div class="card draft"><h4>${rec.draft ? `Draft — accepted by the Output Judge (Microsoft-Decision-1)${rec.revisions ? ` after ${rec.revisions} revision(s)` : ""}` : "Draft"}</h4>${draft || `<span class="muted">${rec.disposition === "SANCTIONS_HOLD" ? "No automatic customer message for sanctions holds." : rec.disposition === "QUARANTINED" ? "Content withheld from the LLM agents." : "No LLM needed — the decision ledger documents the outcome."}</span>`}</div>
    </div>
    <h3>Each Microsoft-Decision-1 answer (calibrated probabilities)</h3>${decisions}
    <p class="hint">Round trip through Foundry: ${fmtMs(res.roundtrip_ms)} · hosted agent <b>${esc(state.config.agent_name)}</b></p>`;
}

// ------------------------------------------------------------------ review queue
async function refreshReviewBadge() {
  try { const items = await api("/api/review"); $("#review-count").textContent = items.filter((i) => i.status === "pending").length; } catch (_) { /* ignore */ }
}
async function loadReview() {
  const items = await api("/api/review");
  $("#review-count").textContent = items.filter((i) => i.status === "pending").length;
  $("#review").innerHTML = items.length ? items.map((i) => `
    <div class="card" style="margin-bottom:10px">
      <div class="row space"><div>${pill(i.disposition)} ${laneTag(i.lane)} <b>${esc(i.case_id)}</b> — ${esc(i.title)}<div class="hint">Queue: ${esc(i.queue)} · received ${esc(i.received)}</div></div>
        <div class="row">${i.status === "pending" ? `<button data-act="approve" data-id="${esc(i.case_id)}">Approve recommendation</button><button data-act="reject" data-id="${esc(i.case_id)}">Override</button>` : `<b class="${i.status === "approved" ? "ok" : "bad"}">${esc(i.status)} ${esc(i.decided || "")}</b>`}</div></div>
      <ul class="reasons">${i.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      ${i.draft ? `<details><summary>Draft prepared for the reviewer</summary><div class="draft">${Object.entries(i.draft).map(([k, v]) => `<div><b>${esc(k)}:</b> ${esc(Array.isArray(v) ? v.join("; ") : v)}</div>`).join("")}</div></details>` : ""}
    </div>`).join("") : `<div class="empty">No cases waiting. Run a hero scenario such as <b>dispute-first-party</b> or <b>scam-impersonation</b>.</div>`;
  document.querySelectorAll("#review button[data-act]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/review/${b.dataset.id}`, { method: "POST", body: JSON.stringify({ action: b.dataset.act }) });
    loadReview();
  }));
}

// ------------------------------------------------------------------ architecture
function renderArchitecture() {
  $("#architecture").innerHTML = `
    <h2>How it works</h2>
    <p class="muted">One Foundry hosted agent runs a Microsoft Agent Framework workflow. Microsoft-Decision-1 makes every trust decision; LLM agents write; people own adverse actions.</p>
    <div class="arch">
      <div class="box"><b>Inbound events</b>Agent payment requests (mandate + cart + browsing trace) · disputes · merchant reviews · P2P payments and chats</div>
      <div class="box d1"><b>Shield ∥ Dispatcher</b>Parallel fan-out. Prompt injection, social engineering, data exfiltration · lane + urgency. Fan-in gate.</div>
      <div class="box d1"><b>Lane decision agents</b>Mandate Guardian (per-line checks) · Dispute Analyst (reason code, Reg E/Z, misuse, evidence) · Merchant Classifier (MCC, prohibited, deceptive billing, per-sentence evidence) · Scam &amp; Sanctions Screen</div>
      <div class="box llm"><b>LLM agents (only when useful)</b>Microsoft-Decision-1 Model Router picks gpt-5.4-mini / gpt-5.6-luna / gpt-6-sol → consumer step-up, dispute letter, underwriting memo, protection message</div>
      <div class="box d1"><b>Output Judge</b>Microsoft-Decision-1 grades every draft against a rubric → accept / revise (max 2) / human edit</div>
    </div>
    <div class="arch" style="margin-top:10px">
      <div class="box h"><b>Confidence policy</b>Calibrated probabilities → AUTO / consumer step-up / analyst queues. Hard rules (caps, blocked MCCs, sanctions) are code.</div>
      <div class="box h"><b>Decision ledger</b>Question, options, probabilities, latency, tokens and cost for every decision → audit and model-risk evidence</div>
      <div class="box h"><b>Human review</b>Consumers confirm step-ups; analysts own denials, holds, referrals and quarantines</div>
      <div class="box"><b>Foundry</b>Hosted agent versions · traces in Application Insights · models: Microsoft-Decision-1 (Global Standard + Data Zone Standard) and GPT tiers</div>
      <div class="box"><b>Trust Console</b>Azure Container Apps · managed identity · firehose · calibration · ROI</div>
    </div>
    <div class="principle"><b>Design principle:</b> Microsoft-Decision-1 never takes an adverse action on its own. It chooses between <i>proceed</i> and <i>ask a human</i> (the consumer or an analyst). Automatic outcomes only ever go in the customer's favour.</div>`;
}

initLive().catch((e) => ($("#result").innerHTML = `<div class="loading bad">${esc(e.message)}</div>`));
renderArchitecture();
refreshReviewBadge();
