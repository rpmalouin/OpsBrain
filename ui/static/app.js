/* OpsBrain real-time dashboard client.
   Connects to /stream, receives merged JSON on every change, renders 5 panels. */
"use strict";

const $ = (id) => document.getElementById(id);
const grid = $("grid");
const connEl = $("conn");
const mtimeEl = $("mtime");

let state = {};

// ----------------------------------------------------------------- helpers
function fmtPct(v) {
  if (v === undefined || v === null || v === "unknown" || v === "") return "—";
  let n = parseFloat(String(v));
  if (isNaN(n)) return String(v);
  return n.toFixed(1) + "%";
}
function fmtMb(v) {
  if (v === undefined || v === null) return "—";
  let n = parseFloat(v);
  if (isNaN(n)) return "—";
  if (n >= 1024) return (n / 1024).toFixed(1) + " GB";
  return n.toFixed(0) + " MB";
}
function chip(text, tone) { return `<span class="chip bg-slate-700 ${tone}">${escapeHtml(text)}</span>`; }
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function driftTone(flag) {
  if (flag === "stuck_process") return "crit";
  if (flag === "vram_overload") return "crit";
  return "warn";
}
function panel(title, bodyHtml) {
  return `<div class="panel p-3"><h2 class="text-sm font-semibold text-slate-200 mb-2 uppercase tracking-wide">${escapeHtml(title)}</h2>${bodyHtml}</div>`;
}
function tsAge(ts) {
  if (!ts) return null;
  const t = Date.parse(String(ts).includes("T") ? ts : ts);
  if (isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / 1000));
}

// ----------------------------------------------------------------- panel renderers
function renderSystem(col, rea) {
  const d = col?.docker || {};
  const vm = col?.vm || {};
  const g = (col?.gpu?.gpus || [])[0] || {};
  const conf = rea?.confidence;
  const disks = vm.disk_root || {};
  const colTs = col?.timestamp;
  const age = tsAge(colTs);
  const lastCycle = age === null ? "—" : `${age}s ago`;
  const next = age === null ? "—" : `${Math.max(0, 120 - age)}s`;
  const cpu = g.util_gpu_percent; // GPU util as the "CPU" proxy for the LLM box
  let rows = "";
  rows += `<div class="flex justify-between"><span class="text-slate-400">GPU util</span><span>${fmtPct(cpu)}</span></div>`;
  rows += `<div class="flex justify-between"><span class="text-slate-400">RAM</span><span>${escapeHtml(disks["Use%"] ?? "—")}</span></div>`;
  rows += `<div class="flex justify-between"><span class="text-slate-400">Disk /</span><span>${escapeHtml(disks["Use%"] ?? "—")} of ${escapeHtml(disks.Size ?? "?")}</span></div>`;
  rows += `<div class="flex justify-between"><span class="text-slate-400">Containers</span><span>${d.running ?? "—"} running / ${d.containers_count ?? "—"}</span></div>`;
  rows += `<div class="flex justify-between"><span class="text-slate-400">Confidence</span><span class="${conf >= 0.6 ? "ok" : "warn"}">${conf === undefined ? "—" : Number(conf).toFixed(2)}</span></div>`;
  rows += `<div class="flex justify-between"><span class="text-slate-400">Last cycle</span><span>${lastCycle}</span></div>`;
  rows += `<div class="flex justify-between"><span class="text-slate-400">Next cycle</span><span>${next}</span></div>`;
  return panel("System Overview", `<div class="space-y-1 text-sm">${rows}</div>`);
}

function renderContainers(col) {
  const d = col?.docker || {};
  const containers = d.containers || [];
  const restarting = d.restarting || [];
  const restartCap = restarting.length ? `<span class="chip bg-slate-700 crit">restarting: ${escapeHtml(restarting.join(", "))}</span>` : "";
  const capStr = restartCap || chip("no restart loop", "ok");
  let body = `<div class="mb-2 text-xs text-slate-400">Restart loop: ${capStr}</div>`;
  if (!containers.length) body += `<div class="text-slate-500 text-sm">no container data</div>`;
  else {
    const rows = containers.slice(0, 25).map(c => {
      const st = c.stats || {};
      const cpu = (st.cpu_percent ?? "0%").replace("%", "%");
      const mem = (st.mem_percent ?? "0%").replace("%", "%");
      const exited = c.state === "exited" ? "text-red-400" : "";
      return `<tr><td class="${exited}">${escapeHtml(c.name)}</td><td>${escapeHtml(c.state)}</td><td>${cpu}</td><td>${mem}</td><td>${c.restart_count ?? 0}</td></tr>`;
    }).join("");
    body += `<table><thead><tr class="text-slate-400 text-xs"><th>Container</th><th>State</th><th>CPU</th><th>RAM</th><th>Restarts</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  return panel("Container Health", body);
}

function renderGpu(col, baseline) {
  const g = (col?.gpu?.gpus || [])[0] || {};
  const flags = col?.gpu?.drift_flags || [];
  const b = baseline || {};
  const vramPct = g.mem_total_mb ? (g.mem_used_mb / g.mem_total_mb * 100).toFixed(1) : "—";
  let body = `<div class="space-y-1 text-sm">`;
  body += `<div class="flex justify-between"><span class="text-slate-400">VRAM</span><span>${fmtMb(g.mem_used_mb)} / ${fmtMb(g.mem_total_mb)} (${vramPct}%)</span></div>`;
  body += `<div class="flex justify-between"><span class="text-slate-400">Power</span><span>${g.power_w ?? "—"} W</span></div>`;
  body += `<div class="flex justify-between"><span class="text-slate-400">Temp</span><span>${g.temp_c ?? "—"} °C</span></div>`;
  body += `<div class="flex justify-between"><span class="text-slate-400">Baseline VRAM</span><span>${fmtMb(b.last_vram)}</span></div>`;
  body += `<div class="flex justify-between"><span class="text-slate-400">Stuck PID</span><span>${escapeHtml(b.last_pid ?? "—")} (${b.cycles_with_same_pid ?? 0} cyc)</span></div>`;
  body += `</div>`;
  const chips = flags.length ? flags.map(f => chip(f, driftTone(f))).join(" ") : chip("no drift", "ok");
  body += `<div class="mt-2 text-xs text-slate-400">Drift: ${chips}</div>`;
  return panel("GPU Drift", body);
}

function renderDecisions(rea, act) {
  const conf = rea?.confidence;
  const dry = act?.dry_run;
  const warnings = rea?.warnings || [];
  const actions = rea?.actions || [];
  const dryStr = dry ? chip("DRY-RUN", "warn") : chip("LIVE", "ok");
  const confTone = conf >= 0.6 ? "ok" : "warn";
  let body = `<div class="text-xs text-slate-400 mb-1">confidence ${Number(conf ?? 0).toFixed(2)} · ${dryStr}</div>`;
  body += `<div class="mb-1 font-semibold text-slate-200 text-sm">Warnings (${warnings.length})</div>`;
  body += warnings.length ? `<ul class="list-disc pl-4 text-sm text-slate-300">${warnings.slice(0, 12).map(w => `<li>${escapeHtml(typeof w === "string" ? w : JSON.stringify(w))}</li>`).join("")}</ul>` : `<div class="text-slate-500 text-sm">none</div>`;
  body += `<div class="mt-2 mb-1 font-semibold text-slate-200 text-sm">Actions (${actions.length})</div>`;
  body += actions.length ? actions.slice(0, 12).map(a => {
    const at = typeof a.type === "string" ? a.type : a.action;
    return `<div class="text-sm"><span class="chip bg-slate-700">${escapeHtml(at)}</span> ${escapeHtml(a.target ?? "")} <span class="text-slate-400">${escapeHtml(a.reason ?? "")}</span></div>`;
  }).join("") : `<div class="text-slate-500 text-sm">none</div>`;
  return panel("OpsBrain Decisions", body);
}

function renderReport(rea, act, col) {
  const summary = rea?.summary || act?.summary || "No summary yet.";
  const anomalies = (rea?.warnings || []).length + (col?.gpu?.drift_flags || []).length;
  const rem = act?.gpu_drift?.remediations || [];
  const driftEvents = act?.gpu_drift?.events || [];
  let body = `<div class="text-sm text-slate-300 mb-2"><span class="text-slate-400">24h summary:</span> ${escapeHtml(summary)}</div>`;
  body += `<div class="flex gap-3 text-xs mb-2">`;
  body += `<span class="text-slate-400">anomalies <b class="text-slate-100">${anomalies}</b></span>`;
  body += `<span class="text-slate-400">remediations <b class="text-slate-100">${rem.length}</b></span>`;
  body += `<span class="text-slate-400">drift events <b class="text-slate-100">${driftEvents.length}</b></span>`;
  body += `</div>`;
  if (rem.length) body += `<div class="text-xs text-slate-400">Remediation: ${rem.slice(0, 8).map(r => `${escapeHtml(r?.verb)}:${escapeHtml(r?.target)}`).join(" · ")}</div>`;
  body += `<div class="mt-2 text-xs"><a class="text-slate-400 underline" href="/report">view report</a></div>`;
  return panel("Daily Report Preview", body);
}

// ----------------------------------------------------------------- render
function render() {
  const collector = state.collector || {};
  const reasoner = state.reasoner || {};
  const actions = state.actions || {};
  const baseline = state.gpu_baseline || {};
  const cards = [
    renderSystem(collector, reasoner),
    renderContainers(collector),
    renderGpu(collector, baseline),
    renderDecisions(reasoner, actions),
    renderReport(reasoner, actions, collector),
  ];
  grid.innerHTML = cards.join("");
  const ts = collector?.timestamp;
  mtimeEl.textContent = ts ? `collector @ ${ts}` : "";
}

// ----------------------------------------------------------------- websocket
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/stream`);
  connEl.textContent = "connecting…";
  connEl.className = "chip bg-slate-700 text-slate-300";
  ws.onopen = () => { connEl.textContent = "live"; connEl.className = "chip bg-green-700 text-white"; };
  ws.onmessage = (ev) => {
    try { state = JSON.parse(ev.data); render(); }
    catch (e) { console.error("parse", e); }
  };
  ws.onclose = () => {
    connEl.textContent = "reconnecting";
    connEl.className = "chip bg-red-700 text-white";
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();
}
connect();