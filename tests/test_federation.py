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