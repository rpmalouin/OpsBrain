# Hermes cron dashboard (loopback-only, read-only, no model)

Three tabs in one page: the existing OpsBrain dashboard (home), **dockerVM — Hermes**
and **Mac Studio — Hermes**. It answers "what are my scheduled jobs doing, what is
failing or overdue, what did they say" without putting a model in the loop: a script
reads files and SQLite on this host and a small local web server renders them.

    https://hermesjobs.home         # the front door, same gate as opsbrain.home
    http://127.0.0.1:9130/          # loopback, bypasses the door
    # CaddyUI route row 70 -> 127.0.0.1:9130, internal_tls=1, basic_auth admin
    # Homepage tile "Hermes jobs" under Admin / Monitoring

Run it: `systemctl status hermes-dash` · install/remove: `deploy/install.sh [--remove]`

## Why it is shaped this way

| Requirement | How it is met |
|---|---|
| No model in the loop | `collector.py` is stdlib file/DB reading; a refresh is one GET, zero tokens |
| Read-only | SQLite opened `mode=ro`; nothing writes to `/root/.hermes`; the only writer is `snapshot.py` writing under `state/` |
| Refresh without losing the view | `app.js` refetches every 30 s **only while visible** and replaces a card's innerHTML only when its content hash changes — scroll, search text, selected tab and an open job drill-down survive |
| Static fallback | `snapshot.py` (systemd timer, 30 min) writes `state/hermes-snapshots/latest.html` — a self-contained file with data, CSS, JS and the text of the newest outputs and log tails. Open it from disk when the server is down |
| Do not crowd job history | the snapshot runs on a **systemd timer**, never as a Hermes cron job |
| Not published directly | `server.py` binds `127.0.0.1` only and refuses any other bind; the LAN gets in through caddy (`hermesjobs.home`, TLS + basic_auth), never at `:9130` |
| GET only | every other method → `405` with `Allow: GET` |
| Host header must match | only `127.0.0.1:9130`, `localhost:9130` and the configured public name(s) (`HERMES_DASH_PUBLIC_HOSTS`, default `hermesjobs.home`) are accepted; anything else → `421 Misdirected Request` (kills DNS rebinding) |
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

## What the LAN can read now (and what it cannot)

`hermesjobs.home` is gated by `basic_auth admin` — the same credential as
`opsbrain.home` and `beets.home` — over `tls internal`, so the connection is encrypted with
the homelab CA (not a public certificate). Anyone with that credential can read: the names of
every Hermes cron job, each job's schedule and last error, the job output text of the newest
runs, grouped warnings and error lines from `logs/errors.log` / `gateway.log`, and the Mac's
node document. That is the reason the page is behind the door rather than on `:9130`.
Secrets are not reachable either way — the file allow-list refuses `.env`, `config.yaml`,
`auth.json`, `state.db`, `jobs.json` and everything outside the four served folders.

## The Mac's per-job detail: an ask, not a guess

Per-job names, run history and output text live in `/Users/ron/.hermes` on the Mac. There is
no SSH key for that host and no share of that folder, so the tab reads what the Mac's own
15-minute producer publishes (`logs/macstudio_node.json`) and nothing more.

On 2026-11-25 a `task_request` went out on the Mac-VM queue —
**`20261125T141718Z-c02ab9c4`**, parked in `/appdata/hermes-Mac-Updates/requests/` — asking
for a schema 3 of that document: `hermes_cron.jobs[]` (id, name, enabled, no_agent,
schedule, last/next run, last_status, failure_streak, last_error), `runs[]` (newest 200
executions), `incidents[]`, `outputs[]` (up to 3 files per job with the first 2000 chars of
text) and `logs[]` (name/size/mtime), with **every existing key unchanged** so OpsBrain's
federation reader keeps working.

The consumer half is already built and tested against that shape
(`tests/test_hermes_dash.py::test_mac_schema3_detail_is_carried_through`): the moment the
Mac publishes schema 3, this tab grows a job table, run history, incidents, an outputs card
whose text opens in the same overlay as a local file, and a job drill-down — with no change
here. Until then it says exactly that, and names the message id on the page.

## Turning it off

    sudo systemctl disable --now hermes-dash.service hermes-dash-snapshot.timer
    # or: sudo /appdata/OpsBrain/hermes_dash/deploy/install.sh --remove

Taking it off the door is a separate, deliberate pass (CaddyUI row 70 + the
`hermesjobs.home` block + the homepage tile): stop `caddyui`, delete the row, start it,
then cut the block from `/appdata/caddy/Caddyfile` and remove the tile from
`/appdata/homepage/services.yaml`. Leaving the row and removing only the service is fine —
the route then answers 502, which is visible rather than silent.

The static snapshot and the rollup live in `/appdata/OpsBrain/state/hermes-*` and are
left in place by `--remove`.

## Tests

    .venv/bin/python -m pytest tests/test_hermes_dash.py -q     # 30 tests

They build a fixture Hermes tree and assert the refusals (foreign Host → 421, POST →
405, `.env`/`config.yaml`/`state.db`/symlink-escaped path → 403), the verdicts
(failing, overdue with its grace, incidents open vs resolved), the ring+rollup merge,
log-tail truncation and the self-contained snapshot.
