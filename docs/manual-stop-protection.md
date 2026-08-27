# Manual Stop Protection (HARD INVARIANT)

**Rule:** *"Manually stopped containers must stay stopped."* Ops Brain must NEVER restart
a container you manually stopped — overriding autonomous remediation, confidence gating,
restart caps, allow-lists, anomaly/drift remediation, daily-report recommendations, and
any LLM-generated action. It must also not destroy one via `docker prune`.

This is **on by default** (`manual_stop_protection.enabled: true`).

## How it works

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

## Re-arm

Start the container again (any method) and protection clears automatically.

## Verify on your system (safe)

1. Start a throwaway container.
2. Run a cycle so the collector seeds it (`python3 scheduler/scheduler.py --once`).
3. `docker stop <container>`.
4. Run another cycle.
5. Confirm it appears in `logs/manual_stops.json` and that no `docker_restart` action is
   ever proposed for it.

## Config

```yaml
manual_stop_protection:
  enabled: true
  track_manual_stops: true
  block_restart_for_manual_stops: true
actions:
  manual_stop_sigkill_protect: true   # 137-without-OOM counts as a manual stop
```