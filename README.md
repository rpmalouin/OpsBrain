# Ops Brain

A unified **collector → reasoner → action** pipeline that consolidates metrics, logs,
container state, GPU state, and VM state on **dockerVM** and turns them into safe,
Qwen-verified remediation actions.

- **Poll:** Netdata, Dozzle, Dockpeek (Docker socket), `nvidia-smi`, `df/top/journalctl`
- **Reason:** Qwen 3:14B via local Ollama (`http://localhost:11434/api/generate`)
- **Act:** Docker restart / prune, systemctl restart, GPU kill, webhook notifications
- **Report:** daily ops digest at `23:55`

## Layout

```
/appdata/OpsBrain/
  collector/          polls & merges all sources -> logs/collector.json
  reasoner/           Qwen prompt template + Ollama wrapper -> logs/reasoner_result.json
  hermes_actions/     remediation module + deterministic RULES -> logs/actions_result.json
  scheduler/          cron-like controller loop + daily report generator
  config/             ops_brain.yaml (all settings/thresholds)
  logs/               runtime JSON + opsbrain.log + notifications.jsonl
  reports/            daily markdown reports
  deploy/             systemd unit + installer
  Dockerfile, docker-compose.yml, README.md, .gitignore
```

## Quick start (on dockerVM, recommended)

Requires: Python 3.10+, `PyYAML`, the `docker` CLI, `nvidia-smi`, and Ollama with
`qwen3:14b` pulled (`ollama pull qwen3:14b`).

```bash
cd /appdata/OpsBrain
python3 -m venv .venv && . .venv/bin/activate
pip install pyyaml

# one-shot test of the whole pipeline (SAFE: dry-run by default)
python3 scheduler/scheduler.py --once

# install as a persistent systemd service (collect->reason->act every 2 min)
sudo ./deploy/install.sh          # or: ./deploy/install.sh --no-systemd (crontab)
```

## How one cycle works

1. **collector/collector.py** — polls Netdata (`:19999`), Docker (`/var/run/docker.sock`),
   Dozzle (`:8080`), Dockpeek (`:8081` probe), `nvidia-smi`, and host commands
   (`df -h`, `top`, `journalctl --since "2 min ago"`). Merges into one JSON doc and
   writes `logs/collector.json`.
2. **reasoner/reasoner.py** — renders `reasoner/prompt.txt` with a *compact risk digest*
   of the collector output, asks Qwen3 14B to produce a structured JSON decision
   `{warnings, actions[{type,target,reason}], summary, confidence}`, normalizes it, writes
   `logs/reasoner_result.json`.
3. **hermes_actions/actions.py** — enforces the **action rules** below and executes the
   union of Qwen's actions + deterministic rules through SAFETY gates
   (dry-run, allow-lists, restart cap). Writes `logs/actions_result.json`.
4. **scheduler/scheduler.py --daemon** loops 1–3 every `interval_seconds` (120s) and, at
   the configured report time (23:55), emits `reports/YYYY-MM-DD.md`.

## Action rules (config/ops_brain.yaml)

| Condition                                      | Action                       | Gate            |
|------------------------------------------------|------------------------------|-----------------|
| container CPU > 80% sustained 5 min        | `docker restart`            | cap/allow-list  |
| container memory creep > 20% over baseline    | `docker restart`            | cap/allow-list  |
| container in restart loop                   | `docker restart` + notify | cap/allow-list  |
| GPU memory > 90% with resident process       | `gpu_kill`                  | `allow_gpu_kill`|
| disk usage > 85%                             | `docker system prune -af` + notify | `allow_prune`  |
| Netdata ACTIVE alarm                        | surfaced as warnings       | —              |
| Qwen confidence < 0.6        | **do nothing, log only**   | —              |

**Safety first:** `actions.dry_run: true` by default. Nothing actually happens until you
set `actions.dry_run: false`. Even then, `allow_restart_containers` /
`allow_service_restart` whitelist which targets may be touched, and the
`restart_limit_per_run` caps destructive restarts. GPU/Netdata/systemd observations are
safe to keep enabled.

## Model routing (find/verify)

This project replies on **Qwen3 14B** for reasoning. Qwen3’s local Ollama generation
must be driven without `format="json"` **and** with an explicit `think` flag — the
default (think unset + `format:json`) makes Qwen3 short-circuit to an empty `{}`.
`reasoner.py` sets `think: true` (config `ollama.think`) and relies on the prompt +
robust JSON extractor instead. If `qwen3:14b` is unavailable it falls back to
`qwen2.5-coder:14b`.

## Files you get

- `logs/collector.json`        unified normalized signal document (2-min cadence)
- `logs/reasoner_result.json`  Qwen decision `{warnings, actions, summary, confidence}`
- `logs/actions_result.json`   what was executed / skipped / blocked per cycle
- `logs/notifications.jsonl`   notification queue (webhook / local)
- `logs/action_state.json`     persisted counters (sustained CPU, baselines)
- `reports/YYYY-MM-DD.md`      daily ops report

## Troubleshooting

- `collector works but reasoner returns {}` — ensure `ollama.think: true` and that the
  model is warm: `curl localhost:11434/api/tags`.
- `hermes_actions` skips everything → is `actions.dry_run` true? (it should be; flip it
  only after you trust the rules).
- `docker.sock` permission → run the collector/actions as `root` or in the `docker` group.
- GPU metrics empty → ensure `nvidia-smi` is on `$PATH` for the running user.

## Containerized alternative

`docker-compose up -d` provides a metrics/logs-only build (host network reaches Ollama
& Netdata). Full GPU/remediation fidelity is only available running on the host — it
needs the Docker socket, `nvidia-smi`, `journalctl`, `systemctl` and the block (see
comments in `docker-compose.yml`).