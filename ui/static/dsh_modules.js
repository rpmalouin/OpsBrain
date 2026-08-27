/* ============================================================
 * ui_modules.js
 * Pure vanilla JS + inline-SVG dashboard helpers. No frameworks.
 * Two standalone modules, separated by banner comments.
 * ============================================================ */


/* ============================================================
 *  MODULE 1  |  sparklines.js
 *
 *  sparkline(canvasId, values, opts)
 *    Draws an inline SVG sparkline into the element with
 *    id = canvasId. Returns nothing (undefined).
 *
 *  opts:
 *    width  : number                      (default 120) viewBox width
 *    height : number                      (default 28)  viewBox height
 *    color  : string | function(v,i)      fixed color, or per-value color fn
 *    fill   : boolean                     draw translucent area (default true)
 *    min    : number                      explicit y-min (default = data min)
 *    max    : number                      explicit y-max (default = data max)
 *    thresholdMid  : number               yellow <= x < high
 *    thresholdHigh : number               green when x >= high
 *
 *  Color rules:
 *    - color is a string        -> used for every segment.
 *    - color is a function      -> called as color(value, index) and its
 *                                  return is used for that segment.
 *    - color missing/undefined  -> value is auto-classified as follows:
 *          value >= thresholdHigh  -> green
 *          value >= thresholdMid   -> yellow
 *          otherwise               -> red
 *      If neither threshold is present the classification is disabled and
 *      a default green is used.
 *
 *  Empty / single-value input is handled: an empty array produces an empty
 *  <svg>, a single value produces a single dot.
 * ============================================================ */

function sparkline(canvasId, values, opts) {
  opts = opts || {};
  var host = document.getElementById(canvasId);
  if (!host) return;

  var W = opts.width != null ? opts.width : 120;
  var H = opts.height != null ? opts.height : 28;
  var wantFill = opts.fill !== false;
  var data = Array.isArray(values)
    ? values.filter(function (v) { return typeof v === 'number' && isFinite(v); })
    : [];

  var NS = 'http://www.w3.org/2000/svg';
  function el(name, attrs) {
    var e = document.createElementNS(NS, name);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  var svg = el('svg', {
    viewBox: '0 0 ' + W + ' ' + H,
    width: W,
    height: H,
    preserveAspectRatio: 'none',
    role: 'img',
    'aria-label': 'sparkline'
  });
  svg.style.display = 'block';
  svg.style.overflow = 'visible';
  host.appendChild(svg);

  if (data.length === 0) return;

  var GREEN = '#2e7d32';
  var YELLOW = '#f9a825';
  var RED = '#c62828';

  var high = opts.thresholdHigh != null ? opts.thresholdHigh : null;
  var mid = opts.thresholdMid != null ? opts.thresholdMid : null;

  function classify(v) {
    if (high == null && mid == null) return null; // no thresholds -> no autoclass
    var g = high != null ? high : mid;
    var y = mid != null ? mid : high;
    if (v >= g) return GREEN;
    if (v >= y) return YELLOW;
    return RED;
  }

  function colorFor(v, i) {
    if (typeof opts.color === 'function') {
      var c = opts.color(v, i);
      if (typeof c === 'string' && c) return c;
    } else if (typeof opts.color === 'string' && opts.color) {
      return opts.color;
    }
    var auto = classify(v);
    return auto || GREEN;
  }

  var min = opts.min != null ? opts.min : Math.min.apply(null, data);
  var max = opts.max != null ? opts.max : Math.max.apply(null, data);
  if (!(max > min)) { // flat or single-value range
    var span = max - min;
    max = min === 0 ? min + 1 : min + (Math.max(Math.abs(max), 1));
    min = max - (span === 0 ? (max || 1) : span);
  }

  var n = data.length;
  var pad = 1;
  var denom = max - min || 1;

  function X(i) { // horizontal position of point i
    if (n === 1) return W / 2;
    return pad + (i / (n - 1)) * (W - pad * 2);
  }
  function Y(v) {
    return H - pad - ((v - min) / denom) * (H - pad * 2);
  }

  // Build the polyline point list.
  var pts = [];
  for (var i = 0; i < n; i++) pts.push([X(i), Y(data[i])]);

  // Per-segment stroke, each segment coloured by its later value.
  for (i = 0; i < n - 1; i++) {
    var p0 = pts[i], p1 = pts[i + 1];
    svg.appendChild(el('line', {
      x1: p0[0], y1: p0[1], x2: p1[0], y2: p1[1],
      stroke: colorFor(data[i + 1], i + 1),
      'stroke-width': 1.4,
      'stroke-linecap': 'round',
      fill: 'none'
    }));
  }

  // Fill area under the line (single translucent polygon).
  if (wantFill) {
    var fillColor = colorFor(data[n - 1], n - 1);
    var areaD = 'M' + pts[0][0] + ',' + pts[0][1];
    for (i = 1; i < n; i++) areaD += ' L' + pts[i][0] + ',' + pts[i][1];
    areaD += ' L' + pts[n - 1][0] + ',' + H + ' L' + pts[0][0] + ',' + H + ' Z';
    svg.appendChild(el('path', {
      d: areaD, fill: fillColor, 'fill-opacity': 0.22, stroke: 'none'
    }));
  }

  // A vertical dot for a lone point.
  if (n === 1) {
    svg.appendChild(el('circle', {
      cx: pts[0][0], cy: pts[0][1], r: 2.2,
      fill: colorFor(data[0], 0), stroke: 'none'
    }));
  }
}


/* ============================================================
 *  MODULE 2  |  groups.js
 *
 *  Group definitions: a plain object mapping a group name to an
 *  array of matchers. Each matcher is either:
 *    - a string, treated as a name SUBSTRING,
 *    - a string of the form "/<expr>/<flags>", treated as a RegExp,
 *    - a RegExp instance.
 *
 *  A container is identified by its name (c.name, c.displayName, or the
 *  first entry of c.Names) and counts as "running" when it carries
 *  running===true / Running===true or state==="running".
 *
 *  A container may match several groups. "misc" = a running container
 *  that matches no group at all. "all" = every running container.
 *
 *  groupContainers(containers, groups, activeGroup)
 *    -> array of containers for the active group ("all" -> all running).
 *
 *  renderGroupButtons(containerEl, groups, onSelect, containers?)
 *    Builds a row of <button> filters ("All", each group, "Misc"), each
 *    with a count badge. Clicking a button sets it active and calls
 *    onSelect(activeGroup). `containers` (optional, for the counts) may be
 *    passed as a 4th arg or attached to containerEl as `.containers`.
 *    Returns nothing.
 * ============================================================ */

function isRunning(c) {
  if (!c) return false;
  if (c.running === true || c.Running === true) return true;
  return typeof c.state === 'string' && c.state.toLowerCase() === 'running';
}

function containerName(c) {
  if (!c) return '';
  if (typeof c.name === 'string') return c.name;
  if (typeof c.displayName === 'string') return c.displayName;
  if (Array.isArray(c.Names) && c.Names.length && typeof c.Names[0] === 'string') return c.Names[0];
  return '';
}

function matcher(entry) {
  if (entry instanceof RegExp) {
    var re = entry;
    return function (name) { re.lastIndex = 0; return re.test(name); };
  }
  if (typeof entry === 'string') {
    var m = /^\/(.*)\/([a-z]*)$/.exec(entry);
    if (m) {
      var re = new RegExp(m[1], m[2]);
      return function (name) { re.lastIndex = 0; return re.test(name); };
    }
    var sub = entry;
    return function (name) { return name.indexOf(sub) !== -1; };
  }
  return function () { return false; };
}

function runningContainers(containers) {
  if (!Array.isArray(containers)) return [];
  return containers.filter(isRunning);
}

// Count of running containers per group + "all" and "misc".
function groupCounts(containers, groups) {
  groups = groups || {};
  var running = runningContainers(containers);
  var counts = { all: running.length };
  var seen = {}; // per-object "matches some group" flag
  var keys = Object.keys(groups);

  keys.forEach(function (k) {
    var pats = (Array.isArray(groups[k]) ? groups[k] : []).map(matcher);
    var n = 0;
    running.forEach(function (c) {
      if (pats.some(function (p) { return p(containerName(c)); })) { n++; seen[c] = true; }
    });
    counts[k] = n;
  });

  counts.misc = running.reduce(function (acc, c) { return acc + (seen[c] ? 0 : 1); }, 0);
  return counts;
}

// Return the containers belonging to the active group ("all" -> all running).
function groupContainers(containers, groups, activeGroup) {
  containers = Array.isArray(containers) ? containers : [];
  groups = groups || {};

  if (!activeGroup || activeGroup === 'all') return runningContainers(containers);
  if (activeGroup === 'misc') {
    var seen = {};
    Object.keys(groups).forEach(function (k) {
      (Array.isArray(groups[k]) ? groups[k] : []).map(matcher)
        .forEach(function (p) {
          containers.forEach(function (c) { if (isRunning(c) && p(containerName(c))) seen[c] = true; });
        });
    });
    return containers.filter(function (c) { return isRunning(c) && !seen[c]; });
  }

  var pats = (Array.isArray(groups[activeGroup]) ? groups[activeGroup] : []).map(matcher);
  if (!pats.length) return [];
  return containers.filter(function (c) {
    if (!isRunning(c)) return false;
    return pats.some(function (p) { return p(containerName(c)); });
  });
}

// Build the row of filter buttons (containerEl.containers or `containers`
// supplies the rows for the counts).
function renderGroupButtons(containerEl, groups, onSelect, containers) {
  if (!containerEl) return;
  groups = groups || {};
  var rows = Array.isArray(containers) ? containers : (containerEl.containers || []);

  var counts = groupCounts(rows, groups);
  var labels = ['all'].concat(Object.keys(groups), ['misc']);

  containerEl.innerHTML = '';

  var active = 'all';
  var buttons = {};

  function makeButton(label) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'group-btn' + (label === active ? ' is-active' : '');
    b.setAttribute('data-group', label);
    b.setAttribute('aria-pressed', label === active ? 'true' : 'false');

    var text = document.createElement('span');
    text.className = 'group-label';
    var display = label === 'misc' ? 'Misc' : (label === 'all' ? 'All' : label);
    text.textContent = display;

    var badge = document.createElement('span');
    badge.className = 'group-count';
    badge.textContent = String(counts[label] || 0);

    b.appendChild(text);
    b.appendChild(badge);
    b.addEventListener('click', function () {
      setActive(label);
      if (typeof onSelect === 'function') onSelect(label);
    });
    buttons[label] = b;
    containerEl.appendChild(b);
  }

  function setActive(label) {
    active = label;
    labels.forEach(function (l) {
      var b = buttons[l];
      if (!b) return;
      var is = l === label;
      b.className = 'group-filter' + (is ? ' is-active' : '');
      b.setAttribute('aria-pressed', is ? 'true' : 'false');
    });
  }

  labels.forEach(makeButton);
}