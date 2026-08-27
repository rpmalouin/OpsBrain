# Tests

The suite covers the decision-critical logic with mocks — **no live Docker/GPU/Ollama**
is touched (GPU/kill tests mock `run()`/`notify`).

```bash
cd /appdata/OpsBrain && . .venv/bin/activate
python3 -m pytest        # 98 tests
python3 -m pytest -q     # quiet
```

## What's covered

- **whitelist gate** (case-insensitive `allow_container` / `allow_service`).
- **`deterministic_rules`** — CPU sustain window, memory creep, GPU threshold, restart
  loop, disk prune, Netdata alarms.
- **`sanitize` / `extract_json`** — Qwen output normalization (accepts `type` or `action`
  key, drops unknown verbs, clamps confidence, tolerates null), `gpu_drift` flag filtering.
- **`collector.evaluate_gpu_drift`** — all five drift flags + baseline bookkeeping.
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

Run it after touching `actions.py`, `reasoner.py`, `common/`, `collector/`, or
`federation/`.

## Config

`pytest.ini` sets `testpaths = tests` and quiet output. The test files live in `tests/`
(`test_opsbrain.py`, `test_manual_stops.py`, `test_collector_manual_stops.py`,
`test_truenas.py`, `test_federation.py`, `test_ui_refinements.py`, plus the dashboard
tests).