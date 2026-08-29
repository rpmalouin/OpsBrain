# Changelog

All notable changes to **Ops Brain** are documented here. Follows [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/) and **SemVer**. Version numbers map to
git tags (`v0.1.0` … `v0.5.0`); the current release is **v0.5.0** (federation-layer
milestone).

## [Unreleased]

### Added
- **Dockhand desired-state drift ingestion.** New `collector/dockhand_ingest.py` reads
  Dockhand's local SQLite registry (read-only; the HTTP API is session-gated) as a
  **desired-state** source, merges it against actual Docker state, and classifies drift
  across `state` / `health` / `replica` / `image` / `volume` / `network` / `dependency` /
  `policy` (+ derived `compose`). It also **correlates** Dockhand's archived
  `container_events` into **restart storms** and **health flaps** (signals the 2-min
  collector can't see itself). Feeds the reasoner digest, a notify-only
  `notify_dockhand_drift` action verb (never auto-restarts — keeps Manual-Stop Protection
  intact), and a new **Dockhand** dashboard panel. `sources.dockhand` config section.
  +26 tests (132 total): 20 in `tests/test_dockhand_ingest.py`, 6 across
  `tests/test_opsbrain.py`.
- `docs/dockhand.md` — feature guide.

### Fixed
- **Dozzle/Dockpeek false "not running" alerts.** Both containers were running but the
  collector probed the wrong hosts: `dozzle.base_url` pointed at host 8080 (actually
  **open-webui**, manually stopped -> connection refused) and `dockpeek.base_url` at 8081
  (actually **dozzle** -> HTTP 404), because the stack publishes dozzle on **8081** and
  dockpeek on **8001**. Config updated to the real ports. Probe routes also fixed for
  version drift: dozzle v10 removed `/api/config` (fallback to `/api/version`, which
  answers `<pre>v10.7.4</pre>`), and dockpeek is a Flask app with **no docker-API proxy**
  — its liveness route is `/health` (container data lives at `/data` behind the login
  wall). Qwen went from "Dozzle and Dockpeek services are not running" (conf 0.9x) every
  cycle to "System is healthy" (conf 1.00). +5 tests (106 total).
- **Ollama llama-server no longer flagged as a stuck GPU process.** The GPU-drift
  detector treated any long-lived GPU PID as `stuck_process`, but ollama's
  `llama-server` is a *permanent* resident (the local inference backend this pipeline
  calls every cycle) — same PID forever, so it fired `stuck_process` every few minutes
  (40+ notifications/day, 30 of 47 drift events in 24h, dead PIDs accumulating in
  `gpu_stuck_pids` state). The collector now tracks only the first **non-ollama**
  compute PID (`last_pid` becomes `"0"` when only ollama holds the GPU), and
  `gpu_drift_actions` defensively drops/suppresses the flag (no notify, no drift event,
  no state accumulation) if a stale collector still surfaces it. A genuine stuck job
  sitting next to ollama is still tracked and killed as before. +3 tests (101 total).

## v0.5.0 — 2026-08-27

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

## v0.4.0 — 2026-08-27

### Added
- Dashboard refinements: **confidence recovery** pulse + tag, **GPU drift decay** curve,
  **container restart impact** bars (9 tests).
- `ui/static/ui_refinements.js` (Flash-generated) for the new panels.
- Persisted group-filter config; event-driven `opsbrain-ui` systemd unit (`Requires`, `After`).

### Changed
- Confidence recovery / drift decay / restart impact recompute on any watched-file change,
  not just collector updates.

## v0.3.0 — 2026-08-27

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

## v0.2.0 — 2026-08-27

### Added
- 36 unit tests for decision-critical logic; fixed a `sanitize` null-confidence crash.
- `allow_restart_containers` whitelist (19 containers), case-insensitive gate.
- `MEMORY.md` (operational memory, Qwen3/Ollama quirk).

## v0.1.0 — 2026-08-27

### Added
- **Core pipeline:** `collector` (Netdata, Dozzle, Dockpeek/Docker-socket, `nvidia-smi`,
  `df`/`top`/`journalctl`), `reasoner` (Qwen3 14B via local Ollama),
  `hermes_actions` (remediation engine w/ safety gates: dry-run default, allow-lists,
  restart cap), `scheduler` (2-min loop + 23:55 daily report).
- `config/ops_brain.yaml`, `common/` helpers, Dockerfile / docker-compose, deploy/systemd,
  README.