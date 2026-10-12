"""Ops Brain - tests for the Federation Layer (collector + reasoner)."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "common"))

from federation import federation_collector as FC  # noqa: E402
from federation import federation_reasoner as FR  # noqa: E402

WEIGHTS = {"confidence": 0.4, "drift": 0.3, "anomalies": 0.2, "restarts": 0.1}


def _status_payload():
    return {"collector": {"reasoner": {"confidence": 0.8},
                          "gpu": {"drift_flags": ["vram_drift"]},
                          "netdata": {"alarms_active": [{"name": "hm_mem"}], "alarms_count": 1},
                          "vm": {"disk_used_percent": "42"},
                          "docker": {"containers_count": 50, "running": 48, "restarting": ["c1"]},
                          "truenas": {"pools_healthy": 1, "pool_count": 1}},
            "reasoner": {"confidence": 0.8}}


@pytest.fixture
def fake_net(monkeypatch):
    class FakeResp:
        def __init__(self, data):
            self._d = data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(self._d).encode()
    def urlopen(url, timeout=None):
        if "down" in str(url):
            raise OSError("connection refused")
        return FakeResp(_status_payload())
    monkeypatch.setattr(FC.urllib.request, "urlopen", urlopen)
    return True


# ------------------------------------------------- collector
def test_collect_parses_node_and_metrics(fake_net):
    nodes = [{"name": "a", "type": "linux", "collector_endpoint": "http://a:1"},
             {"name": "b", "type": "storage", "collector_endpoint": "http://b:1"}]
    doc = FC.collect_cluster(nodes, timeout=2)
    assert doc["nodes"]["a"]["online"] is True
    assert doc["nodes"]["a"]["confidence"] == 0.8
    assert doc["nodes"]["a"]["drift_events"] == 1
    assert doc["nodes"]["a"]["anomalies"] == 1
    assert doc["nodes"]["a"]["restart_events"] == 1
    assert doc["nodes"]["a"]["containers"] == {"running": 48, "total": 50}
    assert doc["nodes"]["a"]["disk_used_percent"] == 42.0
    assert doc["cluster_metrics"]["avg_confidence"] == 0.8
    assert doc["cluster_metrics"]["total_anomalies"] == 2
    assert doc["cluster_metrics"]["drift_events"] == 2


def test_collect_handles_offline(fake_net):
    nodes = [{"name": "down", "type": "linux", "collector_endpoint": "http://down:1"},
             {"name": "good", "type": "storage", "collector_endpoint": "http://good:1"}]
    doc = FC.collect_cluster(nodes, timeout=1)
    assert doc["nodes"]["down"]["online"] is False
    assert "err" in doc["nodes"]["down"]
    assert doc["nodes"]["good"]["online"] is True
    assert doc["cluster_metrics"]["avg_confidence"] == 0.8  # only online counts


def test_collect_all_offline():
    nodes = [{"name": "x", "type": "linux", "collector_endpoint": "http://127.0.0.1:1"}]
    doc = FC.collect_cluster(nodes, timeout=1)
    assert doc["nodes"]["x"]["online"] is False
    assert doc["cluster_metrics"]["avg_confidence"] == 0.0


# ------------------------------------------------- reasoner
SNAP = {
    "nodes": {
        "dockervm": {"online": True, "confidence": 0.8, "drift_events": 0, "anomalies": 0, "restart_events": 0},
        "truenas": {"online": True, "confidence": 0.9, "drift_events": 1, "anomalies": 2, "restart_events": 0},
        "down": {"online": False, "confidence": None, "drift_events": 0, "anomalies": 0, "restart_events": 0},
    },
    "cluster_metrics": {"avg_confidence": 0.85, "total_anomalies": 2, "drift_events": 1, "restart_events": 0},
}


def test_reverse_normalize():
    assert FR.reverse_normalize(0, 20) == 1.0
    assert round(FR.reverse_normalize(5, 20), 3) == 0.75
    assert FR.reverse_normalize(20, 20) == 0.0
    assert FR.reverse_normalize(30, 20) == 0.0


def test_cluster_stability_score_matches_spec():
    s = FR.cluster_stability_score(WEIGHTS, SNAP["cluster_metrics"])
    expected = (0.4 * 0.85 + 0.3 * 0.95 + 0.2 * 0.9 + 0.1 * 1.0) * 100
    assert s == round(expected, 1)


def test_node_stability():
    # 0.8 confidence + clean events -> confidence bucket is 80*0.4, others full
    # (80*0.4 + 100*0.3 + 100*0.2 + 100*0.1) = 92.0
    assert FR.node_stability(SNAP["nodes"]["dockervm"], WEIGHTS) == 92.0
    assert FR.node_stability(SNAP["nodes"]["down"], WEIGHTS) == 0.0  # offline -> 0
    online_null = {"online": True, "confidence": None, "drift_events": 0, "anomalies": 0, "restart_events": 0}
    assert FR.node_stability(online_null, WEIGHTS) == 100.0  # null conf online = full credit


def test_rank_and_recommendations():
    doc = FR.cluster_reason(SNAP, WEIGHTS)
    assert doc["node_ranking"][-1] == "down"
    types = {r["type"] for r in doc["recommendations"]}
    assert "cluster_health_warning" in types  # down node offline
    assert "escalate_cluster" not in types    # score >60
    assert doc["cluster_stability_score"] < 100


def test_empty_snapshot():
    doc = FR.cluster_reason({}, WEIGHTS)
    assert doc["cluster_stability_score"] == 0
    assert doc["node_ranking"] == []
    assert isinstance(doc["recommendations"], list)


def test_all_offline_scores_zero():
    doc = FR.cluster_reason({"nodes": {"x": {"online": False, "confidence": None,
                                             "drift_events": 0, "anomalies": 0, "restart_events": 0}}}, WEIGHTS)
    assert doc["cluster_stability_score"] == 0  # not the misleading 60


def test_cross_correlations():
    nodes = {
        "a": {"online": True, "drift_events": 1, "anomalies": 2, "confidence": 0.5,
              "raw": {"collector": {"netdata": {"alarms_active": [{"name": "hm_mem"}]},
                                    "gpu": {"drift_flags": ["vram_drift"]}}}},
        "b": {"online": True, "drift_events": 1, "anomalies": 2, "confidence": 0.5,
              "raw": {"collector": {"netdata": {"alarms_active": [{"name": "hm_mem"}]},
                                    "gpu": {"drift_flags": ["vram_drift"]}}}},
    }
    c = FR.detect_cross_correlations({"nodes": nodes})
    assert len(c["anomaly_correlations"]) == 1
    assert len(c["drift_correlations"]) == 1
    assert c["anomaly_correlations"][0]["shared_anomaly"] == "hm_mem"
    assert c["drift_correlations"][0]["shared_flag"] == "vram_drift"


# ------------------------------------------------- netdata-kind nodes (the Mac Studio)
def test_collect_node_attaches_raw_so_cross_node_correlation_fires(fake_net):
    """Regression: `_collect_node` did not set node["raw"], so FR.detect_cross_correlations
    (which reads node["raw"]["collector"]["netdata"]["alarms_active"]) could never fire."""
    nodes = [{"name": "a", "type": "linux", "collector_endpoint": "http://a:1"},
             {"name": "b", "type": "linux", "collector_endpoint": "http://b:1"}]
    doc = FC.collect_cluster(nodes, timeout=2)
    assert doc["nodes"]["a"]["raw"]["collector"]["netdata"]["alarms_active"][0]["name"] == "hm_mem"
    c = FR.detect_cross_correlations(doc)
    assert len(c["anomaly_correlations"]) == 1                 # both nodes report hm_mem
    assert c["anomaly_correlations"][0]["nodes"] == ["a", "b"]
    assert len(c["drift_correlations"]) == 1                   # both report vram_drift


def test_raw_payload_stays_compact(fake_net):
    """`raw` exists for correlation only: attaching the whole collector doc blew the cluster
    snapshot up from ~0.5 KB to ~255 KB per cycle."""
    fd_sentinel = "\"containers_count\""           # a marker only the full collector doc carries
    doc = FC.collect_cluster([{"name": "a", "collector_endpoint": "http://a:1"}], timeout=2)
    raw = doc["nodes"]["a"]["raw"]
    assert set(raw["collector"]) == {"netdata", "gpu"}
    assert set(raw["collector"]["netdata"]) == {"alarms_active"}
    assert len(json.dumps(raw)) < 500
    assert fd_sentinel not in json.dumps(doc)      # no bulk collector payload smuggled through


def test_cluster_snapshot_carries_a_node_snapshot_file(tmp_path, monkeypatch):
    """A node with a `snapshot_file` merges its producer's file: the producer's own alerts become
    anomalies while the live poll keeps its own fields."""
    producer = {"schema": 1, "node": "macstudio", "generated_at_utc": "2026-10-10T18:22:24Z",
                "interval_minutes": 15, "alert_count": 1,
                "alerts": ["cron: hermes-patch-watchdog last_status=error streak=533"],
                "host": {"model": "Mac17,14", "load_avg": [5.3, 4.5, 3.4]},
                "time_machine": {"latest_backup_age_hours": 0.72}}
    path = tmp_path / "macstudio_node.json"
    path.write_text(json.dumps(producer))

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(_status_payload()).encode()

    monkeypatch.setattr(FC.urllib.request, "urlopen", lambda url, timeout=None: Resp())
    doc = FC.collect_cluster([{"name": "macstudio", "type": "macos",
                               "collector_endpoint": "http://127.0.0.1:9120/api/status",
                               "snapshot_file": str(path), "snapshot_max_age_s": 3600}], timeout=2)
    node = doc["nodes"]["macstudio"]
    assert node["snapshot"]["state"] == "ok"
    assert node["snapshot"]["alert_count"] == 1
    assert node["snapshot"]["time_machine"]["latest_backup_age_hours"] == 0.72
    assert node["host"]["model"] == "Mac17,14"          # from the producer...
    assert node["host"]["load_avg"]                     # ...and it adds what the poll lacks
    assert node["anomalies"] == 2                       # the poll's own alarm + the producer's alert


def test_cluster_snapshot_missing_file_is_not_an_anomaly(tmp_path, monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(_status_payload()).encode()

    monkeypatch.setattr(FC.urllib.request, "urlopen", lambda url, timeout=None: Resp())
    doc = FC.collect_cluster([{"name": "n", "collector_endpoint": "http://n:1",
                               "snapshot_file": str(tmp_path / "absent.json")}], timeout=2)
    assert doc["nodes"]["n"]["snapshot"]["state"] == "missing"
    assert doc["nodes"]["n"]["anomalies"] == 1          # only the stub's own alarm


def test_collect_cluster_mixes_a_polled_node_and_a_snapshot_sourced_one(tmp_path, monkeypatch):
    """The Mac has no endpoint at all: its producer file decides online/offline, and the polled node is
    left untouched by the merge (regression: kind dispatch and the merge must not leak across nodes)."""
    producer = {"schema": 2, "node": "macstudio", "generated_at_utc": "2026-10-12T23:00:00Z",
                "interval_minutes": 15, "alert_count": 1, "alerts": ["cron: hermes-patch-watchdog"],
                "host": {"model": "Mac17,14", "load_avg": [1.0, 1.0, 1.0]},
                "disks": [{"path": "/", "used_percent": 34.2},
                          {"path": "/Volumes/Data2", "used_percent": 82.9}],
                "mounts": [{"path": "/Volumes/Data", "mounted": True, "read_only": False}],
                "gateway": {"present": True, "age_seconds": 12.0},
                "time_machine": {"latest_backup_age_hours": 0.7}}
    path = tmp_path / "macstudio_node.json"
    path.write_text(json.dumps(producer))

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(_status_payload()).encode()

    monkeypatch.setattr(FC.urllib.request, "urlopen", lambda url, timeout=None: Resp())
    nodes = [
        {"name": "dockervm", "type": "linux",
         "collector_endpoint": "http://127.0.0.1:9120/api/status"},          # no kind -> OpsBrain path
        {"name": "macstudio", "type": "macos", "kind": "snapshot",
         "snapshot_file": str(path), "snapshot_max_age_s": 2700, "snapshot_dead_after_s": 7200},
    ]
    doc = FC.collect_cluster(nodes, timeout=2)
    mac = doc["nodes"]["macstudio"]
    assert mac["online"] is True and mac["type"] == "macos"
    assert mac["confidence"] is None                    # no reasoner on the Mac
    assert mac["anomalies"] == 1                        # the producer's own alert, and nothing else
    assert mac["volumes"] == {"/": 34.2, "/Volumes/Data2": 82.9}
    assert mac["disk_used_percent"] == 82.9             # 82 % is reported, never alarmed on
    assert mac["host"]["model"] == "Mac17,14"
    assert mac["containers"] == {"running": 0, "total": 0}
    assert mac["raw"]["collector"]["snapshot"]["alert_keys"] == ["cron"]
    assert "err" not in mac                             # a node that never polls cannot fail a poll
    # the Mac's file leaves the polled node untouched
    assert doc["nodes"]["dockervm"]["confidence"] == 0.8
    assert doc["nodes"]["dockervm"]["containers"] == {"running": 48, "total": 50}
    assert "host" not in doc["nodes"]["dockervm"]
    # cluster rollup sees two nodes; only the polled one has a confidence to average
    assert doc["cluster_metrics"]["avg_confidence"] == 0.8
    assert doc["cluster_metrics"]["total_anomalies"] == 2

    reason = FR.cluster_reason(doc, WEIGHTS)
    assert reason["node_stability"]["macstudio"] == 99.0   # online + null confidence = full credit
    assert reason["node_ranking"][0] == "macstudio"
    # no cluster_health_warning (nothing offline) and no escalate_cluster, even with 2 anomalies
    assert {r["type"] for r in reason["recommendations"]} == {"notify_cluster"}


def test_per_node_timeout_overrides_the_poll_default(monkeypatch):
    """A netdata-kind node reads several endpoints per cycle; it can carry its own timeout."""
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(_status_payload()).encode()

    def urlopen(url, timeout=None):
        seen.append(timeout)
        return Resp()

    monkeypatch.setattr(FC.urllib.request, "urlopen", urlopen)
    FC.collect_cluster([{"name": "a", "collector_endpoint": "http://a:1", "timeout_s": 2}], timeout=9)
    assert seen and set(seen) == {2.0}


def test_cluster_recommendations_name_the_nodes_behind_them():
    """A bare count ('1 total anomaly(ies) across cluster') is unactionable; with a per-node source
    like the Mac's snapshot alerts, the ticker entry must name the node."""
    snap = {"nodes": {
        "dockervm": {"online": True, "confidence": 0.9, "drift_events": 1,
                     "anomalies": 0, "restart_events": 0},
        "macstudio": {"online": True, "confidence": None, "drift_events": 0,
                      "anomalies": 1, "restart_events": 0}},
        "cluster_metrics": {"avg_confidence": 0.9, "total_anomalies": 1,
                            "drift_events": 1, "restart_events": 0}}
    doc = FR.cluster_reason(snap, WEIGHTS)
    texts = [r["reason"] for r in doc["recommendations"]]
    assert any(t.startswith("1 total anomaly(ies) across cluster: macstudio") for t in texts), texts
    assert any(t.startswith("1 GPU drift event(s) across cluster: dockervm") for t in texts), texts


def test_snapshot_node_goes_offline_when_its_producer_stops(tmp_path):
    """Replaces the old Netdata-offline case: there is no endpoint to fail, so a producer that stopped
    is what takes the node down — as OFFLINE with one anomaly, not as an `err`."""
    import os
    import time

    path = tmp_path / "macstudio_node.json"
    path.write_text(json.dumps({"schema": 2, "node": "macstudio", "alert_count": 0, "alerts": []}))
    old = time.time() - 4 * 3600
    os.utime(path, (old, old))
    doc = FC.collect_cluster([{"name": "macstudio", "type": "macos", "kind": "snapshot",
                               "snapshot_file": str(path), "snapshot_max_age_s": 2700,
                               "snapshot_dead_after_s": 7200}], timeout=1)
    mac = doc["nodes"]["macstudio"]
    assert mac["online"] is False
    assert mac["snapshot"]["state"] == "dead" and mac["anomalies"] == 1
    assert "err" not in mac                                 # nothing was polled, so nothing failed
    assert mac["containers"] == {"running": 0, "total": 0}
    # an all-offline cluster scores 0, not a calm 60, and names the node it cannot see
    reason = FR.cluster_reason(doc, WEIGHTS)
    assert reason["cluster_stability_score"] == 0
    assert reason["node_stability"]["macstudio"] == 0.0
    recs = {r["type"]: r for r in reason["recommendations"]}
    assert recs["cluster_health_warning"]["severity"] == "critical"
    assert "macstudio" in recs["cluster_health_warning"]["reason"]


def test_snapshot_node_stays_online_through_a_missed_tick(tmp_path):
    """The other half of the two-tier rule: 46 minutes of silence is one anomaly, not an outage."""
    import os
    import time

    path = tmp_path / "macstudio_node.json"
    path.write_text(json.dumps({"schema": 2, "node": "macstudio", "alert_count": 0, "alerts": [],
                                "disks": [{"path": "/", "used_percent": 34.2}]}))
    old = time.time() - 46 * 60
    os.utime(path, (old, old))
    doc = FC.collect_cluster([{"name": "macstudio", "type": "macos", "kind": "snapshot",
                               "snapshot_file": str(path), "snapshot_max_age_s": 2700}], timeout=1)
    mac = doc["nodes"]["macstudio"]
    assert mac["online"] is True and mac["snapshot"]["state"] == "stale"
    assert mac["anomalies"] == 1
    assert mac["volumes"] == {"/": 34.2}                    # still the best information we have
    reason = FR.cluster_reason(doc, WEIGHTS)
    assert "cluster_health_warning" not in {r["type"] for r in reason["recommendations"]}


def test_a_snapshot_node_merges_exactly_once(tmp_path):
    """Anomalies and alert counts are additive, so a second merge would double-count one finding."""
    path = tmp_path / "macstudio_node.json"
    path.write_text(json.dumps({"schema": 2, "node": "macstudio", "alert_count": 2,
                                "alerts": ["cron: a", "time_machine: b"]}))
    doc = FC.collect_cluster([{"name": "macstudio", "type": "macos", "kind": "snapshot",
                               "snapshot_file": str(path), "snapshot_max_age_s": 2700}], timeout=1)
    mac = doc["nodes"]["macstudio"]
    assert mac["anomalies"] == 2 and mac["alerts_count"] == 2