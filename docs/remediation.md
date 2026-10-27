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
- **Every GPU-drift flag needs deterministic evidence** — the collector's own `drift_flags`.
  A kill needs `stuck_process` (same PID for `stuck_pid_cycles` **and** util above
  `util_threshold`): an LLM-only kill is blocked with `blocked_no_stuck_evidence`, and the
  drift path drops the flag without notifying. The four notify-only flags
  (`vram_drift` / `power_drift` / `temp_drift` / `vram_overload`) are gated the same way — a
  flag the LLM asserts on its own notifies nothing and records no event, so the reasoner
  relays the detector's verdict instead of re-deciding it. Configured permanent GPU residents
  (`gpu_drift.resident_containers`, e.g. docling-serve) and ollama's `llama-server` are never
  kill targets, and a drift notification now carries the measurement
  (`GPU drift flag: vram_drift (vram 9517 MB (was 842, +8675))`).
- **`docker_prune` needs the deterministic disk rule** — the verb ignores its target and runs
  `docker system prune -af` (every stopped container, every unused image, the build cache), so a
  proposal that does not carry the disk rule's `evidence: disk_threshold` tag is blocked with
  `blocked_no_prune_evidence`. `actions.allow_prune` is a second, independent switch and now sits
  at `false`.
- **A container-CPU fault claim needs the sustained rule** — `stats.cpu_percent` is a single
  instantaneous sample, so an LLM notice naming a container with high CPU is dropped unless
  the deterministic sustained-CPU rule (`cpu_restart_threshold_percent` for
  `cpu_restart_minutes`) fired for that container in the same cycle.
- **Federation remains notify-only** (docs/federation.md).
- **Dockhand drift remains notify-only** (docs/dockhand.md) — it surfaces desired-state
  drift / storms / orphans as notifications and context, and never proposes a restart, so
  it can't override the manual-stop / allow-list protections above.

## Recommended adoption order

Start dry-run → review logs for 24h → enable remediation in stages:
1. notify / webhook only,
2. non-destructive container restarts on your allow-list,
3. prune / GPU-kill only if you truly want them.