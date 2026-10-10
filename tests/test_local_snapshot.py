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


# --------------------------------------------------------------- staleness contract
def test_default_staleness_is_three_missed_intervals():
    """The producer's contract (hermes-mac ask 20261010T182237Z-f2b12bf0): a document older than
    THREE 15-minute intervals (45 min) must read as stale, not healthy."""
    assert LS.DEFAULT_MAX_AGE_S == 45 * 60


def test_forty_six_minute_old_document_is_stale(tmp_path):
    snap = LS.read_snapshot(_write(tmp_path, _producer(), age_s=46 * 60))
    assert snap["state"] == "stale"


def test_thirty_minute_old_document_is_still_ok(tmp_path):
    snap = LS.read_snapshot(_write(tmp_path, _producer(), age_s=30 * 60))
    assert snap["state"] == "ok"


# --------------------------------------------------------------- derived alerts
def test_derived_alerts_flags_an_unmounted_share():
    data = {"mounts": [{"path": "/Volumes/Data", "mounted": True},
                       {"path": "/Volumes/TimeMachine", "mounted": False}]}
    assert LS.derived_alerts(data) == ["mount not mounted: /Volumes/TimeMachine"]


def test_derived_alerts_flags_the_gateway_heartbeat():
    assert LS.derived_alerts({"gateway": {"present": False}}) == ["hermes gateway heartbeat missing"]
    stale = LS.derived_alerts({"gateway": {"present": True, "age_seconds": 900}})
    assert stale == ["hermes gateway heartbeat stale (900s)"]
    assert LS.derived_alerts({"gateway": {"present": True, "age_seconds": 15}}) == []


def test_derived_alerts_flags_an_old_time_machine_backup():
    assert LS.derived_alerts({"time_machine": {"latest_backup_age_hours": 0.7}}) == []
    assert LS.derived_alerts({"time_machine": {"latest_backup_age_hours": 100}}) == \
        ["time machine backup 100.0h old (limit 48h)"]


def test_derived_alerts_NEVER_judge_capacity():
    """A volume at 82% is the Mac's steady state; its own alerts[] carries its judgments."""
    data = {"disks": [{"path": "/Volumes/Data2", "used_percent": 99.0}],
            "volumes_watched": ["/Volumes/Data2"]}
    assert LS.derived_alerts(data) == []


def test_derived_alerts_are_threshold_configurable():
    data = {"time_machine": {"latest_backup_age_hours": 24}, "gateway": {"present": True,
                                                                        "age_seconds": 400}}
    assert LS.derived_alerts(data, tm_stale_hours=12, gateway_stale_s=600) == \
        ["time machine backup 24.0h old (limit 12h)"]


def test_merge_counts_derived_alerts_alongside_the_producers_own(tmp_path):
    producer = _producer()
    producer["alerts"] = ["cron: hermes-patch-watchdog"]
    producer["alert_count"] = 1
    producer["mounts"] = [{"path": "/Volumes/Data", "mounted": False}]
    producer["gateway"] = {"present": False}
    node = {"online": True, "anomalies": 0, "alerts_count": 0}
    LS.merge_snapshot(node, LS.read_snapshot(_write(tmp_path, producer)))
    assert node["snapshot"]["alert_count"] == 1
    assert node["snapshot"]["derived_alerts"] == ["mount not mounted: /Volumes/Data",
                                                  "hermes gateway heartbeat missing"]
    assert node["anomalies"] == 3 and node["alerts_count"] == 3


def test_merge_does_not_derive_alerts_from_a_stale_document(tmp_path):
    """Old facts make bad alerts: a stale document counts once and is not re-judged."""
    producer = _producer()
    producer["mounts"] = [{"path": "/Volumes/Data", "mounted": False}]
    node = {"online": True, "anomalies": 0, "alerts_count": 0}
    LS.merge_snapshot(node, LS.read_snapshot(_write(tmp_path, producer, age_s=60 * 60)))
    assert node["snapshot"]["state"] == "stale"
    assert node["snapshot"]["derived_alerts"] == []
    assert node["anomalies"] == 1
