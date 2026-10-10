"""Ops Brain - tests for the Netdata-sourced federation node adapter.

Fixture-driven where possible: `tests/fixtures/netdata_mac/` holds real payloads captured
from the Mac Studio's Netdata (see its README). No test here touches the network, the live
logs or the live service.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "common"))

from federation import netdata_node as NN  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "netdata_mac"

MAC_NODE = {"name": "macstudio", "type": "macos", "kind": "netdata",
            "collector_endpoint": "http://10.1.10.34:19999",
            "volumes": ["/", "/Volumes/Data"],
            "alarm_skip_names": ["disk_fill_rate", "disk_inode_rate"],
            "alarm_skip_mounts": ["/Volumes/makemkv", "com.apple.TimeMachine.localsnapshots"]}

CONTRACT_KEYS = {"online", "type", "confidence", "drift_events", "anomalies",
                 "restart_events", "disk_used_percent", "containers", "alerts_count"}


def _fx(name):
    return json.loads((FX / name).read_text())


# --------------------------------------------------------------- info / host facts
def test_parse_info_extracts_host_facts():
    host = NN.parse_info(_fx("info.json"))
    assert host["hostname"] == "Mac-Studio.local"
    assert host["cpu"] == "Apple M5 Max"
    assert host["cores"] == 18
    assert host["ram_total_gb"] == 64.0
    assert host["os"].startswith("macOS")
    assert host["arch"] == "arm64"
    assert host["netdata"] == "v2.11.1"


def test_parse_info_tolerates_empty_doc():
    host = NN.parse_info(None)
    assert host["hostname"] is None and host["cores"] is None and host["ram_total_gb"] is None


# --------------------------------------------------------------- alarms
def test_select_alarms_keeps_only_critical_warning_error():
    # the fixture's only CRITICAL is the mounted DMG; the fixture also holds 2 UNINITIALIZED
    # and 6 CLEAR entries, which the keep-set must drop
    al = NN.select_alarms(_fx("alarms.json"))
    assert [a["name"] for a in al] == ["disk_space_usage"]
    assert al[0]["status"] == "CRITICAL"
    assert al[0]["mount"] == "/Volumes/makemkv_v2.0.0"


def test_select_alarms_skips_transient_mounts_and_noisy_names():
    assert NN.select_alarms(_fx("alarms.json"),
                            skip_mounts=MAC_NODE["alarm_skip_mounts"]) == []
    doc = {"alarms": {
        "disk_fill_rate": {"name": "disk_fill_rate", "status": "WARNING",
                           "chart": "disk_space./Volumes/Data", "value": 1.0},
        "ram_usage": {"name": "ram_usage", "status": "WARNING",
                      "chart": "system.ram", "value": 91.0},
        "undef": {"name": "x", "status": "UNDEFINED", "chart": "system.ram", "value": None}}}
    kept = NN.select_alarms(doc, skip_names=("disk_fill_rate",))
    assert [a["name"] for a in kept] == ["ram_usage"]
    assert kept[0]["mount"] is None


def test_select_alarms_tolerates_list_payload():
    doc = {"alarms": [{"name": "ram_usage", "status": "WARNING", "chart": "system.ram"}]}
    assert [a["name"] for a in NN.select_alarms(doc)] == ["ram_usage"]


# --------------------------------------------------------------- disk / ram dimensions
def test_volume_percent_computes_from_gib_dimensions():
    # fixture is /Volumes/Data: used 86.1 GiB + avail 3729.1 GiB -> 2.3 % (dims are GiB, not %)
    assert NN.volume_percent(_fx("data_data_volume.json")) == 2.3


def test_volume_percent_zero_total_is_none():
    assert NN.volume_percent({"labels": ["time", "avail", "used"], "data": [[0, 0, 0]]}) is None
    assert NN.volume_percent({}) is None


def test_ram_used_percent_counts_active_wired_compressor():
    # real macOS dims (MiB) from the Mac Studio: counting inactive/purgeable cache as used would
    # report 96 %, so "in use" is active + wired + compressor -> 36447/65673 = 55.5 %
    doc = {"labels": ["time", "active", "wired", "throttled", "compressor", "inactive",
                      "purgeable", "speculative", "free"],
           "data": [[0, 25512.0, 5596.0, 0, 5339.0, 24817.0, 947.0, 1075.0, 2387.0]]}
    assert NN.ram_used_percent(doc) == 55.5


def test_ram_used_percent_falls_back_to_not_free_without_macos_dims():
    doc = {"labels": ["time", "used", "free"], "data": [[0, 30000.0, 2387.0]]}
    assert NN.ram_used_percent(doc) == 92.6


def test_dimension_value_reads_named_dimension():
    assert NN.dimension_value({"labels": ["time", "uptime"], "data": [[0, 429624.5]]},
                              "uptime") == 429624.5
    assert NN.dimension_value({}, "uptime") is None


def test_chart_mount_extracts_the_mount_path():
    assert NN.chart_mount("disk_space./Volumes/Data") == "/Volumes/Data"
    assert NN.chart_mount("system.ram") is None


# --------------------------------------------------------------- node block
def test_build_node_matches_the_collector_contract():
    host = NN.parse_info(_fx("info.json"))
    node = NN.build_node(MAC_NODE, host, [], {"/": 34.0, "/Volumes/Data": 2.3},
                         ram_used_percent=93.9, uptime_s=429624.5)
    assert CONTRACT_KEYS <= set(node)
    assert node["online"] is True
    assert node["type"] == "macos"
    assert node["confidence"] is None                     # no reasoner on the Mac: full credit
    assert node["containers"] == {"running": 0, "total": 0}
    assert node["anomalies"] == 0 and node["alerts_count"] == 0
    assert node["disk_used_percent"] == 34.0              # worst WATCHED volume
    assert node["volumes"] == {"/": 34.0, "/Volumes/Data": 2.3}
    assert node["host"]["hostname"] == "Mac-Studio.local"


def test_build_node_carries_alarm_names_for_cross_node_correlation():
    alarms = [{"name": "ram_usage", "status": "WARNING", "mount": None, "value": 91.0}]
    node = NN.build_node(MAC_NODE, {}, alarms, {})
    assert node["anomalies"] == 1
    names = [a["name"] for a in node["raw"]["collector"]["netdata"]["alarms_active"]]
    assert names == ["ram_usage"]


def test_build_node_with_no_readable_volume_has_null_disk():
    node = NN.build_node(MAC_NODE, {}, [], {"/": None})
    # the mount is kept with a null reading (we know it exists, we could not read it), and it
    # never wins the "worst volume" max
    assert node["disk_used_percent"] is None and node["volumes"] == {"/": None}


# --------------------------------------------------------------- collect (network, mocked)
def _fake_urlopen(payloads, missing=()):
    class FakeResp:
        def __init__(self, data):
            self._d = data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self, *a):
            return json.dumps(self._d).encode()

    def urlopen(url, timeout=None):
        url = str(url)
        for frag in missing:
            if frag in url:
                raise OSError(f"chart not collected: {url}")
        # longest fragment first: "chart=disk_space.%2F" is a prefix of the Data volume URL
        for frag in sorted(payloads, key=len, reverse=True):
            if frag in url:
                return FakeResp(payloads[frag])
        raise OSError(f"no fixture for {url}")
    return urlopen


def test_collect_netdata_node_end_to_end(monkeypatch):
    payloads = {
        "api/v1/info": _fx("info.json"),
        "api/v1/alarms": _fx("alarms.json"),
        "chart=disk_space.%2F": {"labels": ["time", "avail", "used"], "data": [[0, 611.0, 315.3]]},
        "chart=disk_space.%2FVolumes%2FData": _fx("data_data_volume.json"),
        "chart=system.uptime": {"labels": ["time", "uptime"], "data": [[0, 429624.5]]},
        "chart=system.ram": {"labels": ["time", "active", "wired", "compressor", "inactive", "free"],
                             "data": [[0, 25512.0, 5596.0, 5339.0, 24817.0, 2387.0]]},
    }
    monkeypatch.setattr(NN.urllib.request, "urlopen", _fake_urlopen(payloads))
    node = NN.collect_netdata_node(MAC_NODE, timeout=2)
    assert node["online"] is True and "err" not in node
    assert node["anomalies"] == 0                                   # DMG CRITICAL skipped
    assert node["volumes"] == {"/": 34.0, "/Volumes/Data": 2.3}
    assert node["disk_used_percent"] == 34.0
    assert node["host"]["hostname"] == "Mac-Studio.local"
    assert node["uptime_s"] == 429624.5
    assert node["host"]["ram_used_percent"] == 57.3                 # (25512+5596+5339)/63651


def test_collect_netdata_node_skips_optional_charts(monkeypatch):
    # a volume whose chart is not collected, plus uptime/ram absent (differing Netdata builds):
    # the node must still come back online with nulls, never offline
    payloads = {"api/v1/info": _fx("info.json"), "api/v1/alarms": {"alarms": {}},
                "chart=disk_space.%2F": {"labels": ["time", "avail", "used"], "data": [[0, 611.0, 315.3]]}}
    monkeypatch.setattr(NN.urllib.request, "urlopen",
                        _fake_urlopen(payloads,
                                      missing=("%2FVolumes%2FData", "system.uptime", "system.ram")))
    node = NN.collect_netdata_node(MAC_NODE, timeout=2)
    assert node["online"] is True
    assert node["uptime_s"] is None and node["host"]["ram_used_percent"] is None
    assert node["volumes"]["/Volumes/Data"] is None                  # missing volume -> None, not fatal
    assert node["volumes"]["/"] == 34.0
    assert node["disk_used_percent"] == 34.0                         # None never wins the max


def test_collect_netdata_node_offline_on_transport_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(NN.urllib.request, "urlopen", boom)
    node = NN.collect_netdata_node(MAC_NODE, timeout=2)
    assert node["online"] is False and "connection refused" in node["err"]


def test_collect_netdata_node_offline_without_endpoint():
    node = NN.collect_netdata_node({"name": "x", "type": "macos"}, timeout=2)
    assert node["online"] is False and "collector_endpoint" in node["err"]
