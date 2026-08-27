# Security & operations

Operational guidance for running Ops Brain safely, plus a security baseline.

## Operational notes

- **Config changes require a restart** — `systemctl restart opsbrain opsbrain-ui`.
- **Privileges.** The pipeline runs as `root` (or a user in the `docker` group) to read
  the Docker socket, `nvidia-smi`, `journalctl`, and `systemctl`. Keep the VM isolated;
  the engine can restart/stop containers you allow-list.
- **Dashboard binds `0.0.0.0`** and has **no auth layer**. If exposed beyond a trusted
  LAN, put a reverse proxy with auth in front (docs/dashboard.md).
- **`logs/`, `reports/`, and `__pycache__` are gitignored** — runtime output is never
  committed.

## Security baseline

1. **Dry-run by default.** Change `actions.dry_run` deliberately, and adopt remediation in
   stages (docs/remediation.md).
2. **No secrets in the repo.** TrueNAS creds live in `~/.smbcred` (`chmod 600`), never in
   `ops_brain.yaml` or git.
3. **Least-approval.** Start dry-run, review for 24h, then enable notify → non-destructive
   restarts → prune/GPU only if you truly want them.
4. **Ollama is protected.** The engine will never restart or kill Ollama, even if you
   allow-list it — it is the reasoning backend.
5. **Whitelist everything destructive.** `allow_restart_containers`, `allow_service_restart`,
   `allow_gpu_kill` must each be deliberate. The manual-stop hard invariant and the
   restart cap apply regardless.
6. **Reverse proxy + auth in front of the dashboard** if it leaves your trusted LAN.

## Data flow note

The federation layer is **notify-only** and never performs cross-node remediation. It
reads collector snapshots over HTTP from configured endpoints — keep those endpoints on
your trusted network.