# Ops Brain — MEMORY.md

Operational memory for the Ops Brain project. Read this before changing anything in this
repo. It captures the decisions, gotchas, and current running state that are expensive to
re-derive.

## What this is

A unified **collector → reasoner → action** pipeline on dockerVM. Polls Netdata, Dozzle,
Dockpeek/Docker-socket, `nvidia-smi`, and VM commands (`df`/`top`/`journalctl`) every 2
minutes, asks **Qwen 3:14B** (local Ollama) for a structured JSON decision, then executes
safe remediation through whitelists/dry-run. Storage (TrueNAS), multi-node **federation**,
manual-stop protection, and a real-time dashboard are all part of this build.

Repo root: `/appdata/OpsBrain` (git repo, `main` branch).
Docs: **`IMPLEMENTATION.md`** (entry-point index), **`docs/`** (per-feature guides:
deployment, configuration, reasoning-llm, remediation, dashboard, manual-stop-protection,
truenas, federation, security, tests, troubleshooting), `CHANGELOG.md`, `README.md`
(overview), `ui/README.md` (dashboard), this file (operational memory).

## Current running state (LAST UPDATED: 2026-09-16)

- **Service:** systemd unit `opsbrain` — ACTIVE, enabled, auto-restarts.
  `ExecStart: python3 /appdata/OpsBrain/scheduler/scheduler.py --daemon`
- **Mode:** `actions.dry_run: false` — **LIVE, remediating.** Restarts execute for
  allow-listed containers (cap `restart_limit_per_run: 3`/cycle). Flip to `true` +
  `systemctl restart opsbrain` to observe only.
- **Restart whitelist** (`actions.allow_restart_containers`, 19 containers incl. homepage,
  dozzle, dockpeek, netdata, caddy). Everything else blocked. Note: blocked rule actions
  do NOT notify — they only show up in `logs/actions_result.json`.
- **Cadence:** cycle every **3600s (1 h)** since 2026-09-04 (was 120s — homelab
  retune to free ollama/GPU for interactive chat). Cadence-coupled knobs were
  rescaled with it: `actions.cpu_restart_minutes: 120` (≥2 hourly samples;
  a single 3600s sample would otherwise trip instantly), `gpu_drift.stuck_pid_cycles: 2`
  (~2 h stuck detection), `sources.journalctl_since: "60 min ago"` (must cover the
  inter-cycle gap). Federation every 2 cycles = 2 h. Keep-alive is 5 m, so
  qwen2.5-7b-128k:latest (5 m keep-alive) unloads between cycles — each hourly reasoner call
  pays a model reload, and VRAM is free for the embedder in between.
- **Daily report** fires at the first cycle at/after `report.time: 23:55`; the due
  window extends past midnight to D+1 23:55 (`should_report` in scheduler.py,
  cadence-independent) → `reports/YYYY-MM-DD.md`. `--date` is passed explicitly;
  an on-disk guard skips regeneration after daemon restarts.
- **Model:** `qwen2.5-7b-128k:latest` and nothing else (`ollama.model`), since 2026-10-20; the host
  serves a single LLM plus the `nomic-embed-text` embedder. `fallback_model` / `alt_models` were
  **removed** rather than left naming deleted tags — with one installed model a fallback can only
  point at something that no longer exists, and the probe still fails loudly (rc=3). Non-thinking:
  the `/api/show` capability probe reports completion+tools only, so no `think` field is sent.
  Swapped from `qwen3:14b-64k` (thinking) → `qwen2.5-7b-65k` → `qwen2.5-7b-128k:latest`; the hourly
  decision went from ~48 s to ~3 s at the first swap and the 20k-token reasoning blocks that used to
  hold the GPU are gone. `num_ctx` is **131072**, which is the window every other consumer on the
  host already calls (see "`num_ctx` ceiling" below — the old 32768 cap died with the single-card
  host); only
  config-named models are ever used — see "Model selection & the `think` gate" below.
- **Capabilities live in this build:** GPU drift detection, confidence recovery,
  restart-impact + drift-decay metrics, **Manual Stop Protection (HARD INVARIANT)**,
  **Dockhand desired-state drift** (notify-only), **Hatchdoor vault drift**
  (notify-only), **TrueNAS SCALE panel**,
  **Federation Layer** (multi-node cluster reasoning; `dockervm` polls this host's own
  dashboard at `http://127.0.0.1:9120/api/status`; `macstudio` is a **`kind: snapshot`** node whose
  source is the producer file the Mac itself writes (nothing runs on the Mac, and nothing is polled —
  Netdata was retired 2026-10-12); the `truenas` node is
  disabled — see the Federation section for why the old `:8099` endpoints were wrong).
- **Dashboard:** `opsbrain-ui` service on `:9120`, published as https://opsbrain.home
  via Caddy. Panels now include System, Cluster Overview, Node Comparison, TrueNAS,
  **Dockhand**, GPU Drift, OpsBrain Decisions, Container Health, Manual Stop Protection,
  plus the recovery/decay/impact refinements.

## Pipeline & artifacts

```
collector/collector.py         -> logs/collector.json         (unified signal doc, incl. truenas + dockhand + manual_stops)
reasoner/reasoner.py           -> logs/reasoner_result.json   (Qwen decision)
hermes_actions/actions.py      -> logs/actions_result.json    (executed/skipped/blocked, incl. cluster + dockhand recs)
federation/federation_collector.py -> logs/cluster_snapshot.json       (per-node cluster telemetry)
federation/federation_reasoner.py  -> logs/cluster_reasoner_result.json (score/ranking/correlations/recs)
scheduler/scheduler.py         -> daemon loop + daily report (+ run_federation every 2 cycles)
scheduler/report.py            -> reports/YYYY-MM-DD.md
```
Extra runtime files (gitignored): `logs/action_state.json` (sustained-CPU counters &
memory baselines), `logs/gpu_baseline.json`, `logs/gpu_drift_events.jsonl`,
`logs/gpu_daily_stats.json`, `logs/manual_stops.json` (protected registry),
`logs/prev_running.json`, `logs/notifications.jsonl`, `logs/opsbrain.log`,
`logs/reasoner_low_conf.jsonl` (below-floor model text, appended).

## CRITICAL — Qwen3 / Ollama quirk (do not "fix" this back)

`qwen3:14b` via `http://localhost:11434/api/generate` **short-circuits to a bare `{}`
(2 tokens) for large-ish prompts UNLESS**:
1. you send an **explicit `"think": true`** (or false) field, AND
2. you **omit `"format": "json"`**.

Sending `format:json` together with `think` makes Qwen3 emit `{}`. The prompt
(`reasoner/prompt.txt`) already mandates JSON, and `reasoner.extract_json()` recovers the
object with a balanced-brace regex. `reasoner.py` sets `think` from config
(`ollama.think: true`) and does not pass `format`. This was the hardest bug on the build —
leave the call shape alone.

Other calibration notes:
- `num_ctx` is **131072** in config (32768 until 2026-10-22, 65536 before 2026-09-16 — see
  "`num_ctx` ceiling" below); a 25k-char dump made earlier versions collapse, so the
  reasoner feeds Qwen a **compact risk digest** (`summarize_collector`) that only includes
  anomalies (restarting/exited/over-threshold containers, alarms, GPU, disk), not the full
  63-container fleet.
- The model often keys actions as `"action"` not `"type"`; `sanitize()` accepts both.
- **`think` is per model, not global** — a *non*-thinking model (qwen2.5:*) answers
  `think=true` with HTTP 400 `"does not support thinking"`. See the next section.

## Model selection & the `think` gate (Sep 2026 incident)

The operator swapped the ollama model set (removed `qwen3:14b-64k` and
`qwen2.5-coder:14b`, added `qwen2.5:7b` + `deepseek-r1:7b`). The old resolver fell
through to "any model with 'qwen' in the name" and handed the pipeline
`qwen2.5-7b-128k:latest`; `call_ollama` sent `think: true` unconditionally, ollama
answered **HTTP 400**, and the reasoner exited rc=4 **every hour**. The scheduler treats
a failed reasoner stage as non-fatal, so actions kept running against the *previous*
`reasoner_result.json` — stale LLM decisions replayed silently for hours.

Three rules now hold (all in `reasoner.py`):
1. **Only config-named models**: `resolve_model()` → `pick_model()` matches
   `ollama.model`, then `fallback_model`, then `alt_models` (tags compared with
   `:latest` normalized). No wildcard/guess substitution — if none match, the stage
   fails loudly with rc=3 and logs what is installed.
2. **`think` per model**: `fetch_capabilities()` reads `/api/show` →
   `capabilities` (`{"completion","tools","thinking"}`); `decide_think()` sends
   `think` only for models that declare `thinking`. Capability unknown (older ollama)
   keeps sending it, and a 400 whose body mentions thinking is retried **once** without
   `think`. `format:json` is still never sent.
3. **Freshness guard in actions.py**: `reasoner_decision_state(qwen, coll)` compares the
   decision's snapshot timestamp with the collector's; a mismatch suppresses the LLM half
   (deterministic rules still run), logs it, records
   `reasoner: {fresh, reason}` in `actions_result.json`, and notifies at most once per UTC
   day (`reasoner_stale`).

`logs/reasoner_result.json` now also carries `thinking: true|false` so the answering
model's mode is visible at a glance. Recreate the primary tag after a re-pull with
`/appdata/ollama/qwen3-14b-64k.Modelfile` (`FROM qwen3:14b` + `num_ctx 65536`):
`docker cp qwen3-14b-64k.Modelfile ollama:/tmp/ && docker exec ollama ollama create qwen3:14b-64k -f /tmp/qwen3-14b-64k.Modelfile`.
**Current model (2026-10-20):** `/appdata/ollama/qwen2.5-7b-128k.Modelfile` — live build is
`num_ctx 131072, temperature 0.2, top_p 0.95` over a GGUF blob whose parent is `org/qwen2.5-1m:7b`
(1M-trained, so 131072 is not clamped); its Modelfile carries `TEMPLATE {{ .Prompt }}` (no ChatML
block — verified 2026-10-20 that system-prompt adherence, JSON output and tool calls all still work,
but it is not the Qwen chat template the 65k tag had). The base tag `qwen2.5:7b` is no longer
installed; inspect the live build with `docker exec ollama ollama show qwen2.5-7b-128k:latest`.
Measured 2026-10-20: 8.8 GB resident, `100% GPU`, CONTEXT 131072 — and a request may only use half
of that (65,538-token input).

## `num_ctx` ceiling & the 2-day reasoner outage (Sep 2026 incident)

A window that does not fit the card does not error — ollama fits what it can on the GPU, spills
the rest to CPU, caps the effective window, and serves the request slowly. `num_ctx: 65536` made
this card (16 GB RTX 5070 Ti) load the model at 40960 with **8% of the layers on CPU: 74 → 4
tok/s**, so the hourly call exceeded `ollama.timeout` (120 s), the reasoner stage exited **rc=4**,
and actions replayed the 2026-09-14 decision for ~2 days (`reasoner: {fresh: false}` every cycle).

- Config was cut to **32768** (11 GB, 100% GPU) — the reasoner's digest is ~2.5k tokens, so this
  is ~13x headroom. That was the single-card ceiling; re-measure before changing a window.
- **The cap is GONE as of the two-card host (2026-10-22: 32768 → 131072), and the reason is now
  consistency, not fit.** The 7B at 131072 measures 8.8 GB at `100% GPU` / 153 tok/s on the
  27.1 GiB pool, and EVERY other consumer (paperless-ngx, hindsight, paperless-gpt, open-webui)
  calls at 131072. With `OLLAMA_NUM_PARALLEL=1` the runner loads at whatever `num_ctx` the LAST
  caller asked for, so an hourly 32768 call re-sized and reloaded the model for itself and again
  for the next 128k caller — pure churn. The digest is only ~2.5k tokens, so 131072 is pure
  headroom here; verify the CONTEXT column of `docker exec ollama ollama ps` after a cycle.
- The ollama container also gained `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`
  (KV 6.5 GB → 3.4 GB), which removes any spill: 40960 now runs 100% GPU at 66-72 tok/s.
- **The 40960 window cap does NOT move with the KV type** — ollama sizes it as if the KV were
  f16, leaving 3.4 GB unused. Only smaller weights raise it, so the `-64k` tag name is
  aspirational on this card.
- Diagnose with `docker exec ollama ollama ps`: PROCESSOR must read `100% GPU`, and the CONTEXT
  column is the truth about the window. Ladder + mechanism: skill `ollama-local-inference`,
  "VRAM fit".

## Below-floor reasoner capture (added 2026-10-10)

A decision under `actions.qwen_confidence_floor` (0.6) suppresses the LLM half for that cycle, and
until now the only artefact was `confidence: 0.0` — the raw model text was never kept (only the
manual `--raw` flag wrote `logs/reasoner_raw.json`, and the scheduler never passes it), so a
degenerate answer could not be read back afterwards.

`reasoner.capture_low_confidence()` now appends one record per below-floor cycle to
**`logs/reasoner_low_conf.jsonl`**: `ts`, `snapshot`, `model`, `thinking`, `confidence`, `floor`,
`eval_count`, warning/action counts, `summary`, `raw_chars`, and `raw` (capped at 4000 chars), with
a `log.warning("confidence X < floor Y — raw model text kept…")` line in `logs/opsbrain.log`. The
parse-failure path (also 0.0) is captured too, which is what makes "the model said nothing useful"
decidable from "we could not read its output".

**Measured 2026-10-10 (do not chase this as a pipeline bug):** the reasoner is sporadically
degenerate — a live cycle answered `confidence 0.00` with an "all healthy, no findings" summary on
a digest that yields **0.85 / 0.75 / 0.80** across three fresh runs on the same input, and the
confidence key is present in every response (a parse failure looks different: empty findings +
a `parse error:` summary). Over 6,243 logged cycles **7.2 %** sit below the floor; 84 are exactly
0.00, 60 of those with findings. The rate tracks cadence, not load: 26-140/day in the 2-minute era
vs **1-5/day** since the 2026-09-04 hourly retune. The floor does its job — the deterministic rules
keep running — and the dashboard ticker surfaces it as a *warn*.

## Naming / data-source gotchas

- **`dockpeek`** (not "dockpeak") is the real container. It is a Flask app: `/health` is
  the unauthenticated liveness route (the collector's up-check); container data lives at
  `/data` behind the **login wall** (USERNAME/PASSWORD envs). There is **no docker-API
  proxy** — `/api/v1/containers` (and every `/api/*` route) 404s. Container state comes
  from the Docker CLI (`docker inspect dockpeek`).
- **`dozzle`** is published on host port **8081** (compose `8081:8080`); **`dockpeek`** on
  **8001** (`8001:8000`). Host **8080 is open-webui** (usually manually stopped) — the
  old `dozzle.base_url: 8080` config probed a stopped container. Ports documented in
  `/appdata/A--docker_stacks/PORTS.md`.
- **dozzle v10 removed `/api/config`** (404). `collect_dozzle` tries `/api/config` first
  (legacy), then falls back to `/api/version`, which answers `<pre>v10.7.4</pre>`.
- **`last30days-runner`** is the real container (user's "last30days").
- **`Firefox`** is capitalised in Docker; the allow-list gate is **case-insensitive**.
- Netdata `/api/v1/alarms?all` is huge — fetch with `max_bytes=3_000_000` and filter to
  CRITICAL/WARNING/ERROR (the `UNDEFINED` status is noise; there can be ~300 of them).

## Action rules (see config/ops_brain.yaml for thresholds)

- container CPU > 80% sustained 5 min → `docker restart`
- memory creep > 20% over baseline → `docker restart`. Creep denominator is floored at
  `max(base, 1.0)`% (Aug 2026 fix): sub-1% baselines (e.g. dozzle's 0.01%) amplified a
  0.01pp wiggle into 100% "creep", so allow-listed dozzle got restarted EVERY 2-min
  cycle. With the floor, creep means "growth of ≥N percentage points of RAM" (≈190 MiB
  per 20% on this box). Do NOT revert to `1e-3`.
- container restart loop → `docker restart` + notify
- GPU mem > 90% with resident process → `gpu_kill` (needs `allow_gpu_kill: true`)
- disk usage > 85% → `docker system prune -af` + notify (needs `allow_prune: true`)
- Netdata active alarms → surfaced as warnings
- **Qwen confidence < 0.6** → do nothing, log only
- Safety: `restart_limit_per_run: 3` caps destructive restarts per cycle.

## Useful commands

```bash
journalctl -u opsbrain -f                    # live daemon log
systemctl restart opsbrain                   # reload config changes
python3 scheduler/scheduler.py --once        # force a single cycle
python3 scheduler/scheduler.py --report      # generate today's report immediately
./deploy/install.sh                          # install/reinstall the systemd unit
```

## Change procedure

1. This is a live service — config edits require `systemctl restart opsbrain`.
2. Keep everything dry-run unless you have explicit sign-off to remediate.
3. Git: commit modularly (`git log` shows the pattern). `.gitignore` excludes `logs/` and
   `reports/` runtime output — do not commit those.
4. If you touch `actions.py` allow-list/rules or `reasoner.py` sanitize/parse logic,
   RUN THE TESTS: `/usr/bin/python3 -m pytest` (**338 tests**, `tests/`). They cover
   the exact decision-critical code (whitelist gate, deterministic rules, Qwen output
   sanitization, path/persistence) with mocked collector dicts — no live docker/GPU/Ollama
   needed. pytest + `pytest.ini` are in the repo.

## Real-time dashboard (ui/)

Served by `opsbrain-ui` systemd service (FastAPI + WebSocket, port **9120**, binds 0.0.0.0).
Streams `logs/{collector,reasoner_result,actions_result,gpu_baseline,manual_stops,
cluster_snapshot,cluster_reasoner_result}.json` every 2s on change,
merged into one doc `{_meta, sources:{...}, collector, reasoner, actions, gpu_baseline,
manual_stops, cluster_snapshot, cluster_reasoner}`.
- `ui/server.py`: FastAPI app; `GET /` (Jinja2 dashboard.html), `GET /report` (latest md),
  `WS /stream` (push on change + initial snapshot + ping re-push); mtime+size watcher in
  lifespan task. NOTE: `TemplateResponse(request, name)` — newer Starlette arg order.
  Deps: `fastapi`, `uvicorn`, `websockets`, `jinja2`, `pyyaml` (installed globally).
- `ui/config.yaml`: port 9120 / refresh 2s / watch list. `ui/static/app.js` renders the
  panels (System, Cluster Overview, Node Comparison, TrueNAS, Container Health, GPU Drift,
  OpsBrain Decisions, Manual Stop Protection, Daily Report Preview, plus recovery/decay/
  impact refinements from `ui_refinements.js`).
- Service file: `deploy/opsbrain-ui.service` (also installed to `/etc/systemd/system/`);
  `Restart=always` verified. No auth layer — front only via reverse proxy if LAN-exposed.

### Access via Caddy

The dashboard is published behind the homelab reverse proxy as **https://opsbrain.home**:
- Route added to `/appdata/caddy/Caddyfile` (Admin/Monitoring section):
  ```
  opsbrain.home {
      reverse_proxy 192.0.2.10:9120 {
          flush_interval -1   # keep the 2s WS /stream alive through the proxy
      }
      tls internal
  }
  ```
- Caddy runs as the `caddy` container in `network_mode: host`, so it reaches the dashboard's
  `0.0.0.0:9120` directly. `caddy reload` picks up Caddyfile changes; WS upgrade is proxied
  automatically. Verify: `curl -sk --resolve opsbrain.home:443:192.0.2.10 https://opsbrain.home/`
  → 200; WS via raw socket → `101 Switching Protocols` + live 182KB snapshot.

**PITFALL — bind-mount inode staleness.** Editing `/appdata/caddy/Caddyfile` with a write-replacing
tool (e.g. `patch`/`write_file`) creates a NEW inode, but the running caddy bind-mounts the OLD
inode — so `caddy reload` (and even the admin `/load` API) applies the STALE config and the new site
never appears. Symptoms: `grep -c opsbrain /appdata/caddy/Caddyfile` = 1 on host but 0 inside
`docker exec caddy`. Fix: `docker restart caddy` to re-bind the file, then verify
`docker exec caddy grep -c opsbrain /etc/caddy/Caddyfile` = 1. This is a file-bind-mount (not dir)
gotcha; applies to any future edit of the Caddyfile.

## Dashboard refinements (recovery / decay / impact)

Server keeps 10-cycle history in `_hist` and broadcasts three extra WS top-level keys:
- `confidence_recovery` — `{detected, prev, current, delta}`; fires for one cycle when
  confidence strictly increases (frontend shows a green pulse + "recovery" tag). Delta is
  `null` when no event; null confidence = "no observation", never 0.0.
- `drift_decay` — `{vram[], temp[], power[], decay_cycles, status, baselines{}}`. Baselines
  come from `gpu_baseline.json` (last_vram/last_temp/last_power), NOT fabricated from VRAM.
  Tolerances: vram 250MB / temp 5°C / power 40W. status = ok/slow/bad (worst of three).
- `restart_impact` — per-container `{container, score, samples:[{conf_before, conf_1..3}]}`.
  Restarts detected from BOTH `qwen_actions`/`rule_actions` (type+target) AND
  executed/skipped/blocked (verb+target) — there is NO top-level `actions` key in
  actions_result.json (Pro review caught this). conf_before = actions_result.confidence;
  score = mean(conf_i − conf_before) over 3 cycles, `null` when insufficient data.
- These refinements recompute on ANY watched-file change (reasoner/actions writes matter),
  not only collector.json updates.

Frontend: `ui/static/ui_refinements.js` (dsh/Flash-generated) adds `confRecoveryPulse`,
`driftDecayGraph`, `restartImpactGraphs` + CSS; `app.js` renders 3 new panels. Real restart
impact render goes into the live `restartImpact` element (not grid HTML) so _tests_ that
scan `grid.innerHTML` for container names will false-fail.

## TrueNAS panel (real dashboard)

- Collector `collect_truenas()` polls the TrueNAS **JSON-RPC 2.0 over WebSocket**
  API (`wss://truenas/api/current`) — one connection per cycle, `auth.login_ex`
  with `mechanism: PASSWORD_PLAIN` from `~/.smbcred` (username=/password= lines).
  `_truenas_creds` expands the home dir directly with `Path(raw).expanduser()`
  (Cfg.resolve would wrongly prefix REPO onto `~`).
- NEVER go back to REST (`/api/v2.0`, `transport: rest`): TrueNAS 25.10 deprecates
  it, removes it in 26.04, and **every Basic-auth request increments the
  `RESTAPIUsage` alert** — the hourly poll alone logged 4 auths/cycle (96/day) and
  raised "deprecated REST API was used to authenticate N times". The REST path is
  kept only as an explicit opt-in for hosts without `websockets`; the default
  `transport: jsonrpc` must stay. Cert is self-signed, so the client disables
  verification (LAN-only endpoint).
- Calls `pool.query`, `system.info`, `alert.list`, `disk.query` — same payload
  shapes as the old REST `/pool`, `/system/info`, `/alert/list`, `/disk`, so the
  mapping code and dashboard are transport-agnostic. `system/info` exposes
  `cores`/`physical_cores` directly (NOT `system_product` — that is a string like
  "MS-7D53", not a dict).
- `up` is derived from pool data (array reachable + authed). Degrades gracefully:
  a failed websocket/auth returns `up:false` per-method, never raises.
- Merged into collector.json as `truenas`; dashboard server passes it through the
  snapshot (whole collector is merged), and `app.js` `renderTruenas()` shows pool
  status/health/capacity, system version/model/RAM/uptime, disk count, active alerts.
- Watch out: name the alerts array `alerts`, never `alert` (window.alert shadows).

## Federation Layer (multi-node)

- `federation/federation_collector.py` polls each node's collector endpoint and
  reduces it into `logs/cluster_snapshot.json` (`{timestamp, nodes, cluster_metrics}`).
  Tolerates two shapes: a raw collector.json doc, and an OpsBrain `/api/status`
  envelope (`collector:{...}` / `sources.collector.json`). `_num` coerces numeric
  strings (e.g. disk `"42"`, drops `%`).
- `federation/federation_reasoner.py` consumes the snapshot deterministically (no
  LLM) → `logs/cluster_reasoner_result.json`. Stability math is the spec formula:
  `(conf*0.4 + drift*0.3 + anom*0.2 + restarts*0.1)`, where drift/anomalies/restarts
  are reverse-normalized (clamp `1 - n/max_bad`, budgets 20/20/10) to 0..1 then
  scaled to 0..100. An online node with null confidence gets FULL credit (up =
  healthy); an offline node scores 0.
- Cross-node correlation: nodes sharing the same Netdata alarm `name` →
  `anomaly_correlations[]`; same GPU drift flag → `drift_correlations[]`. Signatures
  are read from `raw.collector.netdata.alarms_active[].name` and
  `raw.collector.gpu.drift_flags[]`.
- Deterministic recommendations: any offline node → `cluster_health_warning`;
  score < 60 → `escalate_cluster`; total_anomalies/drift_events > 0 → `notify_cluster`.
  **NOTIFY-ONLY** — the federation layer never performs cross-node remediation.
- `hermes_actions` `federation_decisions()` enqueues these via `Engine.dispatch`
  cluster verbs (`notify_cluster`/`escalate_cluster`/`cluster_health_warning`), all
  gated by `federation.enabled` and executed as notify-only (never remediation).
  Output lands in `actions_result.json` under `cluster.recommendations`.
- Dashboard: `app.js` `renderClusterOverview()` + `renderNodeComparison()` panels;
  server exposes `cluster_snapshot` + `cluster_reasoner` in the WS/`/api/status` doc
  (watch list includes both logs).
- Daily report: `cluster_summary_section()` renders `## Cluster Summary` (score,
  rankings, cross-node anomalies/drift, recommended actions, node online status).
- Scheduler `run_federation(cycle_no)` runs collector+reasoner every
  `federation.poll_interval_cycles` (default 2 cycles = 4 min), gated by enabled.
- **Endpoints were wrong until 2026-10-10** — the Cluster Overview could only ever read
  "0/2 online, stability 0" with a critical `cluster_health_warning` every cycle.
  `collector_endpoint` must be a node's **OpsBrain dashboard `/api/status`** (or a raw
  `collector.json`), and on this host `:8099` is **caddy-editor**, which answers 401/404 —
  never an OpsBrain. Now `dockervm -> http://127.0.0.1:9120/api/status` (online, stability
  ~92-98) and the `truenas` node is **commented out** in `config/ops_brain.yaml` (nothing
  there serves the route; it only produced a standing "1 node(s) offline"). Re-enable it only
  alongside a real per-node OpsBrain.
- An offline node degrades gracefully (`_failed_node` records `err`, the reasoner scores it 0)
  but a *permanently configured* offline node is NOT quiet: it fires `cluster_health_warning`
  (critical) + `escalate_cluster` on every federation cycle. A stale endpoint is a standing
  alarm, not an all-clear.
- Actions consume the federation result from the PREVIOUS cycle (stage order is
  collector → reasoner → actions → federation), so a cluster fix shows in the notifications one
  cycle later — run `python3 scheduler/scheduler.py --once` twice when verifying by hand.
- A one-node cluster is a normal state: summary reads "Single-node cluster (dockervm):
  stability 98.0/100, confidence 0.95." and `recommendations` is empty.

### Snapshot-sourced nodes — the Mac Studio (Netdata retired 2026-10-12)

The Mac Studio (Mac17,14, macOS 27.0.1, Apple M5 Max, 18 cores, 64 GiB, `macstudio.home`) is a cluster
node with **nothing installed on it and no endpoint to poll**: its own 15-minute cron producer writes
`logs/macstudio_node.json` over the SMB share, and for this node that file IS the node —
`kind: snapshot`, merged by `federation/local_snapshot.py` as the PRIMARY source (`primary=True`),
which decides `online` as well as filling the panel's display fields. The Mac's Netdata was removed on
2026-10-12 (Ron: it was reporting false information) and `federation/netdata_node.py` went with it.

- **Measured before retiring, because "migrate or retire" has to be an answer and not a shrug:**
  replaying the captured Netdata alarm fixture through the shipped filter kept **ZERO** alarms — the
  only CRITICAL was `disk_space_usage` on the transient `/Volumes/makemkv_v2.0.0` installer DMG
  (already on the skip-list), the rest were UNINITIALIZED rate alarms or CLEAR — and no Mac Netdata
  alarm name had ever reached `logs/notifications.jsonl`. The alarm path contributed nothing to this
  node, so **nothing was migrated out of it**: `alarm_keep`, `alarm_skip_names`, the 21
  `alarm_skip_mounts` paths, the endpoint and the adapter all went.
- **Why no endpoint — including the `macmon serve` on :9090 the Mac offered:** our federation poll runs
  every 2 h (`poll_interval_cycles` × `interval_seconds`) while the producer ticks every 15 min, so a
  scrape would sample a finer source more coarsely, and it would restore the pinned-IP failure mode
  below. The Mac's `/json` is the complete macmon read (`/metrics` omits `ram_available`), but we take
  it through the file the node already writes. Answer given to the Mac: do not run `serve` on our
  account; loopback only, if they want it for their own tools (never `0.0.0.0` — no auth, no TLS).
- **The pinned-IP failure mode this deleted.** The node used to poll `http://10.1.10.34:19999` (en0, a
  24 h DHCP lease; mDNS does not resolve from dockerVM), so a moved lease, a changed bind or a stopped
  agent read as an **offline node** = a standing critical `cluster_health_warning` on every federation
  cycle (the `truenas` lesson — and the state the cluster actually sat in for two days). The Mac's
  address is now irrelevant to OpsBrain.
- **Two-tier staleness — do not collapse this back to one threshold.** `ok` → the producer's own
  `alert_count` **plus** `derived_alerts`; `stale` (> `snapshot_max_age_s`, 2700 s = 45 min = three
  missed ticks, the producer's own contract) → **one** anomaly and the node stays **ONLINE**, its old
  facts NOT re-judged; `dead` (> `snapshot_dead_after_s`, 7200 s) → **one** anomaly and the node reads
  **OFFLINE**; `missing` → OFFLINE with one anomaly (for a primary node the file IS the node, so the
  old "missing is not a fault" rule is deliberately inverted here); `unreadable` → OFFLINE with one
  anomaly and the error kept. Two tiers because a reboot or an SMB remount is a transient while a
  producer stopped for hours is a fault, and one threshold makes the two look alike — a standing red
  every 2 h is what got the `truenas` node disabled. The cluster score is computed from ONLINE nodes
  only, so an offline node shows as a dark node plus a critical rec, never as a lower score.
- **The merge runs exactly ONCE per node.** For a `kind: snapshot` node `_collect_node` returns an
  empty shell (`_empty_node`, deliberately without `err` — nothing was polled, so nothing failed) and
  `collect_cluster`'s existing `snapshot_file` merge fills it with `primary=True`. Anomaly and alert
  counts are ADDITIVE, so a second merge would double-count one finding; there is a test for it.
- **`derived_alerts` — the collector's own state checks, on fresh data only:** a mount not mounted, a
  mount mounted **read-only**, the gateway heartbeat missing or older than `snapshot_gateway_stale_s`
  (300 s), a Time Machine backup older than `snapshot_tm_stale_hours` (48 h), **memory pressure**
  (`ram_available` under 10 % of RAM, heavy under 5 %) and a **fan at 0 rpm**. Do NOT add a fallback to
  `last_known_backup_age_hours`: the producer's own `alerts[]` already carries the TM verdict and the
  counts are additive, so a fallback would report one condition as two anomalies.
- **Capacity is never judged.** `disks[].used_percent` is surfaced for all six watched volumes (worst
  named, plus a per-volume line), never alerted: 82 % is the Mac's steady state, and the producer's
  `alerts[]` is authoritative and already threshold-applied. `volumes` / `disk_used_percent` now come
  from `disks[]` (total/used/free/percent) instead of a config list — one source of truth, the Mac's.
- **The inbox cron is agent-mediated — expect a parallel session.** The `hermes-docker inbox` cron
  delivers to bot-chat, so a queue ask can spawn a concurrent Hermes session that edits the SAME repo:
  on 2026-10-10 the Mac's `NEEDS ACTION` ask (received 18:22, while this session was mid-build) produced
  a parallel `federation/node_snapshot.py` draft at 18:26, one minute after this session committed
  `local_snapshot.py`, and a careless `git add -A` swept that unreviewed draft into the commit. The draft
  was removed (content preserved in commit `e66a35d` and in
  `/root/.hermes/cache/scratch/node_snapshot.parallel-draft.py`) and its two good ideas — the 45-minute
  staleness contract and the derived state alerts — were folded into the wired module. **Rule: read
  `git status` before staging in a shared repo, and never `git add -A` blind.**
- **The producer immediately surfaced a real Mac-side fault.** Its own alerts carry
  `cron: hermes-patch-watchdog last_status=error streak=533` (a patch that "no longer applies cleanly"),
  so `macstudio` reads `anomalies: 1` and the cluster emits an info `notify_cluster` naming it until
  the Mac fixes it — that is the intended value of the node, not noise to filter.
- `raw` in a node block is **compact by design** (alarm names + GPU drift flags only): that is all the
  reasoner correlates on, and attaching the whole collector doc there grew `cluster_snapshot.json` from
  ~0.5 KB to ~255 KB per cycle. Cluster recommendations also now **name the nodes behind a count**
  ("1 total anomaly(ies) across cluster: macstudio" — a bare count is unactionable in the ticker).
- **macmon (Ron's fork `rpmalouin/macmon`: the menu bar app + a C ABI over upstream `vladkens/macmon`)
  arrives verbatim in the `soc` block of the same file** — `metrics` is macmon's own `Metrics`
  serialization, `soc_info` its `SocInfo`, plus `sampled_at` / `source` / `available` / `error`. The
  producer REPORTS; every percentage, pressure reading and verdict is derived here. The Homebrew
  `macmon` is upstream's older CLI (no `ram_available`, no app) — read the fork, not its docs.
- **RAM has two figures and only one may be thresholded.** `ram_usage` is Activity Monitor's "Memory
  Used" and counts reclaimable inactive pages, so a machine holding a resident ~35 GB model reads
  87-92 % while nothing is wrong: it is rendered, never alarmed on. `ram_available` is the pressure
  figure (macmon's own `StatusLabel.swift` lines — heavy < 5 %, moderate < 10 %; this Mac reads 15-16 %
  with the model loaded) and the only one judged. `ram_available` is 0 when the kernel does not report
  it, and 0 means UNKNOWN, never "out of memory".
- **Never write 0 W in place of a failed macmon read.** `soc.available` / `soc.error` make a failure
  visible; on macOS 27 upstream documents that the CPU/ANE IOReport counters can stop updating without
  a private entitlement, and the fork's CLPC path is what covers it. If the watts turn out to be
  unavailable, the node renders temperatures, memory and usage and drops the power row. Per-tier CPU
  aggregates (P 12 c / S 6 c with frequency and scaled ratio) render; the 18 per-core rows do not —
  they would swamp a two-node table, and they belong in the fixture and the reasoner digest.
- **Node block details:** `confidence: null` (no reasoner on the Mac — an online node with null
  confidence earns FULL credit in the stability formula), `containers: 0/0` (no Docker there),
  `drift_events` / `restart_events` 0, plus display-only `host`
  (model/os/chip/cores/RAM/power/temps/fans/cpu tiers) and `volumes`.
- **`raw` is attached for a snapshot-sourced node too** — `raw.collector.snapshot.alert_keys` carries
  stable tokens (`time_machine`, `cron`, `mount_read_only`, `memory_pressure`, `fan_stopped`, …),
  because `federation_reasoner._alarm_names()` reads `node["raw"]` and a snapshot node has no Netdata
  alarm names to correlate on. Without that key the whole cluster's cross-node correlation reads
  nothing, silently — the identical bug the first `raw` fix addressed for the polled path.
- Verified live 2026-10-12 (`python3 federation/federation_collector.py && python3
  federation/federation_reasoner.py`): 2/2 nodes online; `macstudio` `online: true` with
  `anomalies: 4` (the producer's own, including its two retired-probe alerts until the Mac ships the
  schema-2 producer), `volumes` all six paths derived from `disks[]`, `disk_used_percent: 86.0`, `raw`
  present with `alert_keys`; reasoner → stability 94.0, `node_stability {dockervm 98.0, macstudio
  96.0}`, ONE info `notify_cluster` naming the node and **no `cluster_health_warning`** — the standing
  critical that had been firing since the Mac's Netdata was removed.

## Notification ticker (message, dedupe, level)

- **`Engine.dispatch` OWNS the notification** for every notify-only verb and uses the **reason**
  as the ticker text: `notify(str(reason) or str(target) or "<default>", category)`. The target
  is a count, a list or a name — it must never be relied on as the message (it used to print
  "3", "sonarr", "stale").
- **Callers must NOT call `notify()` after `engine.dispatch()`** — doing so wrote two ticker
  entries per event for months (a bare number plus the real sentence). Deliberate exception:
  `gpu_unavailable_notice` notifies directly, it does not go through dispatch.
- The generic verb is `dispatch("notify", <message>, <explanation>)`; the explanation is NOT the
  category (an LLM sentence landed there and left the entry unclassified). It is now `action`.
- `ui/server.py::_read_notifications_tail` maps category → level: gpu_drift / gpu_reconcile /
  gpu_unavailable / manual_stop / restart / loop / prune / cluster_health_warning /
  reasoner_stale → **critical**; dockhand / vault / cluster_escalate / cluster_notify / disk /
  low_confidence / packet_drop → **warning**; anything else → **info**. Before this map every
  category except gpu_drift/restart rendered as the same yellow "warn" chip, so the ticker read
  as a wall of warnings with no way to spot a real one.
- Tests that reach `notify` must patch `A.NOTIF_FILE`, `A.STATE_FILE` **and** `A.log` (or `R.log`
  for the reasoner) — the first two alone still wrote into the live `logs/opsbrain.log`. All
  four notify/reasoner-touching test files now do this; the suite leaves the live log
  byte-identical (verified: line delta 0). A pytest burst at an off-cycle minute in
  `logs/opsbrain.log`/`notifications.jsonl` is still the signature of a suite that isn't guarded.

- **The ticker is LIVE-ONLY: the ring is a byte offset, seeded at EOF.** `_read_notifications_tail`
  reads only what was APPENDED to `logs/notifications.jsonl` since the previous call. The old
  line-index ring ignored `max_lines` and returned the WHOLE file on the first call after each UI
  start (measured 2026-10-10: 34,262 records), so every `opsbrain-ui` restart pushed the last 100
  historical records — including pre-fix bare-name junk ("cluster", "radarr") — into the panel as
  if they were live, and re-read all 34k lines every cycle. A partial trailing line waits for the
  next call, and a truncated/rotated file re-seeds instead of mis-indexing. `ts` on a
  log-ingested entry is the RECORD's own time, not the dashboard's read time.
- **Every `severity` must be `critical` / `warning` / `info`.** `_check_notifications` wrote
  `"crit"`, which the frontend does not recognise, so the UI-detected conditions fell through to
  the calmest "info" chip: a 0.20-confidence cycle displayed as `info confidence 0.20 < 0.6` while
  it should be the warning the same category gets through the log path (seen live 2026-10-10).
  `ui/static/app.js::notifLevel` also normalizes `crit*`/`warn*`, so an old payload cannot
  reintroduce it.
- **The header chip counts what is wrong, not the backlog size.** It printed the raw list length
  (`⚠ 50`) — a number that never went down and referred to replayed history; it now reads
  `⚠ N critical` (red) or `N warn`, and hides when everything is info.
- **Browser alerts must not be diffed by array length.** `ws.onmessage` did
  `curN.length > prevN`, so once the 100-entry ring saturated the payload stayed pinned at its
  50-entry cap and no desktop alert ever fired again for that tab. The client now scans the list
  each tick and dedupes by `type:message` (`handleNewNotifications`).

- **The ticker must not wait for a collector change.** `logs/notifications.jsonl` is not a watched
  file, and the pipeline writes it AFTER the collector doc (actions run last in a cycle), so the
  ingest used to happen only on the NEXT collector change — up to an hour later, on a panel called
  "Live". `refresh_snapshot` now ingests the ring on every refresh and treats new alerts as a
  reason to push to clients (`changed = True`); verified live 2026-10-10: a forced cycle's records
  were rendered within ~4 s of being written.

## Container-CPU notices need sustained evidence (do not regress)

`stats.cpu_percent` is ONE `docker stats` sample taken at one instant per cycle — it
carries no duration, and a container legitimately spikes while doing real work (a photo
import, a model load, a backup). Qwen turned a single 634 % sample of the ollama
container — a 20-45 s inference burst — into "consuming excessive CPU resources ...
may require optimization or scaling". The deterministic sustained-CPU rule
(`cpu_restart_threshold_percent` held for `cpu_restart_minutes`) is the ONLY authority
for that claim, and it tags its own action `evidence: "sustained_cpu"`.
`actions.filter_llm_notices()` drops a Qwen `notify` that names a container WITH a CPU
token AND a fault token (the phrasing actually observed) unless that container carries
the tag in the same cycle; everything else passes through untouched, and the drop is
logged (`dropped LLM notice without deterministic evidence`). The prompt states the same
rule — the gate is the backstop, not the first line. A phrasing the token match misses
gets through, so treat the prompt as load-bearing here.

## Manual Stop Protection (HARD INVARIANT)

Rule: **"Manually stopped containers must stay stopped"** — OpsBrain must NEVER
restart a container the user manually stopped, overriding autonomous remediation,
confidence gating, restart caps, allow-lists, anomaly/drift remediation, daily
report recommendations, and any Qwen-generated action. Also must not destroy one
via `docker prune`.

Architecture:
- `common/manual_stops.py` — shared `ManualStops` registry (keyed by container ID,
  never auto-forgets; atomic writes; corrupt read fails CLOSED so the invariant is
  never silently dropped) + `classify_manual_stop(exit_code, oom_killed, ...)`.
  Manual = exit 0/143/137 (137-without-OOM favours protection via
  `actions.manual_stop_sigkill_protect`); crash = OOM or other nonzero exits.
  `restart_count` is deliberately NOT part of the rule (Pro review: too narrow).
- `collector/collector.py` — extends the per-container inspect fingerprint
  (`_inspect_state`: id/exit_code/oom_killed/finished_at/started_at) and does
  transition-based detection in `update_manual_stops`: a container that WAS running
  (prev_running.json) and is now exited with a manual exit signature is added.
  First run seeds prev_running (no false protects). Re-arm (running again) clears.
  Emits `manual_stops`/`manual_stop_protected`/`manual_stop_protected_count` in
  collector.json.
- `hermes_actions/actions.py` — `Engine.dispatch` for `docker_restart` checks the
  manual-stop gate FIRST (before ollama guard / allow-list / cap). Blocked records
  carry `reason:"blocked_manual_stop"` + `proposed_reason`. `docker_prune` blocks
  whenever any protected container is currently stopped (docker prune has no per-name
  exclusion). Summary in actions_result.json `manual_stops.{names,count,blocked}`.
- `reasoner/` — injects `manual_stops` into the prompt digest + result; prompt has a
  HARD RULE never to propose restart for them; `sanitize` carries the list. Defense-in-
  depth also drops protected restart proposals in main().
- `ui/` — watches manual_stops.json; `/api/containers` returns `protected` +
  `protected_count`; app.js renders a "MANUALLY STOPPED" red tag (tooltip: "OpsBrain
  will not restart this container.") + a Manual Stop Protection panel.
- `scheduler/report.py` — "Manual Stop Protection Summary" section (count, protected
  list, blocked count, still-stopped list).

Verified live: created a throwaway container, `docker stop`'d it (exit 137, no OOM) →
correctly recorded as manual_stop → reasoner carried it and Qwen proposed 0 restarts.

## Dockhand desired-state ingestion

New source (Aug 2026): `collector/dockhand_ingest.py` reads **Dockhand's local SQLite**
(`/appdata/dockhand/sqlite/db/dockhand.db`) **read-only** (`mode=ro`, safe with its live WAL
writes) as a **desired-state** registry and merges it against actual Docker state. The HTTP
API (`/api/stacks`, `/api/events`) is session-gated and returns `[]`/`"No environment
selected"` — do NOT switch to it; the DB is the source of truth.

- Config under `sources.dockhand`: `db_path`, `compose_root` (maps Dockhand's in-container
  `/app/data/stacks/` → host `/appdata/A--docker_stacks`), `environment_id` (1 = DockerVM),
  `storm_min_events`/`storm_window_s`/`flap_min_transitions`/`image_drift_skip_local_builds`.
- Pipeline: pull → normalize (parse each stack's compose → desired services) →
  `merge_with_docker_actual` → `classify_drift` (8 flags: state/health/replica/image/
  volume/network/dependency/policy + derived compose) → `correlate` (restart storms ≥3
  die/destroy/restart per container in window; health flaps ≥2 toggles, from Dockhand's
  archived `container_events`, ~10k rows) → `create_context_nodes` → `update_dashboard_snapshot`.
  Merged into collector.json under `dockhand`.
- **NOTIFY-ONLY** (deliberate): `actions.dockhand_drift_actions()` emits
  `notify_dockhand_drift` (registered notify-only verb) for drift/storms/flaps/orphans. It
  NEVER proposes `docker_restart`, so it can't override Manual-Stop Protection or the
  allow-list. Reasoner digest carries a compact `dockhand` object.
- **Locally-built services are NOT image drift** (Sep 2026): a service with `build:` gets its
  image from this host, so the tag moves on every rebuild and — once the previous build's
  image loses its tag — `docker ps` reports the bare image ID for the container, which no
  tag-shaped expectation can equal. `hermes-portal` was flagged every hourly cycle that way,
  for a container that was one build behind its own tag. `merge_with_docker_actual` now skips
  the image comparison for `build:` services (knob
  `sources.dockhand.image_drift_skip_local_builds`, default true); the raw compare and the
  skipped pairs stay in the doc (`image_mismatch_raw`, `image_drift_skipped`,
  `classify.image_local_skipped_items`) but feed no flag, no `drift_count`, no dashboard
  `image_mismatch`, no notification. Registry images are compared exactly as before.
  Note the ingest merges against the **prior** cycle's `collector.json` containers, so a
  container recreated mid-hour is still described by the previous snapshot for one cycle.
- Gotchas fixed during build: the matched `actual` record must carry **`labels`** so
  restart-policy inference can see `com.docker.compose.*` (else every compose-managed
  container reads as `no` → policy-drift flood — 11 false positives before this fix);
  matching and orphan detection are **case-insensitive** (Docker capitalises `Firefox`);
  `restart: no` in compose YAML parses to boolean `False` — normalized back to `"no"`;
  service matching is `<stack>_<service>`-scoped so multi-service stacks don't all match
  the same container. (Oct 2026) **Every scalar that gets compared must be env-resolved:**
  `restart: ${RESTART}` used to reach `classify_drift` as a literal template and, against a
  state-inferred actual (`restarting` ⇒ `always`), emitted `restart policy ${RESTART} vs
  always` every hour beside a genuine restart storm (immich, 2026-10-08 — 6 such notices).
  `_service_doc` now resolves `restart`/`ports`/`volumes`/`networks` like `image`, an
  expected value that still contains `${` makes NO policy claim, and the collector carries
  the real `HostConfig.RestartPolicy.Name` (`_inspect_state` → `restart_policy`) which
  `_actual_restart_policy` prefers over inference (`None` = unknown ⇒ no claim, `""` =
  docker reports no policy ⇒ `"no"`).
- Tests: `tests/test_dockhand_ingest.py` (25) + 6 in `test_opsbrain.py` = 31 new.
- Live verify: `python3 collector/collector.py` then inspect `logs/collector.json["dockhand"]`.

## Hatchdoor vault drift ingestion

Source (Aug 2026): `collector/vault_drift_ingest.py` reads the vault-drift report at
`/appdata/OpsBrain/logs/vault_drift_report.json`. **OpsBrain is the CONSUMER only.**
**Producer moved to the Mac (Sep 2026):** the weekly scan runs there — the Mac's Hermes cron
(Monday 03:00 local) fronts the job with a deterministic report writer and writes through the
SMB share (`/Volumes/appdata/OpsBrain/logs/…` = this path), tmp+rename so the collector never
reads a half file. dockerVM's own Hermes carries NO vault job (its cron set is the
`hermes-docker inbox` only), so do NOT add a second writer here — the collector reads whatever
lands in that one file. The report on disk dated `2026-10-05T07:01Z` (a Monday) is that job's
output. (Oct 2026: a dockerVM-side prototype `collector/vault_drift_detect.py` was removed for
exactly this reason — it wrote the file non-atomically, duplicated a subset of the Mac's
findings, and its hardcoded `HATCHDOOR_VAULT_ID` had gone stale after the vault was recreated.)

- Config under `sources.vault_drift`: `enabled`, `report_path`, `max_age_s`
  (default ~8 days for a weekly cron before marking the report `stale`).
- Pipeline: pull (`pull_report`, read/parse-failed => `up:false` gracefully) →
  classify (broken links / orphans / stale metadata / needs-review, capped at 30) →
  `create_context_nodes` (attention: actionable/low/needs_review/stale_report) →
  `update_dashboard_snapshot` (count + human summary). Merged into
  collector.json as `vault_drift`.
- Report shape (the cron writes EXACTLY this): `{timestamp, attention, findings:[{path, issue}]}`.
- **NOTIFY-ONLY** (deliberate): `actions.vault_drift_actions()` emits
  `notify_vault_drift` (registered notify-only verb) for findings/stale; it NEVER
  proposes `docker_restart` or touches the vault filesystem — Hatchdoor owns the
  vault behind its sync engine, so OpsBrain must not remediate it.
- Reasoner digest carries a compact `vault_drift` block; collector presence is
  enough for the dashboard (no server.py change needed).
- Tests: `tests/test_vault_drift_ingest.py` (14). Full suite now 146.
- Verify live: no report => `up:false` gracefully; place a sample report at the
  configured path, run `python3 collector/collector.py` + `actions.py --dry-run`,
  expect `NOTIFY[vault_drift]` and no remediation.

## GPU visibility (an invisible GPU is not an all-clear)

Sep 2026: the host's nvidia userspace was upgraded to 595.91.07 (Sep 11) without reloading
the kernel module (still 595.84), so `nvidia-smi` fails with *"Failed to initialize NVML:
Driver/library version mismatch"* → the collector wrote `gpus: []` → GPU-drift checks
compared nothing and reported nothing (looked healthy) for days. Ollama's container was
unaffected (39/41 layers offloaded, ~15 GB in VRAM) — only host-side NVML/smi is broken.
Fix requires a VM reboot (or unloading/reloading the module with all GPU consumers
stopped). Meanwhile the pipeline now says so:

- `collector.collect_gpu` records `gpu.available` (bool) + `gpu.error` (the nvidia-smi
  text); the baseline keeps `gpu_name`/`total_vram` so "it vanished" stays detectable.
- `actions.gpu_unavailable_notice()` notifies once per UTC day (`gpu_unavailable`) when
  the GPU is invisible *and* the baseline proves one existed; `actions_result.json`
  carries `gpu: {available, error}`.
- `ui/server.py` tolerates an empty gpu list (`gpus[0] if gpus else {}`) and `lifespan()`
  wraps its first `refresh_snapshot()`: the old `(…).get("gpus", [{}])[0]` raised
  IndexError and crash-looped `opsbrain-ui` 65+ times (dashboard dark, restart counter
  climbing). `/api/gpu` exposes `available`/`error`.

### 2026-10-10 recurrence — fixed in place, no reboot

Same failure class, next version step. `apt-daily-upgrade` at **06:47:46** upgraded the whole
`nvidia-*` set **595.91.07 -> 595.99.02**, `nvidia-dkms-595-open` included (so the on-disk
module WAS rebuilt for the new version) — but the LOADED module stayed 595.91.07.
`nvidia-persistenced` died at 06:48:08; `nvidia-smi` then failed with *"Failed to initialize
NVML: Driver/library version mismatch / NVML library version: 595.99"*. The collector caught it
the same way the Sep incident taught it to: `gpu.available:false` + the nvidia-smi text in
`gpu.error`, from the 11:41 cycle onward. The `gpu-watchdog.timer` (5 min, log-only) appended
"nvidia-smi failed" until 12:49:40.

Fixed at 12:51 **without a reboot** (the Sep note says reboot-or-reload; reload is enough):
stop every GPU consumer (`docker stop ollama`) -> `rmmod nvidia_uvm nvidia_drm nvidia_modeset
nvidia` -> `modprobe nvidia` (module now reports 595.99.02) -> **`nvidia-ctk cdi generate
--output=/var/run/cdi/nvidia.yaml`** -> `systemctl start nvidia-persistenced` -> **recreate the
GPU container** (`cd /appdata/A--docker_stacks/ollama && docker compose up -d`) -> `systemctl
restart opsbrain opsbrain-ui`. The last two are the easy-to-miss ones: a container's GPU mount
list is baked at create time from the then-loaded module version, so `docker start` alone keeps
failing on the old-version lib path, and a stale CDI spec asks for paths the new version does
not have.

Verified after the fix: `nvidia-smi` reports 595.99.02; `collector.json` `gpu.available:true`
with `error:null` (RTX 5070 Ti, 16303 MB); ollama's `llama-server` holds ~11.6 GB in VRAM.
`opsbrain`/`opsbrain-ui` were restarted too, because both had initialised against the broken
state and would otherwise have kept serving the cached error.

Cheapest recurrence check, no tools: `nvidia-smi` vs `cat /sys/module/nvidia/version` — a
mismatch IS the bug. Since DKMS rebuilds the module at upgrade time, the only missing step is
reloading it, and any future `apt-daily-upgrade` can reintroduce this silently. GPU drift
detection is BLIND for every cycle that runs before the reload; `gpu.available`/`gpu.error` is
the only signal that it is blind.

(Unrelated defect in the same area, fixed in the same change: `/usr/local/bin/gpu-watchdog.sh`
probed a hardcoded PCI slot `06:10.0` while this VM's card is at `01:00.0`, so its "zombie
state (rev ff)" branch was dead code that could never fire. It also logged no timestamps —
which is why the 06:48-12:49 failure window could not be read off its log.)

### Guards added 2026-10-10 (this failure class should now self-heal or say why it cannot)

- **`/usr/local/sbin/nvidia-module-reconcile`** + **`/etc/apt/apt.conf.d/99-nvidia-module-reconcile`**
  (`DPkg::Post-Invoke`, runs after every apt/dpkg operation incl. unattended-upgrades): compares
  `cat /sys/module/nvidia/version` against `modinfo -F version` of the on-disk `nvidia.ko` for the
  running kernel; on a mismatch it reloads the module, restarts `nvidia-persistenced` and
  regenerates the CDI spec. It **DEFERS** — logs the mismatch plus the remedy, touches nothing —
  while a GPU container holds the device, because rmmod cannot succeed under a live `llama-server`
  and the repair is not worth killing inference. So a mismatch while ollama runs still needs a
  human: `docker stop ollama && /usr/local/sbin/nvidia-module-reconcile --force`. Log:
  `/var/log/nvidia-module-reconcile.log` (written only on a mismatch). `--dry-run` prints the plan.
  Test hooks, unset in production: `NVIDIA_SYS_VERSION_FILE`, `NVIDIA_KO_ROOT`, `NVIDIA_KO_FILE`,
  `NVIDIA_RECONCILE_LOG`. Note the scope: it is an apt hook, so a bare `dpkg -i` or a hand-copied
  module does NOT trigger it.
- **`/usr/local/bin/gpu-watchdog.sh`** (5-min timer, log-only): resolves the card's PCI slot every
  run instead of hardcoding it, extracts the revision from the `(rev xx)` field (the old
  `grep Rev | awk '{print $3}'` also matched unrelated `Rev=` capability lines), timestamps every
  entry, and records WHY nvidia-smi failed plus the loaded module version — enough to tell a
  driver mismatch from a dead card without digging.

## GPU driver reconcile (apt hook + ingest, added 2026-10-10)

The host repairs the loaded nvidia module after a driver upgrade: `/usr/local/sbin/nvidia-module-reconcile`
runs from `/etc/apt/apt.conf.d/99-nvidia-module-reconcile` (`DPkg::Post-Invoke`) and compares
`/sys/module/nvidia/version` (LOADED) with `modinfo -F version` of the on-disk `nvidia.ko` for the
running kernel. On a mismatch it reloads the module, restarts `nvidia-persistenced` and regenerates
the CDI spec — but ONLY when nothing holds the GPU. With a GPU container running it **DEFERS** (rmmod
cannot succeed under a live `llama-server`) and logs the mismatch plus the remedy to
`/var/log/nvidia-module-reconcile.log`. That deferral is the case a human must act on, so OpsBrain
ingests it.

- `collector/gpu_reconcile_ingest.py` reads that log AND compares the live module versions; merged
  into `collector.json` as `gpu_reconcile`. The LOG says what the hook decided, when, which containers
  held the GPU and the exact remedy; the LIVE comparison says whether the mismatch is still true —
  without it, a deferral a human fixed by hand would alarm forever. `state` is driven by the live
  verdict (`ok` = loaded == disk, `mismatch` = live and NVML is broken), the log is context.
- `attention`: `actionable` only while the mismatch is live; `low` = a deferral/failure the hook never
  resolved that is no longer live (informational, and `none` once the hook itself reloaded past it);
  `none` otherwise. A `deferred`/`failed` event older than `max_age_s` is reported as `stale_event`
  and stops reading as news.
- **NOTIFY-ONLY**: `actions.gpu_reconcile_actions()` emits `notify_gpu_reconcile` at most once per UTC
  day while actionable and NEVER restarts/kills/prunes — a driver mismatch is fixed by stopping the
  GPU containers and reloading. Overlaps `gpu_unavailable_notice` on purpose: that one names the
  nvidia-smi error, this one names the versions, the holder, the hook's verdict and the command.
- Reasoner digest key `gpu_reconcile`; `actions_result.json` → `gpu_reconcile.{enabled,state,notifications}`;
  the GPU Drift panel renders a red strip when actionable (with the remedy) and an info line when
  `low`; `/api/gpu` → `reconcile`.
- Config `sources.gpu_reconcile`: `log_path`, `module_version_file`, `ko_root`, `max_age_s`.
- Scope gap worth knowing: the hook is an APT hook, so a bare `dpkg -i` or a hand-copied module writes
  no log line — the live comparison still catches that case (there is a test for it).
- Tests: `tests/test_gpu_reconcile_ingest.py` (27). Those tests patch `A.NOTIF_FILE`/`A.STATE_FILE` AND
  `A.log` — patching the first two alone still let `notify()`'s `log.info` write into the live
  `logs/opsbrain.log`.

## GPU drift detection

Live in collector → reasoner → actions → report. Config under `gpu_drift:`:
- `collector` queries nvidia-smi incl. **power.draw** and **uuid**, writes a persistent baseline to
  `logs/gpu_baseline.json`, and computes deterministic `drift_flags`:
  `vram_drift` (> `vram_creep_mb` jump), `vram_overload` (> `vram_max_percent`),
  `stuck_process` (same NON-ollama pid >= `stuck_pid_cycles` AND util > `util_threshold`),
  `power_drift` / `temp_drift` (high power/temp while util <= threshold). Baseline fields:
  `last_vram, last_pid, last_power, last_temp, cycles_with_same_pid`, plus `per_gpu`.
- **EVERY GPU is evaluated, not just index 0 (do not regress to `gpus[0]`).** This host runs two
  cards (5070 Ti 16 GB + 3060 12 GB), and an index-0-only evaluator toggles silently: the second
  card can sit at 100 %, over `vram_max_percent` or wedged for days with no flag, no event and
  nothing in the UI. `evaluate_gpu_drift` loops over the gpu list, the rules live in
  `_evaluate_one_gpu(g, procs, prev, dr, resident_pids)`, and everything is keyed by
  `str(gpu["index"])`:
  - `baseline["per_gpu"]["<index>"]` carries each card's own
    `last_vram/last_pid/last_power/last_temp/cycles_with_same_pid`; the **top-level `last_*` keys
    stay the GPU-0 baseline in the old shape**, because the reasoner digest, `actions.py`, the
    federation `drift_events` count and `ui/server.py` all read the flat keys and cannot be
    re-purposed. A pre-migration flat baseline is read as GPU 0's entry, so the first run upgrades
    in place — no baseline reset.
  - **Flag vocabulary: only GPU 0 keeps the bare names.** Other cards emit `<flag>:gpu<index>`
    (`vram_overload:gpu1`). The suffix is load-bearing, not cosmetic — actions and kill paths match
    bare strings, so a suffixed flag is visible in collector JSON/UI/events without silently
    widening what an action will act on. A second card needs an explicit opt-in there.
  - **Compute apps are attributed by `gpu_uuid`** (`--query-compute-apps=pid,process_name,
    used_memory,gpu_uuid`); without it a busy process on card 1 becomes card 0's `last_pid` and
    `stuck_process` fires on the wrong GPU. A row with no uuid (older nvidia-smi, hand-built
    fixtures) is treated as belonging to every card, which keeps the single-GPU tests valid.
- **Ollama exclusion (do not regress):** ollama's `llama-server` is a *permanent* GPU
  resident (the local inference backend this pipeline calls every cycle) — by design the
  same PID persists forever. The collector's tracked pid is the first **non-ollama**,
  **non-resident** compute process (`last_pid` = `"0"` when only residents hold the GPU),
  so it can never be counted toward `stuck_process`; `gpu_drift_actions` re-checks via
  `_ollama_owned` / `_resident_owned` and silently drops the flag (no notify/event/state)
  if a stale collector surfaces it.
- **Resident model servers beyond ollama — `gpu_drift.resident_containers` (do not regress).**
  docling-serve is the second permanent GPU resident on this host (it loads its layout/table
  models on `cuda:0` at boot and idles at 0 % util holding ~1.2 GB). The exclusion is by
  **container PID** (`resident_container_pids()` → `docker inspect {{.State.Pid}}`), because
  nvidia-smi names its compute app `/opt/app-root/bin/python3` — no name marker can identify
  it, and the container's PID 1 *is* the compute-app PID. `evaluate_gpu_drift(..., resident_pids=)`
  skips it when picking the tracked pid; the collector records `gpu.resident_pids` and
  `_killable_stuck_pid(procs, last_pid, resident_pids)` refuses to target one.
- **`stuck_process` is the DETERMINISTIC detector's verdict, never the LLM's (2026-10-14 incident,
  do not regress).** The flag requires same pid ≥ `stuck_pid_cycles` **AND** util >
  `util_threshold`; Qwen was observed asserting it from the cycle count alone (62 cycles at 0 %
  util on an idle docling-serve), which produced a `gpu_kill` proposal and a notification claiming
  a kill that `allow_gpu_kill: false` had actually blocked. Two layers now:
  `gpu_drift_actions` calls `stuck_process_corroborated(coll)` (i.e. `stuck_process` in the
  collector's `gpu.drift_flags`) and drops an uncorroborated flag silently; `Engine.dispatch`
  blocks `gpu_kill` with `reason: blocked_no_stuck_evidence` when the engine was constructed
  with `gpu_kill_corroborated=False` — which `main()` always passes — covering the SECOND
  dispatch path (Qwen's own `actions[]` list is dispatched verbatim). Do not duplicate the
  thresholds in the actions layer: the detector is the single source of truth.
- **A kill must report what the ENGINE did.** The kill notification is built from the
  dispatch record's `state`: `killed pid X` only for `executed`, otherwise
  `kill pid X NOT applied (<detail>)`. Announcing an intent as an outcome is what made this
  incident read as a self-inflicted service kill.
- `reasoner` prompt includes the same five rules + `"gpu_drift": []` in the output schema;
  `sanitize()` keeps only known flags (constant `GPU_DRIFT_FLAGS`).
- `actions.gpu_drift_actions(coll, qwen, conf, engine, st)` unions Qwen's `gpu_drift` with
  the deterministic collector flags, but `stuck_process` additionally requires the COLLECTOR's
  flag (`stuck_process_corroborated`) — an LLM-only verdict is dropped silently:
  - **stuck_process (corroborated) AND conf > 0.8 AND resolvable pid → `gpu_kill` pid + notify**
    (the notify quotes the dispatch state: `killed pid X` only when the kill executed)
  - vram_drift | power_drift | temp_drift | vram_overload → **notify only**
  - **ollama restart is hard-blocked** in `Engine.dispatch` (policy guard) even if allow-listed.
  Drift events append to `logs/gpu_drift_events.jsonl`; daily maxima/remediations roll into
  `logs/gpu_daily_stats.json` (reset each UTC date).
- `report` renders a `## GPU drift` section (24h events, peak VRAM/temp, stuck pids, current
  state, remediation actions) via `render_gpu_drift_section` (dsh/Flash-generated).

## Tests

`/usr/bin/python3 -m pytest` (or `-q`). **338 tests** across `tests/` covering:
- `test_opsbrain.py` — whitelist gate (case-insensitive), deterministic rules (CPU
  sustain, memory creep, GPU threshold, restart loop, disk prune, Netdata alarms),
  `sanitize`/`extract_json` (Qwen normalization, type-or-action key, gpu_drift flag
  filtering), `evaluate_gpu_drift`, `gpu_drift_actions`, ollama-restart guard,
  digest trimming, `Cfg.resolve` & JSON round-trip, `dockhand_drift_actions`,
  `summarize_collector` dockhand digest.
- `test_manual_stops.py` (+ `test_collector_manual_stops.py`) — ManualStops registry,
  classify_manual_stop (exit codes incl. sigkill protect), transition detection,
  hard-gate dispatch + prune-block.
- `test_truenas.py` — TrueNAS collector parse, unreachable/disabled degradation, creds.
- `test_federation.py` — `_num`/`_cnt` coercion, reverse_normalize, cluster stability
  score (spec math), node stability (null-conf-online credit, offline=0), ranking,
  recommendations, empty/all-offline, cross-node anomaly + drift correlation.
- `test_ui_refinements.py` — confidence recovery, drift decay, restart impact, and the
  empty-gpu history guard (`_update_history` with `gpus: []`).
- `test_dockhand_ingest.py` (25) — Dockhand DB pull (host path mapping, missing-DB /
  missing-table), compose `normalize`, service-matching (exact/prefix/case-insensitive),
  all 8 drift classifications, restart-storm / health-flap correlate, context nodes cap,
  dashboard snapshot.
- `test_reasoner_models.py` (23) — model selection (`pick_model`/`resolve_model`: primary
  → fallback → alt, `:latest` normalization, and *never* an unconfigured substitution),
  the `think` gate (`decide_think` for thinking / non-thinking / unknown capability),
  `build_body` (think sent, `format` never sent), `call_ollama` (400-with-thinking →
  single retry without `think`; unrelated 400 propagates), and the stale-decision guard
  `actions.reasoner_decision_state`.
- `test_gpu_visibility.py` (7) — `collect_gpu` surfacing `available`/`error` and keeping the
  baseline identity, plus `actions.gpu_unavailable_notice` (once per UTC day, silent when
  the GPU is visible or was never there).
- `test_gpu_reconcile_ingest.py` (27) — parsing the apt hook's log (real line format,
  truncated/handwritten lines skipped, an `rmmod ... failed` warning attached to the attempt
  instead of read as a failed reload), the live loaded-vs-disk comparison, `classify`'s
  deferral-resolved-by-a-later-reload logic, context/attention states, and the notify-only
  action (once per UTC day, never a remediation verb).

These caught real bugs during development — `sanitize` crashed on null confidence, and
run/parse edge cases. GPU/kill tests mock `run()`/`notify` so they never touch live
docker/GPU/Ollama.
