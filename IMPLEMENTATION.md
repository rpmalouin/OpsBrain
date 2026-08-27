# Ops Brain — Implementation Guide

This guide walks a **fresh system** through deploying the complete Ops Brain homelab
automation engine: the `collector → reasoner → action` pipeline, the real-time dashboard,
manual-stop protection (hard invariant), TrueNAS integration, and the multi-node
federation layer.

It assumes a **Linux host** (systemd, optionally with an NVIDIA GPU), Docker for
containers, and Python 3.10+. Everything can run on a single VM; the federation layer
also supports multiple nodes.

> **Safety model up front:** Ops Brain ships with `actions.dry_run: true`. It will
> *observe*, *reason*, and *recommend* — but it will not touch your containers, services,
> or GPU until you explicitly disable dry-run (Section 8). Keep it dry-run until you have
> seen it make sensible decisions on live data.

---

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Get the code & install dependencies](#2-get-the-code--install-dependencies)
3. [Configure the LLM (Ollama + Qwen3)](#3-configure-the-llm-ollama--qwen3)
4. [Configure `ops_brain.yaml`](#4-configure-ops_brainyaml)
5. [First run & verify the pipeline](#5-first-run--verify-the-pipeline)
6. [Run as background services](#6-run-as-background-services)
7. [Dashboard](#7-dashboard)
8. [Enable remediation (turn off dry-run)](#8-enable-remediation-turn-off-dry-run)
9. [Manual Stop Protection (HARD INVARIANT)](#9-manual-stop-protection-hard-invariant)
10. [TrueNAS integration](#10-truenas-integration)
11. [Federation layer (multi-node)](#11-federation-layer-multi-node)
12. [Tests](#12-tests)
13. [Security & operational notes](#13-security--operational-notes)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Prerequisites

- **OS:** any modern Linux with systemd (Ubuntu/Debian recommended). One VM is fine;
  add nodes for federation.
- **Python:** 3.10+ (`python3 --version`).
- **Docker** CLI + daemon. The collector reads `/var/run/docker.sock` — run the daemon
  as `root` or add the service user to the `docker` group. ([Install Docker](https://docs.docker.com/engine/install/))
- **Ollama** with a Qwen model (see §3). ([Install Ollama](https://ollama.com/download/linux))
- **`nvidia-smi`** (optional but recommended) on `$PATH` of the running user for GPU
  metrics + GPU drift detection.
- **Netdata** on `:19999` and optionally **Dozzle** (`:8080`) / **Dockpeek** (`:8081`)
  for extra container/health sources. These are polled but degrade gracefully if absent.
- **systemd** (for the recommended service install).
- `sudo` access.

Optional, feature-specific:
- **TrueNAS SCALE** reachable for storage telemetry (§10).
- **Caddy / nginx** if you want to reverse-proxy the dashboard (§7).

---

## 2. Get the code & install dependencies

```bash
git clone <your-repo-url> /appdata/OpsBrain
cd /appdata/OpsBrain

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install pyyaml fastapi uvicorn websockets jinja2
```

- `pyyaml` is required for the core pipeline.
- `fastapi uvicorn websockets jinja2` are only needed for the dashboard (skip if you
  don't want the UI).
- No build tools needed; the code is pure Python stdlib apart from those.

> If installs are blocked and you cannot create a venv, the pipeline also runs on the
> system Python as long as `pyyaml` is importable.

---

## 3. Configure the LLM (Ollama + Qwen3)

Ops Brain uses a local LLM for *node-level* reasoning (recommendations + confidence).
Cluster reasoning is deterministic.

1. Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
2. Pull the reasoning model:
   ```bash
   ollama pull qwen3:14b
   ```
   (fallback model `qwen2.5-coder:14b` is used automatically if `qwen3:14b` is absent.)
3. Confirm it serves: `curl -s http://localhost:11434/api/tags` lists the model.

### CRITICAL — Qwen3/Ollama request shape (do not "fix")

`qwen3:14b` via `/api/generate` **short-circuits to a bare `{}` (2 tokens)** for
reasonably large prompts unless **both**:

1. you send an explicit `"think": true` (or false) field — set `ollama.think: true` in
   config, **and**
2. you **omit** `"format": "json"`.

Sending `format:json` alongside `think` makes Qwen3 emit `{}`. The prompt
(`reasoner/prompt.txt`) already mandates JSON, and `reasoner.extract_json()` recovers the
object with a balanced-brace regex. This is handled by the code — just never try to "fix"
the call shape back to `format:json`.

Ollama must be reachable at `http://localhost:11434` (or set `ollama.base_url` in config).
If the model is cold, the first cycle can be slow (~10–15s).

---

## 4. Configure `ops_brain.yaml`

All tuning lives in `config/ops_brain.yaml`. The core settings you must review:

```yaml
hostname: dockerVM              # label used in reports/notifications
interval_seconds: 120           # pipeline cadence (2 min)
ollama:
  base_url: http://localhost:11434
  model: qwen3:14b
  fallback_model: qwen2.5-coder:14b
  think: true                   # REQUIRED — see Qwen3 quirk above

actions:
  dry_run: true                 # <-- SAFETY. false = real remediation
  restart_limit_per_run: 3
  allow_restart_containers:      # names the engine may restart (case-insensitive)
    - homepage
    - dozzle
    # ... list YOUR containers ...
  allow_service_restart: []      # systemd units whitelist
  allow_gpu_kill: false
  allow_prune: true
  notify_webhook: ""             # optional POST-JSON webhook (ntfy / Telegram bot)
```

### `sources` — data endpoints

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
  truenas:                        # optional — see §10
    base_url: http://truenas/api/v2.0
    creds_file: ~/.smbcred
    enabled: true
    timeout_s: 8
  docker_socket: /var/run/docker.sock
  gpu_query: whitelisted
  journalctl_since: "2 min ago"
```

Disable any source you don't run (`enabled: false`); the pipeline degrades gracefully.

### `thresholds`

- `actions.cpu_restart_threshold_percent` (80), `mem_creep_threshold_percent` (20),
  `gpu_mem_threshold_percent` (90), `disk_threshold_percent` (85),
  `qwen_confidence_floor` (0.6).
- `gpu_drift:` vram creep / vram max / stuck-pid cycles / power / temp idle thresholds.
- `manual_stop_protection:` enabled (see §9).
- `report.time` (default `23:55`), `report.retention_days`.
- `paths:` point at the repo logs layout (defaults are fine for a fresh deploy).

---

## 5. First run & verify the pipeline

```bash
cd /appdata/OpsBrain && . .venv/bin/activate

# Force one full cycle (SAFE: dry-run). Runs collector -> reasoner -> actions,
# and federation at cycle 0.
python3 scheduler/scheduler.py --once
```

Check the artifacts:

```bash
python3 -m json.tool logs/collector.json         # unified signal doc + truenas + manual_stops
python3 -m json.tool logs/reasoner_result.json   # Qwen decision: warnings/actions/summary/confidence
python3 -m json.tool logs/actions_result.json    # executed / skipped / blocked + cluster
```

A healthy first run: `reasoner_result.json` has a `confidence` (e.g. 0.5–0.9) and
`actions_result.json` shows `"dry_run": true` with everything in `skipped`/`blocked` (or
`executed` as `notify`). If the reasoner returned `confidence: 0` and empty everything,
check the Qwen3/Ollama section — see §14.

Generate today's report immediately (dry, harmless):

```bash
python3 scheduler/scheduler.py --report   # -> reports/YYYY-MM-DD.md
```

---

## 6. Run as background services

### systemd (recommended)

```bash
cd /appdata/OpsBrain
sudo ./deploy/install.sh
```

This installs and starts `opsbrain.service` (the 2-min pipeline daemon). Verify:

```bash
systemctl --no-pager status opsbrain
journalctl -u opsbrain -f       # live pipeline log
```

**Install the dashboard service too** (not auto-installed by install.sh):

```bash
sudo install -m 0644 deploy/opsbrain-ui.service /etc/systemd/system/opsbrain-ui.service
sudo systemctl daemon-reload
sudo systemctl enable --now opsbrain-ui
```

### crontab fallback (no systemd)

```bash
sudo ./deploy/install.sh --no-systemd
# prints two crontab lines (every 2 min + daily report); add them via `crontab -e`.
```

---

## 7. Dashboard

The dashboard is a FastAPI + WebSocket single page served at `http://<host>:9120/`.

- Config: `ui/config.yaml` (`server.port`, `refresh_seconds`, `watch[]` list of logs).
- Panels: System Overview, **Cluster Overview**, **Node Comparison**, **TrueNAS**,
  Container Health, GPU Drift, OpsBrain Decisions, Manual Stop Protection, Daily Report
  Preview, plus confidence-recovery / drift-decay / restart-impact refinements.
- It streams the live JSON logs on change (event-driven + 2s fallback).

```bash
# already started via opsbrain-ui.service above; or run manually:
uvicorn server:app --app-dir /appdata/OpsBrain/ui --host 0.0.0.0 --port 9120
```

### Reverse proxy (optional, recommended for LAN exposure)

Example **Caddy** route (dashboard has no auth layer — front it with auth if exposed
beyond a trusted LAN):

```
opsbrain.home {
    reverse_proxy 127.0.0.1:9120 {
        flush_interval -1    # keep the 2s WebSocket stream alive through the proxy
    }
    tls internal
}
```

> **PITFALL — bind-mount inode staleness.** If the Caddy(reverse-proxy) container
> bind-mounts a single config file, editing it with a write-replacing tool (patch/write)
> creates a NEW inode the running container won't see — `caddy reload` silently applies
> the STALE config. Run `docker restart caddy` after editing, then confirm the change
> landed inside the container.

---

## 8. Enable remediation (turn off dry-run)

> Only do this once you trust the decisions. The engine is **safe-by-default** and a
> whitelist is required for destructive actions.

1. Verify a few dry-run cycles produced only sensible recommendations.
2. Review `actions.allow_restart_containers` — only list containers you're comfortable
   auto-restarting. Everything else is blocked even in live mode.
3. Decide on dangerous actions: `allow_gpu_kill`, `allow_prune`.
4. Flip the switch:
   ```yaml
   actions:
     dry_run: false
   ```
5. Apply:
   ```bash
   systemctl restart opsbrain
   ```

Even in live mode these still apply: the **manual-stop hard invariant** (§9), the restart
**allow-list**, the **restart cap** (`restart_limit_per_run`), the **Qwen confidence
floor** (`qwen_confidence_floor`), and the **ollama restart is hard-blocked** policy guard
(the engine never restarts/kills Ollama, even if whitelisted). Federation remains
**notify-only** (§11).

---

## 9. Manual Stop Protection (HARD INVARIANT)

**Rule:** *"Manually stopped containers must stay stopped."* Ops Brain must NEVER restart
a container you manually stopped — overriding autonomous remediation, confidence gating,
restart caps, allow-lists, anomaly/drift remediation, daily-report recommendations, and
any LLM-generated action. It must also not destroy one via `docker prune`.

This is **on by default** (`manual_stop_protection.enabled: true`).

How it works:

- **Detection (collector, transition-based):** a container that *was running* and is now
  *exited with a manual exit signature* is recorded. Manual signatures: exit `0`/`143`, or
  `137` **without** OOM (configurable — `actions.manual_stop_sigkill_protect: true` treats
  a SIGKILL'd non-OOM exit as a manual stop, favouring protection). OOM or other nonzero
  exits are treated as *crashes*, not manual stops.
- **Registry:** recorded in `logs/manual_stops.json`, keyed by container **ID**, and it
  **never auto-forgets** (corrupt reads fail closed — the invariant is never silently
  dropped).
- **Enforcement (actions):** the manual-stop gate is checked **first** in the action
  dispatcher, before the allow-list, restart cap, or LLM. `docker prune` is also blocked
  while any protected container is stopped.
- **Reasoner:** the prompt carries a HARD RULE never to propose restarts for protected
  containers; the result also carries the list (defense-in-depth drops any that slip
  through).
- **Dashboard:** a red **"MANUALLY STOPPED"** tag on container rows + a **Manual Stop
  Protection** panel.
- **Re-arm:** start the container again (any method) and protection clears automatically.

**To verify on your system** (safe): start a throwaway container, run a cycle so the
collector seeds it, `docker stop` it, run another cycle, and confirm it appears in
`logs/manual_stops.json` and that no `docker_restart` action is ever proposed for it.

---

## 10. TrueNAS integration

Optional — adds a storage panel and storage telemetry to the collector.

1. **Create a credentials file** the collector can read (never commit it):
   ```bash
   printf 'username=your_truenas_user\npassword=your_truenas_password\n' > ~/.smbcred
   chmod 600 ~/.smbcred
   ```
   The user needs at least read access to the TrueNAS SCALE REST API (`/api/v2.0`).
2. **Enable the source** in config:
   ```yaml
   sources:
     truenas:
       base_url: http://truenas/api/v2.0   # or your TrueNAS IP/hostname
       creds_file: ~/.smbcred
       enabled: true
   ```
3. Run a cycle (`python3 scheduler/scheduler.py --once`). The collector fetches
   `/pool`, `/system/info`, `/alert/list`, and `/disk`, merging them into `collector.json`
   under `truenas`.
4. Dashboard shows a **TrueNAS** panel (pool status/health, system version/RAM/uptime,
   disk count, alerts).

If auth fails or the endpoint is down, the collector marks the source unavailable and
degrades gracefully (no crash). `~/.smbcred` is expanded literally (`Path(raw).expanduser()`)
to the real home dir.

---

## 11. Federation layer (multi-node)

Ops Brain can reason about **multiple nodes as a unified cluster**. Each node must expose
a collector snapshot as JSON over HTTP — the easiest way is to run Ops Brain on that node
too and use its dashboard endpoint (`http://<node>:9120/api/status`, which includes the
`collector` doc). The federation collector also tolerates a raw `collector.json` document.

1. **Configure nodes** in `config/ops_brain.yaml`:
   ```yaml
   federation:
     enabled: true
     nodes:
       - name: dockervm
         type: linux
         collector_endpoint: "http://dockervm:8099/api/status"
       - name: truenas
         type: storage
         collector_endpoint: "http://truenas:8099/api/status"
     cluster_stability_weights:   # weights in the stability formula
       confidence: 0.4
       drift: 0.3
       anomalies: 0.2
       restarts: 0.1
     poll_interval_cycles: 2      # run federation every N scheduler cycles (4 min at 120s cadence)
   ```
   Replace the endpoints with your nodes' actual snapshot URLs.
2. **Restart** the daemon. Every `poll_interval_cycles`, the federation collector merges
   each node into `logs/cluster_snapshot.json` and the reasoner writes
   `logs/cluster_reasoner_result.json`.
3. **What it computes:**
   - **Cluster stability score (0–100):**
     `(confidence*0.4 + drift*0.3 + anomalies*0.2 + restarts*0.1) * 100`, where
     drift/anomalies/restarts are reverse-normalized (`1 − n/max_bad`, budgets 20/20/10).
     An online node with null confidence gets full credit; an offline node scores 0.
   - **Node ranking** by per-node stability.
   - **Cross-node correlations:** nodes sharing the same Netdata alarm `name`
     (`anomaly_correlations`) or the same GPU drift flag (`drift_correlations`).
   - **Recommendations:** offline node → `cluster_health_warning`; score < 60 →
     `escalate_cluster`; any anomalies/drift → `notify_cluster`.
4. **Safety:** all federation actions are **notify-only** — the cluster layer never
   performs cross-node remediation. Per-node remediation still obeys confidence gating,
   manual-stop protection, and restart caps.
5. **Dashboard:** **Cluster Overview** (stability, nodes online, totals, recs) and
   **Node Comparison** (per-node confidence/drift/anomalies/restarts/stability) panels.
   Daily report gains a **Cluster Summary** section.

**Degradation:** if a node is unreachable it is marked offline, the cluster score drops,
and a health warning is emitted — no automation is triggered.

---

## 12. Tests

```bash
cd /appdata/OpsBrain && . .venv/bin/activate
python3 -m pytest            # 98 tests
```

The suite covers the decision-critical logic with mocks (no live Docker/GPU/Ollama):
whitelist gate, deterministic rules, Qwen sanitization, GPU drift, manual-stop invariant,
TrueNAS parsing, federation math + correlations, and dashboard refinements. Run it after
touching `actions.py`, `reasoner.py`, `common/`, `collector/`, or `federation/`.

---

## 13. Security & operational notes

- **Dry-run by default.** Change `actions.dry_run` deliberately (§8).
- **No secrets in the repo.** TrueNAS creds live in `~/.smbcred` (`chmod 600`), not in
  config. `logs/`, `reports/`, and `__pycache__` are gitignored.
- **Dashboard has no auth.** It binds `0.0.0.0`. If exposed beyond a trusted LAN, put a
  reverse proxy with auth (Caddy/nginx) in front.
- **Privileges.** The pipeline runs as `root` (or a user in the `docker` group) to read
  the Docker socket, `nvidia-smi`, `journalctl`, and `systemctl`. Keep the VM isolated;
  the engine can restart/stop containers you allow-list.
- **Ollama is protected.** The engine will never restart or kill Ollama, even if you
  allow-list it — it is the reasoning backend.
- **Permission-in — least approval.** Start dry-run, review logs for 24h, then enable
  remediation in stages (notify first, then non-destructive restarts, then prune/GPU if
  you truly want them).
- **Config changes require a restart** (`systemctl restart opsbrain opsbrain-ui`).

---

## 14. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `collector works but reasoner returns {}` | Qwen3/Ollama shape wrong. Ensure `ollama.think: true` and that `format:json` is NOT set. Warm the model (`curl localhost:11434/api/tags`). |
| `actions_result` skips everything | `actions.dry_run` is true (that's the default — intentional). It will keep doing this until you set it false. |
| `docker.sock` permission denied | Run as `root` or add the user to the `docker` group, then restart the service. |
| GPU metrics empty / no GPU panel | `nvidia-smi` not on `$PATH` for the running user. |
| Federation nodes show **offline** | The `collector_endpoint`s aren't reachable. Each node must serve a snapshot JSON (e.g. its dashboard `/api/status`, or a raw `collector.json`). Test locally by pointing them at `http://127.0.0.1:9120/api/status`. |
| TrueNAS source unavailable | Check `~/.smbcred` exists/readable and the user can hit `/api/v2.0`. Degrades gracefully if not. |
| A container you stopped keeps getting restarted | You have **not** seen this with manual-stop protection on — it's a hard block. Verify it's recorded in `logs/manual_stops.json`. A *crashing* container (OOM / nonzero exit) is not protected; that's by design. |
| Dashboard WS not updating | Check `opsbrain-ui` is active; the server watches the log files specified in `ui/config.yaml` `watch[]`. Make sure the watched logs exist and are being written. |
| `actions_result.json` `cluster.recommendations` empty | Federation `enabled: false` or no cluster output yet. Run the federation scripts once: `python3 federation/federation_collector.py && python3 federation/federation_reasoner.py`. |

### Quick diagnostics

```bash
journalctl -u opsbrain -f          # pipeline log
journalctl -u opsbrain-ui -f       # dashboard log
python3 scheduler/scheduler.py --once   # force a cycle (dry)
python3 scheduler/scheduler.py --report # force today's report
python3 -m pytest -q               # run the test suite
```

---

## Layout reference

```
collector/          polls & merges sources -> logs/collector.json
reasoner/           Qwen prompt + Ollama wrapper -> logs/reasoner_result.json
hermes_actions/     remediation + deterministic rules -> logs/actions_result.json
federation/         multi-node collector + reasoner -> logs/cluster_*.json
scheduler/          controller loop + daily report generator
common/             Cfg, ManualStops registry, JSON/log helpers
config/             ops_brain.yaml (all settings)
ui/                 real-time dashboard (FastAPI + WebSocket, :9120)
deploy/             systemd units + installer
tests/              pytest suite (98 tests)
```