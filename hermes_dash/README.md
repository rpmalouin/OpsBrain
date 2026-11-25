# Hermes cron dashboard (loopback-only, read-only, no model)

Three tabs in one page: the existing OpsBrain dashboard (home), **dockerVM — Hermes**
and **Mac Studio — Hermes**. It answers "what are my scheduled jobs doing, what is
failing or overdue, what did they say" without putting a model in the loop: a script
reads files and SQLite on this host and a small local web server renders them.

    http://127.0.0.1:9130/          # loopback only — not published on the LAN

Run it: `systemctl status hermes-dash` · install/remove: `deploy/install.sh [--remove]`

## Why it is shaped this way

| Requirement | How it is met |
|---|---|
| No model in the loop | `collector.py` is stdlib file/DB reading; a refresh is one GET, zero tokens |
| Read-only | SQLite opened `mode=ro`; nothing writes to `/root/.hermes`; the only writer is `snapshot.py` writing under `state/` |
| Refresh without losing the view | `app.js` refetches every 30 s **only while visible** and replaces a card's innerHTML only when its content hash changes — scroll, search text, selected tab and an open job drill-down survive |
| Static fallback | `snapshot.py` (systemd timer, 30 min) writes `state/hermes-snapshots/latest.html` — a self-contained file with data, CSS, JS and the text of the newest outputs and log tails. Open it from disk when the server is down |
| Do not crowd job history | the snapshot runs on a **systemd timer**, never as a Hermes cron job |
| Loopback only | binds `127.0.0.1`; `server.py` refuses any other bind |
| GET only | every other method → `405` with `Allow: GET` |
| Host header must match | any other `Host` → `421 Misdirected Request` (kills DNS rebinding) |
| Serve linked reports, not `file://` | `/api/file?path=…` serves text, but only when the **real path** is inside an allowed folder |
| Secrets and config refused | `.env`, `config.yaml`, `auth.json`, `state.db`, `jobs.json`, any `*.json`/`*.db`, any filename carrying `secret`/`token`/`credential`/`password`/`key` → `403` with the reason |

## What each section reads, and how fresh it is

dockerVM tab (`HERMES_HOME=/root/.hermes`, profile `default`):

| Section | Source | Freshness |
|---|---|---|
| Jobs, schedule, last/next run, failure streak, delivery | `cron/jobs.json` | file mtime, rewritten on every job change |
| Run history (status, duration, source, error) | `cron/executions.db` → `executions` (1000-row ring, ~1 day at this cadence) **plus** `state/hermes_runs.jsonl`, appended by the snapshot timer | ring is live; rollup adds ~12 days |
| Open / closed incidents | `cron/executions.db` → `cron_incidents` | live |
| Output files (the "recent reports") | `cron/output/<job_id>/<YYYY-MM-DD_HH-MM-SS>.md` (50 files per job) | newest file's mtime |
| Gateway + platform state | `gateway_state.json`, `state/gateway.heartbeat` | heartbeat every ~30 s |
| Scheduler liveness | `cron/ticker_heartbeat`, `cron/ticker_last_success` | ~60 s ticker |
| Warnings / errors | tails of `logs/errors.log` + `logs/gateway.log`, grouped by message signature | file tail, re-read per request (cached 3 s) |
| Log files | top-level `logs/*.log` + `state/*.log` | mtime per file |

Mac Studio tab:

| Section | Source | Freshness |
|---|---|---|
| Job counts, failing, overdue, worst age/period | `logs/macstudio_node.json` → `hermes_cron` (written by the Mac's own 15-minute producer) | document age from file mtime; `ok` < 45 min, `stale` < 2 h, `dead` after |
| Alerts, gateway heartbeat, host, Time Machine, mounts, disk % | same document (`alerts[]`, `gateway`, `host`, `time_machine`, `mounts[]`, `disks[]`) | same |

## Not connected (stated on the page too)

Mac Studio per-job detail — job names, run history, job output text, log files — lives
in `/Users/ron/.hermes` on the Mac. dockerVM has no SSH key for that host and no share
of that folder, so the Mac tab is aggregate only. Two ways to connect it, both needing
Ron's decision:

1. **Producer extension (preferred, no new permission):** have the Mac's
   `~/.hermes/scripts/mac-node-snapshot.py` add a `hermes_cron.jobs[]` array
   (`id, name, enabled, schedule, last_run_at, last_status, next_run_at,
   failure_streak, last_error, output_files[5]`) plus `logs[]` to the document it
   already writes into `/appdata/OpsBrain/logs/`. Then the Mac tab gains the same job
   list and drill-down text; output *bodies* would need to be inlined or copied.
2. **Read-only SSH:** install a key for `ron@10.1.10.34` and read the Mac's files
   directly. That is a new system permission on another host — ask first.

## Turning it off

    sudo systemctl disable --now hermes-dash.service hermes-dash-snapshot.timer
    # or: sudo /appdata/OpsBrain/hermes_dash/deploy/install.sh --remove

The static snapshot and the rollup live in `/appdata/OpsBrain/state/hermes-*` and are
left in place by `--remove`.

## Tests

    .venv/bin/python -m pytest tests/test_hermes_dash.py -q     # 30 tests

They build a fixture Hermes tree and assert the refusals (foreign Host → 421, POST →
405, `.env`/`config.yaml`/`state.db`/symlink-escaped path → 403), the verdicts
(failing, overdue with its grace, incidents open vs resolved), the ring+rollup merge,
log-tail truncation and the self-contained snapshot.
