# Changelog

All notable changes to **Ops Brain** are documented here. Follows [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/) and **SemVer**. Version numbers map to
git tags (`v0.1.0` … `v0.5.0`); the current release is **v0.5.0** (federation-layer
milestone).

## [Unreleased]

### Fixed
- **Cross-node correlation was dead code.** `federation_reasoner.detect_cross_correlations()` reads
  `node["raw"]["collector"]["netdata"]["alarms_active"]` / `["gpu"]["drift_flags"]`, but
  `federation_collector._collect_node()` never put a `raw` key in the node block — so
  `anomaly_correlations` / `drift_correlations` could never fire, for any node, in any cluster
  size. The collector now attaches `raw`; a regression test collects two nodes and asserts the
  shared alarm name and drift flag are correlated.
- **Cluster Overview could only ever read red.** `federation.nodes` pointed `dockervm` at
  `http://dockervm:8099/api/status` — and `:8099` on this host is *caddy-editor* (401/404), not
  an OpsBrain — while `truenas` pointed at a box with nothing listening. Both nodes were
  therefore permanently offline, so the panel showed "0/2 online, stability 0" and the pipeline
  raised a critical `cluster_health_warning` + `escalate_cluster` every federation cycle. The
  `dockervm` node now polls this host's own dashboard (`http://127.0.0.1:9120/api/status`, the
  route that actually serves the `/api/status` envelope) and the unservable `truenas` node is
  commented out in the config. Verified live: 1/1 online, stability 98/100, no recommendations.
- **Notification ticker: unreadable and duplicated entries.** `Engine.dispatch` used the
  *target* as the notification text, and callers passed the event COUNT there — so the ticker
  showed bare values ("3", "1", "sonarr", "stale") and, because the callers also called
  `notify()` themselves after dispatching, every event was written twice (a bare number plus the
  real sentence). dispatch now notifies with the **reason** (the ticker entry's text), each
  caller notifies once, and the reasons are self-contained sentences (e.g. "Dockhand restart
  storm sonarr: 4 in 30m"). The generic `notify` verb no longer stores the caller's explanation
  as the CATEGORY.
- **Every notification rendered as the same yellow "warn" chip.** `ui/server.py`
  `_read_notifications_tail` only treated `gpu_drift`/restart categories as critical and mapped
  everything else to warning, so a blocked manual stop, a GPU driver mismatch or a cluster node
  down was indistinguishable from a vault note. Categories now map to critical / warning / info.
- **The test suite wrote into the live logs.** `logs/opsbrain.log` and
  `logs/notifications.jsonl` are append-only pipeline output, but four suites reached
  `notify()`/the shared reasoner logger without redirecting them, so a pytest run left lines at
  an off-cycle minute that read as real cycle output. All four now patch the ticker/state files
  AND the logger; the suite leaves the live log byte-identical (verified: 0-line delta).

### Added
- **Node snapshot files — the Mac's Phase 2 producer is consumed (`snapshot_file`).** A node entry may
  carry `snapshot_file` / `snapshot_max_age_s`, pointing at a JSON file the node itself writes into
  this host's `logs/` (OpsBrain is the consumer only, the vault-drift rule). The Mac's 15-minute cron
  supplies what no poll can reach — Time Machine's last successful backup, per-mount health, Hermes
  cron health, the gateway heartbeat, the local model router and the loopback-only services
  (Portal 8087, Hindsight 9177/19177, llama.cpp 18434) — and `federation/local_snapshot.py` merges it
  into the same node block, with the producer's own `alert_count` becoming the node's anomalies (a
  **stale** file counts once, a **missing** one is not a fault but is stated as
  `snapshot.state: missing`, an unreadable one keeps its error). Host facts merge with the live poll's
  values winning. Verified live: `macstudio` reports `anomalies: 1` from its own
  `cron: hermes-patch-watchdog … streak=533` alert, `snapshot.state: ok`, `age_s` under 5 minutes.
- **Cluster recommendations name the nodes behind them.** A ticker entry that read
  "1 total anomaly(ies) across cluster" was unactionable; with per-node sources it now reads
  "1 total anomaly(ies) across cluster: macstudio" (same for GPU drift events).
- **`raw` in a node block is compact** (alarm names + GPU drift flags only). It exists for cross-node
  correlation, and attaching the whole collector document there had grown `cluster_snapshot.json` from
  ~0.5 KB to ~255 KB per cycle for no extra signal (now ~5 KB).
- **Netdata-sourced cluster nodes (`kind: netdata`) — the Mac Studio joins the cluster.**
  `federation.nodes[]` entries now select their source with `kind` (`opsbrain`, the default, or
  `netdata`). A `netdata` node is reduced from a host's own Netdata HTTP API by the new
  `federation/netdata_node.py` (`/api/v1/info`, `/api/v1/alarms?all`, per-volume
  `/api/v1/data?chart=disk_space.<mount>`, `system.ram`, `system.uptime`) into the same node block
  an OpsBrain node produces — so a machine that runs Netdata but not Ops Brain (the Mac Studio,
  `macstudio`, `type: macos`, `http://10.1.10.34:19999`) contributes host facts, watched-volume
  usage and its own active alarms to the cluster, with **nothing installed on it and nothing
  written to it**. It is deliberately filtered: only CRITICAL/WARNING/ERROR alarms count, minus a
  configured `alarm_skip_names`/`alarm_skip_mounts` list — without it the Mac's single CRITICAL
  (`disk_space_usage` on a transient **mounted installer DMG**) would be a permanent cluster
  anomaly and a `notify_cluster` every federation cycle. `volumes:` is likewise explicit: the host
  exposes 3288 charts, mostly Time Machine snapshot mounts. Two measurement traps are handled in
  the adapter: `disk_space.*` dimensions are **GiB, not percent**, and macOS RAM "used" is
  `active + wired + compressor` (the naive `total - free` reads ~98 % on a healthy box).
  `collect_cluster` also honours a node-level `timeout_s`. Verified live: 2/2 nodes online,
  `macstudio` stability 100/100, `anomalies: 0`, `disk_used_percent` 82.3 (Data2).
  The Mac's own answer to the wiring questionnaire (2026-10-10) widened the skip-list to the mounts
  that must never become this node's anomaly: **SMB volumes belonging to another machine**
  (`/Volumes/appdata` and `/Volumes/hermes` are dockerVM shares, `/Volumes/media` is TrueNAS), plus
  personal archives and macOS system views — a remote box's fullness is not this host's fault. It also
  confirmed the standing CRITICAL is **stale** (the DMG was not mounted when checked, but Netdata keeps
  serving the chart) and that `.34` is a 24 h DHCP lease, so durability depends on a router
  reservation. A test loads the shipped config and asserts the whole exclusion list, while proving a
  watched volume (`/Volumes/Data2`) still raises.
- **GPU driver-reconcile ingest (`gpu_reconcile`, notify-only).** The host's new
  `/etc/apt/apt.conf.d/99-nvidia-module-reconcile` hook (`DPkg::Post-Invoke` →
  `/usr/local/sbin/nvidia-module-reconcile`) compares the LOADED nvidia module with the one on
  disk after every apt operation and reloads it on a mismatch — the failure that silently blinds
  every host-side GPU read (`Failed to initialize NVML: Driver/library version mismatch`). With a
  GPU container running it cannot reload (rmmod fails under a live `llama-server`), so it logs the
  mismatch plus the remedy and defers instead.
  `collector/gpu_reconcile_ingest.py` now ingests that log AND compares the live module versions,
  merged into `collector.json` as `gpu_reconcile`: `state` (`ok`/`mismatch`/`deferred`/`failed`)
  comes from the live verdict, the log supplies the holders and the exact remedy, and a deferral
  the hook later reloaded past stops reading as an open item. `attention: actionable` drives
  `actions.gpu_reconcile_actions()` → `notify_gpu_reconcile` (once per UTC day, never a
  remediation verb), a red strip in the dashboard's GPU Drift panel, the `gpu_reconcile` reasoner
  digest block, `actions_result.json.gpu_reconcile` and `/api/gpu → reconcile`.
  Config: `sources.gpu_reconcile.{log_path,module_version_file,ko_root,max_age_s}`.
  +27 tests.
- **Per-model `think` gate + strict model selection.** `think` is now decided per model
  instead of globally: `reasoner.fetch_capabilities()` reads `/api/show` and
  `decide_think()` sends `think` only for models that declare the `thinking` capability
  (unknown capability keeps the old behaviour and a `400 ... does not support thinking` is
  retried once without it). `format:json` is still never sent. Model choice is limited to
  `ollama.model` → `fallback_model` → `alt_models` (`:latest` normalized) — the old
  "any qwen" fallback is gone, and if none of the named models is installed the stage
  exits **rc=3** and logs what *is* installed instead of reasoning with a model nobody
  chose. `logs/reasoner_result.json` now records `thinking` alongside `model`.
  +18 tests.
- **GPU visibility (`gpu.available` / `gpu.error`).** `collect_gpu` records why the GPU is
  unreadable (e.g. `Failed to initialize NVML: Driver/library version mismatch`) instead of
  writing an empty list that reads like an all-clear, `actions.gpu_unavailable_notice()`
  notifies once per UTC day when a GPU the baseline knows about has vanished,
  `actions_result.json` carries `gpu: {available, error}`, and the dashboard exposes both
  via `/api/gpu`. +7 tests.
- **Stale-decision guard.** `actions.reasoner_decision_state()` compares the decision's
  collector snapshot with the current one: on a mismatch the LLM half of the cycle is
  suppressed (deterministic rules still run), the reason is logged, recorded as
  `reasoner: {fresh, reason}`, and notified at most once per UTC day. +5 tests.
- **Resilient model-output parsing.** `reasoner.extract_json()` now recovers the decision
  object from fenced, prose-wrapped, or repeated model output (balanced-brace candidates,
  preferring the latest that carries a decision key) — deepseek-r1 needed this as a
  fallback model. +5 tests.
- `tests/test_reasoner_models.py` (23) and `tests/test_gpu_visibility.py` (7) — see
  docs/tests.md. Suite is now **200 tests**.

### Fixed
- **Restart-policy drift invented from an unresolved compose template.** `_service_doc`
  interpolated `${VAR}` for `image` but handed `restart` through raw, so a service whose
  compose says `restart: ${RESTART}` reached `classify_drift` as the literal `${RESTART}`;
  the actual side then *inferred* a policy from container state (anything `restarting` read
  as `always`), so a flapping container emitted `restart policy ${RESTART} vs always` every
  cycle beside its genuine state drift (immich, 2026-10-08 — hourly for 5 h during a real
  restart storm). Three changes: `_service_doc` resolves `restart`, `ports`, `volumes` and
  `networks` through `_resolve_compose_env` exactly like `image`; `classify_drift` makes
  **no** policy claim while the expected value still contains `${` (an unresolvable template
  is not a policy); and the collector now carries each container's real
  `HostConfig.RestartPolicy.Name` (`collector._inspect_state` → `restart_policy`), which
  `_actual_restart_policy` prefers over its evidence-based fallback. Verified against the
  live Dockhand DB: 53 stacks / 69 services parse with zero unresolved templates, and
  immich-server's expected policy reads `unless-stopped` rather than `${RESTART}`. +12 tests.
- **Image drift on locally-built services, reported forever.** A compose service with
  `build:` produces its image on this host, so its tag moves on every rebuild; once the
  previous image loses its tag `docker ps` reports the bare image ID for the running
  container, which a tag-shaped expectation can never equal — `hermes-portal` was flagged on
  every hourly cycle (and by Qwen as "image drift requiring verification") for a container
  that was simply one build behind its own tag. `merge_with_docker_actual` now skips the
  image comparison for `build:` services (`sources.dockhand.image_drift_skip_local_builds`,
  default `true`). The raw compare is still recorded (`image_mismatch_raw`,
  `image_drift_skipped`) and the skipped pairs are published as
  `classify.image_local_skipped_items`, which feeds no flag, no `drift_count`, no dashboard
  `image_mismatch` and no notification; registry images are compared exactly as before.
  +5 tests.
- **Hourly reasoner timeout from an over-large context window.** `ollama.options.num_ctx` was
  65536, which does not fit this 16 GB card: ollama silently capped the window at 40960, spilled
  ~8% of the layers to CPU and dropped throughput 74 → 4 tok/s, so the call blew past
  `ollama.timeout` (120 s) and the stage exited **rc=4** — for ~2 days, with actions replayed
  against the 2026-09-14 decision and only `reasoner: {fresh: false}` marking it. Config is now
  **32768** (11 GB, `100% GPU`; the digest is ~2.5k tokens, so ~13x headroom). The ollama
  container also gained `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`, which removes
  the spill entirely — note these buy throughput, not window: the 40960 cap is unchanged. The
  sizing rule, the measurement ladder and the diagnosis (`docker exec ollama ollama ps`) are in
  docs/reasoning-llm.md.
- **Reasoner silently dead after an Ollama model swap.** Removing the configured primary
  model made the old resolver substitute an unrelated non-thinking 7B, which Ollama
  answered with `HTTP 400 "does not support thinking"`; the scheduler treats a failed
  reasoner stage as non-fatal, so actions kept replaying the *previous*
  `reasoner_result.json` for hours (stale LLM warnings/actions re-notified every cycle).
  Fixed by the per-model `think` gate, strict model selection, and the stale-decision
  guard above.
- **Dashboard crash-loop on an unreadable GPU.** `ui/server.py` indexed
  `collector.gpu.gpus[0]` while building history; with `gpus: []` (host `nvidia-smi`
  failing) `_update_history` raised `IndexError`, application startup failed, and
  `opsbrain-ui` restart-looped 65+ times — the dashboard was dark exactly when the GPU
  was broken. History now tolerates an empty list and `lifespan()` starts degraded
  (logging `initial snapshot failed`) rather than not at all. +3 tests.
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

### Changed
- **Scheduler cadence 120s → 3600s (1 h)** — homelab retune (2026-09-04) to free
  ollama/GPU for interactive chat (Open WebUI shares the same daemon). Rescaled with
  it: `cpu_restart_minutes: 5 → 120` (a lone hourly sample = 3600s would otherwise
  trip the 300s restart threshold instantly), `gpu_drift.stuck_pid_cycles: 5 → 2`,
  `sources.journalctl_since: "2 min ago" → "60 min ago"` (must span the inter-cycle
  gap). Federation cadence unchanged in cycles → now 2 h.
- **Daily report trigger is now cadence-independent.** `should_report()` no longer
  exact-matches the `%H:%M` report minute (which an hourly phase would never hit);
  a report for date D is due from D's `report.time` until the same time on D+1, so
  the first cycle at/after 23:55 fires it (post-midnight straddle included). The
  scheduler passes `--date <day>` explicitly and skips firing when the day's report
  file already exists (restart-safe). +8 tests (`tests/test_scheduler_cadence.py`).

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