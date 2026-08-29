# Ops Brain — Implementation Guide

This is the entry point for deploying and operating **Ops Brain**, the homelab
automation / orchestration engine. It links to the how-to pages that live in the
[`docs/`](docs/) folder.

> **Maintainership:** this is a **use it / you fix it** project — built for the
> author's own homelab, maintained to the level they want to add functionality, with no
> support-schedule promises (see [README](README.md) → Maintainership). It's MIT-licensed,
> so fork / fix / adapt freely.

> **Safety model up front:** Ops Brain ships with `actions.dry_run: true`. It will
> *observe*, *reason*, and *recommend* — but it will not touch your containers, services,
> or GPU until you explicitly disable dry-run ([docs/remediation.md](docs/remediation.md)).
> Keep it dry-run until you have seen it make sensible decisions on live data.

## What it is

A **collector → reasoner → action** pipeline that consolidates metrics, logs, container
state, GPU state, VM state, and storage (TrueNAS) on each node; asks a local LLM (Qwen3)
for a structured decision; and executes **safe, whitelisted** remediation. It also
**federates** multiple nodes into one cluster and streams everything to a real-time
dashboard.

- **Poll:** Netdata, Dozzle, Dockpeek/Docker-socket, `nvidia-smi`, `df`/`top`/`journalctl`,
  TrueNAS SCALE, and Dockhand (desired-state registry → drift vs actual).
- **Reason:** Qwen3 14B (local Ollama) for node decisions; deterministic math for cluster
  reasoning.
- **Act:** Docker restart / prune, systemctl restart, GPU kill, webhook notifications —
  dry-run by default, whitelist + restart-cap gated.
- **Protect:** manual-stop protection — a container you stopped stays stopped (hard
  invariant overriding all remediation).
- **Federate:** reason about many nodes as one cluster (stability score, ranking,
  cross-node correlation). **Notify-only** — never cross-node remediation.
- **Report:** daily ops digest at `23:55`.
- **Watch:** real-time dashboard over WebSocket (`:9120`).

## Quick start (Linux VM with Docker + optional NVIDIA GPU)

```bash
git clone <your-repo-url> /appdata/OpsBrain && cd /appdata/OpsBrain
python3 -m venv .venv && . .venv/bin/activate
pip install pyyaml fastapi uvicorn websockets jinja2

# one-shot test of the whole pipeline (SAFE: dry-run)
python3 scheduler/scheduler.py --once

# install as persistent systemd services
sudo ./deploy/install.sh                              # opsbrain (pipeline)
sudo install -m 0644 deploy/opsbrain-ui.service /etc/systemd/system/ && \
  sudo systemctl daemon-reload && sudo systemctl enable --now opsbrain-ui    # dashboard
```

See **[docs/deployment.md](docs/deployment.md)** for the full step-by-step.

## Layout

```
collector/          polls & merges sources -> logs/collector.json (incl. truenas, dockhand, manual_stops)
reasoner/           Qwen prompt + Ollama wrapper -> logs/reasoner_result.json
hermes_actions/     remediation + deterministic rules -> logs/actions_result.json
federation/         multi-node collector + reasoner -> logs/cluster_{snapshot,reasoner_result}.json
scheduler/          controller loop + daily report generator
common/             Cfg, ManualStops registry, JSON/log helpers
config/             ops_brain.yaml (all settings)
ui/                 real-time dashboard (FastAPI + WebSocket, :9120)
deploy/             systemd units + installer
tests/              132 tests
docs/               these implementation docs
IMPLEMENTATION.md   this entry point
CHANGELOG.md, README.md, MEMORY.md
```

## Documentation index (per-feature pages)

| Topic | Doc |
|---|---|
| Prerequisites, install, first run, systemd services | [docs/deployment.md](docs/deployment.md) |
| `ops_brain.yaml` reference | [docs/configuration.md](docs/configuration.md) |
| Ollama + Qwen3 (incl. the critical request-shape quirk) | [docs/reasoning-llm.md](docs/reasoning-llm.md) |
| Turning off dry-run safely | [docs/remediation.md](docs/remediation.md) |
| Real-time dashboard + reverse proxy | [docs/dashboard.md](docs/dashboard.md) |
| Manual Stop Protection (HARD INVARIANT) | [docs/manual-stop-protection.md](docs/manual-stop-protection.md) |
| TrueNAS integration | [docs/truenas.md](docs/truenas.md) |
| Dockhand desired-state drift | [docs/dockhand.md](docs/dockhand.md) |
| Federation layer (multi-node) | [docs/federation.md](docs/federation.md) |
| Security & operations | [docs/security.md](docs/security.md) |
| Test suite | [docs/tests.md](docs/tests.md) |
| Troubleshooting table + diagnostics | [docs/troubleshooting.md](docs/troubleshooting.md) |

## Key invariants & guarantees

- **Manual-stop protection is a HARD INVARIANT** (default on): a manually stopped
  container is NEVER restarted or pruned, regardless of remediation/confidence/caps/LLM.
- **Ollama is never restarted or killed** by the engine, even if allow-listed.
- **Federation is notify-only** — no cross-node remediation.
- **Dockhand drift is notify-only** — it never auto-restarts containers, so it can't
  override Manual-Stop Protection or the allow-list.
- **132 tests** cover the decision-critical logic with mocks (docs/tests.md).

## Release history

See [CHANGELOG.md](CHANGELOG.md).