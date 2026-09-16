# Configuration — `ops_brain.yaml`

All tuning lives in `config/ops_brain.yaml`. This is the reference for its top-level
sections. Values below mirror the live file.

## Core

```yaml
hostname: dockerVM              # label used in reports/notifications
interval_seconds: 3600          # scheduler poll cadence (1 h — see "Cadence" below)
ollama:
  url: http://localhost:11434/api/generate
  model: qwen3:14b-64k          # primary (must be installed; thinking — effective window is
                                # capped at 40960 by VRAM regardless of the 64k tag/request)
  fallback_model: deepseek-r1:7b   # thinking-capable 7B, used only if the primary is absent
  alt_models:                   # last resort; `think` is omitted for non-thinking models
    - qwen2.5-7b-128k:latest
  timeout: 120
  think: true                   # sent per model — see docs/reasoning-llm.md
  options:
    temperature: 0.2
    num_ctx: 32768            # ceiling: a window that does not fit spills to CPU (docs/reasoning-llm.md)
    keep_alive: 5m
```

Only the models named here are ever used; if none is installed the reasoner stage fails
with rc=3 instead of substituting an arbitrary model (docs/reasoning-llm.md).

### Cadence

The scheduler polls every `interval_seconds` (**3600 s / 1 h** in this deploy — was 120 s
before the 2026-09 homelab retune that frees the GPU for interactive chat between
cycles). Several knobs are scaled to that cadence and must move together:

- `actions.cpu_restart_minutes: 120` — a single 3600 s sample must not trip the sustained
  CPU rule.
- `gpu_drift.stuck_pid_cycles: 2` — ~2 h of the same GPU PID before `stuck_process`.
- `sources.journalctl_since: "60 min ago"` — must cover the gap between cycles.
- `federation.poll_interval_cycles: 2` — federation runs every 2 h.

## `actions` — remediation engine

```yaml
actions:
  dry_run: true                 # <-- SAFETY. false = real remediation (see remediation.md)
  restart_limit_per_run: 3      # cap docker/systemctl restarts per cycle
  cpu_restart_threshold_percent: 80.0
  cpu_restart_minutes: 120      # sustained CPU needed before restart (>= 2 hourly samples)
  mem_creep_threshold_percent: 20.0   # memory growth vs baseline (floor: 1% of RAM)
  gpu_mem_threshold_percent: 90.0
  disk_threshold_percent: 85.0
  qwen_confidence_floor: 0.6    # below this -> do nothing and log
  allow_restart_containers:      # names the engine may restart (case-insensitive)
    - homepage
    - dozzle
    # ... list YOUR containers ... GUI/VNC browsers are intentionally excluded:
    # a restart destroys the in-use session and docker's own restart policy covers crashes
  allow_service_restart: []      # systemd units whitelist (shell-injection safe)
  allow_gpu_kill: false
  allow_prune: true
  notify_webhook: ""             # optional POST-JSON webhook (ntfy / Telegram bot)
  manual_stop_sigkill_protect: true   # treat 137-without-OOM as a manual stop
```

A blocked rule action does **not** notify — it only appears in `logs/actions_result.json`.

## `sources` — data endpoints

```yaml
sources:
  netdata:
    base_url: http://localhost:19999
    enabled: true
  dozzle:
    base_url: http://localhost:8081
    enabled: true
  dockpeek:
    base_url: http://localhost:8001
    enabled: true
  truenas:                        # optional — see docs/truenas.md
    base_url: http://truenas/api/v2.0
    creds_file: ~/.smbcred
    enabled: true
    timeout_s: 8
  dockhand:                       # optional desired-state source — see docs/dockhand.md
    enabled: true
    db_path: /appdata/dockhand/sqlite/db/dockhand.db
    compose_root: /appdata/A--docker_stacks
    environment_id: 1
    storm_min_events: 3
    storm_window_s: 1800
    flap_min_transitions: 2
  vault_drift:                    # Hatchdoor vault findings — notify-only
    enabled: true
    report_path: /appdata/OpsBrain/logs/vault_drift_report.json   # written by an external cron
    max_age_s: 691200             # ~8 days before the report is flagged stale
  docker_socket: /var/run/docker.sock
  gpu_query: whitelisted
  journalctl_since: "60 min ago"
```

Disable any source you don't run (`enabled: false`); the pipeline degrades gracefully.

## `federation` — multi-node

See [docs/federation.md](federation.md).

## `manual_stop_protection` — hard invariant

See [docs/manual-stop-protection.md](manual-stop-protection.md). On by default.

## `gpu_drift`

```yaml
gpu_drift:
  enabled: true
  vram_creep_mb: 250           # VRAM increase between cycles that flags vram_drift
  vram_max_percent: 90         # VRAM usage % of total that flags vram_overload
  stuck_pid_cycles: 2          # same GPU PID this many cycles + util>thr -> stuck_process (~2 h)
  util_threshold: 10           # GPU util % treated as "idle" for power/temp drift
  power_idle_max_watts: 40
  temp_idle_max_c: 55
```

ollama's `llama-server` is a permanent GPU resident and is excluded from stuck-PID
tracking by design. If the host cannot read the GPU at all (`nvidia-smi` failing), the
collector records `gpu.available: false` + `gpu.error` and a `gpu_unavailable`
notification fires once per UTC day.

## `report`, `paths`, `logging`

```yaml
report:
  time: "23:55"                # daily ops report
  dir: reports
  retention_days: 30
paths:                          # defaults to repo log layout; override if relocating
  collector_json: logs/collector.json
  reasoner_json: logs/reasoner_result.json
  actions_json: logs/actions_result.json
  baseline_json: logs/baseline.json
  gpu_baseline: logs/gpu_baseline.json
  gpu_drift_events: logs/gpu_drift_events.jsonl
  gpu_daily_stats: logs/gpu_daily_stats.json
  manual_stops: logs/manual_stops.json
  prev_running: logs/prev_running.json
  cluster_snapshot: logs/cluster_snapshot.json
  cluster_reasoner: logs/cluster_reasoner_result.json
logging:
  level: INFO
```

Config changes require a service restart: `systemctl restart opsbrain opsbrain-ui`.
