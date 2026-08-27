# Deployment — install Ops Brain on a fresh system

Assumes a **Linux host** (systemd, optionally with an NVIDIA GPU), Docker for containers,
and Python 3.10+.

## Prerequisites

- **OS:** any modern Linux with systemd (Ubuntu/Debian recommended). One VM is fine; add
  nodes for federation.
- **Python:** 3.10+ (`python3 --version`).
- **Docker** CLI + daemon. The collector reads `/var/run/docker.sock` — run the daemon as
  `root` or add the service user to the `docker` group. ([Install Docker](https://docs.docker.com/engine/install/))
- **Ollama** with a Qwen model (see [Reasoning LLM](reasoning-llm.md)).
  ([Install Ollama](https://ollama.com/download/linux))
- **`nvidia-smi`** (optional but recommended) on `$PATH` of the running user for GPU
  metrics + GPU drift detection.
- **Netdata** on `:19999` and optionally **Dozzle** (`:8080`) / **Dockpeek** (`:8081`)
  for extra container/health sources. These are polled but degrade gracefully if absent.
- **systemd** (for the recommended service install).
- `sudo` access.

## Get the code & install dependencies

```bash
git clone <your-repo-url> /appdata/OpsBrain
cd /appdata/OpsBrain

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install pyyaml fastapi uvicorn websockets jinja2
```

- `pyyaml` is required for the core pipeline.
- `fastapi uvicorn websockets jinja2` are only needed for the dashboard (skip if you don't
  want the UI).
- No build tools needed; the code is pure Python stdlib apart from those.

> If installs are blocked and you cannot create a venv, the pipeline also runs on the
> system Python as long as `pyyaml` is importable.

## First run & verify the pipeline

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
check the [Qwen3/Ollama section](reasoning-llm.md) — see [Troubleshooting](troubleshooting.md).

Generate today's report immediately (dry, harmless):

```bash
python3 scheduler/scheduler.py --report   # -> reports/YYYY-MM-DD.md
```

## Run as background services

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

## Next steps

- [Configuration](configuration.md) — tune `ops_brain.yaml`
- [Enabling remediation](remediation.md) — safely turn off dry-run
- [Dashboard](dashboard.md) — real-time UI + reverse proxy
- [Manual Stop Protection](manual-stop-protection.md) — the hard invariant
- [TrueNAS integration](truenas.md)
- [Federation layer](federation.md)
- [Security & operations](security.md)
- [Troubleshooting](troubleshooting.md)