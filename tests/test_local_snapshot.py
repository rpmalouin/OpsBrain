"""Ops Brain - tests for the node snapshot ingest (the producer half, and for the Mac the only half).

Fixture-driven: `tests/fixtures/macstudio/macstudio_node.json` is a real snapshot written by the Mac's
15-minute producer (2026-10-10 capture, schema 1, no `soc` block — the macmon half is captured in
`macmon_pipe.json` once the Mac sends its literal sample). The `soc` payloads used below are small
inline dicts, deliberately built to macmon's documented shape rather than copied from a live read: a
test fixture must never pass itself off as a measurement. No test touches the network or the service.
"""
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "common"))

from federation import local_snapshot as LS  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "macstudio"

GIB = 1024 ** 3


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


def _macmon(memory=None, fans=None, available=True, error=None, **metric_over):
    """One macmon sample shaped as the producer will write it (macmon's own field names)."""
    metrics = {
        "temp": {"cpu_temp_avg": 43.7, "gpu_temp_avg": 36.9},
        "memory": memory if memory is not None else {
            "ram_total": 64 * GIB, "ram_usage": 56 * GIB, "ram_available": 10 * GIB,
            "swap_total": 0, "swap_usage": 0},
        "fans": [{"name": "fan0", "rpm": 1180, "max_rpm": 4900}] if fans is None else fans,
        "cpu_scaled_ratio": 0.05, "cpu_active_ratio": 0.07,
        "cpu_tiers": [
            {"label": "P", "freq_mhz": 1974, "scaled_ratio": 0.015, "active_ratio": 0.03, "cores": []},
            {"label": "S", "freq_mhz": 1181, "scaled_ratio": 0.083, "active_ratio": 0.09, "cores": []},
        ],
        "gpu_freq_mhz": 461, "gpu_scaled_ratio": 0.021, "gpu_active_ratio": 0.03,
        "cpu_power": 1.83, "gpu_power": 0.42, "ane_power": 0.0, "all_power": 2.25,
        "sys_power": 6.10, "ram_power": 0.12, "gpu_ram_power": 0.0,
    }
    metrics.update(metric_over)
    return {"sampled_at": "2026-10-12T23:00:00-0400",
            "source": "macmon pipe --soc-info -s 1 -i 1000",
            "available": available, "error": error,
            "metrics": metrics,
            "soc_info": {"mac_model": "Mac17,14", "chip_name": "Apple M5 Max", "memory_gb": 64,
                         "cpu_tiers": [{"label": "P", "cores": 12, "freqs": []},
                                       {"label": "S", "cores": 6, "freqs": []}]}}


def _producer_v2(soc=None, **over):
    """A schema-2 producer payload: the Mac's file with the macmon block instead of Netdata's."""
    data = {"schema": 2, "node": "macstudio", "generated_at_utc": "2026-10-12T23:00:00Z",
            "interval_minutes": 15, "alert_count": 0, "alerts": [],
            "host": {"model": "Mac17,14", "os": "27.0.1", "cpu_cores": 18, "uptime_hours": 16.7},
            "disks": [{"path": "/", "total_gib": 926.3, "used_gib": 317.2, "free_gib": 609.1,
                       "used_percent": 34.2},
                      {"path": "/Volumes/Data2", "total_gib": 3725.8, "used_gib": 3090.0,
                       "free_gib": 635.8, "used_percent": 82.9}],
            "mounts": [{"path": "/Volumes/Data", "mounted": True, "read_only": False}],
            "gateway": {"present": True, "age_seconds": 12.0},
            "hermes_cron": {"jobs_total": 21, "jobs_enabled": 21, "jobs_paused": 0,
                            "jobs_failing": [], "oldest_last_run_hours": 264.78}}
    if soc is not None:
        data["soc"] = soc
    data.update(over)
    return data


def _shell(node_type="macos"):
    """What `federation_collector._empty_node` hands the merge for a `kind: snapshot` node."""
    return {"online": False, "type": node_type, "confidence": None, "drift_events": 0,
            "anomalies": 0, "restart_events": 0, "disk_used_percent": None,
            "containers": {"running": 0, "total": 0}, "alerts_count": 0}


def _primary(tmp_path, payload, age_s=0.0, **cfg):
    snap = LS.read_snapshot(_write(tmp_path, payload, age_s=age_s),
                            max_age_s=cfg.pop("max_age_s", LS.DEFAULT_MAX_AGE_S),
                            dead_after_s=cfg.pop("dead_after_s", LS.DEFAULT_DEAD_AFTER_S))
    return LS.merge_snapshot(_shell(), snap, source="logs/macstudio_node.json", primary=True, **cfg)


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


# --------------------------------------------------------------- the two-tier staleness rule
def test_dead_after_splits_stale_from_dead(tmp_path):
    """45 min of silence is a missed tick; 2 h of silence is a producer that has stopped."""
    p = _write(tmp_path, _producer(), age_s=46 * 60)
    assert LS.read_snapshot(p, max_age_s=2700, dead_after_s=7200)["state"] == "stale"
    p = _write(tmp_path, _producer(), age_s=3 * 3600)
    assert LS.read_snapshot(p, max_age_s=2700, dead_after_s=7200)["state"] == "dead"


def test_dead_after_is_ignored_without_a_primary_source(tmp_path):
    """A node with a live poll keeps the old single `stale` state: its age is informational there."""
    p = _write(tmp_path, _producer(), age_s=9 * 3600)
    assert LS.read_snapshot(p, max_age_s=2700)["state"] == "stale"


def test_dead_never_overrides_a_never_expiring_read(tmp_path):
    """max_age_s <= 0 means 'never expire' and wins, so a dead tier cannot contradict it."""
    p = _write(tmp_path, _producer(), age_s=9 * 3600)
    assert LS.read_snapshot(p, max_age_s=0, dead_after_s=7200)["state"] == "ok"


# --------------------------------------------------------------- merging (a node with a live poll)
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
    assert node["online"] is True            # the live poll still owns online/offline


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


def test_merge_snapshot_carries_the_soc_block_not_the_old_netdata_key(tmp_path):
    """The producer's macmon block arrives as `soc`; Netdata's key is retired, not reused."""
    payload = _producer_v2(soc=_macmon())
    node = _primary(tmp_path, payload)
    assert "soc" in node["snapshot"] and "netdata" not in node["snapshot"]
    assert node["snapshot"]["soc_available"] is True
    assert node["snapshot"]["soc_sampled_at"] == "2026-10-12T23:00:00-0400"


# --------------------------------------------------------------- merging (the file IS the node)
def test_primary_fresh_file_is_online(tmp_path):
    node = _primary(tmp_path, _producer_v2(soc=_macmon()))
    assert node["online"] is True
    assert node["snapshot"]["state"] == "ok"
    assert node["anomalies"] == 0 and node["alerts_count"] == 0


def test_primary_counts_the_producers_alerts(tmp_path):
    node = _primary(tmp_path, _producer_v2(soc=_macmon(), alerts=["cron: x"], alert_count=1))
    assert node["online"] is True and node["anomalies"] == 1


def test_primary_stale_file_keeps_the_node_online_with_one_anomaly(tmp_path):
    """A missed tick is not a fault: the last known facts are still the best we have."""
    node = _primary(tmp_path, _producer_v2(soc=_macmon()), age_s=46 * 60)
    assert node["online"] is True
    assert node["anomalies"] == 1
    assert node["snapshot"]["note"] == "producer snapshot is stale"


def test_primary_stale_file_is_not_re_judged(tmp_path):
    """Old facts make bad alerts: the derived checks do not run on a stale document."""
    payload = _producer_v2(soc=_macmon(memory={"ram_total": 64 * GIB, "ram_usage": 60 * GIB,
                                               "ram_available": 2 * GIB,
                                               "swap_total": 0, "swap_usage": 0}),
                           mounts=[{"path": "/Volumes/Data", "mounted": False}])
    node = _primary(tmp_path, payload, age_s=46 * 60)
    assert node["snapshot"]["derived_alerts"] == []
    assert node["anomalies"] == 1


def test_primary_dead_file_reads_offline(tmp_path):
    node = _primary(tmp_path, _producer_v2(soc=_macmon()), age_s=3 * 3600)
    assert node["online"] is False
    assert node["anomalies"] == 1
    assert node["snapshot"]["note"] == "producer snapshot is dead (no tick for over an hour)"


def test_primary_missing_file_reads_offline_with_one_anomaly(tmp_path):
    snap = LS.read_snapshot(tmp_path / "absent.json", max_age_s=2700, dead_after_s=7200)
    node = LS.merge_snapshot(_shell(), snap, source="logs/macstudio_node.json", primary=True)
    assert node["online"] is False and node["anomalies"] == 1
    assert node["snapshot"]["note"] == "producer snapshot is missing"
    assert "err" not in node                # nothing to blame on a node that never polls


def test_primary_unreadable_file_reads_offline(tmp_path):
    p = tmp_path / "node.json"
    p.write_text("{half")
    node = LS.merge_snapshot(_shell(), LS.read_snapshot(p), primary=True)
    assert node["online"] is False and node["anomalies"] == 1
    assert node["snapshot"]["state"] == "unreadable"


# --------------------------------------------------------------- display fields (disks + macmon)
def test_primary_reads_volumes_and_the_worst_disk_from_disks(tmp_path):
    node = _primary(tmp_path, _producer_v2(soc=_macmon()))
    assert node["volumes"] == {"/": 34.2, "/Volumes/Data2": 82.9}
    assert node["disk_used_percent"] == 82.9


def test_primary_renders_both_memory_figures_and_the_pressure_verdict(tmp_path):
    """ram_usage is Activity Monitor's number; only ram_available is a pressure figure."""
    node = _primary(tmp_path, _producer_v2(soc=_macmon()))
    host = node["host"]
    assert host["ram_total_gb"] == 64.0
    assert host["ram_used_percent"] == 87.5          # 56/64 GiB
    assert host["ram_available_percent"] == 15.6     # 10/64 GiB
    assert host["memory_pressure"] == "clear"


def test_primary_reads_temps_power_fans_and_cpu_tiers(tmp_path):
    node = _primary(tmp_path, _producer_v2(soc=_macmon()))
    host = node["host"]
    assert host["chip_name"] == "Apple M5 Max"
    assert host["temp"] == {"cpu_c": 43.7, "gpu_c": 36.9}
    assert host["power_w"] == {"cpu_w": 1.83, "gpu_w": 0.42, "ane_w": 0.0, "sys_w": 6.10}
    assert host["fans"] == [{"name": "fan0", "rpm": 1180.0, "max_rpm": 4900.0}]
    # per-tier aggregates, with the core counts only SocInfo knows
    assert [(t["label"], t["cores"]) for t in host["cpu"]["tiers"]] == [("P", 12), ("S", 6)]
    assert host["cpu"]["tiers"][0]["freq_mhz"] == 1974.0
    assert host["gpu"] == {"freq_mhz": 461.0, "scaled_ratio": 0.021, "active_ratio": 0.03}


def test_primary_without_a_soc_block_still_renders_the_rest(tmp_path):
    """A schema-1 producer (the Mac has not updated yet) must not break the node."""
    node = _primary(tmp_path, _producer_v2(soc=None))
    assert node["online"] is True
    assert node["volumes"] == {"/": 34.2, "/Volumes/Data2": 82.9}
    assert "temp" not in node["host"] and "power_w" not in node["host"]
    assert node["snapshot"]["alert_count"] == 0


def test_primary_surfaces_a_failed_macmon_read_instead_of_a_fake_zero(tmp_path):
    payload = _producer_v2(soc=_macmon(available=False, error="macmon: no CLPC counters"))
    node = _primary(tmp_path, payload)
    assert node["snapshot"]["soc_available"] is False
    assert node["snapshot"]["soc_error"] == "macmon: no CLPC counters"
    assert node["online"] is True            # a missing sample is not a dead node


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


def test_default_dead_after_is_eight_missed_intervals():
    assert LS.DEFAULT_DEAD_AFTER_S == 8 * 15 * 60


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


def test_derived_alerts_flags_a_read_only_share():
    data = {"mounts": [{"path": "/Volumes/Data", "mounted": True, "read_only": True},
                       {"path": "/Volumes/appdata", "mounted": True, "read_only": False}]}
    assert LS.derived_alerts(data) == ["mount is read-only: /Volumes/Data"]


def test_derived_alerts_flags_the_gateway_heartbeat():
    assert LS.derived_alerts({"gateway": {"present": False}}) == ["hermes gateway heartbeat missing"]
    stale = LS.derived_alerts({"gateway": {"present": True, "age_seconds": 900}})
    assert stale == ["hermes gateway heartbeat stale (900s)"]
    assert LS.derived_alerts({"gateway": {"present": True, "age_seconds": 15}}) == []


def test_derived_alerts_flags_an_old_time_machine_backup():
    assert LS.derived_alerts({"time_machine": {"latest_backup_age_hours": 0.7}}) == []
    assert LS.derived_alerts({"time_machine": {"latest_backup_age_hours": 100}}) == \
        ["time machine backup 100.0h old (limit 48h)"]


def test_derived_alerts_flags_memory_pressure_on_ram_available():
    warn = {"memory": {"ram_total": 64 * GIB, "ram_usage": 60 * GIB, "ram_available": 6 * GIB}}
    heavy = {"memory": {"ram_total": 64 * GIB, "ram_usage": 62 * GIB, "ram_available": 2 * GIB}}
    assert LS.derived_alerts({"soc": {"metrics": {"memory": {**_macmon()["metrics"]["memory"]}}}}) == []
    assert LS.derived_alerts({"soc": {"metrics": {"memory": warn["memory"]}}}) == \
        ["memory pressure: 9.4% of RAM available (below 10%)"]
    assert LS.derived_alerts({"soc": {"metrics": {"memory": heavy["memory"]}}}) == \
        ["memory pressure: 3.1% of RAM available (below 5%)"]


def test_ram_available_zero_is_unknown_not_an_out_of_memory_machine():
    """macmon documents ram_available as 0 when the kernel does not report it."""
    metrics = {"memory": {"ram_total": 64 * GIB, "ram_usage": 60 * GIB, "ram_available": 0}}
    assert LS.memory_pressure(metrics) is None
    assert LS.derived_alerts({"soc": {"metrics": metrics}}) == []


def test_derived_alerts_flags_a_stopped_fan():
    metrics = _macmon(fans=[{"name": "fan0", "rpm": 0, "max_rpm": 4900},
                            {"name": "fan1", "rpm": 1200, "max_rpm": 4900}])["metrics"]
    assert LS.derived_alerts({"soc": {"metrics": metrics}}) == ["fan stopped: fan0"]


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


# --------------------------------------------------------------- correlation tokens
def test_alert_keys_reduce_prose_and_our_own_checks_to_stable_tokens():
    data = {"alerts": ["time_machine: no backup visible and the last confirmed one is 52.4 h old",
                       "cron: hermes-patch-watchdog last_status=error streak=31",
                       "service: netdata is not listening on 127.0.0.1:19999"]}
    derived = ["mount is read-only: /Volumes/Data", "memory pressure: 9.4% of RAM available (below 10%)",
               "fan stopped: fan0"]
    keys = LS.alert_keys(data, derived)
    assert keys == ["time_machine", "cron", "service", "mount_read_only", "memory_pressure", "fan_stopped"]
    assert len(keys) == len(set(keys))


def test_alert_keys_dedupe_the_same_condition():
    data = {"alerts": ["cron: a", "cron: b"]}
    assert LS.alert_keys(data) == ["cron"]


def test_every_derived_alert_has_a_token():
    """The reasoner correlates on these: an unmapped derived check would silently never correlate."""
    derived = LS.derived_alerts({"mounts": [{"path": "/x", "mounted": False},
                                           {"path": "/y", "mounted": True, "read_only": True}],
                                "gateway": {"present": True, "age_seconds": 900},
                                "time_machine": {"latest_backup_age_hours": 100},
                                "soc": {"metrics": {"memory": {"ram_total": 64 * GIB,
                                                               "ram_usage": 62 * GIB,
                                                               "ram_available": 2 * GIB},
                                                    "fans": [{"name": "fan0", "rpm": 0}]}}})
    assert len(derived) == 6
    assert LS.alert_keys({}, derived) == ["mount_not_mounted", "mount_read_only", "gateway_stale",
                                          "time_machine_stale", "memory_pressure", "fan_stopped"]


def test_primary_attaches_raw_alert_keys_for_cross_node_correlation(tmp_path):
    """A snapshot-sourced node has no Netdata alarms; without `raw` the correlation reads nothing."""
    payload = _producer_v2(soc=_macmon(), alerts=["cron: hermes-patch-watchdog"], alert_count=1)
    node = _primary(tmp_path, payload)
    assert node["raw"]["collector"]["snapshot"]["alert_keys"] == ["cron"]
    assert node["raw"]["collector"]["gpu"]["drift_flags"] == []


def test_primary_offline_node_carries_no_raw(tmp_path):
    node = _primary(tmp_path, _producer_v2(soc=_macmon()), age_s=3 * 3600)
    assert node["online"] is False and "raw" not in node
