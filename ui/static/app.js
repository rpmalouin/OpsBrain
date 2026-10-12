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
// Nodes are not all container hosts: a node with a `host` block (a producer-sourced node such as the
// Mac) reports OS/chip/RAM instead of containers. Render that as sub-lines, not as new columns, so the
// table shape stays stable for every node kind.
function nodeHostLine(n) {
  const h = n.host || {};
  const parts = [];
  if (h.os) parts.push(h.os);
  if (h.chip_name) parts.push(h.chip_name);
  if (h.cpu) parts.push(h.cpu);
  if (h.cores !== undefined && h.cores !== null) parts.push(h.cores + "c");
  if (h.ram_total_gb) parts.push(h.ram_total_gb + " GB");
  // Two memory numbers, on purpose. "RAM x%" is Activity Monitor's Memory Used, which counts
  // reclaimable inactive pages, so 87% on a machine holding a resident model is its NORMAL state.
  // "avail y%" is the pressure figure — the one macmon's own menu bar judges — and the only one that
  // ever gets tone: below 10% worth a look, below 5% bad. Only the tone marks are raw HTML; the rest
  // goes through escapeHtml.
  const ram = num(h.ram_used_percent);
  if (ram !== null) parts.push("RAM " + ram.toFixed(0) + "%");
  // name the worst watched volume, not just its number: "82%" alone never says WHICH disk to clean
  const vols = (n.volumes && typeof n.volumes === "object") ? n.volumes : null;
  if (vols) {
    let worstPath = null, worstPct = -1;
    for (const [path, pct] of Object.entries(vols)) {
      const v = num(pct);
      if (v !== null && v > worstPct) { worstPct = v; worstPath = path; }
    }
    if (worstPath !== null) parts.push("worst disk " + worstPath + " " + worstPct.toFixed(0) + "%");
  } else {
    const disk = num(n.disk_used_percent);
    if (disk !== null) parts.push("worst disk " + disk.toFixed(0) + "%");
  }
  const base = parts.length ? escapeHtml(parts.join(" · ")) : "";
  const avail = num(h.ram_available_percent);
  if (avail === null) return base;
  const pressure = String(h.memory_pressure || "");
  const label = "avail " + avail.toFixed(0) + "%";
  const tone = pressure === "heavy" ? "text-red-300" : pressure === "moderate" ? "text-amber-300" : "";
  if (!tone) return (base ? base + " · " : "") + escapeHtml(label);
  return (base ? base + " · " : "") + `<span class="${tone}">${escapeHtml(label)}</span>`;
}

// The node's own SoC telemetry (macmon), when its producer supplies it: temperatures, power, the
// per-tier CPU aggregate and the fans. Per-core rows stay out of this table on purpose — eighteen of
// them would swamp a two-node comparison; the fixture and the reasoner digest keep that detail.
function nodeMacmonLine(n) {
  const h = n.host || {};
  const bits = [];
  const t = h.temp || {};
  const cpuT = num(t.cpu_c), gpuT = num(t.gpu_c);
  if (cpuT !== null || gpuT !== null) {
    bits.push("cpu " + (cpuT === null ? "—" : cpuT.toFixed(0) + "°C") + " / gpu " + (gpuT === null ? "—" : gpuT.toFixed(0) + "°C"));
  }
  const p = h.power_w || {};
  const cpuW = num(p.cpu_w), gpuW = num(p.gpu_w);
  if (cpuW !== null || gpuW !== null) {
    bits.push("power " + (cpuW === null ? "—" : cpuW.toFixed(1)) + " + " + (gpuW === null ? "—" : gpuW.toFixed(1)) + " W");
  }
  const cpu = h.cpu || {};
  const tiers = (Array.isArray(cpu.tiers) ? cpu.tiers : []).filter(x => x && num(x.scaled_ratio) !== null);
  if (tiers.length) {
    bits.push(tiers.map(x => (x.label || "?") + " " + (x.cores === null || x.cores === undefined ? "" : x.cores + "c ") + (num(x.scaled_ratio) * 100).toFixed(0) + "%").join(" · "));
  }
  const fans = (Array.isArray(h.fans) ? h.fans : []).filter(f => f && num(f.rpm) !== null);
  if (fans.length) {
    const stopped = fans.some(f => num(f.rpm) === 0);
    bits.push("fan " + fans.map(f => (f.name || "fan") + " " + Math.round(num(f.rpm)) + "rpm").join(" · ") + (stopped ? " STOPPED" : ""));
  }
  if (!bits.length) return "";
  return escapeHtml("macmon " + bits.join(" · "));
}

// A hard fault on a non-container node is worth a colour: a stopped fan is unambiguous, and memory
// pressure is the one threshold macmon's own menu bar applies.
function nodeFaultTone(n) {
  const h = n.host || {};
  const fans = Array.isArray(h.fans) ? h.fans : [];
  if (fans.some(f => f && num(f.rpm) === 0)) return "text-red-300";
  const pressure = String(h.memory_pressure || "");
  if (pressure === "heavy") return "text-red-300";
  if (pressure === "moderate") return "text-amber-300";
  return "text-slate-500";
}

// Every watched volume by name: the host line names only the worst, and "which disk" is the question a
// near-full volume actually raises.
function nodeVolumesLine(n) {
  const vols = (n.volumes && typeof n.volumes === "object") ? n.volumes : null;
  if (!vols) return "";
  const rows = Object.entries(vols)
    .map(([path, pct]) => [path, num(pct)])
    .filter(row => row[1] !== null)
    .sort((a, b) => b[1] - a[1]);
  if (!rows.length) return "";
  return escapeHtml("volumes " + rows.map(row => row[0] + " " + row[1].toFixed(0) + "%").join(" · "));
}

// A node's own producer snapshot carries what no poll can see; show its freshness, the file it came
// from, and EVERY finding — because a "stale"/"missing" producer is itself worth seeing on the board,
// and one alert plus a hidden remainder is how a second problem goes unnoticed.
function nodeSnapshotLine(n) {
  const s = n.snapshot || {};
  if (!s.state) return "";
  const bits = [s.state];
  const age = num(s.age_s);
  if (age !== null) bits.push(Math.round(age / 60) + "m old");
  const tm = s.time_machine && num(s.time_machine.latest_backup_age_hours);
  if (tm !== null) bits.push("TM " + tm.toFixed(1) + "h");
  const gw = s.gateway && num(s.gateway.age_seconds);
  if (gw !== null) bits.push("gw " + Math.round(gw) + "s");
  const mounts = Array.isArray(s.mounts) ? s.mounts : [];
  if (mounts.length) {
    const mounted = mounts.filter(m => m && m.mounted).length;
    bits.push("mounts " + mounted + "/" + mounts.length);
  }
  const cron = s.hermes_cron || {};
  if (cron.jobs_total !== undefined && cron.jobs_total !== null) {
    const failing = Array.isArray(cron.jobs_failing) ? cron.jobs_failing.length : 0;
    bits.push("cron " + failing + "/" + cron.jobs_total + " failing");
  }
  if (s.soc_available === false) bits.push("macmon unavailable");
  let html = escapeHtml("snapshot " + bits.join(" · "));
  if (s.source) html += escapeHtml(" · source " + s.source);
  const alarms = []
    .concat(Array.isArray(s.derived_alerts) ? s.derived_alerts : [])
    .concat(Array.isArray(s.alerts) ? s.alerts : []);
  if (alarms.length) {
    html += `<ul class="mt-0.5 space-y-0.5">`;
    for (const a of alarms.slice(0, 3)) html += `<li>• ${escapeHtml(String(a))}</li>`;
    if (alarms.length > 3) html += `<li class="text-slate-500">+${alarms.length - 3} more</li>`;
    html += `</ul>`;
  }
  return html;
}

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
    const hostLine = nodeHostLine(n);
    const macLine = nodeMacmonLine(n);
    const volLine = nodeVolumesLine(n);
    const snapLine = nodeSnapshotLine(n);
    rows += `<tr class="border-t border-slate-800">` +
      `<td class="py-1"><span class="drift-dot" style="background:${dot}"></span> ${escapeHtml(name)} <span class="text-xs text-slate-500">(${escapeHtml(n.type || "?")})</span>` +
      (hostLine ? `<div class="text-xs text-slate-500">${hostLine}</div>` : "") +
      (macLine ? `<div class="text-xs ${nodeFaultTone(n)}">${macLine}</div>` : "") +
      (volLine ? `<div class="text-xs text-slate-500">${volLine}</div>` : "") +
      (snapLine ? `<div class="text-xs text-slate-500">${snapLine}</div>` : "") +
      `</td>` +
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

function renderDockhandPanel(col) {
  const dh = col?.dockhand || {};
  if (!dh.enabled) return panel("Dockhand", `<div class=\"text-slate-500 text-sm\">Dockhand ingestion disabled.</div>`);
  if (!dh.up) return panel("Dockhand", `<div class=\"text-slate-500 text-sm\">Dockhand DB unreachable (docker hand desired-state source down).</div>`);
  const cl = dh.classify || {};
  const corr = dh.correlate || {};
  const dash = dh.dashboard || {};
  const ctx = dh.context_nodes || {};
  const driftCount = cl.drift_count || 0;
  const tone = driftCount > 0 ? "#f87171" : "#4ade80";
  const toneText = driftCount > 0 ? "text-red-300" : "text-green-300";
  let b = `<div class=\"flex items-center gap-2 mb-1\">` +
    `<span class=\"drift-dot\" style=\"background:${tone}\"></span>` +
    `<span class=\"text-sm ${toneText}\">${driftCount} drift</span>` +
    `<span class=\"text-slate-400 text-xs ml-auto\">${ctx.attention || "normal"}</span></div>`;
  b += `<div class=\"text-xs text-slate-400 mb-2\">${escapeHtml(ctx.drift_summary || "no drift")}</div>`;

  const storms = corr.restart_storms || [];
  const flaps = corr.health_flaps || [];
  const orphans = dash.orphaned_containers || [];
  const miss = dash.missing_resources || [];

  const row = (label, list) => list && list.length
    ? `<div class=\"flex justify-between text-sm\"><span class=\"text-slate-300\">${label}</span>` +
      `<span class=\"text-red-300\">${list.length}</span></div>`
    : `<div class=\"flex justify-between text-sm\"><span class=\"text-slate-400\">${label}</span>` +
      `<span class=\"text-slate-500\">0</span></div>`;
  b += `<div class=\"space-y-0.5 text-xs\">` +
    row("Stacks drifting", dash.stack_drift) +
    row("Restart storms", storms) +
    row("Health flaps", flaps) +
    row("Orphaned containers", orphans) +
    row("Missing resources", miss) +
    `</div>`;

  if (storms.length) {
    b += `<ul class=\"mt-1 space-y-0.5 text-xs text-red-200/80\">`;
    for (const s of storms.slice(0, 4)) b += `<li>• ${escapeHtml(s.service)} · ${s.count} restarts / ${s.window_min}m</li>`;
    b += `</ul>`;
  }
  if (flaps.length) {
    b += `<ul class=\"mt-1 space-y-0.5 text-xs text-amber-200/80\">`;
    for (const f of flaps.slice(0, 3)) b += `<li>• ${escapeHtml(f.service)} · ${f.toggle_count} toggles / ${f.window_min}m</li>`;
    b += `</ul>`;
  }
  if (orphans.length) {
    b += `<ul class=\"mt-1 space-y-0.5 text-xs text-slate-400\">`;
    for (const o of orphans.slice(0, 5)) b += `<li>• ${escapeHtml(o)}</li>`;
    b += `</ul>`;
  }
  return panel("Dockhand", `<div class=\"space-y-1\">${b}</div>`);
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
  // Driver/library mismatch: the apt hook repairs it after an nvidia upgrade UNLESS a GPU
  // container holds the device — then it defers and the GPU reads as invisible here. Say why.
  const grc = col?.gpu_reconcile?.context_nodes || {};
  if (grc.attention === "actionable") {
    b += `<div class="mt-2 notif critical">GPU driver mismatch: loaded ${escapeHtml(grc.loaded ?? "?")} vs disk ${escapeHtml(grc.disk ?? "?")}` +
         (grc.last_event_kind === "deferred" ? " — the apt hook DEFERRED the reload" : "") +
         (grc.remedy ? `<br>${escapeHtml(grc.remedy)}` : "") + `</div>`;
  } else if (grc.attention === "low") {
    b += `<div class="mt-2 notif info">GPU driver in sync${grc.loaded ? ` (${escapeHtml(grc.loaded)})` : ""}; last hook event: ${escapeHtml(grc.last_event_kind || "none")}</div>`;
  }
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

function notifLevel(x) {
  // Accepts "crit"/"critical" and "warn"/"warning": the server has written both forms and
  // anything unrecognised fell through to the calmest chip (a low-confidence alert showed
  // as info). Normalising here keeps the panel correct even against an older payload.
  const s = String(x.severity || x.level || "").toLowerCase();
  if (s.startsWith("crit")) return "critical";
  if (s.startsWith("warn")) return "warning";
  return "info";
}

function notifTime(x) {
  if (!x.ts) return "";
  const d = new Date(x.ts);
  return isNaN(d.getTime()) ? "" : `${d.toISOString().slice(11, 16)}Z `;
}

function renderNotifications(n) {
  const fresh = n || [];
  if (!fresh.length) { notifBox.innerHTML = ""; notifCountEl.classList.add("hidden"); return; }
  const badge = { critical:'<span class="chip bg-red-900 text-red-100">critical</span>',
                  warning:'<span class="chip bg-yellow-900 text-yellow-100">warn</span>',
                  info:'<span class="chip bg-slate-700 text-slate-200">info</span>' };
  const items = fresh.slice(-8).reverse().map(x => {
    const lvl = notifLevel(x);
    return `<div class="notif ${lvl}">${badge[lvl] || ""}<span class="text-slate-500">${escapeHtml(notifTime(x))}</span><span>${escapeHtml(x.msg || x.message || x.category || "")}</span></div>`;
  }).join("");
  notifBox.innerHTML = `<div class="text-xs text-slate-500 mb-1">Live notifications</div>` + items;
  // The chip counts what is WRONG, not how long the in-memory list is: it used to print the
  // raw backlog length (⚠ 50) — a number that never went down and meant nothing, because the
  // panel was showing replayed history rather than anything live.
  const crit = fresh.filter(x => notifLevel(x) === "critical").length;
  const warn = fresh.filter(x => notifLevel(x) === "warning").length;
  notifCountEl.classList.remove("hidden");
  if (crit) { notifCountEl.className = "chip bg-red-800 text-red-100"; notifCountEl.textContent = `⚠ ${crit} critical`; }
  else if (warn) { notifCountEl.className = "chip bg-yellow-800 text-yellow-100"; notifCountEl.textContent = `${warn} warn`; }
  else { notifCountEl.classList.add("hidden"); }
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
    renderDockhandPanel(collector),
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
  // Dedupe by content and scan the whole list every tick: the payload is the last 50 of a
  // 100-entry in-process ring, so the old "curN.length > prevN" diff stopped firing as soon
  // as the ring saturated and desktop alerts died silently.
  (n || []).forEach(x => {
    const msg = x.msg || x.message || x.category || "";
    const key = `${x.type || ""}:${msg}`;
    if (msg && !browserNotified[key]) { browserNotified[key] = true; browserNotify("OpsBrain alert", msg); }
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
      state = JSON.parse(ev.data);
      render();
      handleNewNotifications(state.notifications || []);
    } catch (e) { console.error("parse", e); }
  };
  ws.onclose = () => { connEl.textContent = "reconnecting"; connEl.className = "chip bg-red-700 text-white"; setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
}

loadGroups();
connect();