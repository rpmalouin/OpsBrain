# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `collector works but reasoner returns {}` | Qwen3/Ollama shape wrong. Ensure `ollama.think: true` and that `format:json` is NOT set. Warm the model (`curl localhost:11434/api/tags`). See docs/reasoning-llm.md. |
| `actions_result` skips everything | `actions.dry_run` is true (the default — intentional). It will keep doing this until you set it false (docs/remediation.md). |
| `docker.sock` permission denied | Run as `root` or add the user to the `docker` group, then restart the service. |
| GPU metrics empty / no GPU panel | `nvidia-smi` not on `$PATH` for the running user. |
| Federation nodes show **offline** | The `collector_endpoint`s aren't reachable. Each node must serve a snapshot JSON (e.g. its dashboard `/api/status`, or a raw `collector.json`). Test locally by pointing them at `http://127.0.0.1:9120/api/status`. |
| TrueNAS source unavailable | Check `~/.smbcred` exists/readable and the user can hit `/api/v2.0`. Degrades gracefully if not. |
| A container you stopped keeps getting restarted | With manual-stop protection on this is a hard block — verify it's recorded in `logs/manual_stops.json`. A *crashing* container (OOM / nonzero exit) is NOT protected; that's by design (docs/manual-stop-protection.md). |
| Dashboard WS not updating | Check `opsbrain-ui` is active; the server watches the log files in `ui/config.yaml` `watch[]`. Ensure the watched logs exist and are being written. |
| `actions_result.json` `cluster.recommendations` empty | Federation `enabled: false` or no cluster output yet. Run `python3 federation/federation_collector.py && python3 federation/federation_reasoner.py` once. |
| Caddy site won't appear after editing Caddyfile | Bind-mount inode staleness — `docker restart caddy` to re-bind the file (docs/dashboard.md). |

## Quick diagnostics

```bash
journalctl -u opsbrain -f          # pipeline log
journalctl -u opsbrain-ui -f       # dashboard log
python3 scheduler/scheduler.py --once   # force a cycle (dry)
python3 scheduler/scheduler.py --report # force today's report
python3 -m pytest -q               # run the test suite
```