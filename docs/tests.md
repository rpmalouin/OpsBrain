# Tests

The suite covers the decision-critical logic with mocks — **no live Docker/GPU/Ollama**
is touched (GPU/kill tests mock `run()`/`notify`).

```bash
cd /appdata/OpsBrain && . .venv/bin/activate
python3 -m pytest        # 317 tests
python3 -m pytest -q     # quiet
```

## What's covered

- **whitelist gate** (case-insensitive `allow_container` / `allow_service`).
- **`deterministic_rules`** — CPU sustain window, memory creep, GPU threshold, restart
  loop, disk prune, Netdata alarms.
- **`sanitize` / `extract_json`** — Qwen output normalization (accepts `type` or `action`
  key, drops unknown verbs, clamps confidence, tolerates null), `gpu_drift` flag filtering.
- **`collector.evaluate_gpu_drift`** — all five drift flags + baseline bookkeeping.
- **GPU visibility** (`tests/test_gpu_visibility.py`, 7 tests) — `collect_gpu` records
  `available`/`error` when `nvidia-smi` is unusable and keeps the baseline identity;
  `actions.gpu_unavailable_notice` fires once per UTC day, only when a GPU is known.
- **GPU driver reconcile** (`tests/test_gpu_reconcile_ingest.py`, 27 tests) — parses the apt
  hook's log (`nvidia-module-reconcile`), compares the LOADED module version against the one
  on disk, and drives `state`/`attention` (`ok` / `mismatch` / `deferred` / `failed`); a
  deferral the hook later reloaded past stops alarming. `gpu_reconcile_actions` is notify-only
  (once per UTC day, never `docker_restart`/`gpu_kill`/`docker_prune`).
- **Model selection + `think` gate** (`tests/test_reasoner_models.py`, 32 tests) —
  `pick_model`/`resolve_model` (primary → fallback → alt, `:latest` normalization, never an
  unconfigured substitution), `decide_think` (thinking / non-thinking / unknown
  capability), `build_body` (think sent, `format` never), `call_ollama` (400-with-thinking →
  one retry without), `extract_json` resilience, the stale-decision guard
  `actions.reasoner_decision_state`, and the below-floor raw capture
  (`capture_low_confidence` writes/appends/truncates, `main()` captures at 0.0 and stays silent
  at 0.85 — both end-to-end through a stubbed ollama and a temp output path).
- **`actions.gpu_drift_actions`** — stuck_process kill (conf>0.8) vs notify-only
  (conf<=0.8), notify-only for the other four flags, Qwen∪deterministic flag union.
- **`Engine.dispatch`** — ollama-restart guard, manual-stop gate, cluster verbs.
- **ManualStop registry** — classify_manual_stop (exit codes incl. sigkill protect),
  transition detection, prune-block.
- **TrueNAS collector** — parse, unreachable/disabled degradation, creds.
- **Federation** — `_num`/`_cnt` coercion, `reverse_normalize`, cluster stability score
  (spec math), node stability (null-conf-online credit, offline=0), ranking,
  recommendations, empty/all-offline, cross-node correlation.
- **Netdata-sourced node** (`tests/test_netdata_node.py`, 19 tests) — `parse_info` host facts,
  the alarm keep-set plus the name/mount skip-lists (a mounted-DMG CRITICAL must NOT survive),
  GiB→percent volume math, the macOS RAM definition, the node-block contract, and degradation to
  `null` fields (a 404 chart is never an offline node). One test loads the SHIPPED config and asserts
  the whole Mac exclusion list (transient images, remote/SMB capacity, system views) while proving
  `/Volumes/Data2` still raises.
- **Node snapshot files** (`tests/test_local_snapshot.py`, 24 tests) — freshness states
  (`ok`/`stale`/`missing`/`unreadable`) against the producer's **45-minute** contract (three missed
  15-minute ticks), the anomaly semantics (the producer's own alerts count; a stale producer counts
  ONCE and its old facts are not re-judged; an absent file is not a fault), the derived state checks
  (unmounted share, gateway heartbeat, old Time Machine backup) and that **capacity is never judged**,
  host-fact merge with the live poll winning, and a contract test over a real producer payload.
  `test_federation.py` covers the merge through `collect_cluster` and that the compact `raw` stays
  under 500 bytes.
- **Dashboard refinements** — confidence recovery, drift decay, restart impact, and the
  notification ticker's contract (`tests/test_ui_refinements.py`): the category→level map, that
  the UI-detected conditions emit real ticker levels, and that the ring ingests only NEW lines
  (a first call is seeded at EOF, a partial trailing record waits, and a re-read yields nothing).
- **Dockhand desired-state ingest** (`tests/test_dockhand_ingest.py`, 25 tests) — DB pull
  (ok / missing-DB / missing-table, host path mapping), `normalize` (compose parse,
  grouped by stack, unreadable-file degradation, `build:` capture),
  `merge_with_docker_actual` (exact /
  prefix / case-insensitive matching, image mismatch, the local-build exemption,
  health + resources),
  `classify_drift` (every drift kind), `correlate` (restart storm, health flap,
  below-threshold/outside-window), context nodes cap + drift summary, and the dashboard
  snapshot. `tests/test_opsbrain.py` also covers `dockhand_drift_actions` (notify-only
  verbs, down/noop/storms-flaps-orphans) and the `summarize_collector` dockhand digest.

Run it after touching `actions.py`, `reasoner.py`, `common/`, `collector/`, or
`federation/`.

## Config

`pytest.ini` sets `testpaths = tests` and quiet output. The test files live in `tests/`
(`test_opsbrain.py`, `test_manual_stops.py`, `test_collector_manual_stops.py`,
`test_truenas.py`, `test_federation.py`, `test_netdata_node.py`,
`test_local_snapshot.py`, `test_ui_refinements.py`,
`test_dockhand_ingest.py`, `test_vault_drift_ingest.py`, `test_scheduler_cadence.py`,
`test_reasoner_models.py`, `test_gpu_visibility.py`, `test_gpu_reconcile_ingest.py`, plus the
dashboard tests).