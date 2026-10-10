"""Ops Brain - tests for dashboard refinements (confidence recovery,
drift decay, restart impact). Tests the pure functions directly."""
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI = REPO / "ui"
sys.path.insert(0, str(UI))

import server as S  # noqa: E402


def _reset():
    S._conf_recovery = {"detected": False, "prev": None, "current": None, "delta": 0.0}
    S._drift_decay = {"vram": [], "temp": [], "power": [], "decay_cycles": 0, "status": "ok"}
    S._restart_impact = {}
    for q in ("gpu_vram", "gpu_temp", "gpu_power", "gpu_baseline_vram", "confidence"):
        S._hist[q].clear()
    S._hist["containers"].clear()


# ------------------------------------------------- notification ticker levels
def test_notification_levels_follow_category(tmp_path, monkeypatch):
    """A GPU/driver fault or a blocked manual stop must not render as the same yellow "warn"
    chip as a vault note — the ticker's level comes from this category map."""
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [{"category": "gpu_reconcile", "message": "driver mismatch"},
            {"category": "manual_stop_protection", "message": "blocked restart of x"},
            {"category": "cluster_health_warning", "message": "2 node(s) offline"},
            {"category": "vault_drift", "message": "10 vault finding(s)"},
            {"category": "action", "message": "noticed something"}]
    (logs / "notifications.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(S, "REPO", tmp_path)
    S._ingest_offsets.clear()
    try:
        out = S._read_notifications_tail()
    finally:
        S._ingest_offsets.clear()
    lv = {o["category"]: o["severity"] for o in out}
    assert lv["gpu_reconcile"] == "critical"
    assert lv["manual_stop_protection"] == "critical"
    assert lv["cluster_health_warning"] == "critical"
    assert lv["vault_drift"] == "warning"
    assert lv["action"] == "info"
    assert all(o["msg"] for o in out)


# ------------------------------------------------- confidence recovery
def test_recovery_fires_on_increase():
    _reset()
    S._update_conf_recovery(0.4)
    r = S._update_conf_recovery(0.75)
    assert r["detected"] is True
    assert r["prev"] == 0.4 and r["current"] == 0.75
    assert abs(r["delta"] - 0.35) < 1e-3


def test_recovery_not_fired_on_decrease():
    _reset()
    S._update_conf_recovery(0.75)
    r = S._update_conf_recovery(0.6)
    assert r["detected"] is False


def test_recovery_null_safe():
    _reset()
    r = S._update_conf_recovery(None)
    assert r["detected"] is False


# ------------------------------------------------- empty / invisible GPU
def test_update_history_survives_missing_gpu():
    """Regression: gpus==[] (driver mismatch, lost passthrough) crash-looped the UI."""
    _reset()
    S._update_history({"gpu": {"gpus": [], "baseline": {}}, "docker": {"containers": []}},
                      {"confidence": 0.5})
    assert list(S._hist["gpu_vram"]) == [0]
    assert list(S._hist["gpu_temp"]) == [0]
    assert list(S._hist["gpu_power"]) == [0.0]


def test_update_history_survives_absent_gpu_key():
    _reset()
    S._update_history({"docker": {"containers": []}}, {"confidence": None})
    assert list(S._hist["gpu_vram"]) == [0]


def test_update_history_uses_real_gpu_when_present():
    _reset()
    S._update_history({"gpu": {"gpus": [{"mem_used_mb": 14046, "temp_c": 39, "power_w": 11.2}],
                               "baseline": {"last_vram": 14000}},
                       "docker": {"containers": []}}, {"confidence": 0.85})
    assert list(S._hist["gpu_vram"]) == [14046]
    assert list(S._hist["gpu_baseline_vram"]) == [14000]
    assert list(S._hist["confidence"]) == [0.85]


# ------------------------------------------------- drift decay
def test_drift_decay_ok_when_within_tolerance():
    _reset()
    # history within 250MB / 5C / 40W of baseline
    S._hist["gpu_vram"].extend([14000, 14010, 14005])
    S._hist["gpu_temp"].extend([40, 40, 40])
    S._hist["gpu_power"].extend([11.0, 11.2, 11.1])
    S._last_state = {f"{S.REPO}/logs/gpu_baseline.json":
                     (0, 0, {"last_vram": 14000, "last_temp": 40, "last_power": 11.0})}
    d = S._update_drift_decay()
    assert d["status"] == "ok"
    assert d["decay_cycles"] == 0


def test_drift_decay_bad_when_persistent_overload():
    _reset()
    S._hist["gpu_vram"].extend([16000, 16050, 16020])  # far above 14000 baseline
    S._hist["gpu_temp"].extend([70, 71, 72])
    S._hist["gpu_power"].extend([200, 210, 205])
    S._last_state = {f"{S.REPO}/logs/gpu_baseline.json":
                     (0, 0, {"last_vram": 14000, "last_temp": 40, "last_power": 11.0})}
    d = S._update_drift_decay()
    assert d["status"] == "bad"
    assert d["decay_cycles"] >= 3


# ------------------------------------------------- restart impact
def test_restart_detects_from_multiple_sources():
    _reset()
    actions = {
        "qwen_actions": [{"type": "docker_restart", "target": "jellyfin"}],
        "rule_actions": [{"type": "docker_restart", "target": "plex"}],
        "executed": [{"verb": "docker_restart", "target": "tdarr"}],
        "confidence": 0.75,
    }
    out = S._detect_restarts(actions)
    assert set(out) == {"jellyfin", "plex", "tdarr"}
    # no false positives from other verbs
    assert "caddy" not in out


def test_restart_impact_scores_after_3_cycles():
    _reset()
    S._register_restart("jellyfin", 0.70)
    S._advance_restart_impact(0.80)   # cycle 1
    S._advance_restart_impact(0.85)   # cycle 2
    S._advance_restart_impact(0.75)   # cycle 3 -> done
    imp = S._restart_impact["jellyfin"]
    assert imp["done"] is True
    # deltas: +0.10, +0.15, +0.05 -> avg 0.10
    assert abs(imp["score"] - 0.10) < 1e-3


def test_restart_impact_null_when_insufficient():
    _reset()
    S._register_restart("jellyfin", 0.70)
    S._advance_restart_impact(0.80)   # only 1 of 3 cycles
    imp = S._restart_impact["jellyfin"]
    assert imp["done"] is False
    assert imp["score"] is None


def test_restart_impact_payload_shape():
    _reset()
    S._register_restart("jellyfin", 0.70)
    payload = S._restart_impact_payload()
    assert payload[0]["container"] == "jellyfin"
    assert "samples" in payload[0]
    assert isinstance(payload[0]["samples"], list)
    assert "conf_before" in payload[0]["samples"][0]
    assert "conf_1" in payload[0]["samples"][0]