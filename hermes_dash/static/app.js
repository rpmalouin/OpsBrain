/* Hermes cron dashboard — vanilla JS, no build step, no CDN.
 *
 * Refresh contract: every 30 s while the tab is visible, re-fetch /api/state and
 * redraw IN PLACE — each card's innerHTML is replaced only when its content hash
 * changes, so scroll position, the search box, the selected tab and an open job
 * drill-down all survive a refresh.
 *
 * Static mode: snapshot.py inlines window.__HERMES_STATIC__ / __STATIC_DATA__ /
 * __STATIC_FILES__, and this file then renders from memory with no network at all.
 */
(function () {
  "use strict";

  var REFRESH_MS = 30000;
  var HOSTS = ["dockervm", "macstudio"];
  var STATIC = !!window.__HERMES_STATIC__;
  var OPSBRAIN_URL = window.__OPSBRAIN_URL__ || "http://127.0.0.1:9120/";

  var ui = {
    tab: "home",
    detail: null, // {host, jobId}
    q: "",
    data: { dockervm: null, macstudio: null },
    generatedAt: null,
    err: null,
    lastOk: null,
    nextAt: 0,
  };

  // ---------------------------------------------------------------- helpers
  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function sig(str) {
    var hash = 5381;
    for (var i = 0; i < str.length; i++) hash = ((hash << 5) + hash + str.charCodeAt(i)) | 0;
    return String(hash) + ":" + str.length;
  }

  function num(value, fallback) {
    return typeof value === "number" && isFinite(value) ? value : fallback === undefined ? null : fallback;
  }

  function fmtAge(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds)) return "—";
    var s = Math.abs(seconds);
    if (s < 90) return Math.round(s) + "s";
    if (s < 5400) return Math.round(s / 60) + "m";
    if (s < 172800) return (s / 3600).toFixed(1) + "h";
    return Math.round(s / 86400) + "d";
  }

  function fmtIn(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    // the ticker is a ~60 s cycle, so a sub-2-minute lag is the schedule working,
    // not a job that missed: only call it late past that.
    if (seconds < -120) return "late " + fmtAge(seconds);
    if (seconds < 0) return "due now";
    return "in " + fmtAge(seconds);
  }

  function fmtAgo(iso) {
    if (!iso) return "—";
    var t = Date.parse(iso);
    if (isNaN(t)) return esc(iso);
    return fmtAge((Date.now() - t) / 1000) + " ago";
  }

  function fmtClock(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    var hh = String(d.getHours()).padStart(2, "0");
    var mm = String(d.getMinutes()).padStart(2, "0");
    var ss = String(d.getSeconds()).padStart(2, "0");
    if (sameDay) return hh + ":" + mm + ":" + ss;
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + hh + ":" + mm;
  }

  function statusTag(status) {
    var s = (status || "").toLowerCase();
    var cls = "tag";
    if (s === "completed" || s === "ok" || s === "success") cls += " ok";
    else if (s === "failed" || s === "error") cls += " crit";
    else if (s === "missed" || s === "unknown" || s === "cancelled") cls += " warn";
    else if (s === "running" || s === "claimed") cls += " info";
    return '<span class="' + cls + '">' + esc(s || "—") + "</span>";
  }

  function fileLink(path, label, extraClass) {
    if (!path) return esc(label || "");
    var cls = extraClass ? ' class="' + extraClass + '"' : ' class="link"';
    if (STATIC) {
      var files = window.__STATIC_FILES__ || {};
      if (Object.prototype.hasOwnProperty.call(files, path)) {
        return '<a href="#"' + cls + ' data-inline="' + esc(path) + '">' + esc(label) + "</a>";
      }
      return '<span class="muted" title="not captured in this snapshot">' + esc(label) + "</span>";
    }
    return (
      '<a' + cls + ' href="/api/file?path=' + encodeURIComponent(path) +
      '" target="_blank" rel="noopener noreferrer">' + esc(label) + "</a>"
    );
  }

  function levelChip(level) {
    var l = (level || "").toUpperCase();
    var cls = l === "CRITICAL" || l === "ERROR" ? "crit" : l === "WARNING" ? "warn" : "info";
    return '<span class="chip ' + cls + '">' + esc(l || "INFO") + "</span>";
  }

  // ---------------------------------------------------------------- routing
  function parseHash() {
    var raw = (location.hash || "#home").replace(/^#/, "");
    if (!raw || raw === "home") return { tab: "home" };
    var parts = raw.split("/");
    var tab = parts[0];
    if (HOSTS.indexOf(tab) === -1) return { tab: "home" };
    if (parts[1] === "job" && parts[2]) return { tab: tab, detail: { host: tab, jobId: decodeURIComponent(parts[2]) } };
    return { tab: tab };
  }

  function setHash(tab, jobId) {
    var target = "#" + tab + (jobId ? "/job/" + encodeURIComponent(jobId) : "");
    if (location.hash !== target) history.replaceState(null, "", target);
  }

  function onRoute() {
    var route = parseHash();
    ui.tab = route.tab;
    ui.detail = route.detail || null;
    render();
  }

  // ---------------------------------------------------------------- data
  function fetchHost(host) {
    return fetch("/api/state?host=" + encodeURIComponent(host), { cache: "no-store", credentials: "omit" }).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    });
  }

  function refresh() {
    if (STATIC) return;
    if (document.visibilityState !== "visible") return;
    Promise.all(HOSTS.map(fetchHost))
      .then(function (docs) {
        ui.data.dockervm = docs[0];
        ui.data.macstudio = docs[1];
        ui.generatedAt = new Date().toISOString();
        ui.err = null;
        ui.lastOk = new Date();
        ui.nextAt = Date.now() + REFRESH_MS;
        render();
      })
      .catch(function (err) {
        ui.err = err && err.message ? err.message : String(err);
        render();
      });
  }

  // ---------------------------------------------------------------- chrome
  function renderChrome() {
    var tabs = document.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) {
      var active = tabs[i].getAttribute("data-tab") === ui.tab;
      tabs[i].classList.toggle("is-active", active);
      tabs[i].setAttribute("aria-selected", active ? "true" : "false");
    }
    var conn = document.getElementById("conn");
    var dot = document.getElementById("dot");
    if (STATIC) {
      conn.className = "chip info";
      conn.textContent = "static snapshot";
      dot.style.background = "#60a5fa";
    } else if (ui.err) {
      conn.className = "chip crit";
      conn.textContent = "server unreachable";
      dot.style.background = "#f87171";
    } else if (ui.generatedAt) {
      conn.className = "chip ok";
      conn.textContent = "live · refresh 30s";
      dot.style.background = "#4ade80";
    } else {
      conn.className = "chip";
      conn.textContent = "connecting…";
    }
    var asof = document.getElementById("asof");
    var stamp = ui.generatedAt || (window.__STATIC_DATA__ || {}).generated_at || null;
    asof.textContent = stamp ? "as of " + fmtClock(stamp) : "";

    var banner = document.getElementById("banner");
    banner.className = "banner hidden";
    banner.textContent = "";
    if (STATIC) {
      var gen = (window.__STATIC_DATA__ || {}).generated_at;
      banner.className = "banner static";
      banner.textContent =
        "STATIC SNAPSHOT — captured " + (gen ? fmtClock(gen) + " (" + fmtAgo(gen) + ")" : "at build time") +
        ". The live server is not running. Drill-downs show the text captured at that moment; nothing here is live.";
    } else if (ui.err) {
      banner.className = "banner crit";
      banner.textContent =
        "LIVE SERVER UNREACHABLE (" + ui.err + "). Showing the last data received" +
        (ui.lastOk ? " at " + fmtClock(ui.lastOk.toISOString()) : "") + ". Retrying every 30 s.";
    }
  }

  function renderHome() {
    var frame = document.getElementById("opsbrainFrame");
    var note = document.getElementById("homeNote");
    if (STATIC) {
      frame.classList.add("hidden");
      note.classList.remove("hidden");
      note.textContent =
        "The home tab embeds the live OpsBrain dashboard, which a static file cannot load. " +
        "Open http://127.0.0.1:9120/ directly when the server is back.";
      return;
    }
    frame.classList.remove("hidden");
    note.classList.add("hidden");
    if (!frame.src || frame.src === "about:blank") frame.src = OPSBRAIN_URL;
  }

  // ---------------------------------------------------------------- card plumbing
  function cardShell(container, id, opts) {
    var el = container.querySelector('section[data-card="' + id + '"]');
    if (!el) {
      el = document.createElement("section");
      el.setAttribute("data-card", id);
      container.appendChild(el);
    }
    // re-apply every time: a card's width class must not be frozen at creation
    el.className = "panel" + (opts && opts.wide ? " wide" : "");
    return el;
  }

  function putCard(container, id, html, opts) {
    var el = cardShell(container, id, opts);
    var next = sig(html);
    if (el.getAttribute("data-sig") !== next) {
      el.innerHTML = html;
      el.setAttribute("data-sig", next);
    }
  }

  function cardTitle(title, count) {
    return "<h2>" + esc(title) + (count !== undefined && count !== null ? ' <span class="count">' + esc(count) + "</span>" : "") + "</h2>";
  }

  // ---------------------------------------------------------------- dockerVM cards
  function tile(label, value, sub, cls) {
    return (
      '<div class="tile ' + (cls || "") + '"><div class="k">' + esc(label) + "</div>" +
      '<div class="v">' + esc(value) + "</div>" + (sub ? '<div class="s">' + sub + "</div>" : "") + "</div>"
    );
  }

  function attentionItems(doc) {
    var items = [];
    (doc.jobs || []).forEach(function (job) {
      if (job.failing) {
        items.push({
          level: "error",
          text: "Job failing: " + job.name,
          detail: (job.last_status || "?") + (job.failure_streak ? " · streak " + job.failure_streak : "") +
            (job.last_error ? " · " + String(job.last_error).split("\n")[0].slice(0, 160) : ""),
          host: doc.host_id,
          jobId: job.id,
        });
      }
      if (job.overdue) {
        items.push({
          level: "warn",
          text: "Job overdue: " + job.name,
          detail: "next run was due " + fmtAgo(job.next_run_at) + " (grace " + fmtAge(job.grace_s) + ")",
          host: doc.host_id,
          jobId: job.id,
        });
      }
    });
    (doc.open_incidents || []).forEach(function (inc) {
      var orphan = !inc.job_name;
      items.push({
        level: orphan ? "warn" : "error",
        text: "Open incident: " + (inc.job_name || inc.job_id || "unknown job") + " (" + inc.failure_type + ")" +
          (orphan ? " — job no longer exists in jobs.json" : ""), 
        detail: "state " + inc.state + " · last seen " + fmtAgo(inc.last_seen_at) +
          (inc.error ? " · " + String(inc.error).split("\n")[0].slice(0, 160) : ""),
        host: doc.host_id,
        jobId: inc.job_id,
      });
    });
    if (doc.gateway && doc.gateway.error) {
      items.push({ level: "error", text: "Gateway state unreadable", detail: doc.gateway.error });
    } else if (doc.gateway && doc.gateway.heartbeat_present && num(doc.gateway.heartbeat_age_s, 0) > 180) {
      items.push({
        level: "error",
        text: "Gateway heartbeat stale",
        detail: "last beat " + fmtAge(doc.gateway.heartbeat_age_s) + " ago",
      });
    }
    if (doc.ticker && doc.ticker.healthy === false) {
      items.push({
        level: "error",
        text: "Cron ticker heartbeat stale",
        detail: "last tick " + fmtAge((doc.ticker.heartbeat || {}).age_s) + " ago — jobs will not fire",
      });
    }
    (doc.gateway && doc.gateway.platforms_bad ? doc.gateway.platforms_bad : []).forEach(function (name) {
      items.push({ level: "warn", text: "Platform not connected: " + name, detail: "deliveries to it will fail" });
    });
    (doc.warnings || []).forEach(function (w) {
      if (w.level === "ERROR" || w.level === "CRITICAL") {
        items.push({
          level: "error",
          text: w.level + " in " + w.source + (w.count > 1 ? " ×" + w.count : "") + " · last " + fmtAgo(w.last_seen),
          detail: w.message,
          path: (doc.logs || []).filter(function (l) { return l.name === w.source; }).map(function (l) { return l.path; })[0],
        });
      }
    });
    return items;
  }

  function renderDockerVm(pane, doc) {
    var grid = pane.querySelector(".grid");
    var counts = doc.counts || {};
    var gw = doc.gateway || {};

    var openInc = doc.open_incidents || [];
    var oldestOpen = openInc.reduce(function (m, i) {
      var age = (Date.now() - Date.parse(i.last_seen_at)) / 1000;
      return isFinite(age) && age > m ? age : m;
    }, 0);
    // a week-old incident on a job that no longer exists is a record, not an alarm
    var incidentCls = !openInc.length ? "ok" : oldestOpen > 7 * 86400 ? "warn" : "crit";
    var tiles =
      tile("Jobs", counts.jobs, counts.enabled + " enabled · " + counts.paused + " paused") +
      tile("Failing", counts.failing, counts.failing ? "needs a look" : "none", counts.failing ? "crit" : "ok") +
      tile("Overdue", counts.overdue, counts.overdue ? "past its grace" : "on schedule", counts.overdue ? "warn" : "ok") +
      tile("Open incidents", counts.open_incidents, openInc.length ? "oldest " + fmtAge(oldestOpen) : "none", incidentCls) +
      tile("Gateway", gw.platforms_bad && gw.platforms_bad.length ? "degraded" : "running",
        "beat " + fmtAge(gw.heartbeat_age_s) + " ago · v" + esc(gw.version || "?"), gw.platforms_bad && gw.platforms_bad.length ? "warn" : "ok") +
      tile("Ticker", doc.ticker && doc.ticker.healthy ? "alive" : "stale",
        "tick " + fmtAge(((doc.ticker || {}).heartbeat || {}).age_s) + " ago",
        doc.ticker && doc.ticker.healthy ? "ok" : "crit") +
      tile("Runs in view", counts.runs_seen, "ring + rollup") +
      tile("Warnings", counts.warnings, (counts.warnings_error_or_worse || 0) + " error+", counts.warnings_error_or_worse ? "crit" : "");
    putCard(grid, "tiles", cardTitle("At a glance") + '<div class="tiles">' + tiles + "</div>", { wide: true });

    var items = attentionItems(doc);
    var attHtml = cardTitle("Needs attention", items.length);
    if (!items.length) {
      attHtml += '<div class="empty good">Nothing needs attention: no failing or overdue jobs, no open incidents.</div>';
    } else {
      items.forEach(function (it) {
        var target = it.jobId
          ? '<a href="#' + it.host + "/job/" + encodeURIComponent(it.jobId) + '">' + esc(it.text) + "</a>"
          : it.path
            ? fileLink(it.path, it.text)
            : esc(it.text);
        attHtml +=
          '<div class="row">' +
          '<span class="chip ' + (it.level === "error" ? "crit" : "warn") + '">' + (it.level === "error" ? "!" : "·") + "</span>" +
          '<div class="grow">' + target + (it.detail ? '<div class="muted">' + esc(it.detail) + "</div>" : "") + "</div>" +
          "</div>";
      });
    }
    putCard(grid, "attention", attHtml, { wide: true });

    var up = '<table class="upnext"><thead><tr><th>Job</th><th>Schedule</th><th class="n">Next run</th><th class="n">In</th></tr></thead><tbody>';
    (doc.upcoming || []).slice(0, 10).forEach(function (job) {
      up +=
        '<tr class="clickable" data-goto="' + esc(doc.host_id) + "/" + esc(job.id) + '">' +
        "<td><strong>" + esc(job.name) + "</strong><div class=\"muted mono\">" + esc(job.id) + "</div></td>" +
        '<td class="nowrap">' + esc(job.schedule_display) + "</td>" +
        '<td class="n nowrap">' + esc(fmtClock(job.next_run_at)) + "</td>" +
        '<td class="n nowrap">' + esc(fmtIn(job.next_in_s)) + "</td></tr>";
    });
    up += "</tbody></table>";
    putCard(grid, "upcoming", cardTitle("Upcoming runs", (doc.upcoming || []).length) + '<div class="tbl-scroll">' + up + "</div>", { wide: true });

    // jobs table (search-filtered)
    var q = ui.q.toLowerCase();
    var jobs = (doc.jobs || []).filter(function (job) {
      if (!q) return true;
      return [job.name, job.id, job.script, job.schedule_display, job.last_status]
        .filter(Boolean).join(" ").toLowerCase().indexOf(q) !== -1;
    });
    var jt = '<table><thead><tr><th>Job</th><th>Schedule</th><th>Last run</th><th>Next</th><th class="n">Runs</th><th class="n">Out</th></tr></thead><tbody>';
    jobs.forEach(function (job) {
      var flags = "";
      if (!job.enabled) flags += ' <span class="tag warn">paused</span>';
      if (job.failing) flags += ' <span class="tag crit">failing</span>';
      if (job.overdue) flags += ' <span class="tag warn">overdue</span>';
      if (job.no_agent) flags += ' <span class="tag info">script</span>';
      jt +=
        '<tr class="clickable" data-goto="' + esc(doc.host_id) + "/" + esc(job.id) + '">' +
        "<td><strong>" + esc(job.name) + "</strong>" + flags + '<div class="muted mono">' + esc(job.id) + (job.script ? " · " + esc(job.script) : "") + "</div></td>" +
        '<td class="nowrap">' + esc(job.schedule_display) + "</td>" +
        '<td class="nowrap">' + statusTag(job.last_status) + ' <span class="muted">' + esc(fmtAgo(job.last_run_at)) + "</span></td>" +
        '<td class="nowrap">' + esc(fmtIn(job.next_in_s)) + "</td>" +
        '<td class="n">' + esc(job.runs_seen) + "</td>" +
        '<td class="n">' + ((job.outputs || []).length ? '<a href="#' + esc(doc.host_id) + "/job/" + encodeURIComponent(job.id) + '">' + (job.outputs || []).length + "</a>" : "0") + "</td></tr>";
    });
    if (!jobs.length) jt += '<tr><td colspan="6" class="empty">no job matches "' + esc(ui.q) + '"</td></tr>';
    jt += "</tbody></table>";
    putCard(grid, "jobs", cardTitle("Jobs", jobs.length + "/" + (doc.jobs || []).length) + '<div class="tbl-scroll">' + jt + "</div>", { wide: true });

    // run history
    var runs = (doc.runs || []).filter(function (r) {
      if (!q) return true;
      return [r.job_name, r.job_id, r.status, r.error].filter(Boolean).join(" ").toLowerCase().indexOf(q) !== -1;
    }).slice(0, 60);
    var rt = '<table><thead><tr><th>When</th><th>Job</th><th>Status</th><th class="n">Took</th><th>Source</th><th>Output</th></tr></thead><tbody>';
    runs.forEach(function (run) {
      rt +=
        '<tr class="clickable" data-goto="' + esc(doc.host_id) + "/" + esc(run.job_id) + '">' +
        '<td class="nowrap">' + esc(fmtClock(run.started_at || run.claimed_at)) + '<div class="muted">' + esc(fmtAgo(run.started_at || run.claimed_at)) + "</div></td>" +
        "<td>" + esc(run.job_name || run.job_id) + "</td>" +
        "<td>" + statusTag(run.status) + (run.error ? '<div class="muted">' + esc(String(run.error).split("\n")[0].slice(0, 90)) + "</div>" : "") + "</td>" +
        '<td class="n nowrap">' + (run.duration_s === null || run.duration_s === undefined ? "—" : esc(run.duration_s) + "s") + "</td>" +
        '<td class="nowrap">' + esc(run.source || "") + "</td>" +
        "<td>" + (run.output ? fileLink(run.output.path, run.output.name) : '<span class="muted">—</span>') + "</td></tr>";
    });
    if (!runs.length) rt += '<tr><td colspan="6" class="empty">no run matches "' + esc(ui.q) + '"</td></tr>';
    rt += "</tbody></table>";
    putCard(grid, "runs", cardTitle("Run history", (doc.runs || []).length) + '<div class="tbl-scroll">' + rt + "</div>", { wide: true });

    // incidents
    var it = '<table><thead><tr><th>Job</th><th>State</th><th>Type</th><th class="n">Last seen</th><th>Error</th></tr></thead><tbody>';
    (doc.incidents || []).slice(0, 20).forEach(function (inc) {
      it +=
        '<tr class="clickable" data-goto="' + esc(doc.host_id) + "/" + esc(inc.job_id || "") + '">' +
        "<td>" + esc(inc.job_name || inc.job_id) + "</td>" +
        "<td>" + statusTag(inc.state) + "</td>" +
        '<td class="nowrap">' + esc(inc.failure_type) + "</td>" +
        '<td class="n nowrap">' + esc(fmtAgo(inc.last_seen_at)) + "</td>" +
        '<td class="muted">' + esc(String(inc.error || "").split("\n")[0].slice(0, 90)) + "</td></tr>";
    });
    if (!(doc.incidents || []).length) it += '<tr><td colspan="5" class="empty">no incidents recorded</td></tr>';
    it += "</tbody></table>";
    putCard(grid, "incidents", cardTitle("Incidents", (doc.incidents || []).length) + '<div class="tbl-scroll">' + it + "</div>", { wide: true });

    // outputs
    var ot = '<table><thead><tr><th>Written</th><th>Job</th><th>File</th><th class="n">Size</th></tr></thead><tbody>';
    (doc.outputs || []).slice(0, 25).forEach(function (out) {
      ot +=
        '<tr><td class="nowrap">' + esc(fmtClock(out.mtime)) + '<div class="muted">' + esc(fmtAgo(out.mtime)) + "</div></td>" +
        "<td>" + esc(out.job_id) + "</td>" +
        "<td>" + fileLink(out.path, out.name) + "</td>" +
        '<td class="n">' + Math.max(1, Math.round((out.size || 0) / 1024)) + " kB</td></tr>";
    });
    if (!(doc.outputs || []).length) ot += '<tr><td colspan="4" class="empty">no job output files yet</td></tr>';
    ot += "</tbody></table>";
    putCard(grid, "outputs", cardTitle("Recent job outputs", (doc.outputs || []).length) + '<div class="tbl-scroll">' + ot + "</div>", { wide: true });

    // warnings + logs
    var wt = "";
    (doc.warnings || []).slice(0, 20).forEach(function (w) {
      var logPath = (doc.logs || []).filter(function (l) { return l.name === w.source; }).map(function (l) { return l.path; })[0];
      wt +=
        '<div class="row">' + levelChip(w.level) +
        '<div class="grow"><strong>' + esc(w.message) + "</strong>" +
        '<div class="muted">×' + esc(w.count) + " · last " + esc(fmtAgo(w.last_seen)) + " · " + (logPath ? fileLink(logPath, w.source) : esc(w.source)) +
        (w.job_name ? " · " + esc(w.job_name) : "") + "</div></div></div>";
    });
    if (!wt) wt = '<div class="empty good">no warnings or errors in the log tail</div>';
    wt += '<div class="muted" style="margin-top:.55rem"><strong>Log files</strong> (links open the served text):</div><div class="row" style="flex-wrap:wrap;gap:.35rem .8rem">';
    (doc.logs || []).slice(0, 10).forEach(function (log) {
      wt += '<span class="nowrap">' + fileLink(log.path, log.name) + ' <span class="muted">' + esc(fmtAge((Date.now() - Date.parse(log.mtime)) / 1000)) + "</span></span>";
    });
    wt += "</div>";
    putCard(grid, "warnings", cardTitle("System warnings & logs", (doc.warnings || []).length) + wt, { wide: true });

    // sources / freshness
    var src = doc.sources || {};
    var st = '<table><thead><tr><th>Source</th><th>Path</th><th class="n">Age</th><th class="n">Size</th></tr></thead><tbody>';
    Object.keys(src).forEach(function (key) {
      var s = src[key] || {};
      var present = s.present === false ? '<span class="tag crit">missing</span>' : s.error ? '<span class="tag crit">' + esc(s.error) + "</span>" : '<span class="tag ok">ok</span>';
      st +=
        "<tr><td>" + esc(key) + " " + present + "</td>" +
        '<td class="muted mono" style="overflow-wrap:anywhere">' + esc(s.path || "") + "</td>" +
        '<td class="n nowrap">' + esc(fmtAge(s.age_s)) + "</td>" +
        '<td class="n nowrap">' + (s.size ? Math.max(1, Math.round(s.size / 1024)) + " kB" : "—") + "</td></tr>";
    });
    st += "</tbody></table>";
    var prof = (doc.profiles || []).map(function (p) { return p.name + " (" + (p.executions === null ? "?" : p.executions) + ")"; }).join(", ");
    st += '<div class="muted" style="margin-top:.5rem">Other profiles with a cron store: ' + esc(prof || "none") + " — 0 jobs each.</div>";
    putCard(grid, "sources", cardTitle("What this reads, and how fresh") + st, { wide: true });

    if ((doc.not_connected || []).length) {
      putCard(grid, "gap", cardTitle("Not connected") + (doc.not_connected || []).map(function (line) {
        return '<div class="row"><div class="grow">' + esc(line) + "</div></div>";
      }).join(""), { wide: true });
    }
  }

  // ---------------------------------------------------------------- Mac Studio cards
  function renderMacStudio(pane, doc) {
    var grid = pane.querySelector(".grid");
    var cron = doc.cron || {};
    var counts = doc.counts || {};
    var state = doc.state || "missing";
    var stateCls = state === "ok" ? "ok" : state === "stale" ? "warn" : "crit";

    var tiles =
      tile("Snapshot", state, "age " + fmtAge((doc.file || {}).age_s) + " · tick " + esc(doc.interval_minutes || "?") + "m", stateCls) +
      tile("Jobs", counts.jobs === null || counts.jobs === undefined ? "?" : counts.jobs,
        (counts.enabled || 0) + " enabled · " + (counts.paused || 0) + " paused") +
      tile("Failing", counts.failing, counts.failing ? "named below" : "none", counts.failing ? "crit" : "ok") +
      tile("Overdue", counts.overdue, counts.overdue ? "named below" : "none", counts.overdue ? "warn" : "ok") +
      tile("Alerts", counts.alerts, counts.alerts ? "from the producer" : "none", counts.alerts ? "crit" : "ok") +
      tile("Gateway", doc.gateway && doc.gateway.present ? "running" : "missing",
        "beat " + fmtAge((doc.gateway || {}).age_seconds) + " ago", doc.gateway && doc.gateway.present ? "ok" : "crit");
    putCard(grid, "tiles", cardTitle("At a glance") + '<div class="tiles">' + tiles + "</div>", { wide: true });

    var body = "";
    if (state !== "ok") {
      body += '<div class="empty warnc"><strong>Snapshot ' + esc(state) + ".</strong> " +
        esc(doc.error || "The Mac's producer has not written a fresh document; the facts below may be old.") + "</div>";
    }
    var failing = cron.failing || [];
    var overdue = cron.overdue || [];
    if (failing.length) {
      body += '<div class="muted"><strong>Failing jobs</strong></div>';
      failing.forEach(function (f) {
        body += '<div class="row"><span class="chip crit">!</span><div class="grow"><strong>' +
          esc(f.name || f.job_id || JSON.stringify(f)) + "</strong>" +
          (f.last_error || f.error ? '<div class="muted">' + esc(String(f.last_error || f.error).slice(0, 200)) + "</div>" : "") +
          "</div></div>";
      });
    }
    if (overdue.length) {
      body += '<div class="muted"><strong>Overdue jobs</strong></div>';
      overdue.forEach(function (f) {
        body += '<div class="row"><span class="chip warn">·</span><div class="grow"><strong>' +
          esc(f.name || f.job_id || JSON.stringify(f)) + "</strong>" +
          (f.overdue_hours ? '<div class="muted">' + esc(f.overdue_hours) + " h past due</div>" : "") + "</div></div>";
      });
    }
    if (!failing.length && !overdue.length) {
      body += '<div class="empty good">No failing or overdue job reported by the Mac producer.</div>';
    }
    body += '<dl class="kv" style="margin-top:.6rem">' +
      "<dt>oldest job age</dt><dd>" + esc(cron.oldest_last_run_hours === null || cron.oldest_last_run_hours === undefined ? "—" : cron.oldest_last_run_hours + " h") + " <span class=\"muted\">(raw age; a monthly job is legitimately weeks old)</span></dd>" +
      "<dt>worst age/period</dt><dd>" + esc(cron.max_age_period_ratio === null || cron.max_age_period_ratio === undefined ? "—" : cron.max_age_period_ratio) + " <span class=\"muted\">(1.0 = exactly at its period, &gt;1 = overdue)</span></dd>" +
      "<dt>oldest overdue</dt><dd>" + esc(cron.oldest_overdue_hours === null || cron.oldest_overdue_hours === undefined ? "none" : cron.oldest_overdue_hours + " h") + "</dd>" +
      "</dl>";
    putCard(grid, "cron", cardTitle("Cron health (as reported)") + body, { wide: true });

    var alerts = doc.alerts || [];
    var at = "";
    if (alerts.length) {
      alerts.forEach(function (a) {
        at += '<div class="row">' + levelChip(a.severity || a.level || "warning") +
          '<div class="grow"><strong>' + esc(a.name || a.key || "alert") + "</strong>" +
          '<div class="muted">' + esc(a.detail || a.message || "") + "</div></div></div>";
      });
    } else {
      at = '<div class="empty good">The producer reports no alerts of its own.</div>';
    }
    putCard(grid, "alerts", cardTitle("Alerts", alerts.length) + at);

    var tm = doc.time_machine || {};
    var mounts = doc.mounts || [];
    var hostInfo = doc.host || {};
    var sys = '<dl class="kv">' +
      "<dt>gateway beat</dt><dd>" + esc(fmtAge((doc.gateway || {}).age_seconds)) + " ago (pid " + esc((doc.gateway || {}).pid || "?") + ")</dd>" +
      "<dt>host</dt><dd>" + esc(hostInfo.model || "?") + " · " + esc(hostInfo.os || "?") + " · " + esc(hostInfo.cpu_cores || "?") + " cores</dd>" +
      "<dt>uptime</dt><dd>" + esc(hostInfo.uptime_hours || "?") + " h</dd>" +
      "<dt>Time Machine</dt><dd>" + esc(tm.latest_backup_age_hours === null || tm.latest_backup_age_hours === undefined ? "—" : tm.latest_backup_age_hours + " h") +
      (tm.mount_point_mounted === false ? ' <span class="tag crit">destination not mounted</span>' : "") + "</dd>" +
      "</dl>";
    var diskUse = {};
    (doc.disks || []).forEach(function (d) { if (d.path) diskUse[d.path] = d.used_percent; });
    sys += '<table style="margin-top:.5rem"><thead><tr><th>Mount</th><th>Source</th><th class="n">Used %</th></tr></thead><tbody>';
    mounts.slice(0, 12).forEach(function (m) {
      var flags = "";
      if (m.present === false) flags += ' <span class="tag crit">missing</span>';
      else if (m.mounted === false && (m.fstype || m.source)) flags += ' <span class="tag crit">not mounted</span>';
      else if (m.mounted === false) flags += ' <span class="tag info">internal</span>';
      else if (m.read_only) flags += ' <span class="tag warn">read-only</span>';
      var used = diskUse[m.path];
      sys += '<tr><td class="mono" style="overflow-wrap:anywhere">' + esc(m.path) + flags + "</td>" +
        '<td class="muted mono" style="overflow-wrap:anywhere">' + esc(m.source || m.fstype || "—") + "</td>" +
        '<td class="n">' + (used === undefined || used === null ? "—" : esc(used) + "%") + "</td></tr>";
    });
    sys += "</tbody></table>";
    putCard(grid, "system", cardTitle("Node health") + sys, { wide: true });

    var fresh = doc.file || {};
    var fr = '<dl class="kv">' +
      "<dt>document</dt><dd class=\"mono\" style=\"overflow-wrap:anywhere\">" + esc(fresh.path || "") + "</dd>" +
      "<dt>written</dt><dd>" + esc(doc.as_of || "?") + " (" + esc(fmtAgo(doc.as_of)) + ", file age " + esc(fmtAge(fresh.age_s)) + ")</dd>" +
      "<dt>producer</dt><dd>" + esc(doc.produced_by || "?") + "</dd>" +
      "<dt>address</dt><dd>" + esc(doc.address || "?") + "</dd>" +
      "</dl>";
    putCard(grid, "sources", cardTitle("What this reads, and how fresh") + fr, { wide: true });

    var gaps = doc.not_connected || [];
    if (gaps.length) {
      var g = gaps.map(function (line) { return '<div class="row"><div class="grow">' + esc(line) + "</div></div>"; }).join("");
      g += '<div class="muted" style="margin-top:.5rem">Everything on this tab is aggregate — ' +
        "counts, named failures, overdue names, alerts, gateway and node health. There is no per-job drill-down or file link until the Mac publishes one.</div>";
      putCard(grid, "gap", cardTitle("Not connected") + g, { wide: true });
    }
  }

  // ---------------------------------------------------------------- job detail
  function renderDetail(pane, doc) {
    var job = (doc.jobs || []).filter(function (j) { return j.id === ui.detail.jobId; })[0];
    var grid = pane.querySelector(".grid");
    var back = '<button class="back" id="backBtn">← back to ' + esc(doc.label || doc.host_id) + "</button>";
    if (!job) {
      putCard(grid, "detail", back + cardTitle("Job not found") + '<div class="empty">No job ' + esc(ui.detail.jobId) +
        " in the current data. It may have been deleted since this snapshot.</div>", { wide: true });
      return;
    }
    var info = '<dl class="kv">' +
      "<dt>id</dt><dd class=\"mono\">" + esc(job.id) + "</dd>" +
      "<dt>schedule</dt><dd>" + esc(job.schedule_display) + " (" + esc(job.schedule_kind) + ")</dd>" +
      "<dt>enabled</dt><dd>" + (job.enabled ? '<span class="tag ok">yes</span>' : '<span class="tag warn">paused</span>' + (job.paused_reason ? " — " + esc(job.paused_reason) : "")) + "</dd>" +
      "<dt>next run</dt><dd>" + esc(job.next_run_at || "—") + " (" + esc(fmtIn(job.next_in_s)) + ")</dd>" +
      "<dt>last run</dt><dd>" + statusTag(job.last_status) + " " + esc(job.last_run_at || "—") + " (" + esc(fmtAgo(job.last_run_at)) + ")</dd>" +
      "<dt>failure streak</dt><dd>" + esc(job.failure_streak) + "</dd>" +
      "<dt>mode</dt><dd>" + (job.no_agent ? "script only (no LLM)" : "agent run") + (job.script ? " · " + esc(job.script) : "") + "</dd>" +
      "<dt>delivery</dt><dd>" + esc(job.deliver || "—") + " · on failure: " + esc(job.failure_deliver || "—") + "</dd>" +
      "<dt>runs seen</dt><dd>" + esc(job.runs_seen) + " in retained history</dd>" +
      "</dl>";
    if (job.last_error) info += '<div class="muted" style="margin-top:.5rem">Last error</div><pre class="err">' + esc(job.last_error) + "</pre>";
    else if (job.last_failure && job.last_failure.detail) info += '<div class="muted" style="margin-top:.5rem">Last failure</div><pre class="err">' + esc(job.last_failure.detail) + "</pre>";

    var runs = job.recent_runs || [];
    var rt = '<table><thead><tr><th>When</th><th>Status</th><th class="n">Took</th><th>Source</th><th>Output</th></tr></thead><tbody>';
    runs.forEach(function (run) {
      rt += "<tr><td class=\"nowrap\">" + esc(fmtClock(run.started_at || run.claimed_at)) + '<div class="muted">' + esc(fmtAgo(run.started_at || run.claimed_at)) + "</div></td>" +
        "<td>" + statusTag(run.status) + (run.error ? '<div class="muted">' + esc(String(run.error).split("\n")[0].slice(0, 120)) + "</div>" : "") + "</td>" +
        '<td class="n nowrap">' + (run.duration_s === null || run.duration_s === undefined ? "—" : esc(run.duration_s) + "s") + "</td>" +
        '<td class="nowrap">' + esc(run.source || "") + "</td>" +
        "<td>" + (run.output ? fileLink(run.output.path, run.output.name) : '<span class="muted">—</span>') + "</td></tr>";
    });
    if (!runs.length) rt += '<tr><td colspan="5" class="empty">no run of this job in retained history</td></tr>';
    rt += "</tbody></table>";

    var outs = job.outputs || [];
    var ot = '<div class="row" style="flex-wrap:wrap;gap:.35rem .9rem">';
    outs.forEach(function (out) {
      ot += '<span class="nowrap">' + fileLink(out.path, out.name) + ' <span class="muted">' + Math.max(1, Math.round((out.size || 0) / 1024)) + " kB</span></span>";
    });
    if (!outs.length) ot += '<span class="empty">no output files on disk for this job</span>';
    ot += "</div>";

    putCard(grid, "detail", back +
      cardTitle(job.name) + info, { wide: true });
    putCard(grid, "detail-runs", cardTitle("Recent runs", runs.length) + '<div class="tbl-scroll">' + rt + "</div>", { wide: true });
    putCard(grid, "detail-outputs", cardTitle("Output files", outs.length) + ot, { wide: true });
  }

  // ---------------------------------------------------------------- render
  function gridShell(host) {
    return (
      '<div class="search">' +
      '<input id="q" type="search" autocomplete="off" spellcheck="false" placeholder="filter jobs, runs, scripts…" aria-label="Filter jobs and runs">' +
      '<span class="muted" id="qcount"></span>' +
      "</div>" +
      '<div class="grid" id="grid-' + host + '"></div>'
    );
  }

  function macShell(host) {
    // no search bar: the Mac tab is aggregate only, there are no job rows to filter
    return '<div class="grid" id="grid-' + host + '"></div>';
  }

  function detailShell(host) {
    return '<div class="grid" id="grid-' + host + '"></div>';
  }

  function renderHost(host) {
    var pane = document.getElementById("pane-" + host);
    var doc = ui.data[host];
    var mode = ui.detail && ui.detail.host === host ? "detail" : "grid";
    if (pane.getAttribute("data-mode") !== mode) {
      pane.innerHTML = mode === "detail" ? detailShell(host) : host === "dockervm" ? gridShell(host) : macShell(host);
      pane.setAttribute("data-mode", mode);
      if (mode === "grid") {
        var input = pane.querySelector("#q");
        if (input) {
          input.value = ui.q;
          input.addEventListener("input", function (ev) {
            ui.q = ev.target.value;
            renderHost(host);
          });
        }
      }
    }
    var grid = pane.querySelector(".grid");
    if (!doc) {
      if (!grid.querySelector("section[data-placeholder]")) {
        grid.innerHTML = '<section class="panel wide" data-placeholder="1"><h2>Loading</h2><div class="empty">reading ' +
          (STATIC ? "the snapshot" : "the Hermes sources") + "…</div></section>";
      }
      return;
    }
    var placeholder = grid.querySelector("section[data-placeholder]");
    if (placeholder) placeholder.remove();
    if (mode === "detail") renderDetail(pane, doc);
    else if (host === "dockervm") renderDockerVm(pane, doc);
    else renderMacStudio(pane, doc);
  }

  function render() {
    renderChrome();
    ["home", "dockervm", "macstudio"].forEach(function (name) {
      var pane = document.getElementById("pane-" + name);
      if (!pane) return;
      var show = ui.tab === name;
      pane.classList.toggle("hidden", !show);
    });
    if (ui.tab === "home") renderHome();
    else renderHost(ui.tab);
    var qcount = document.getElementById("qcount");
    if (qcount) qcount.textContent = ui.q ? 'filtering "' + ui.q + '"' : "";
  }

  // ---------------------------------------------------------------- events
  function showInline(path) {
    var files = window.__STATIC_FILES__ || {};
    var text = files[path];
    if (text === undefined) return;
    var overlay = document.createElement("div");
    overlay.className = "overlay";
    overlay.innerHTML =
      '<div class="box"><header><span class="mono">' + esc(path) + '</span><button class="back" data-close="1">close</button></header>' +
      "<pre>" + esc(text) + "</pre></div>";
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay || ev.target.getAttribute("data-close")) overlay.remove();
    });
    document.body.appendChild(overlay);
  }

  document.addEventListener("click", function (ev) {
    var tab = ev.target.closest ? ev.target.closest(".tab") : null;
    if (tab) {
      var name = tab.getAttribute("data-tab");
      setHash(name);
      onRoute();
      return;
    }
    var goto = ev.target.closest ? ev.target.closest("[data-goto]") : null;
    if (goto) {
      var parts = goto.getAttribute("data-goto").split("/");
      if (parts[1]) {
        setHash(parts[0], parts[1]);
        onRoute();
        window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
      }
      return;
    }
    if (ev.target.id === "backBtn") {
      setHash(ui.tab);
      onRoute();
      return;
    }
    var inline = ev.target.closest ? ev.target.closest("[data-inline]") : null;
    if (inline) {
      ev.preventDefault();
      showInline(inline.getAttribute("data-inline"));
    }
  });

  window.addEventListener("hashchange", onRoute);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") refresh();
  });

  // countdown ticker: repaints only the chip text, never the cards
  setInterval(function () {
    if (STATIC || ui.err || !ui.nextAt) return;
    var left = Math.max(0, Math.round((ui.nextAt - Date.now()) / 1000));
    var conn = document.getElementById("conn");
    if (conn && conn.className.indexOf("ok") !== -1) conn.textContent = "live · refresh " + left + "s";
  }, 1000);

  // ---------------------------------------------------------------- boot
  if (STATIC) {
    var data = window.__STATIC_DATA__ || { hosts: {} };
    ui.data.dockervm = (data.hosts || {}).dockervm || null;
    ui.data.macstudio = (data.hosts || {}).macstudio || null;
    ui.generatedAt = data.generated_at || null;
    onRoute();
  } else {
    onRoute();
    refresh();
    setInterval(refresh, REFRESH_MS);
  }
})();
