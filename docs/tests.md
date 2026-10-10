# Tests

The suite covers the decision-critical logic with mocks — **no live Docker/GPU/Ollama**
is touched (GPU/kill tests mock `run()`/`notify`).

```bash
cd /appdata/OpsBrain && . .venv/bin/activate
python3 -m pytest        # 244 tests
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
- **Model selection + `think` gate** (`tests/test_reasoner_models.py`, 23 tests) —
  `pick_model`/`resolve_model` (primary → fallback → alt, `:latest` normalization, never an
  unconfigured substitution), `decide_think` (thinking / non-thinking / unknown
  capability), `build_body` (think sent, `format` never), `call_ollama` (400-with-thinking →
  one retry without), `extract_json` resilience, and the stale-decision guard
  `actions.reasoner_decision_state`.
- **`actions.gpu_drift_actions`** — stuck_process kill (conf>0.8) vs notify-only
  (conf<=0.8), notify-only for the other four flags, Qwen∪deterministic flag union.
- **`Engine.dispatch`** — ollama-restart guard, manual-stop gate, cluster verbs.
- **ManualStop registry** — classify_manual_stop (exit codes incl. sigkill protect),
  transition detection, prune-block.
- **TrueNAS collector** — parse, unreachable/disabled degradation, creds.
- **Federation** — `_num`/`_cnt` coercion, `reverse_normalize`, cluster stability score
  (spec math), node stability (null-conf-online credit, offline=0), ranking,
  recommendations, empty/all-offline, cross-node correlation.
- **Dashboard refinements** — confidence recovery, drift decay, restart impact.
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
`test_truenas.py`, `test_federation.py`, `test_ui_refinements.py`,
`test_dockhand_ingest.py`, `test_vault_drift_ingest.py`, `test_scheduler_cadence.py`,
`test_reasoner_models.py`, `test_gpu_visibility.py`, `test_gpu_reconcile_ingest.py`, plus the
dashboard tests).