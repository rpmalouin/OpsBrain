# Enabling remediation (turn off dry-run)

> Only do this once you trust the decisions. The engine is **safe-by-default** and a
> whitelist is required for destructive actions.

## Steps

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

## What still protects you even in live mode

- The **manual-stop hard invariant** (docs/manual-stop-protection.md).
- The restart **allow-list** (`allow_restart_containers`).
- The **restart cap** (`restart_limit_per_run`).
- The **Qwen confidence floor** (`qwen_confidence_floor`) — below it, the engine only
  logs.
- The **ollama restart hard-block** — the engine never restarts or kills Ollama, even if
  allow-listed (it is the reasoning backend).
- **Federation remains notify-only** (docs/federation.md).

## Recommended adoption order

Start dry-run → review logs for 24h → enable remediation in stages:
1. notify / webhook only,
2. non-destructive container restarts on your allow-list,
3. prune / GPU-kill only if you truly want them.