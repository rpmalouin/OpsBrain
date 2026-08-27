# Ops Brain

A unified **collector → reasoner → action** pipeline for a homelab. It consolidates
metrics, logs, container state, GPU state, VM state, and storage (TrueNAS) on each node,
asks a local LLM (Qwen3) for a structured decision, and executes **safe, whitelisted**
remediation — all gated by dry-run by default. It also reasons about **multiple nodes as
a unified cluster** (federation) and streams everything to a real-time dashboard.

- **Poll:** Netdata, Dozzle, Dockpeek (Docker socket), `nvidia-smi`, `df`/`top`/`journalctl`,
  TrueNAS SCALE REST API
- **Reason:** Qwen 3:14B via local Ollama (`http://localhost:11434/api/generate`) for
  node decisions; deterministic math for cluster reasoning
- **Act:** Docker restart / prune, systemctl restart, GPU kill, webhook notifications
  (dry-run by default; whitelist + restart-cap gated)
- **Protect:** **manual-stop protection** (a container you stopped stays stopped — hard
  invariant that overrides all remediation)
- **Federate:** reason about many nodes as one cluster (stability score, node ranking,
  cross-node anomaly/drift correlation)
- **Report:** daily ops digest at `23:55`
- **Watch:** real-time dashboard over WebSocket (`:9120`)

## Layout

```
/appdata/OpsBrain/
  collector/          polls & merges all sources -> logs/collector.json (incl. truenas, manual_stops)
  reasoner/           Qwen prompt template + Ollama wrapper -> logs/reasoner_result.json
  hermes_actions/     remediation module + deterministic RULES -> logs/actions_result.json
  federation/         multi-node cluster collector + reasoner -> logs/cluster_{snapshot,reasoner_result}.json
  scheduler/          cron-like controller loop + daily report generator
  common/             Cfg (config), ManualStops registry, JSON/log helpers
  config/             ops_brain.yaml (all settings/thresholds)
  logs/               runtime JSON + opsbrain.log + notifications.jsonl
  reports/            daily markdown reports
  ui/                 real-time dashboard (FastAPI + WebSocket, :9120)
  deploy/             systemd units + installer
  tests/              pytest suite (98 tests)
  docs/               per-feature implementation guides
  Dockerfile, docker-compose.yml, README.md, IMPLEMENTATION.md, CHANGELOG.md, .gitignore
```

## Real-time dashboard

A live single-page dashboard streams `collector`, `reasoner`, `actions`, `gpu_baseline`,
`manual_stops`, and the federation `cluster_snapshot`/`cluster_reasoner` logs over
WebSockets and refreshes every 2 seconds. Panels: **System Overview, Cluster Overview,
Node Comparison, TrueNAS, Container Health, GPU Drift, OpsBrain Decisions, Manual Stop
Protection, Daily Report Preview**, plus confidence-recovery / GPU-drift-decay /
restart-impact refinements. Served by the `opsbrain-ui` systemd service at
`http://<host>:9120/` — see `ui/README.md`.

## Quick start (recommended: on a Linux VM with Docker + NVIDIA GPU)

Requires: Python 3.10+, `PyYAML`, the `docker` CLI, `nvidia-smi`, and Ollama with
`qwen3:14b` pulled (`ollama pull qwen3:14b`).

```bash
git clone <repo> /appdata/OpsBrain && cd /appdata/OpsBrain
python3 -m venv .venv && . .venv/bin/activate
pip install pyyaml fastapi uvicorn websockets jinja2   # pyyaml required; dashboard deps for ui

# one-shot test of the whole pipeline (SAFE: dry-run by default)
python3 scheduler/scheduler.py --once

# install as a persistent systemd service (collect->reason->act every 2 min)
sudo ./deploy/install.sh   # installs opsbrain.service; opsbrain-ui.service also available
```

**See `IMPLEMENTATION.md` for the entry-point index and `docs/` for full per-feature
guides** (deployment, configuration, reasoning-LLM, remediation, dashboard, manual-stop
protection, TrueNAS, federation, security, tests, troubleshooting). Release history in
`CHANGELOG.md`.

## How one cycle works

1. **collector/collector.py** — polls Netdata (`:19999`), Docker (`/var/run/docker.sock`),
   Dozzle (`:8080`), Dockpeek (`:8081` probe), `nvidia-smi`, host commands
   (`df -h`, `top`, `journalctl --since "2 min ago"`), and TrueNAS SCALE (`/pool`,
   `/system/info`, `/alert/list`, `/disk`). Also runs manual-stop transition detection.
   Merges into one JSON doc → `logs/collector.json`.
2. **reasoner/reasoner.py** — renders `reasoner/prompt.txt` with a *compact risk digest*
   of the collector output, asks Qwen3 14B for a structured JSON decision
   `{warnings, actions[{type,target,reason}], summary, confidence}`, normalizes it, and
   injects the manual-stop list (HARD RULE: never propose restarts for them) →
   `logs/reasoner_result.json`.
3. **hermes_actions/actions.py** — enforces the **action rules** plus the **manual-stop
   hard gate** (checked first), then executes the union of Qwen's actions + deterministic
   rules through SAFETY gates (dry-run, allow-lists, restart cap). Also dispatches
   **federation recommendations** (notify-only). → `logs/actions_result.json`.
4. **federation/** (every 2 cycles) — `federation_collector.py` polls each node endpoint
   → `logs/cluster_snapshot.json`; `federation_reasoner.py` computes cluster stability,
   node ranking, and cross-node correlations → `logs/cluster_reasoner_result.json`.
5. **scheduler/scheduler.py --daemon** loops 1–4 every `interval_seconds` (120s) and, at
   `report.time` (23:55), emits `reports/YYYY-MM-DD.md`.

## Action rules (config/ops_brain.yaml)

| Condition                                      | Action                       | Gate            |
|------------------------------------------------|------------------------------|-----------------|
| container CPU > 80% sustained 5 min        | `docker restart`            | cap/allow-list  |
| container memory creep > 20% over baseline    | `docker restart`            | cap/allow-list  |
| container in restart loop                   | `docker restart` + notify | cap/allow-list  |
| GPU memory > 90% with resident process       | `gpu_kill`                  | `allow_gpu_kill`|
| **GPU drift**: stuck_process + conf>0.8    | `gpu_kill` + notify         | conf>0.8 + pid  |
| **GPU drift**: vram/power/temp/overload    | notify only                | —              |
| disk usage > 85%                            | `docker system prune -af` + notify | `allow_prune`  |
| Netdata ACTIVE alarm                        | surfaced as warnings       | —              |
| Qwen confidence < 0.6        | **do nothing, log only**   | —              |

**Safety first:** `actions.dry_run: true` by default. Nothing actually happens until you
set `actions.dry_run: false`. Even then, `allow_restart_containers` /
`allow_service_restart` whitelist which targets may be touched, `restart_limit_per_run`
caps destructive restarts, and the **manual-stop hard invariant** overrides everything.

## Manual Stop Protection (HARD INVARIANT)

**"Manually stopped containers must stay stopped."** OpsBrain must NEVER restart a
container you manually stopped — this overrides autonomous remediation, confidence
gating, restart caps, allow-lists, anomaly/drift remediation, daily report
recommendations, and any Qwen-generated action. It must also not destroy one via
`docker prune`.

- Detection is **transition-based** in the collector: a container that WAS running and is
  now exited with a manual exit signature (0 / 143 / 137-without-OOM) is recorded in
  `logs/manual_stops.json`, keyed by container **ID** (never auto-forgets).
- The action engine checks this gate **first** in dispatch, and blocks `docker prune` when
  any protected container is stopped.
- The dashboard shows a **"MANUALLY STOPPED"** tag and a **Manual Stop Protection** panel.
- Re-arm: starting the container again clears protection.

## Federation Layer (multi-node)

OpsBrain can reason about multiple nodes as one cluster. Configure `federation.nodes`
with each node's collector snapshot endpoint; every 2 cycles the federation collector
merges them and the reasoner computes:

- **Cluster stability score** (0–100): `(conf*0.4 + drift*0.3 + anomalies*0.2 + restarts*0.1) * 100`
  (drift/anomalies/restarts reverse-normalized to 0..1)
- **Node ranking** by stability
- **Cross-node anomaly / drift correlations** (shared Netdata alarm names, shared GPU flags)
- **Recommended cluster actions** — **NOTIFY-ONLY** (notify_cluster / escalate_cluster /
  cluster_health_warning). The federation layer never performs cross-node remediation;
  per-node remediation still obeys confidence gating, manual-stop protection, and caps.

The dashboard surfaces a **Cluster Overview** (stability, avg confidence, totals) and a
**Node Comparison** table; the daily report gains a **Cluster Summary** section.

Federation degrades gracefully: if a node endpoint is unreachable it is marked offline,
the cluster score drops, and a health warning is emitted rather than any automation.

## Model routing (Qwen3 — see IMPLEMENTATION.md)

This project relies on **Qwen3 14B** for node reasoning. Qwen3's local Ollama generation
must be driven without `format="json"` **and** with an explicit `think` flag — the
default (think unset + `format:json`) makes Qwen3 short-circuit to an empty `{}`.
`reasoner.py` sets `think: true` (config `ollama.think`) and relies on the prompt +
robust JSON extractor. If `qwen3:14b` is unavailable it falls back to
`qwen2.5-coder:14b`. **Cluster reasoning is deterministic (no LLM)**.

## Files you get

- `logs/collector.json`                 unified signal doc (2-min cadence)
- `logs/reasoner_result.json`           Qwen decision `{warnings, actions, summary, confidence}`
- `logs/actions_result.json`            executed / skipped / blocked per cycle (+ `cluster` recs)
- `logs/manual_stops.json`              protected (manually stopped) container registry
- `logs/cluster_snapshot.json`          per-node cluster telemetry
- `logs/cluster_reasoner_result.json`   cluster stability / ranking / correlations / recs
- `logs/notifications.jsonl`            notification queue (webhook / local)
- `logs/action_state.json`              persisted counters (sustained CPU, baselines)
- `logs/gpu_baseline.json` `logs/gpu_drift_events.jsonl` `logs/gpu_daily_stats.json`
- `reports/YYYY-MM-DD.md`               daily ops report

## Tests

`python3 -m pytest` — **98 tests** across `tests/` (whitelist gate, deterministic rules,
Qwen sanitization, GPU drift, manual-stop invariant, TrueNAS parse, federation math &
correlations, dashboard refinements). Run before pushing changes to `actions.py`,
`reasoner.py`, `common/`, `collector/`, or `federation/`.

## Troubleshooting

- `collector works but reasoner returns {}` — ensure `ollama.think: true` and that the
  model is warm: `curl localhost:11434/api/tags`.
- `hermes_actions` skips everything → is `actions.dry_run` true? (it should be; flip it
  only after you trust the rules).
- `docker.sock` permission → run the collector/actions as `root` or in the `docker` group.
- GPU metrics empty → ensure `nvidia-smi` is on `$PATH` for the running user.
- Federation shows nodes offline → the endpoints in `federation.nodes` aren't reachable;
  each node must serve an OpsBrain `/api/status` (or raw collector.json) snapshot. Test
  locally by pointing them at a running dashboard `http://<host>:9120/api/status`.

## License

Released under the [MIT License](LICENSE). Free to use, modify, and distribute —
see `LICENSE` for the full terms.

## Containerized alternative

`docker-compose up -d` provides a metrics/logs-only build (host network reaches Ollama
& Netdata). Full GPU/remediation fidelity is only available running on the host — it
needs the Docker socket, `nvidia-smi`, `journalctl`, `systemctl` and the block (see
comments in `docker-compose.yml`).