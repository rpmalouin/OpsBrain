# Ops Brain — MEMORY.md

Operational memory for the Ops Brain project. Read this before changing anything in this
repo. It captures the decisions, gotchas, and current running state that are expensive to
re-derive.

## What this is

A unified **collector → reasoner → action** pipeline on dockerVM. Polls Netdata, Dozzle,
Dockpeek/Docker-socket, `nvidia-smi`, and VM commands (`df`/`top`/`journalctl`) every 2
minutes, asks **Qwen 3:14B** (local Ollama) for a structured JSON decision, then executes
safe remediation through whitelists/dry-run.

Repo root: `/appdata/OpsBrain` (git repo, `main` branch).

## Current running state (LAST UPDATED: 2026-08-27)

- **Service:** systemd unit `opsbrain` — ACTIVE, enabled, auto-restarts.
  `ExecStart: python3 /appdata/OpsBrain/scheduler/scheduler.py --daemon`
- **Mode:** `actions.dry_run: true` — **observing only, NOT remediating live.**
  Flip to `false` + `systemctl restart opsbrain` to allow real actions.
- **Restart whitelist** (`actions.allow_restart_containers`, 19 containers): homepage,
  dozzle, dockpeek, netdata, caddy, caddy-editor, calibre, calibre-web, dockscope,
  filerise, firefox, flacsentry, grimmory, homelab-hub, homepage-editor,
  last30days-runner, librewolf, yamtrack, youtube-dl-server. Everything else blocked.
- **Cadence:** cycle every 120s; daily report at `report.time: 23:55` →
  `reports/YYYY-MM-DD.md`.
- **Model:** `qwen3:14b` (pulled locally). Fallback `qwen2.5-coder:14b`.

## Pipeline & artifacts

```
collector/collector.py      -> logs/collector.json       (unified signal doc)
reasoner/reasoner.py        -> logs/reasoner_result.json (Qwen decision)
hermes_actions/actions.py   -> logs/actions_result.json  (executed/skipped/blocked)
scheduler/scheduler.py      -> daemon loop + daily report
scheduler/report.py         -> reports/YYYY-MM-DD.md
```
Extra runtime files (gitignored): `logs/action_state.json` (sustained-CPU counters &
memory baselines), `logs/notifications.jsonl`, `logs/opsbrain.log`.

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
- `num_ctx` is 32768 in config; a 25k-char dump made earlier versions collapse, so the
  reasoner feeds Qwen a **compact risk digest** (`summarize_collector`) that only includes
  anomalies (restarting/exited/over-threshold containers, alarms, GPU, disk), not the full
  63-container fleet.
- The model often keys actions as `"action"` not `"type"`; `sanitize()` accepts both.

## Naming / data-source gotchas

- **`dockpeek`** (not "dockpeak") is the real container; its HTTP API returns 404 on the
  Docker-proxy route, so the collector relies on the **Docker socket/docker CLI** as the
  primary container-state source (same data dockpeek's UI shows). `collect_dockpeek` is a
  best-effort probe.
- **`last30days-runner`** is the real container (user's "last30days").
- **`Firefox`** is capitalised in Docker; the allow-list gate is **case-insensitive**.
- Netdata `/api/v1/alarms?all` is huge — fetch with `max_bytes=3_000_000` and filter to
  CRITICAL/WARNING/ERROR (the `UNDEFINED` status is noise; there can be ~300 of them).

## Action rules (see config/ops_brain.yaml for thresholds)

- container CPU > 80% sustained 5 min → `docker restart`
- memory creep > 20% over baseline → `docker restart`
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
   RUN THE TESTS: `python3 -m pytest` (36 tests, `tests/test_opsbrain.py`). They cover
   the exact decision-critical code (whitelist gate, deterministic rules, Qwen output
   sanitization, path/persistence) with mocked collector dicts — no live docker/GPU/Ollama
   needed. pytest + `pytest.ini` are in the repo.

## Real-time dashboard (ui/)

Served by `opsbrain-ui` systemd service (FastAPI + WebSocket, port **9120**, binds 0.0.0.0).
Streams `logs/{collector,reasoner_result,actions_result,gpu_baseline}.json` every 2s on change,
merged into one doc `{_meta, sources:{...}, collector, reasoner, actions, gpu_baseline}`.
- `ui/server.py`: FastAPI app; `GET /` (Jinja2 dashboard.html), `GET /report` (latest md),
  `WS /stream` (push on change + initial snapshot + ping re-push); mtime+size watcher in
  lifespan task. NOTE: `TemplateResponse(request, name)` — newer Starlette arg order.
  Deps: `fastapi`, `uvicorn`, `websockets`, `jinja2`, `pyyaml` (installed globally).
- `ui/config.yaml`: port 9120 / refresh 2s / watch list. `ui/static/app.js` renders 5 panels.
- Service file: `deploy/opsbrain-ui.service` (also installed to `/etc/systemd/system/`);
  `Restart=always` verified. No auth layer — front only via reverse proxy if LAN-exposed.

## GPU drift detection

Live in collector → reasoner → actions → report. Config under `gpu_drift:`:
- `collector` queries nvidia-smi incl. **power.draw**, writes a persistent baseline to
  `logs/gpu_baseline.json`, and computes deterministic `drift_flags` (primary GPU index 0):
  `vram_drift` (> `vram_creep_mb` jump), `vram_overload` (> `vram_max_percent`),
  `stuck_process` (same pid >= `stuck_pid_cycles` AND util > `util_threshold`),
  `power_drift` / `temp_drift` (high power/temp while util <= threshold). Baseline fields:
  `last_vram, last_pid, last_power, last_temp, cycles_with_same_pid`.
- `reasoner` prompt includes the same five rules + `"gpu_drift": []` in the output schema;
  `sanitize()` keeps only known flags (constant `GPU_DRIFT_FLAGS`).
- `actions.gpu_drift_actions(coll, qwen, conf, engine, st)` unions Qwen's `gpu_drift` with
  the deterministic collector flags:
  - **stuck_process AND conf > 0.8 AND resolvable pid → `gpu_kill` pid + notify**
  - vram_drift | power_drift | temp_drift | vram_overload → **notify only**
  - **ollama restart is hard-blocked** in `Engine.dispatch` (policy guard) even if allow-listed.
  Drift events append to `logs/gpu_drift_events.jsonl`; daily maxima/remediations roll into
  `logs/gpu_daily_stats.json` (reset each UTC date).
- `report` renders a `## GPU drift` section (24h events, peak VRAM/temp, stuck pids, current
  state, remediation actions) via `render_gpu_drift_section` (dsh/Flash-generated).

## Tests

`python3 -m pytest` (or `-q`). **53 tests** covering:
- `allow_container` / `allow_service` whitelist gate (including case-insensitivity)
- `deterministic_rules`: CPU sustain window, memory creep, GPU threshold, restart loop,
  disk prune, Netdata alarms
- `sanitize` / `extract_json`: Qwen output normalization (accepts `type` or `action` key,
  drops unknown verbs, clamps confidence, tolerates `null`), `gpu_drift` flag filtering
- `collector.evaluate_gpu_drift`: all five drift flags + baseline bookkeeping
- `actions.gpu_drift_actions`: stuck_process kill (conf>0.8) vs notify-only (conf<=0.8),
  notify-only for the other four flags, Qwen∪deterministic flag union
- `Engine.dispatch` ollama-restart guard
- `summarize_collector` digest trimming, `pct` parser, `Cfg.resolve` & JSON round-trip

These caught real bugs during development — `sanitize` crashed on null confidence, and
run/parse edge cases. GPU tests mock `run()`/`notify` so they never touch docker/kill.
