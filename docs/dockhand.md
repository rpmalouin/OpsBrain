# Dockhand desired-state drift

OpsBrain ingests **Dockhand** (the homelab stack manager at `:3000`) as an extra
**desired-state** source: the `collector` reads Dockhand's local SQLite registry to learn
what every stack *should* be running, merges that against what Docker is *actually*
running, classifies drift, and correlates archived container events into restart storms /
health flaps.

## Why a direct SQLite read (not the HTTP API)

Dockhand's HTTP API (`/api/stacks`, `/api/events`) requires an active UI session and an
environment to be selected; with none selected it returns `[]` / `"No environment
selected"`. The reliable, always-available source is Dockhand's local SQLite database,
which the Dockhand container bind-mounts from the host. OpsBrain opens it **read-only**
(`mode=ro`, safe with Dockhand's live WAL writes) — it never writes to Dockhand's state.

## Configuration (`sources.dockhand`)

```yaml
sources:
  dockhand:
    enabled: true
    db_path: /appdata/dockhand/sqlite/db/dockhand.db   # Dockhand SQLite (WAL, read-only)
    compose_root: /appdata/A--docker_stacks            # host prefix mapped from /app/data/stacks
    environment_id: 1                                  # 1 = DockerVM (local desired-state)
    storm_min_events: 3                                # die/destroy/restart in window -> storm
    storm_window_s: 1800
    flap_min_transitions: 2                            # healthy<->unhealthy toggles -> flap
```

- `db_path` — the Dockhand SQLite file on the host (defaults to the DockerVM path).
- `compose_root` — Dockhand stores `compose_path` as its in-container
  `/app/data/stacks/<name>/compose.yml`; OpsBrain maps that prefix to this host directory
  (default `/appdata/A--docker_stacks`, which the Dockhand container bind-mounts).
- `environment_id` — which Dockhand environment is "this host"; 1 = DockerVM.

## Module: `collector/dockhand_ingest.py`

Self-contained, every stage a pure function, degrades gracefully (a missing/unreadable DB
yields `{"up": false, "err": ...}`, never a raised exception):

1. **`pull_dockhand_state()`** — read environments, `stack_sources` (the desired-state
   registry, host-mapped), the most recent `container_events`, a curated `settings_summary`,
   and `git_repositories`/`git_stacks` sync state.
2. **`normalize()`** — filter to `environment_id`, group by stack, best-effort parse each
   stack's `compose.yml`/`compose.yaml` into desired services (image, restart policy,
   depends_on, ports, volumes, networks, env, labels, replicas).
3. **`merge_with_docker_actual()`** — match each desired service to a running container
   (exact, `<stack>_<service>[_n]`, instance-prefix, then substring — case-insensitive;
   Docker capitalises e.g. `Firefox`). Flags image mismatch + missing volumes/networks.
4. **`classify_drift()`** — eight drift flags + capped item lists: `state`, `health`,
   `replica`, `image`, `volume`, `network`, `dependency`, `policy` (+ derived `compose`).
5. **`correlate()`** — from Dockhand's archived `container_events` (the saved dockerd event
   stream, ~10k rows) compute **restart storms** (≥3 die/destroy/restart/`recreate` per
   container in the window) and **health flaps** (≥2 healthy↔unhealthy toggles). This is a
   signal OpsBrain's own 2-min polling cannot see.
6. **`create_context_nodes()`** — collapse drift into compact reasoner-facing nodes
   (`{stack, service, expected, actual, cause, evidence}`) + a one-line `drift_summary` +
   an `attention` level.
7. **`update_dashboard_snapshot()`** — the dashboard shape: `stack_drift`, `service_drift`,
   `dependency_failures`, `image_mismatch`, `health_violations`, `restart_storms`,
   `orphaned_containers` (running containers not in any stack), `missing_resources`.
   `orphaned` is case-insensitive so `Firefox` matches the `firefox` stack.

`collect_dockhand(cfg)` orchestrates all of it and returns the full multi-level doc
(normalize / merge / classify / correlate / context_nodes / dashboard), which the collector
merges into `logs/collector.json` under the **`dockhand`** key.

## How OpsBrain reacts (NOTIFY-ONLY)

Dockhand drift is **informational** — the module ends at dashboard / reasoner context and
**never proposes a `docker_restart`**. This is deliberate:

- It cannot collide with the **Manual-Stop Protection HARD INVARIANT** (a manually stopped
  container must stay stopped) or bypass the allow-list.
- `hermes_actions.dockhand_drift_actions()` emits `notify_dockhand_drift` events (a
  registered, notify-only verb) for drift, restart storms, health flaps, and orphans. They
  land in `logs/actions_result.json` under `dockhand.notifications` and in
  `logs/notifications.jsonl` with category `dockhand_drift`.

## Reasoner digest

`reasoner.summarize_collector()` includes a compact `dockhand` object (up, attention,
drift_summary, counts, top state/image drift, storms, flaps, orphans) so Qwen can factor
desired-state drift into its decision — without blowing the small-prompt budget.

## Dashboard

`ui/static/app.js` renders a **Dockhand** panel: drift count (colour-coded), attention,
one-line summary, and counts/listings for stacks drifting, restart storms, health flaps,
orphaned containers, and missing resources. The panel's status reflects `dockhand.up` /
`enabled`, so a down Dockhand DB is visible immediately.

## Verification

```bash
# one cycle, then inspect the dockhand doc in collector.json
python3 collector/collector.py
python3 - <<'PY'
import json
d = json.load(open("logs/collector.json"))["dockhand"]
print(d["normalize"], d["classify"]["drift_count"], d["context_nodes"]["drift_summary"])
PY
python3 -m pytest tests/test_dockhand_ingest.py -q     # 20 module tests
```