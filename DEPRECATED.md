# RESTORED — 2026-11-22 (this file is history now)

**OpsBrain is running again.** The 2026-11-14 retirement below was undone on 2026-11-22 as part of the
three-host revive (`dockerVM` + Mac Studio + the ollama box at `10.1.10.30`); the plan and its measured
facts are `/appdata/A--docker_stacks/.hermes/plans/2026-11-22_224445-opsbrain-revive-three-host.md`, and
the current operating state is the top section of `MEMORY.md`.

What that revive changed relative to the notes below:

- The tree was cloned back from `rpmalouin/OpsBrain` to `/appdata/OpsBrain` with its own `.venv`; both
  units are installed, enabled and running, on that interpreter.
- The reasoner no longer talks to a local ollama: it calls `http://10.1.10.30:11434` with
  `qwen3.5:27b` (the tag hindsight uses), `think: false`, `num_ctx: 65536`.
- The front door is back as CaddyUI row **66** (`opsbrain.home` -> `10.1.10.10:9120`, `basic_auth`),
  with the Caddyfile block appended in place and a homepage tile under *Admin / Monitoring*.
- The Telegram relay was **recreated, not resumed** (`63fef345340a` had been deleted): Hermes cron
  `d18255daea09`, every 2 min, CRITICAL-only, deduped per condition per hour.
- The `ollama` stack retirement reasoning below is obsolete: the GPUs and the LLM moved to their own box,
  and OpsBrain is a client of it over HTTP.

---

# RETIRED — 2026-11-14 (superseded by the section above)

**OpsBrain is retired, not parked.** This tree was moved from `/appdata/OpsBrain` to
`/appdata/archive/OpsBrain-20261114` on 2026-11-14. Nothing runs it: the new bare-metal dockerVM has no
`opsbrain.service`/`opsbrain-ui.service` units, no `opsbrain.home` Caddy route and no Dockhand stack, and
the Hermes Telegram relay (`63fef345340a`) was deleted. The deprecated-on-the-old-VM notes below are kept
for history; the restore steps still describe what it would take to bring it back by hand.

---

# DEPRECATED — 2026-11-13

OpsBrain is **deprecated, not retired**. Both units are stopped and disabled; the repo, its config,
its logs and its data are intact so it can be brought back.

- `opsbrain.service` (scheduler/reasoner) — stopped, **disabled**
- `opsbrain-ui.service` (dashboard, `:9120`) — stopped, **disabled**
- `opsbrain.home` — route removed (CaddyUI `proxy_hosts` row 32 deleted with the app stopped, Caddyfile
  block cut, srv0 31 → 30 hosts). Homepage tile removed.
- Hermes cron job `63fef345340a` ("OpsBrain critical → Telegram", every 2m) — **paused**.

## Why

Ron parked it while the Mac-side migrations are in flight (2026-11-13): its Mac node was already reading
offline after Netdata was removed there, its reasoner is inert, and the host is being simplified. It may
come back once the Mac work settles.

## What was NOT touched

`/appdata/OpsBrain` (repo + `config/`, `logs/`, `reports/`), the Code-Review-Graph registration, the
vault project pages (marked `status: deprecated` instead of archived), every Docker stack it used to
observe, and its own `MEMORY.md`.

## Bringing it back (the 2026-11-13 recipe — kept for the record)

```sh
systemctl enable --now opsbrain.service opsbrain-ui.service
# then re-add the front door, in this order: insert the CaddyUI proxy_hosts row with the app stopped,
# start it, read activity_log for "sync_applied", then append the Caddyfile block (inode-preserving)
# and validate in a throwaway caddy:2-alpine container.
hermes cron resume 63fef345340a        # or: cronjob_manage action=resume job_id=63fef345340a
```

Restore material: `/root/backups/pre-retirement-20261113/caddyui.db.opsbrain-pre` (the row set with row
32 present), `/appdata/caddy/snapshots/Caddyfile.20261113-2115-opsbrain-deprecated`, and
`services.yaml.opsbrain-pre` in the same backup dir.

**Before re-enabling, note what its absence takes with it:** nothing on this host calls OpsBrain. Its
`ollama.url` (`http://localhost:11434/api/generate`) is the only reason the `ollama` stack is still on
disk — see `/appdata/A--docker_stacks/.hermes/plans/2026-11-13_191620-retire-dormant-stacks.md`.
