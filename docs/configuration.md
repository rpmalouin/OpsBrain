# Configuration — `ops_brain.yaml`

All tuning lives in `config/ops_brain.yaml`. This is the reference for its top-level
sections. Values below mirror the live file.

## Core

```yaml
hostname: dockerVM              # label used in reports/notifications
interval_seconds: 3600          # scheduler poll cadence (1 h — see "Cadence" below)
ollama:
  url: http://localhost:11434/api/generate
  model: qwen2.5-7b-128k:latest  # the host's only LLM (must be installed; non-thinking, so no
                                # `think` field is sent); no fallback_model/alt_models by design
  timeout: 120
  think: true                   # sent per model — see docs/reasoning-llm.md
  options:
    temperature: 0.2
    num_ctx: 131072           # matches every other consumer on the host (docs/reasoning-llm.md)
    keep_alive: 5m
```

Only the models named here are ever used; if none is installed the reasoner stage fails
with rc=3 instead of substituting an arbitrary model (docs/reasoning-llm.md).

### Cadence

The scheduler polls every `interval_seconds` (**3600 s / 1 h** in this deploy — was 120 s
before the 2026-09 homelab retune that frees the GPU for interactive chat between
cycles). Several knobs are scaled to that cadence and must move together:

- `actions.cpu_restart_minutes: 120` — a single 3600 s sample must not trip the sustained
  CPU rule. That rule is also the only authority for a *container-CPU* claim: the sample is
  one instantaneous reading with no duration, so an LLM-authored notify naming a container
  with high CPU is dropped unless this rule fired for it in the same cycle
  (`actions.filter_llm_notices`).
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
    transport: jsonrpc            # jsonrpc = wss://<host>/api/current (supported)
    ws_url: wss://truenas/api/current
    creds_file: ~/.smbcred
    enabled: true
    timeout_s: 8
    # transport: rest + base_url: http://truenas/api/v2.0 = deprecated by
    # TrueNAS 25.10 (removed 26.04) and counted by its RESTAPIUsage alert.
  dockhand:                       # optional desired-state source — see docs/dockhand.md
    enabled: true
    db_path: /appdata/dockhand/sqlite/db/dockhand.db
    compose_root: /appdata/A--docker_stacks
    environment_id: 1
    storm_min_events: 3
    storm_window_s: 1800
    flap_min_transitions: 2
    image_drift_skip_local_builds: true   # `build:` services are built here; a rebuilt tag is churn, not drift
  vault_drift:                    # Hatchdoor vault findings — notify-only
    enabled: true
    report_path: /appdata/OpsBrain/logs/vault_drift_report.json   # written by an external cron
    max_age_s: 691200             # ~8 days before the report is flagged stale
  gpu_reconcile:                  # NVIDIA driver/library mismatch — notify-only
    enabled: true
    log_path: /var/log/nvidia-module-reconcile.log   # written by the driver-reconcile apt hook
    module_version_file: /sys/module/nvidia/version  # the LOADED module
    ko_root: /lib/modules         # on-disk module: <ko_root>/<uname -r>/**/nvidia.ko*
    max_age_s: 604800             # 7 days: older hook events are history, not news
  docker_socket: /var/run/docker.sock
  gpu_query: whitelisted
  journalctl_since: "60 min ago"
```

Disable any source you don't run (`enabled: false`); the pipeline degrades gracefully.

## `federation` — multi-node

See [docs/federation.md](federation.md). Nodes carry a `kind`: `opsbrain` (default — an OpsBrain
dashboard `/api/status` or a raw `collector.json`) or `netdata` (a host's Netdata API, reduced by
`federation/netdata_node.py` with per-node `volumes`, `alarm_keep`, `alarm_skip_names`,
`alarm_skip_mounts` and `timeout_s`). A node may also carry `snapshot_file` / `snapshot_max_age_s`
(default 2700 s) / `snapshot_tm_stale_hours` (48) / `snapshot_gateway_stale_s` (300) — a JSON file the
node itself writes into `logs/` for what no poll can see (Time Machine, mount and cron health,
loopback-only services), merged by `federation/local_snapshot.py`.

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
  resident_containers: [docling-serve]   # permanent GPU model servers: never stuck candidates
  power_idle_max_watts: 40
  temp_idle_max_c: 55
```

ollama's `llama-server` is a permanent GPU resident and is excluded from stuck-PID
tracking by design. `resident_containers` extends the same treatment to containers whose GPU
process is a **model server** — a process that holds VRAM forever and idles at 0 % util
between jobs, which is why the cycle counter alone means nothing for it. It is matched by
the container's own PID (`State.Pid`), not by a process name: nvidia-smi reports the compute
app as the interpreter path (`/opt/app-root/bin/python3` for docling-serve), so no name
marker can identify it. Entries must name a real container; a member is skipped by
`evaluate_gpu_drift` when picking the tracked PID, appears in the collector's
`gpu.resident_pids`, and is never a `_killable_stuck_pid` target. Note that
`stuck_process` itself is only ever raised by the collector (same pid **and** util >
`util_threshold`): the reasoner's LLM half cannot introduce it, and `gpu_kill` is blocked
outright when that deterministic flag is absent (`blocked_no_stuck_evidence`). If the host
cannot read the GPU at all (`nvidia-smi` failing), the collector records
`gpu.available: false` + `gpu.error` and a `gpu_unavailable` notification fires once per UTC day.

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
