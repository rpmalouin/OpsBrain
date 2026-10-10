"""Ops Brain - tests for the local node snapshot merge (Phase 2 producer).

Fixture-driven: `tests/fixtures/netdata_mac/macstudio_node.json` is a real snapshot written by the
Mac's 15-minute producer (see its README). No test touches the network or the live service.
"""
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "common"))

from federation import local_snapshot as LS  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "netdata_mac"


def _write(tmp_path, payload, age_s=0.0):
    p = tmp_path / "node.json"
    p.write_text(json.dumps(payload))
    if age_s:
        import os
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


def _producer():
    return json.loads((FX / "macstudio_node.json").read_text())


# --------------------------------------------------------------- reading
def test_read_missing_file_is_missing_state(tmp_path):
    snap = LS.read_snapshot(tmp_path / "nope.json")
    assert snap["state"] == "missing" and snap["data"] is None and snap["age_s"] is None


def test_read_fresh_file_is_ok(tmp_path):
    snap = LS.read_snapshot(_write(tmp_path, _producer()), max_age_s=3600)
    assert snap["state"] == "ok"
    assert snap["age_s"] is not None and snap["age_s"] < 5
    assert snap["data"]["node"] == "macstudio"


def test_read_old_file_is_stale(tmp_path):
    snap = LS.read_snapshot(_write(tmp_path, _producer(), age_s=7200), max_age_s=3600)
    assert snap["state"] == "stale" and snap["age_s"] >= 7000


def test_read_max_age_zero_never_expires(tmp_path):
    snap = LS.read_snapshot(_write(tmp_path, _producer(), age_s=99999), max_age_s=0)
    assert snap["state"] == "ok"


def test_read_junk_file_is_unreadable_not_fatal(tmp_path):
    p = tmp_path / "node.json"
    p.write_text("{not json")
    snap = LS.read_snapshot(p)
    assert snap["state"] == "unreadable" and "error" in snap and snap["data"] is None


def test_read_non_object_payload_is_unreadable(tmp_path):
    snap = LS.read_snapshot(_write(tmp_path, [1, 2, 3]))
    assert snap["state"] == "unreadable"


# --------------------------------------------------------------- merging
def test_merge_fresh_snapshot_counts_the_producers_own_alerts(tmp_path):
    node = {"online": True, "type": "macos", "anomalies": 0, "alerts_count": 0,
            "host": {"hostname": "Mac-Studio.local", "os": "macOS 27.0.1"}}
    snap = LS.read_snapshot(_write(tmp_path, _producer()), max_age_s=3600)
    out = LS.merge_snapshot(node, snap, source="logs/macstudio_node.json")
    assert out is node
    # the fixture's producer reports one active alert (a Hermes cron in an error streak)
    assert node["snapshot"]["alert_count"] == 1
    assert node["snapshot"]["state"] == "ok"
    assert node["snapshot"]["alerts"] == _producer()["alerts"]
    assert node["anomalies"] == 1 and node["alerts_count"] == 1


def test_merge_keeps_the_live_polls_own_host_facts(tmp_path):
    node = {"online": True, "anomalies": 0, "alerts_count": 0,
            "host": {"hostname": "Mac-Studio.local", "os": "macOS 27.0.1", "cores": 18}}
    snap = LS.read_snapshot(_write(tmp_path, _producer()), max_age_s=3600)
    LS.merge_snapshot(node, snap)
    # live values win where both sources have a fact...
    assert node["host"]["os"] == "macOS 27.0.1" and node["host"]["cores"] == 18
    # ...and the producer ADDS what the poll cannot see
    assert node["host"]["load_avg"] and node["host"]["uptime_hours"] > 0
    assert node["host"]["model"] == "Mac17,14"


def test_merge_stale_snapshot_adds_exactly_one_anomaly(tmp_path):
    node = {"online": True, "anomalies": 0, "alerts_count": 0}
    snap = LS.read_snapshot(_write(tmp_path, _producer(), age_s=7200), max_age_s=3600)
    LS.merge_snapshot(node, snap)
    assert node["snapshot"]["state"] == "stale"
    assert node["anomalies"] == 1            # not one per missed 15-minute tick
    assert node["snapshot"]["note"] == "producer snapshot is stale"


def test_merge_missing_snapshot_is_not_an_anomaly(tmp_path):
    node = {"online": True, "type": "macos", "anomalies": 0, "alerts_count": 0}
    LS.merge_snapshot(node, LS.read_snapshot(tmp_path / "absent.json"))
    assert node["snapshot"]["state"] == "missing"
    assert node["anomalies"] == 0 and node["alerts_count"] == 0


def test_merge_unreadable_snapshot_is_not_an_anomaly_but_keeps_the_error(tmp_path):
    p = tmp_path / "node.json"
    p.write_text("{half")
    node = {"online": True, "anomalies": 0, "alerts_count": 0}
    LS.merge_snapshot(node, LS.read_snapshot(p))
    assert node["snapshot"]["state"] == "unreadable"
    assert "error" in node["snapshot"] and node["anomalies"] == 0


def test_merge_carries_the_telemetry_the_poll_cannot_see(tmp_path):
    node = {"online": True, "anomalies": 0, "alerts_count": 0}
    LS.merge_snapshot(node, LS.read_snapshot(_write(tmp_path, _producer())))
    snap = node["snapshot"]
    for key in ("time_machine", "mounts", "services", "hermes_cron", "local_model", "disks"):
        assert key in snap, key
    assert snap["time_machine"]["latest_backup_age_hours"] is not None
    assert snap["hermes_cron"]["jobs_total"] >= 1


def test_merge_gives_a_node_without_host_a_host_block(tmp_path):
    node = {"online": True, "anomalies": 0, "alerts_count": 0}
    LS.merge_snapshot(node, LS.read_snapshot(_write(tmp_path, _producer())))
    assert node["host"]["model"] == "Mac17,14"


# --------------------------------------------------------------- producer contract
def test_producer_fixture_still_carries_the_contract_we_consume():
    """Guard against a producer schema change: these keys are what the merge reads."""
    data = _producer()
    for key in ("schema", "node", "generated_at_utc", "interval_minutes", "alerts", "alert_count"):
        assert key in data, key
    assert data["node"] == "macstudio"
    assert isinstance(data["alerts"], list)
