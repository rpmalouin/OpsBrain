# Configuration — `ops_brain.yaml`

All tuning lives in `config/ops_brain.yaml`. This is the reference for its top-level
sections.

## Core

```yaml
hostname: dockerVM              # label used in reports/notifications
interval_seconds: 120           # pipeline cadence (2 min)
ollama:
  base_url: http://localhost:11434
  model: qwen3:14b
  fallback_model: qwen2.5-coder:14b
  think: true                   # REQUIRED — see docs/reasoning-llm.md
```

## `actions` — remediation engine

```yaml
actions:
  dry_run: true                 # <-- SAFETY. false = real remediation (see remediation.md)
  restart_limit_per_run: 3      # cap docker/systemctl restarts per cycle
  cpu_restart_threshold_percent: 80.0
  cpu_restart_minutes: 5        # sustained CPU needed before restart
  mem_creep_threshold_percent: 20.0   # memory growth vs baseline
  gpu_mem_threshold_percent: 90.0
  disk_threshold_percent: 85.0
  qwen_confidence_floor: 0.6    # below this -> do nothing and log
  allow_restart_containers:      # names the engine may restart (case-insensitive)
    - homepage
    - dozzle
    # ... list YOUR containers ...
  allow_service_restart: []      # systemd units whitelist (shell-injection safe)
  allow_gpu_kill: false
  allow_prune: true
  notify_webhook: ""             # optional POST-JSON webhook (ntfy / Telegram bot)
  manual_stop_sigkill_protect: true   # treat 137-without-OOM as a manual stop
```

## `sources` — data endpoints

```yaml
sources:
  netdata:
    base_url: http://localhost:19999
    enabled: true
  dozzle:
    base_url: http://localhost:8080
    enabled: true
  dockpeek:
    base_url: http://localhost:8081
    enabled: true
  truenas:                        # optional — see docs/truenas.md
    base_url: http://truenas/api/v2.0
    creds_file: ~/.smbcred
    enabled: true
    timeout_s: 8
  docker_socket: /var/run/docker.sock
  gpu_query: whitelisted
  journalctl_since: "2 min ago"
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
  stuck_pid_cycles: 5          # same GPU PID cycled this many times + util>thr -> stuck_process
  util_threshold: 10           # GPU util % treated as "idle" for drift
  power_idle_max_watts: 40
  temp_idle_max_c: 55
```

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