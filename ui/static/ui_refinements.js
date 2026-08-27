/* OpsBrain UI refinements — vanilla JS + plain CSS, no deps.
   Companion to app.js / dsh_modules.js (inline-SVG style).
   Provides: confidence recovery pulse, drift decay graph, restart impact bars. */
"use strict";

/* ============================================================
 * F1 | confRecoveryPulse(prevConf, curConf, containerId)
 * Pulse + "recovery" tag when confidence strictly increased.
 * ============================================================ */
function confRecoveryPulse(prevConf, curConf, containerId) {
  const el = document.getElementById(containerId);
  if (!el) return false;
  const prev = Number(prevConf);
  const cur = Number(curConf);
  if (!isFinite(prev) || !isFinite(cur)) return false;
  if (!(cur > prev)) return false;

  // remove any stale tag first
  const old = el.querySelector(".recovery-tag");
  if (old) old.remove();

  const tag = document.createElement("span");
  tag.className = "recovery-tag";
  tag.textContent = "recovery";
  el.appendChild(tag);

  const numEl = el.querySelector(".mono.text-lg") || el;
  numEl.classList.add("pulse-green");
  setTimeout(() => numEl.classList.remove("pulse-green"), 2000);
  return true;
}

/* ============================================================
 * F2 | driftDecayGraph(id, vram[], temp[], power[], bases...)
 * 3-series inline-SVG line chart with per-series decay status
 * dots (green decaying / yellow slow / red persistent).
 * ============================================================ */
function driftDecayGraph(id, vram, temp, power, vramBase, tempBase, powerBase) {
  const host = document.getElementById(id);
  if (!host) return;
  const NS = "http://www.w3.org/2000/svg";
  const el = (name, attrs) => {
    const e = document.createElementNS(NS, name);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };
  const clean = (a) => (Array.isArray(a) ? a : []).filter(x => typeof x === "number" && isFinite(x));

  const series = [
    { name: "VRAM", data: clean(vram).slice(-10), base: Number(vramBase) || 0, color: "#3b82f6" },
    { name: "Temp", data: clean(temp).slice(-10), base: Number(tempBase) || 0, color: "#f97316" },
    { name: "Power", data: clean(power).slice(-10), base: Number(powerBase) || 0, color: "#a78bfa" },
  ];
  const present = series.filter(s => s.data.length);
  if (!present.length) {
    host.innerHTML = `<div class="text-xs text-slate-500">no drift history</div>`;
    return;
  }

  const W = 220, H = 64, pad = 3;
  const cap = Math.min(Math.max.apply(null, present.map(s => s.data.length)), 10);
  let min = Infinity, max = -Infinity;
  present.forEach(s => s.data.forEach(v => { min = Math.min(min, v); max = Math.max(max, v); }));
  if (!(max > min)) { max = min + 1; }
  const X = i => (cap === 1 ? W / 2 : pad + (i / (cap - 1)) * (W - pad * 2));
  const Y = v => H - pad - ((v - min) / (max - min)) * (H - pad * 2);

  const statusOf = (s) => {
    if (!s.data.length) return "ok";
    const last = s.data[s.data.length - 1];
    const peak = Math.max.apply(null, s.data);
    const base = s.base;
    if (base > 0 && last <= base * 1.05) return "ok";            // within 5% of baseline
    if (base > 0 && last > base * 1.05 && last < peak * 0.8) return "slow"; // >20% off peak
    if (peak === 0 || last === peak) return "bad";
    return "bad";
  };
  const dots = { ok: "#4ade80", slow: "#facc15", bad: "#f87171" };

  const svg = el("svg", {
    viewBox: `0 0 ${W} ${H}`, width: W, height: H,
    preserveAspectRatio: "none", role: "img", "aria-label": "drift decay"
  });
  svg.style.display = "block";
  svg.style.overflow = "visible";

  present.forEach(s => {
    const pts = s.data.map((v, i) => `${X(i).toFixed(2)},${Y(v).toFixed(2)}`);
    const line = el("polyline", { fill: "none", stroke: s.color, "stroke-width": 1.5, points: pts.join(" ") });
    svg.appendChild(line);
    // baseline markers
    if (s.base > 0) {
      const bl = el("line", { x1: pad, y1: Y(s.base), x2: W - pad, y2: Y(s.base),
        stroke: s.color, "stroke-width": 0.6, "stroke-dasharray": "3 3", opacity: 0.5 });
      svg.appendChild(bl);
    }
  });

  let legend = "";
  present.forEach(s => {
    const st = statusOf(s);
    legend += `<span class="decay-${st}" title="${s.name}">` +
      `<span class="drift-dot" style="background:${dots[st]}"></span>${s.name}</span>`;
  });

  host.innerHTML = "";
  host.appendChild(svg);
  const leg = document.createElement("div");
  leg.className = "decay-legend text-xs";
  leg.innerHTML = legend;
  host.appendChild(leg);
}

/* ============================================================
 * F3 — restartImpactGraphs(impacts)
 * Horizontal bars of confidence delta after container restarts.
 * ============================================================ */
function restartImpactGraphs(impacts) {
  const host = document.getElementById("restartImpact");
  if (!host) return;
  const list = (Array.isArray(impacts) ? impacts : []).slice(0, 12);
  if (!list.length) {
    host.innerHTML = `<div class="text-xs text-slate-500">no restart data yet</div>`;
    return;
  }
  const toneOf = sc => sc > 0.05 ? "good" : (sc >= 0 ? "slow" : "bad");
  const pct = sc => Math.min(100, Math.max(4, Math.round(Math.abs(sc) * 100)));
  const esc = s => String(s ?? "").replace(/[<>&"]/g, c => ({ "<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;" }[c]));
  const rows = list.map(it => {
    const score = Number(it.score) || 0;
    const label = score > 0 ? "+" + score.toFixed(2) : score.toFixed(2);
    const cls = `impact-bar impact-${toneOf(score)}`;
    return `<div class="impact-row">` +
      `<span class="impact-name">${esc(it.container || it.name || "?")}</span>` +
      `<div class="impact-track"><div class="${cls}" style="width:${pct(score)}%"></div></div>` +
      `<span class="impact-score impact-tone-${toneOf(score)}">${label}</span>` +
      `</div>`;
  }).join("");
  host.innerHTML = `<div class="text-xs text-slate-400 mb-1">Confidence delta after restart</div>` + rows;
}

/* ============================================================
 * CSS BLOCK — recovery pill, confidence pulse, decay tints,
 * impact bars. Inline here as a string for injection.
 * ============================================================ */
const OPSBRAIN_REFINE_CSS = `
.recovery-tag {
  display: inline-block; margin-left: 6px; padding: 1px 8px;
  border-radius: 9999px; background: #14532d; color: #bbf7d0;
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
}
.pulse-green { animation: pulseGreen 2s ease-in-out; }
@keyframes pulseGreen {
  0%   { box-shadow: 0 0 0 0 rgba(74,222,128,0.75); }
  50%  { box-shadow: 0 0 0 8px rgba(74,222,128,0); }
  100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); }
}
.decay-ok   { color: #4ade80; }
.decay-slow { color: #facc15; }
.decay-bad  { color: #f87171; }
.decay-legend span { display: inline-flex; align-items: center; gap: 4px; margin-right: 10px; }
.impact-row     { display: flex; align-items: center; gap: 8px; margin: 3px 0; }
.impact-name    { width: 110px; flex: 0 0 auto; text-align: right; color: #cbd5e1; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.impact-track   { flex: 1 1 auto; height: 14px; border-radius: 4px; background: rgba(148,163,184,.18); overflow: hidden; }
.impact-score   { width: 52px; flex: 0 0 auto; text-align: left; font-size: 12px; font-variant-numeric: tabular-nums; }
.impact-bar     { height: 100%; border-radius: 4px; transition: width .4s ease; }
.impact-good    { background: linear-gradient(90deg, #166534, #4ade80); }
.impact-slow    { background: linear-gradient(90deg, #854d0e, #facc15); }
.impact-bad     { background: linear-gradient(90deg, #7f1d1d, #f87171); }
.impact-tone-good { color: #4ade80; }
.impact-tone-slow { color: #facc15; }
.impact-tone-bad  { color: #f87171; }
`;

// Inject once on load
(function() {
  if (document.getElementById("refine-css")) return;
  const s = document.createElement("style");
  s.id = "refine-css";
  s.textContent = OPSBRAIN_REFINE_CSS;
  document.head.appendChild(s);
})();