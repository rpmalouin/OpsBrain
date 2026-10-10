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


NN_FIXTURES = Path(__file__).parent / "fixtures" / "netdata_mac"


def _mixed_net(monkeypatch):
    """Answer both an OpsBrain /api/status node and a Netdata node from one patched urlopen.

    federation_collector and netdata_node both call urllib.request.urlopen, so a single
    monkeypatch covers the two node kinds.
    """
    class FakeResp:
        def __init__(self, data):
            self._d = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(self._d).encode()

    info = json.loads((NN_FIXTURES / "info.json").read_text())
    alarms = json.loads((NN_FIXTURES / "alarms.json").read_text())

    def urlopen(url, timeout=None):
        url = str(url)
        if "/api/status" in url:
            return FakeResp(_status_payload())
        if "api/v1/info" in url:
            return FakeResp(info)
        if "api/v1/alarms" in url:
            return FakeResp(alarms)
        if "chart=disk_space.%2F" in url:
            return FakeResp({"labels": ["time", "avail", "used"], "data": [[0, 611.0, 315.3]]})
        raise OSError(f"no fixture for {url}")

    monkeypatch.setattr(FC.urllib.request, "urlopen", urlopen)


def test_collect_cluster_mixes_opsbrain_and_netdata_kinds(monkeypatch):
    _mixed_net(monkeypatch)
    nodes = [
        {"name": "dockervm", "type": "linux",
         "collector_endpoint": "http://127.0.0.1:9120/api/status"},        # no kind -> OpsBrain path
        {"name": "macstudio", "type": "macos", "kind": "netdata",
         "collector_endpoint": "http://10.1.10.34:19999",
         "volumes": ["/"],
         "alarm_skip_mounts": ["/Volumes/makemkv", "com.apple.TimeMachine.localsnapshots"]},
    ]
    doc = FC.collect_cluster(nodes, timeout=2)
    mac = doc["nodes"]["macstudio"]
    assert mac["online"] is True and mac["type"] == "macos"
    assert mac["confidence"] is None                       # no reasoner on the Mac
    assert mac["anomalies"] == 0                           # the mounted-DMG CRITICAL is skipped
    assert mac["volumes"] == {"/": 34.0}
    assert mac["host"]["hostname"] == "Mac-Studio.local"
    assert mac["containers"] == {"running": 0, "total": 0}
    # the Mac keeps dockerVM's node untouched (regression: kind dispatch must not leak)
    assert doc["nodes"]["dockervm"]["confidence"] == 0.8
    assert doc["nodes"]["dockervm"]["containers"] == {"running": 48, "total": 50}
    assert "host" not in doc["nodes"]["dockervm"]
    # cluster rollup sees two nodes, the Mac contributing no anomalies or drift
    assert doc["cluster_metrics"]["avg_confidence"] == 0.8   # only the OpsBrain node has confidence
    assert doc["cluster_metrics"]["total_anomalies"] == 1

    # the Mac scores 100 (online, null confidence, clean) and is ranked, not alarmed
    reason = FR.cluster_reason(doc, WEIGHTS)
    assert reason["node_stability"]["macstudio"] == 100.0
    assert reason["node_ranking"][0] == "macstudio"
    # the stub node carries 1 anomaly + 1 drift event -> notify_cluster only; crucially no
    # cluster_health_warning (nothing offline) and no escalate_cluster
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


def test_netdata_kind_node_goes_offline_without_crashing_the_cluster(monkeypatch):
    def boom(url, timeout=None):
        if "/api/status" in str(url):
            raise OSError("no fixture")     # keep the assertion simple: everything unreachable
        raise OSError("connection refused")

    monkeypatch.setattr(FC.urllib.request, "urlopen", boom)
    nodes = [{"name": "macstudio", "type": "macos", "kind": "netdata",
              "collector_endpoint": "http://10.1.10.34:19999", "volumes": ["/"]}]
    doc = FC.collect_cluster(nodes, timeout=1)
    assert doc["nodes"]["macstudio"]["online"] is False
    assert "connection refused" in doc["nodes"]["macstudio"]["err"]
    assert doc["nodes"]["macstudio"]["containers"] == {"running": 0, "total": 0}