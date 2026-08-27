/* OpsBrain real-time dashboard client.
   Receives merged JSON (incl. history[] + notifications[]) over WS each cycle,
   renders panels, sparklines, drift trend, confidence trend, live notifications,
   and container group filters. */
"use strict";

const $ = (id) => document.getElementById(id);
const grid = $("grid");
const connEl = $("conn");
const mtimeEl = $("mtime");
const notifCountEl = $("notifCount");
const notifBox = $("notifications");

let state = {};
let groups = {};                 // container group filters (/api/groups)
let activeGroup = "all";
const browserNotified = {};      // dedupe HTML5 notifications

// ----------------------------------------------------------------- helpers
function num(v) { const n = Number(v); return isFinite(n) ? n : null; }
function fmtPct(v) { const n = num(v); return n === null ? "—" : n.toFixed(1) + "%"; }
function fmtMb(v) {
  const n = num(v);
  return n === null ? "—" : (n >= 1024 ? (n / 1024).toFixed(1) + " GB" : n.toFixed(0) + " MB");
}
function chip(text, tone) { return `<span class="chip bg-slate-700 ${tone}">${escapeHtml(String(text))}</span>`; }
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function panel(title, bodyHtml) { return `<div class="panel p-3"><h2 class="text-sm font-semibold text-slate-200 mb-2 uppercase tracking-wide">${escapeHtml(title)}</h2>${bodyHtml}</div>`; }
function tsAge(ts) { const t = Date.parse(String(ts)); return isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 1000)); }
function confColor(c) {
  if (c === null || c === undefined) return "#64748b";
  if (c > 0.8) return "#4ade80";
  if (c >= 0.6) return "#facc15";
  return "#f87171";
}
function driftTone(flag) { return (flag === "stuck_process" || flag === "vram_overload") ? "crit" : "warn"; }
function clean(vals) { return (vals || []).map(num).filter(v => v !== null); }
function sparkHost(id) { return `<div id="${id}"></div>`; }   // placeholder filled post-paint
function drawSpark(id, values, opts) {
  requestAnimationFrame(() => {
    const el = $(id);
    if (el && (values || []).length) sparkline(id, clean(values), opts);
  });
}

// ------------------------------------------------------------- panel renderers
function renderSystem(col) {
  const d = col?.docker || {};
  const vm = col?.vm || {};
  const g = (col?.gpu?.gpus || [])[0] || {};
  const disks = vm.disk_root || {};
  const age = tsAge(col?.timestamp);
  let r = "";
  r += `<div class="flex justify-between"><span class="text-slate-400">GPU util</span><span>${fmtPct(g.util_gpu_percent)}</span></div>`;
  r += `<div class="flex justify-between"><span class="text-slate-400">Disk /</span><span>${escapeHtml(disks["Use%"] ?? "—")} of ${escapeHtml(disks.Size ?? "?")}</span></div>`;
  r += `<div class="flex justify-between"><span class="text-slate-400">Containers</span><span>${d.running ?? "—"} running / ${d.containers_count ?? "—"}</span></div>`;
  r += `<div class="flex justify-between"><span class="text-slate-400">Last cycle</span><span>${age === null ? "—" : age + "s ago"}</span></div>`;
  r += `<div class="flex justify-between"><span class="text-slate-400">Next cycle</span><span>${age === null ? "—" : Math.max(0, 120 - age) + "s"}</span></div>`;
  return panel("System Overview", `<div class="space-y-1 text-sm">${r}</div>`);
}

// ------------------------------------------------- Cluster Overview
function renderClusterOverview() {
  const snap = state.cluster_snapshot || {};
  const cr = state.cluster_reasoner || {};
  const nodes = snap.nodes || {};
  const metrics = snap.cluster_metrics || {};
  const score = num(cr.cluster_stability_score);
  const online = Object.values(nodes).filter(n => n.online).length;
  const total = Object.keys(nodes).length;
  const scoreColor = (score === null) ? "#64748b" : (score >= 80 ? "#4ade80" : (score >= 60 ? "#facc15" : "#f87171"));
  let r = `<div class="flex items-center gap-2 mb-2">` +
    `<span class="text-2xl mono" style="color:${scoreColor}">${score === null ? "—" : score.toFixed(0)}</span>` +
    `<span class="text-xs text-slate-400">cluster stability /100</span></div>`;
  r += `<div class="flex justify-between text-sm"><span>Nodes</span><span>${online}/${total} online</span></div>`;
  r += `<div class="flex justify-between text-sm"><span>Avg confidence</span><span>${metrics.avg_confidence === 0 ? "0.00" : num(metrics.avg_confidence) === null ? "—" : num(metrics.avg_confidence).toFixed(2)}</span></div>`;
  r += `<div class="flex justify-between text-sm"><span>Total anomalies</span><span>${num(metrics.total_anomalies) ?? 0}</span></div>`;
  r += `<div class="flex justify-between text-sm"><span>Drift events</span><span>${num(metrics.drift_events) ?? 0}</span></div>`;
  r += `<div class="flex justify-between text-sm"><span>Restart events</span><span>${num(metrics.restart_events) ?? 0}</span></div>`;
  if (cr.recommendations && cr.recommendations.length) {
    r += `<div class="mt-2 text-xs space-y-0.5">`;
    for (const rec of cr.recommendations.slice(0, 3)) {
      const tone = rec.severity === "critical" ? "text-red-300" : rec.severity === "warning" ? "text-amber-300" : "text-slate-400";
      r += `<div class="${tone}">• [${escapeHtml(rec.type)}] ${escapeHtml(rec.reason)}</div>`;
    }
    if (cr.recommendations.length > 3) r += `<div class="text-slate-500">+${cr.recommendations.length - 3} more</div>`;
    r += `</div>`;
  }
  const summary = cr.summary;
  if (summary) r += `<div class="mt-2 text-xs text-slate-400 italic">${escapeHtml(summary)}</div>`;
  return panel("Cluster Overview", `<div class="space-y-1">${r}</div>`);
}

// ------------------------------------------------- Node Comparison
function renderNodeComparison() {
  const snap = state.cluster_snapshot || {};
  const cr = state.cluster_reasoner || {};
  const nodes = snap.nodes || {};
  const stab = cr.node_stability || {};
  const names = Object.keys(nodes);
  if (!names.length) return panel("Node Comparison", `<div class="text-slate-500 text-sm">No cluster node data yet.</div>`);
  let rows = "";
  for (const name of names) {
    const n = nodes[name] || {};
    const s = num(stab[name]);
    const dot = n.online ? "#4ade80" : "#f87171";
    const sColor = (s === null) ? "#64748b" : (s >= 80 ? "#4ade80" : (s >= 60 ? "#facc15" : "#f87171"));
    rows += `<tr class="border-t border-slate-800">` +
      `<td class="py-1"><span class="drift-dot" style="background:${dot}"></span> ${escapeHtml(name)} <span class="text-xs text-slate-500">(${escapeHtml(n.type || "?")})</span></td>` +
      `<td class="py-1 text-center">${num(n.confidence) === null ? "—" : num(n.confidence).toFixed(2)}</td>` +
      `<td class="py-1 text-center">${num(n.drift_events) ?? 0}</td>` +
      `<td class="py-1 text-center">${num(n.anomalies) ?? 0}</td>` +
      `<td class="py-1 text-center">${num(n.restart_events) ?? 0}</td>` +
      `<td class="py-1 text-center mono" style="color:${sColor}">${s === null ? "—" : s.toFixed(0)}</td>` +
      `</tr>`;
  }
  let r = `<table class="w-full text-sm"><thead><tr class="text-xs text-slate-400 text-left"><th>Node</th><th class="text-center">Conf</th><th class="text-center">Drift</th><th class="text-center">Anom</th><th class="text-center">Restart</th><th class="text-center">Stab</th></tr></thead><tbody>${rows}</tbody></table>`;
  return panel("Node Comparison", r);
}

// ------------------------------------------------- TrueNAS panel
function renderTruenas(col) {
  const tn = col?.truenas || {};
  if (!tn.enabled) return panel("TrueNAS", `<div class="text-slate-500 text-sm">TrueNAS collection disabled.</div>`);
  const up = tn.up;
  const pools = tn.pools || [];
  const alerts = tn.alerts_active || [];
  const age = tsAge(col?.timestamp);
  let r = `<div class="flex items-center gap-2 mb-1">` +
    `<span class="drift-dot" style="background:${up ? "#4ade80" : "#f87171"}"></span>` +
    `<span class="text-sm ${up ? "text-green-300" : "text-red-300"}">${up ? "ONLINE" : "UNREACHABLE"}</span>` +
    `<span class="text-slate-400 text-xs ml-auto">${escapeHtml(tn.hostname)}</span></div>`;
  r += `<div class="text-xs text-slate-400 mb-2">${escapeHtml(tn.version || "—")}</div>`;
  // pools
  for (const p of pools) {
    const h = p.healthy ? "text-green-300" : "text-red-300";
    const dot = p.healthy ? "#4ade80" : "#f87171";
    r += `<div class="flex justify-between text-sm"><span class="text-slate-300">Pool ${escapeHtml(p.name)}</span>` +
      `<span class="${h}"><span class="drift-dot" style="background:${dot}"></span> ${escapeHtml(p.status)}</span></div>`;
    if (p.free || p.size) {
      const usedPct = (p.size && p.free) ? Math.round((1 - p.free / p.size) * 100) : null;
      r += `<div class="flex justify-between text-xs text-slate-400"><span>capacity</span><span>${usedPct ?? "—"}% used · ${fmtMb(p.free)} free of ${fmtMb(p.size)}</span></div>`;
    }
  }
  if (!pools.length) r += `<div class="text-xs text-slate-500">no pool data (${age === null ? "no timestamp" : age + "s ago"})</div>`;
  r += `<div class="flex justify-between text-xs text-slate-400 mt-1"><span>System</span>` +
    `<span>${escapeHtml(tn.model || "—")}</span></div>`;
  r += `<div class="flex justify-between text-xs text-slate-400"><span>RAM&nbsp;/&nbsp;uptime</span>` +
    `<span>${fmtMb(tn.physmem)} / ${ageDays(tn.uptime_seconds)}</span></div>`;
  // row: disks + alerts
  r += `<div class="flex justify-between text-xs text-slate-400"><span>Disks</span><span>${tn.disk_count ?? "—"}</span></div>`;
  const alertTone = alerts.length ? "text-red-300" : "text-slate-400";
  const alertDot = alerts.length ? "#f87171" : "#4ade80";
  r += `<div class="flex justify-between text-xs mt-1"><span class="text-slate-400">Active alerts</span>` +
    `<span class="${alertTone}"><span class="drift-dot" style="background:${alertDot}"></span> ${alerts.length ?? 0}</span></div>`;
  if (alerts.length) {
    r += `<ul class="mt-1 space-y-0.5 text-xs text-red-200/80">`;
    for (const a of alerts.slice(0, 3)) {
      r += `<li>• ${escapeHtml(a.formatted)}</li>`;
    }
    if (alerts.length > 3) r += `<li class="text-slate-500">+${alerts.length - 3} more</li>`;
    r += `</ul>`;
  }
  return panel("TrueNAS", `<div class="space-y-1">${r}</div>`);
}

function ageDays(seconds) {
  const n = num(seconds);
  if (n === null) return "—";
  const d = n / 86400;
  return d >= 1 ? d.toFixed(1) + " days" : (n / 3600).toFixed(1) + " h";
}

function renderGpuPanel(col) {
  const g = (col?.gpu?.gpus || [])[0] || {};
  const base = col?.gpu?.baseline || {};
  const vramPct = g.mem_total_mb ? ((g.mem_used_mb / g.mem_total_mb) * 100).toFixed(1) : "—";
  let b = `<div class="space-y-1 text-sm">`;
  b += `<div class="flex justify-between"><span class="text-slate-400">VRAM</span><span>${fmtMb(g.mem_used_mb)} / ${fmtMb(g.mem_total_mb)} (${vramPct}%)</span></div>`;
  b += `<div class="flex justify-between"><span class="text-slate-400">Power</span><span>${g.power_w ?? "—"} W</span></div>`;
  b += `<div class="flex justify-between"><span class="text-slate-400">Temp</span><span>${g.temp_c ?? "—"} °C</span></div>`;
  b += `<div class="flex justify-between"><span class="text-slate-400">Baseline VRAM</span><span>${fmtMb(base.last_vram)}</span></div>`;
  b += `<div class="flex justify-between"><span class="text-slate-400">Stuck PID</span><span>${escapeHtml(base.last_pid ?? "—")} (${base.cycles_with_same_pid ?? 0} cyc)</span></div>`;
  b += `</div>`;
  const flags = col?.gpu?.drift_flags || [];
  b += `<div class="mt-2 text-xs text-slate-400">Drift: ${flags.length ? flags.map(f => chip(f, driftTone(f))).join(" ") : chip("no drift", "ok")}</div>`;
  return panel("GPU Drift", b);
}

function renderDriftTimeline(col) {
  const h = state.history || {};
  const gpu = (col?.gpu?.gpus || [])[0] || {};
  const flags = col?.gpu?.drift_flags || [];
  const base = col?.gpu?.baseline || {};
  const critical = (flags || []).some(f => ["stuck_process", "vram_overload"].includes(f));
  const dotColor = critical ? "#f87171" : (flags.length ? "#facc15" : "#4ade80");
  const levelLabel = critical ? "drift" : (flags.length ? "mild drift" : "stable");

  let b = `<div class="flex items-center gap-2 mb-2 text-sm">` +
    `<span class="drift-dot" style="background:${dotColor}"></span>` +
    `<span class="text-slate-200 font-semibold">${levelLabel}</span>` +
    (flags.length ? flags.map(f => chip(f, driftTone(f))).join(" ") : "") + `</div>`;

  const series = [
    ["VRAM vs baseline", "sp-vram", h.gpu_vram || [], v => {
      const pct = gpu.mem_total_mb ? (v / gpu.mem_total_mb * 100) : 0;
      return pct > 90 ? "#f87171" : (pct > 70 ? "#facc15" : "#4ade80");
    }],
    ["Temperature", "sp-temp", h.gpu_temp || [], v => v > 70 ? "#f87171" : (v > 55 ? "#facc15" : "#4ade80")],
    ["Power draw", "sp-power", h.gpu_power || [], v => v > 40 ? "#f87171" : "#4ade80"],
  ];
  for (const [label, id, vals, colorFn] of series) {
    b += `<div class="mt-1 text-xs text-slate-400">${label}</div>${sparkHost(id)}`;
  }
  const cyc = base.cycles_with_same_pid ?? 0;
  b += `<div class="mt-2 text-xs text-slate-400">Stuck PID counter</div>`;
  b += `<div class="flex items-center gap-2"><span class="drift-dot" style="background:${cyc >= 5 ? "#f87171" : (cyc >= 3 ? "#facc15" : "#4ade80")}"></span><span class="mono text-sm">${cyc} cycles</span></div>`;
  return panel("GPU Drift Timeline", b);
}

function renderContainers(col) {
  const d = col?.docker || {};
  const containers = d.containers || [];
  const restarting = d.restarting || [];
  const capStr = restarting.length
    ? `<span class="chip bg-slate-700 crit">restarting: ${escapeHtml(restarting.join(", "))}</span>`
    : chip("no restart loop", "ok");

  const filtered = groupContainers(containers, groups, activeGroup);

  // Manual Stop Protection: protected containers (from WS snapshot)
  const ms = state.manual_stops || {};
  const msStops = ms.stops || {};
  const protectedCount = Object.keys(msStops).length;

  let b = `<div id="groupBtns" class="mb-2"></div>`;
  b += `<div class="mb-2 text-xs text-slate-400">Restart loop: ${capStr}</div>`;
  // Manual Stop Protection header
  b += `<div class="mb-2 text-xs flex flex-wrap gap-2 items-center">` +
    `<span>Manual Stop Protection: <b class="${protectedCount ? 'text-red-400' : 'text-green-400'}">${protectedCount ? 'ENABLED (' + protectedCount + ' protected)' : 'enabled · 0 protected'}</b></span>` +
    `</div>`;
  if (!filtered.length) b += `<div class="text-slate-500 text-sm">no containers in this group</div>`;
  else {
    const rows = filtered.slice(0, 40).map(c => {
      const st = c.stats || {};
      const cpu = num(st.cpu_percent), mem = num(st.mem_percent);
      const exited = c.state === "exited" ? "text-red-400" : "";
      // manual-stop protected badge
      let badge = "";
      if (c.manual_stop_protected || c.protected) {
        badge = ` <span class="chip bg-red-900 text-red-100" title="OpsBrain will not restart this container.">MANUALLY STOPPED</span>`;
      }
      return `<tr><td>${escapeHtml(c.name)}${badge}</td><td class="${exited}">${escapeHtml(c.state)}</td><td>${fmtPct(cpu)}</td>` +
             `<td>${fmtPct(mem)}</td><td>${c.restart_count ?? 0}</td></tr>`;
    }).join("");
    b += `<table><thead><tr class="text-slate-400 text-xs"><th>Container</th><th>State</th><th>CPU</th><th>RAM</th><th>Restarts</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  return panel("Container Health", b);
}

// ------------------------------------------------- Manual Stop Protection
function renderManualStopPanel() {
  const ms = state.manual_stops || {};
  const msStops = ms.stops || {};
  const names = Object.keys(msStops);
  // protected names resolved via /api/containers list is authoritative, but the
  // snapshot's manual_stops json names are good enough; prefer registry names.
  let status = "enabled";
  let b = `<div class="text-sm mb-2">Manual Stop Protection</div>`;
  b += `<div class="flex items-center gap-2 mb-2">` +
    `<span class="chip ${names.length ? 'bg-red-900 text-red-100' : 'bg-green-900 text-green-200'}">${status} · ${names.length} protected</span>` +
    `</div>`;
  if (!names.length) {
    b += `<div class="text-slate-500 text-sm">No manually-stopped containers are protected.</div>`;
  } else {
    b += `<ul class="text-sm text-slate-300 space-y-1">`;
    const now = Date.now();
    for (const cid of names) {
      const rec = msStops[cid] || {};
      const name = rec.name || cid;
      const stoppedAt = rec.stopped_at || rec.detected_at || "?";
      b += `<li>• <span class="text-red-300 font-semibold">${escapeHtml(name)}</span>` +
           `<span class="text-slate-400 text-xs"> (stopped ${escapeHtml(String(stoppedAt))})</span></li>`;
    }
    b += `</ul>`;
    b += `<div class="mt-2 text-xs text-slate-400">OpsBrain will not restart or prune these containers.</div>`;
  }
  return panel("Manual Stop Protection", b);
}

function renderConfidencePanel(reasoner) {
  const hist = state.history?.confidence || [];
  const conf = num(reasoner?.confidence);
  const color = confColor(conf);
  let b = `<div class="flex items-center gap-2 mb-2"><span class="drift-dot" style="background:${color}"></span>` +
    `<span class="mono text-lg" style="color:${color}">${conf === null ? "—" : conf.toFixed(2)}</span>` +
    `<span class="text-xs text-slate-400">(last ${clean(hist).length} cycles)</span></div>`;
  b += sparkHost("sp-conf");
  b += `<div class="mt-2 flex gap-3 text-xs">` +
    `<span><span class="drift-dot" style="background:#4ade80"></span> &gt;0.8</span>` +
    `<span><span class="drift-dot" style="background:#facc15"></span> 0.6–0.8</span>` +
    `<span><span class="drift-dot" style="background:#f87171"></span> &lt;0.6</span></div>`;
  return panel("Confidence Trend", b);
}

// ------------------------------------------------- confidence recovery
function renderConfidenceRecovery(reasoner) {
  const rec = state.confidence_recovery || {};
  const conf = num(reasoner?.confidence);
  const prev = num(rec.prev);
  const cur = num(rec.current);
  const detected = !!rec.detected && conf !== null;
  let b = `<div class="text-sm text-slate-300 mb-2">Confidence recovery</div>`;
  b += `<div class="flex items-center gap-2">`;
  b += `<span class="text-slate-400 text-xs">current</span>`;
  b += `<span id="confRecoveryNum" class="mono text-lg" style="color:${confColor(conf)}">${conf === null ? "—" : conf.toFixed(2)}</span>`;
  if (detected) {
    b += `<span class="recovery-tag">recovery</span>`;
    b += `<span class="text-green-400 text-xs">+${rec.delta.toFixed(2)}</span>`;
  } else {
    b += `<span class="text-xs text-slate-500">(steady / held)</span>`;
  }
  b += `</div>`;
  if (cur !== null && prev !== null) {
    b += `<div class="mt-2 text-xs text-slate-400">prev ${prev.toFixed(2)} → ${cur.toFixed(2)} (Δ${(cur - prev).toFixed(2)})</div>`;
  }
  return panel("Confidence Recovery", b);
}

// ------------------------------------------------- drift decay curve
function renderDriftDecay(col) {
  const decay = state.drift_decay || {};
  const vram = decay.vram || state.history?.gpu_vram || [];
  const temp = decay.temp || state.history?.gpu_temp || [];
  const power = decay.power || state.history?.gpu_power || [];
  const dst = decay.status || "ok";
  const cycles = decay.decay_cycles || 0;
  const statusColor = dst === "ok" ? "#4ade80" : (dst === "slow" ? "#facc15" : "#f87171");
  const statusLabel = dst === "ok" ? "decaying normally" : (dst === "slow" ? "slow decay" : "no decay");

  let b = `<div class="flex items-center gap-2 mb-1">` +
    `<span class="drift-dot" style="background:${statusColor}"></span>` +
    `<span class="decay-${dst} text-sm font-semibold">${statusLabel}</span>` +
    `<span class="text-xs text-slate-400">· ${cycles} cycles to baseline</span>` +
    `</div>`;
  b += `<div id="driftDecay"></div>`;
  return panel("Drift Decay Curve", b);
}

// ------------------------------------------------- restart impact
function renderRestartImpact() {
  return panel("Restart Impact", `<div id="restartImpact"></div>`);
}

function renderDecisions(reasoner, actions) {
  const conf = num(reasoner?.confidence);
  const warnings = reasoner?.warnings || [];
  const acts = reasoner?.actions || [];
  let b = `<div class="text-xs text-slate-400 mb-1">confidence <b style="color:${confColor(conf)}">${conf === null ? "—" : conf.toFixed(2)}</b> · ${actions?.dry_run ? chip("DRY RUN","warn") : chip("LIVE","ok")}</div>`;
  b += `<div class="mb-1 font-semibold text-slate-200 text-sm">Warnings (${warnings.length})</div>`;
  b += warnings.length ? `<ul class="list-disc pl-4 text-sm text-slate-300">${warnings.slice(0,10).map(w => `<li>${escapeHtml(typeof w === "string" ? w : JSON.stringify(w))}</li>`).join("")}</ul>` : `<div class="text-slate-500 text-sm">none</div>`;
  b += `<div class="mt-2 mb-1 font-semibold text-slate-200 text-sm">Actions (${acts.length})</div>`;
  if (acts.length)
    b += acts.slice(0, 10).map(a => { const at = a.type || a.action; return `<div class="text-sm"><span class="chip bg-slate-700">${escapeHtml(at)}</span> ${escapeHtml(a.target ?? "")} <span class="text-slate-400">${escapeHtml(a.reason ?? "")}</span></div>`; }).join("");
  else b += `<div class="text-slate-500 text-sm">none</div>`;
  return panel("OpsBrain Decisions", b);
}

function renderReport(reasoner, actions, collector) {
  const summary = reasoner?.summary || actions?.summary || "No summary yet.";
  const anomalies = (reasoner?.warnings || []).length + (collector?.gpu?.drift_flags || []).length;
  const rem = actions?.gpu_drift?.remediations || [];
  const driftEvents = actions?.gpu_drift?.events || [];
  let b = `<div class="text-sm text-slate-300 mb-2"><span class="text-slate-400">24h:</span> ${escapeHtml(summary)}</div>`;
  b += `<div class="flex gap-3 text-xs mb-2"><span class="text-slate-400">anomalies <b class="text-slate-100">${anomalies}</b></span><span class="text-slate-400">remediations <b class="text-slate-100">${rem.length}</b></span><span class="text-slate-400">drift events <b class="text-slate-100">${driftEvents.length}</b></span></div>`;
  if (rem.length) b += `<div class="text-xs text-slate-400">Remediation: ${rem.slice(0,8).map(r => `${escapeHtml(r?.verb)}:${escapeHtml(r?.target)}`).join(" · ")}</div>`;
  b += `<div class="mt-2 text-xs"><a class="text-slate-400 underline" href="/report">view report</a></div>`;
  return panel("Daily Report Preview", b);
}

function renderNotifications(n) {
  const fresh = n || [];
  if (!fresh.length) { notifBox.innerHTML = ""; notifCountEl.classList.add("hidden"); return; }
  const badge = { critical:'<span class="chip bg-red-900 text-red-100">critical</span>',
                  warning:'<span class="chip bg-yellow-900 text-yellow-100">warn</span>',
                  info:'<span class="chip bg-slate-700 text-slate-200">info</span>' };
  const items = fresh.slice(-8).reverse().map(x => {
    const lvl = (x.severity === "critical" || x.level === "critical") ? "critical"
              : (x.severity === "warning" || x.level === "warning") ? "warning" : "info";
    return `<div class="notif ${lvl}">${badge[lvl] || ""}<span>${escapeHtml(x.msg || x.message || x.category || "")}</span></div>`;
  }).join("");
  notifBox.innerHTML = `<div class="text-xs text-slate-500 mb-1">Live notifications</div>` + items;
  notifCountEl.textContent = `⚠ ${fresh.length}`;
  notifCountEl.classList.remove("hidden");
}

// ------------------------------------------------------------- render
function render() {
  const collector = state.collector || {};
  const reasoner = state.reasoner || {};
  const actions = state.actions || {};
  const cards = [
    renderSystem(collector),
    renderClusterOverview(),
    renderNodeComparison(),
    renderTruenas(collector),
    renderManualStopPanel(),
    renderGpuPanel(collector),
    renderDriftTimeline(collector),
    renderConfidencePanel(reasoner),
    renderConfidenceRecovery(reasoner),
    renderDriftDecay(collector),
    renderRestartImpact(),
    renderContainers(collector),
    renderDecisions(reasoner, actions),
    renderReport(reasoner, actions, collector),
  ];
  grid.innerHTML = cards.join("");
  renderGroupButtons($("groupBtns"), groups, (sel) => { activeGroup = sel; render(); }, state.collector?.docker?.containers || []);
  // deferred sparklines fill the placeholders
  const h = state.history || {};
  const gpu = collector?.gpu?.gpus?.[0] || {};
  drawSpark("sp-vram", h.gpu_vram, { width: 170, height: 26, fill: true, color: v => { const pct = gpu.mem_total_mb ? (v / gpu.mem_total_mb * 100) : 0; return pct > 90 ? "#f87171" : (pct > 70 ? "#facc15" : "#4ade80"); } });
  drawSpark("sp-temp", h.gpu_temp, { width: 170, height: 26, fill: true, color: v => v > 70 ? "#f87171" : (v > 55 ? "#facc15" : "#4ade80") });
  drawSpark("sp-power", h.gpu_power, { width: 170, height: 26, fill: true, color: v => v > 40 ? "#f87171" : "#4ade80" });
  drawSpark("sp-conf", h.confidence, { width: 220, height: 26, fill: true, color: confColor });

  // --- refinements: recovery pulse, drift decay graph, restart impact bars ---
  const rec = state.confidence_recovery || {};
  const confVal = num(reasoner?.confidence);
  if (confVal !== null && rec.detected) {
    const nEl = document.getElementById("confRecoveryNum");
    if (nEl) {
      nEl.classList.add("pulse-green");
      setTimeout(() => nEl.classList.remove("pulse-green"), 2000);
    }
  }
  const decay = state.drift_decay || {};
  driftDecayGraph("driftDecay", decay.vram || h.gpu_vram || [],
                  decay.temp || h.gpu_temp || [], decay.power || h.gpu_power || [],
                  h.gpu_baseline_vram?.slice(-1)?.[0] ?? 0, 55, 40);
  restartImpactGraphs(state.restart_impact);

  renderNotifications(state.notifications);
  mtimeEl.textContent = state.collector?.timestamp ? `collector @ ${state.collector.timestamp}` : "";
}

// ------------------------------------------------------------- notifications
function browserNotify(title, body) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") new Notification(title, { body });
  else if (Notification.permission === "default") Notification.requestPermission();
}
function handleNewNotifications(n) {
  (n || []).forEach(x => {
    const key = `${x.type || ""}:${x.msg || x.category || ""}`;
    if (!browserNotified[key]) { browserNotified[key] = true; browserNotify("OpsBrain alert", x.msg || x.category || ""); }
  });
}

// ------------------------------------------------------------- groups
async function loadGroups() {
  try { groups = (await (await fetch("/api/groups")).json()) || []; }
  catch (e) { groups = []; }
}

// ------------------------------------------------------------- websocket
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/stream`);
  connEl.textContent = "connecting…";
  connEl.className = "chip bg-slate-700 text-slate-300";
  ws.onopen = () => { connEl.textContent = "live"; connEl.className = "chip bg-green-700 text-white"; };
  ws.onmessage = (ev) => {
    try {
      const prevN = (state.notifications || []).length;
      state = JSON.parse(ev.data);
      render();
      const curN = state.notifications || [];
      if (curN.length > prevN) handleNewNotifications(curN.slice(prevN));
    } catch (e) { console.error("parse", e); }
  };
  ws.onclose = () => { connEl.textContent = "reconnecting"; connEl.className = "chip bg-red-700 text-white"; setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
}

loadGroups();
connect();