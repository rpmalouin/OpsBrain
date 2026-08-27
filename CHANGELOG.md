# Changelog

All notable changes to **Ops Brain** are documented here. Follows [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/) and **SemVer**.

## [Unreleased]

- Nothing yet.

## [0.5.0] — 2026-08-27

### Added
- **Federation Layer (multi-node)** — reason about many nodes as a unified cluster.
  - `federation/federation_collector.py` polls each node's collector endpoint → `logs/cluster_snapshot.json`.
  - `federation/federation_reasoner.py` computes cluster stability score (0–100), node
    ranking, and cross-node anomaly/drift correlations → `logs/cluster_reasoner_result.json`.
  - New action verbs `notify_cluster` / `escalate_cluster` / `cluster_health_warning`
    (**notify-only** — never cross-node remediation).
  - Dashboard **Cluster Overview** + **Node Comparison** panels; daily report **Cluster Summary** section.
  - Scheduler runs federation every `poll_interval_cycles` (default 2 cycles = 4 min).
  - 10 new federation tests. Full suite: **98 tests**.
- **TrueNAS SCALE panel** — collector polls `/pool`, `/system/info`, `/alert/list`,
  `/disk` (basic auth from `~/.smbcred`, degrades gracefully); dashboard **TrueNAS** panel.

### Fixed
- **Manual Stop Protection (HARD INVARIANT)** — "Manually stopped containers must stay
  stopped." Transition-based detection in the collector (prev-running → exited with a
  manual exit signature), registry keyed by container ID (never auto-forgets), hard gate
  checked first in the action dispatcher (overrides allow-list/caps/LLM), and a block on
  `docker prune` of protected containers. Dashboard "MANUALLY STOPPED" tag + panel;
  report section.
- **Manual stop classification** — uses exit-code/OOM signature (0/143/137-without-OOM)
  rather than the too-narrow `restart_count == 0` rule. Configurable via
  `manual_stop_sigkill_protect`.
- All-offline cluster no longer scores a misleading "healthy" value — scores 0.
- Online node with null confidence now gets full stability credit (was 0).

## [0.4.0] — 2026-08-27

### Added
- Dashboard refinements: **confidence recovery** pulse + tag, **GPU drift decay** curve,
  **container restart impact** bars (9 tests).
- `ui/static/ui_refinements.js` (Flash-generated) for the new panels.
- Persisted group-filter config; event-driven `opsbrain-ui` systemd unit (`Requires`, `After`).

### Changed
- Confidence recovery / drift decay / restart impact recompute on any watched-file change,
  not just collector updates.

## [0.3.0] — 2026-08-27

### Added
- **Real-time dashboard** (`ui/`, FastAPI + WebSocket, `:9120`) + `opsbrain-ui` systemd
  service — 5 live panels, event-driven (inotify) push with a 30s fallback.
- **GPU drift detection** end-to-end (collector baseline + power/flags, reasoner
  `gpu_drift` schema, actions gpu kill/notify with an **ollama-restart hard-block**,
  report GPU section, tests).
- Pro-review GPU fixes: baseline-validated kill PID, GPU identity re-baseline, util<thr
  boundary, daily `stuck_pids`, unhealthy precedence (+3 tests).
- Daily report **GPU drift** section (24h events, peaks, stuck pids, remediations).
- Documented the Caddy reverse proxy for the dashboard (`opsbrain.home`) + the bind-mount
  inode reload gotcha.

## [0.2.0] — 2026-08-27

### Added
- 36 unit tests for decision-critical logic; fixed a `sanitize` null-confidence crash.
- `allow_restart_containers` whitelist (19 containers), case-insensitive gate.
- `MEMORY.md` (operational memory, Qwen3/Ollama quirk).

## [0.1.0] — 2026-08-27

### Added
- **Core pipeline:** `collector` (Netdata, Dozzle, Dockpeek/Docker-socket, `nvidia-smi`,
  `df`/`top`/`journalctl`), `reasoner` (Qwen3 14B via local Ollama),
  `hermes_actions` (remediation engine w/ safety gates: dry-run default, allow-lists,
  restart cap), `scheduler` (2-min loop + 23:55 daily report).
- `config/ops_brain.yaml`, `common/` helpers, Dockerfile / docker-compose, deploy/systemd,
  README.