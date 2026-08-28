"""
Ops Brain - unit tests for decision-critical pure logic.

Covers the exact code that decides whether to `docker restart` / `prune` / `kill` /
notify, WITHOUT touching a live docker daemon, GPU, or Ollama:
  - actions.allow_container / allow_service   (the whitelist gate)
  - actions.pct                               (percent parser)
  - actions.deterministic_rules               (CPU sustain, mem creep, GPU, restart loop, disk)
  - actions._merge_warnings                   (dedupe)
  - reasoner.sanitize / extract_json          (Qwen -> structured decisions, verb filtering)
  - reasoner.summarize_collector / pct        (digest trimming)
  - common.Cfg.resolve / read_json/write_json (path + persistence helpers)

Run:  python3 -m pytest -q   (from /appdata/OpsBrain)
"""
import json
import os
import sys
import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("OPSBRAIN_REPO", str(REPO))

from common import Cfg, write_json, read_json          # noqa: E402
import hermes_actions.actions as A                     # noqa: E402
import reasoner.reasoner as R                          # noqa: E402
import collector.collector as C                        # noqa: E402


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def base_config(tmp_path, monkeypatch):
    """Point every module at a minimal, reproducible config."""
    cfg = {
        "hostname": "testvm",
        "interval_seconds": 120,
        "actions": {
            "cpu_restart_threshold_percent": 80.0,
            "cpu_restart_minutes": 5,
            "mem_creep_threshold_percent": 20.0,
            "gpu_mem_threshold_percent": 90.0,
            "disk_threshold_percent": 85.0,
            "qwen_confidence_floor": 0.6,
            "restart_limit_per_run": 3,
            "allow_restart_containers": ["homepage", "dozzle", "netdata"],
            "allow_gpu_kill": True,
            "allow_prune": True,
        },
        "gpu_drift": {
            "enabled": True,
            "vram_creep_mb": 250,
            "vram_max_percent": 90,
            "stuck_pid_cycles": 5,
            "util_threshold": 10,
            "power_idle_max_watts": 40,
            "temp_idle_max_c": 55,
        },
        "paths": {
            "collector_json": str(tmp_path / "collector.json"),
            "gpu_baseline": str(tmp_path / "gpu_baseline.json"),
            "gpu_drift_events": str(tmp_path / "gpu_drift_events.jsonl"),
            "gpu_daily_stats": str(tmp_path / "gpu_daily_stats.json"),
        },
    }
    monkeypatch.setattr(Cfg, "data", cfg)
    # keep notifications out of the real repo during drift tests
    monkeypatch.setattr(A, "notify", lambda *a, **k: None)
    # stub subprocess execution so unit tests never touch docker/kill/systemctl
    monkeypatch.setattr(A, "run", lambda *a, **k: {"ok": True, "rc": 0, "out": "ok", "err": ""})
    return cfg


def make_container(name, state="running", restarting=False, restart_count=0, stats=None):
    return {"name": name, "state": state, "restarting": restarting,
            "restart_count": restart_count, "stats": stats or {}}


def rules_coll(containers, netdata=None, gpu=None, vm=None, disk=None):
    coll = {
        "docker": {"containers": containers,
                   "restarting": [c["name"] for c in containers if c.get("restarting")]},
        "netdata": {"alarms_active": netdata or []},
        "gpu": gpu or {"gpus": [], "compute_processes": []},
        "vm": {"disk_used_percent": disk},
    }
    if vm:
        coll["vm"].update(vm)
    return coll


# --------------------------------------------------------------------------- whitelist gate
class TestAllowContainer:
    def test_whitelisted(self, base_config):
        assert A.allow_container("homepage") is True
        assert A.allow_container("dozzle") is True

    def test_non_whitelisted_blocked(self, base_config):
        assert A.allow_container("plex") is False
        assert A.allow_container("jellyfin") is False
        assert A.allow_container("ollama") is False

    def test_case_insensitive(self, base_config):
        # Docker may report "Firefox"; whitelist has "firefox"
        assert A.allow_container("Firefox") is False   # not in THIS cfg whitelist
        # add uppercase variant to whitelist
        base_config["actions"]["allow_restart_containers"].append("Firefox")
        assert A.allow_container("firefox") is True
        assert A.allow_container("FIREFOX") is True

    def test_empty_whitelist_means_allow_all(self, base_config):
        base_config["actions"]["allow_restart_containers"] = []
        assert A.allow_container("anything") is True

    def test_allow_service_requires_exact(self, base_config):
        base_config["actions"]["allow_service_restart"] = ["opsbrain"]
        assert A.allow_service("opsbrain") is True
        assert A.allow_service("other") is False


# --------------------------------------------------------------------------- pct
class TestPct:
    def test_parses_percent_strings(self):
        assert A.pct("2.31%") == 2.31
        assert A.pct("90%") == 90.0
        assert A.pct("0.00%") == 0.0

    def test_none_for_garbage(self):
        assert A.pct("N/A") is None
        assert A.pct("abc") is None
        assert A.pct(None) is None


# --------------------------------------------------------------------------- deterministic rules
class TestDeterministicRules:
    def test_clean_system_no_actions(self, base_config):
        coll = rules_coll([make_container("homepage", stats={"cpu_percent": "1.0%", "mem_percent": "10%"})])
        rules, warns = A.deterministic_rules(coll, {}, {})
        assert rules == []
        assert warns == []

    def test_cpu_sustain_requires_window(self, base_config):
        # one observation doesn't trip the 5-min sustain rule
        coll = rules_coll([make_container("web", stats={"cpu_percent": "95%"})])
        st = {}
        rules, _ = A.deterministic_rules(coll, st, {})
        assert rules == []
        assert "cpu_high:web" in st  # counter accumulates

    def test_cpu_sustain_trips_after_window(self, base_config):
        # pre-seed counter just below the 5*60 threshold
        coll = rules_coll([make_container("web", stats={"cpu_percent": "95%"})])
        st = {"cpu_high:web": 5 * 60 - 1}
        rules, _ = A.deterministic_rules(coll, st, {})
        assert any(r["type"] == "docker_restart" and r["target"] == "web" for r in rules)
        # counter reset after firing
        assert "cpu_high:web" not in st

    def test_memory_creep_over_baseline(self, base_config):
        baseline = {"web": {"mem_percent": 30.0}}
        coll = rules_coll([make_container("web", stats={"mem_percent": "40%"})])
        rules, _ = A.deterministic_rules(coll, {}, baseline)
        # 33% creep > 20% threshold
        assert any(r["type"] == "docker_restart" and r["target"] == "web" for r in rules)

    def test_memory_within_baseline_no_action(self, base_config):
        baseline = {"web": {"mem_percent": 40.0}}
        coll = rules_coll([make_container("web", stats={"mem_percent": "45%"})])
        rules, _ = A.deterministic_rules(coll, {}, baseline)
        # 12.5% creep < 20%
        assert not any(r["type"] == "docker_restart" for r in rules)

    def test_restart_loop(self, base_config):
        coll = rules_coll([make_container("cron", restarting=True),
                           make_container("ok", restarting=False)])
        rules, warns = A.deterministic_rules(coll, {}, {})
        assert any(r["type"] == "docker_restart" and r["target"] == "cron" for r in rules)
        assert any("cron" in w and "restart loop" in w for w in warns)

    def test_gpu_high_mem_flagged(self, base_config):
        gpu = {"gpus": [{"name": "GPU0", "mem_used_mb": "9500", "mem_total_mb": "10000"}],
               "compute_processes": [{"pid": "1234", "name": "foo"}]}
        coll = rules_coll([], gpu=gpu)
        rules, warns = A.deterministic_rules(coll, {}, {})
        # 95% > 90% -> gpu_kill proposed (gating happens at Engine.dispatcher)
        assert any(r["type"] == "gpu_kill" and r["target"] == "1234" for r in rules)

    def test_gpu_under_threshold_no_action(self, base_config):
        gpu = {"gpus": [{"name": "GPU0", "mem_used_mb": "5000", "mem_total_mb": "10000"}],
               "compute_processes": []}
        coll = rules_coll([], gpu=gpu)
        rules, _ = A.deterministic_rules(coll, {}, {})
        assert not any(r["type"] == "gpu_kill" for r in rules)

    def test_disk_over_threshold_prune(self, base_config):
        coll = rules_coll([], disk="90")
        rules, warns = A.deterministic_rules(coll, {}, {})
        assert any(r["type"] == "docker_prune" for r in rules)
        assert any("disk" in w for w in warns)

    def test_disk_under_threshold_no_prune(self, base_config):
        coll = rules_coll([], disk="30")
        rules, _ = A.deterministic_rules(coll, {}, {})
        assert not any(r["type"] == "docker_prune" for r in rules)

    def test_netdata_alarms_become_warnings(self, base_config):
        netdata = [{"status": "CRITICAL", "name": "disk_fill_rate", "component": "disk"}]
        coll = rules_coll([], netdata=netdata)
        rules, warns = A.deterministic_rules(coll, {}, {})
        assert any("CRITICAL" in w and "disk" in w for w in warns)
        # alarms do NOT directly cause actions
        assert not any(r["type"] == "docker_restart" for r in rules)


# --------------------------------------------------------------------------- warning merge
class TestMergeWarnings:
    def test_dedupes_qwen_and_rule_warnings(self):
        m = A._merge_warnings(["a", "b"], ["b", "c"])
        assert m == ["a", "b", "c"]

    def test_empty(self):
        assert A._merge_warnings([], []) == []


# --------------------------------------------------------------------------- reasoner
class TestSanitize:
    def test_accepts_type_key(self):
        obj = {"warnings": ["w"], "actions": [{"type": "docker_restart", "target": "x", "reason": "r"}],
               "summary": "s", "confidence": 0.8}
        out = R.sanitize(obj)
        assert out["actions"][0]["type"] == "docker_restart"
        assert out["confidence"] == 0.8

    def test_accepts_action_key_alias(self):
        # qwen sometimes emits "action" instead of "type"
        obj = {"actions": [{"action": "docker_prune", "target": "x"}], "confidence": 0.9}
        out = R.sanitize(obj)
        assert out["actions"][0]["type"] == "docker_prune"

    def test_drops_unknown_verb(self):
        obj = {"actions": [{"type": "rm_rf", "target": "/"}], "confidence": 0.9}
        out = R.sanitize(obj)
        assert out["actions"] == []

    def test_clamps_confidence(self):
        assert R.sanitize({"confidence": 1.7})["confidence"] == 1.0
        assert R.sanitize({"confidence": -0.2})["confidence"] == 0.0

    def test_warnings_defaults_to_list(self):
        out = R.sanitize({"warnings": "oops", "actions": None, "summary": None, "confidence": None})
        assert out["warnings"] == []
        assert out["actions"] == []
        # null confidence must not crash (see fix in sanitize) and flattens to 0.0
        assert out["confidence"] == 0.0

    def test_sanitize_tolerates_null_confidence(self):
        out = R.sanitize({"confidence": None, "actions": []})
        assert out["confidence"] == 0.0

    def test_sanitize_tolerates_non_dict(self):
        out = R.sanitize(None)
        assert out == {"warnings": [], "summary": "", "confidence": 0.0,
                       "gpu_drift": [], "manual_stops": [], "actions": []}

    def test_sanitize_preserves_manual_stops_list(self):
        out = R.sanitize({"manual_stops": ["web", "db", ""], "confidence": 0.9})
        assert out["manual_stops"] == ["web", "db"]  # empties dropped

    def test_sanitize_preserves_valid_gpu_drift(self):
        out = R.sanitize({"gpu_drift": ["vram_drift", "stuck_process", "bogus"], "confidence": 0.9})
        assert out["gpu_drift"] == ["vram_drift", "stuck_process"]  # unknown flag dropped

    def test_sanitize_defaults_gpu_drift_empty(self):
        out = R.sanitize({"confidence": 0.5})
        assert out["gpu_drift"] == []


class TestExtractJson:
    def test_clean_json(self):
        assert R.extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        raw = "```json\n{\"warnings\": []}\n```"
        assert R.extract_json(raw) == {"warnings": []}

    def test_strips_surrounding_text(self):
        raw = "Here is my analysis...\n{\"actions\": [{\"type\":\"notify\"}]}\nHope that helps."
        out = R.extract_json(raw)
        assert out["actions"][0]["type"] == "notify"

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError):
            R.extract_json("no json here")

    def test_raises_on_invalid(self):
        with pytest.raises(ValueError):
            R.extract_json("{\"broken\": ")


class TestSummarizeCollector:
    def test_flags_anomalous_containers_only(self):
        c = {
            "host": "h", "timestamp": "t",
            "netdata": {"up": True, "alarms_active": [], "alarms_count": 0},
            "dozzle": {"up": True}, "dockpeek": {"up": True, "container_running": True},
            "docker": {"containers_count": 3, "running": 2, "restarting": [],
                       "containers": [
                           make_container("good", state="running", stats={"cpu_percent": "1%", "mem_percent": "5%"}),
                           make_container("loop", state="exited", restart_count=4),
                           make_container("hot", state="running", stats={"cpu_percent": "95%", "mem_percent": "99%"}),
                       ]},
            "gpu": {"gpus": [], "compute_processes": []},
            "vm": {"disk_used_percent": "27", "uptime_load": {}, "top_by_mem_top5": [], "syslog_error_count_2min": 0},
        }
        out = R.summarize_collector(c)
        flagged = {f["name"] for f in out["docker"]["flagged_containers"]}
        assert "loop" in flagged       # exited, restart_count 4
        assert "good" not in flagged   # healthy
        over = {f["name"] for f in out["docker"]["over_dev_containers"]}
        assert "hot" in over           # cpu 95% / mem 99% over thresholds

    def test_pct_helper(self):
        from reasoner.reasoner import pct
        assert pct("80%") == 80.0
        assert pct("n/a") is None


# --------------------------------------------------------------------------- common
class TestCommon:
    def test_cfg_resolve_relative_and_absolute(self, tmp_path, base_config):
        base_config["paths"]["relative"] = "logs/x.json"
        base_config["paths"]["absolute"] = str(tmp_path / "abs.json")
        rel = Cfg.resolve("paths.relative")
        assert rel.name == "x.json"
        assert str(rel).endswith("OpsBrain/logs/x.json")
        abs_p = Cfg.resolve("paths.absolute")
        assert str(abs_p) == str(tmp_path / "abs.json")

    def test_write_read_json_roundtrip(self, tmp_path, base_config):
        p = tmp_path / "sub" / "doc.json"
        write_json(p, {"k": [1, 2, 3]})
        assert read_json(p) == {"k": [1, 2, 3]}
        assert read_json(tmp_path / "missing.json", "fallback") == "fallback"


# --------------------------------------------------------------------------- GPU drift (collector)
def _gpu(mem_used=500, total=16303, util=0, temp=40, power=20):
    return [{"index": 0, "name": "GPU", "mem_used_mb": mem_used, "mem_total_mb": total,
             "util_gpu_percent": util, "temp_c": temp, "power_w": power}]


def _procs(pid="123"):
    return [{"pid": str(pid), "name": "proc", "mem_mb": "100"}] if pid else []


def _drift_cfg(**over):
    base = {"vram_creep_mb": 250, "vram_max_percent": 90, "stuck_pid_cycles": 5,
            "util_threshold": 10, "power_idle_max_watts": 40, "temp_idle_max_c": 55}
    base.update(over)
    return base


class TestGpuDriftCollector:
    def test_no_drift_healthy(self, base_config):
        g = _gpu(mem_used=500, util=5, temp=40, power=15)
        flags, newb = C.evaluate_gpu_drift(g, _procs(), {}, {"gpu_drift": _drift_cfg()})
        assert flags == []
        assert newb["last_vram"] == 500

    def test_vram_drift_when_creep_exceeds(self, base_config):
        prev = {"last_vram": 500, "last_pid": "123", "last_power": 15, "last_temp": 40, "cycles_with_same_pid": 1}
        g = _gpu(mem_used=900)  # +400 > 250 creep
        flags, _ = C.evaluate_gpu_drift(g, _procs(), prev, {"gpu_drift": _drift_cfg()})
        assert "vram_drift" in flags

    def test_no_vram_drift_within_limit(self, base_config):
        prev = {"last_vram": 500, "last_pid": "123", "last_power": 15, "last_temp": 40, "cycles_with_same_pid": 1}
        g = _gpu(mem_used=600)  # +100 < 250
        flags, _ = C.evaluate_gpu_drift(g, _procs(), prev, {"gpu_drift": _drift_cfg()})
        assert "vram_drift" not in flags

    def test_vram_overload_over_90pct(self, base_config):
        g = _gpu(mem_used=16000, total=16303)  # 98%
        flags, _ = C.evaluate_gpu_drift(g, _procs(), {}, {"gpu_drift": _drift_cfg()})
        assert "vram_overload" in flags

    def test_stuck_process_after_cycles_and_busy(self, base_config):
        prev = {"last_vram": 500, "last_pid": "123", "last_power": 15, "last_temp": 40, "cycles_with_same_pid": 4}
        g = _gpu(util=90)
        flags, newb = C.evaluate_gpu_drift(g, _procs("123"), prev, {"gpu_drift": _drift_cfg()})
        assert "stuck_process" in flags
        assert newb["cycles_with_same_pid"] == 5

    def test_stuck_process_gate_idle_util(self, base_config):
        # same pid many cycles but util idle (2 < 10) -> NOT stuck
        prev = {"last_vram": 500, "last_pid": "123", "last_power": 15, "last_temp": 40, "cycles_with_same_pid": 10}
        g = _gpu(util=2)
        flags, _ = C.evaluate_gpu_drift(g, _procs("123"), prev, {"gpu_drift": _drift_cfg()})
        assert "stuck_process" not in flags

    def test_ollama_llama_server_never_stuck(self, base_config):
        # ollama's llama-server is a PERMANENT GPU resident: same pid forever with
        # busy util — must NEVER be flagged stuck_process or counted in cycles.
        procs = [{"pid": "999", "name": "/usr/lib/ollama/llama-server", "mem_mb": "14000"}]
        prev = {"last_vram": 14000, "last_pid": "999", "last_power": 20, "last_temp": 40, "cycles_with_same_pid": 20}
        g = _gpu(mem_used=14000, util=90)
        flags, newb = C.evaluate_gpu_drift(g, procs, prev, {"gpu_drift": _drift_cfg()})
        assert "stuck_process" not in flags
        assert newb["cycles_with_same_pid"] == 0   # ollama pid never counted
        assert newb["last_pid"] == "0"             # nothing trackable on the GPU

    def test_stuck_tracks_real_pid_alongside_ollama(self, base_config):
        # a genuine stuck job sitting next to ollama is still tracked + flagged
        procs = [
            {"pid": "999", "name": "/usr/lib/ollama/llama-server", "mem_mb": "14000"},
            {"pid": "777", "name": "ffmpeg", "mem_mb": "300"},
        ]
        prev = {"last_vram": 14000, "last_pid": "777", "last_power": 20, "last_temp": 40, "cycles_with_same_pid": 4}
        g = _gpu(mem_used=14100, util=90)
        flags, newb = C.evaluate_gpu_drift(g, procs, prev, {"gpu_drift": _drift_cfg()})
        assert "stuck_process" in flags
        assert newb["last_pid"] == "777"
        assert newb["cycles_with_same_pid"] == 5

    def test_power_drift_when_idle_but_high_power(self, base_config):
        g = _gpu(util=2, power=150)  # idle util, high draw
        flags, _ = C.evaluate_gpu_drift(g, _procs(), {}, {"gpu_drift": _drift_cfg()})
        assert "power_drift" in flags

    def test_temp_drift_when_idle_but_hot(self, base_config):
        g = _gpu(util=2, temp=80)  # idle util, hot
        flags, _ = C.evaluate_gpu_drift(g, _procs(), {}, {"gpu_drift": _drift_cfg()})
        assert "temp_drift" in flags


# --------------------------------------------------------------------------- GPU drift (actions)
def _drift_coll(flags=None, gpu=None, procs=None):
    return {"gpu": {"drift_flags": flags or [], "gpus": gpu or _gpu(), "compute_processes": procs or _procs()},
            "timestamp": "t"}


class TestGpuDriftActions:
    def test_no_flags_noop(self, base_config):
        e = A.Engine(True)
        events, rem = A.gpu_drift_actions(_drift_coll(), {"gpu_drift": []}, 0.9, e, {})
        assert events == [] and rem == []

    def test_stuck_highconf_dispatches_kill(self, base_config):
        e = A.Engine(False)  # not dry-run
        qwen = {"gpu_drift": ["stuck_process"]}
        coll = _drift_coll(flags=["stuck_process"])
        coll["gpu"]["baseline"] = {"last_pid": "123"}  # tracked stuck pid == procs[0].pid
        events, rem = A.gpu_drift_actions(coll, qwen, 0.9, e, {})
        kill = [r for r in rem if isinstance(r, dict) and r.get("verb") == "gpu_kill"]
        assert kill and kill[0].get("target") == "123"
        assert e.executed  # gpu_kill executed (allow_gpu_kill true, live, run() stubbed)

    def test_stuck_never_kills_ollama(self, base_config):
        # collector shows ollama's llama-server as the tracked process -> must NOT kill
        from hermes_actions.actions import _killable_stuck_pid
        procs = [{"pid": "999", "name": "/usr/lib/ollama/llama-server", "mem_mb": "14000"}]
        assert _killable_stuck_pid(procs, "999") is None
        # even with high confidence + live engine, no gpu_kill is dispatched
        e = A.Engine(False)
        coll = _drift_coll(flags=["stuck_process"], gpu=_gpu(), procs=procs)
        # baseline.last_pid must equal the ollama pid so it's the tracked stuck pid
        coll["gpu"]["baseline"] = {"last_pid": "999"}
        events, rem = A.gpu_drift_actions(coll, {"gpu_drift": ["stuck_process"]}, 0.95, e, {})
        assert not any(r.get("verb") == "gpu_kill" for r in e.executed)
        assert not any(isinstance(r, dict) and r.get("verb") == "gpu_kill" for r in rem)

    def test_stuck_ollama_notify_spam_suppressed(self, base_config):
        # a stale collector that still lists stuck_process for ollama's llama-server
        # must NOT notify, must NOT record a drift event, must NOT accumulate its
        # pid in gpu_stuck_pids state (this was the constant false-positive loop).
        procs = [{"pid": "999", "name": "/usr/lib/ollama/llama-server", "mem_mb": "14000"}]
        coll = _drift_coll(flags=["stuck_process"], gpu=_gpu(), procs=procs)
        # post-fix collector baseline: ollama excluded -> last_pid "0"
        coll["gpu"]["baseline"] = {"last_pid": "0"}
        st = {"gpu_stuck_pids": ["976644"]}
        e = A.Engine(False)
        events, rem = A.gpu_drift_actions(coll, {"gpu_drift": ["stuck_process"]}, 0.95, e, {})
        assert events == []                      # flag dropped, nothing logged
        assert rem == []                         # no notify-only remediation either
        assert st["gpu_stuck_pids"] == ["976644"]  # stale pid NOT appended/replaced
        # and the tracked-pid path (last_pid still naming the llama-server) is
        # suppressed too, instead of falling into the notify-only branch
        coll2 = _drift_coll(flags=["stuck_process"], gpu=_gpu(), procs=procs)
        coll2["gpu"]["baseline"] = {"last_pid": "999"}
        events2, rem2 = A.gpu_drift_actions(coll2, {"gpu_drift": ["stuck_process"]}, 0.95, e, {})
        assert events2 == [] and rem2 == []

    def test_stuck_kill_requires_baseline_match(self, base_config):
        # baseline.last_pid differs from the only current process -> no kill
        from hermes_actions.actions import _killable_stuck_pid
        procs = [{"pid": "999", "name": "some-app", "mem_mb": "10"}]
        assert _killable_stuck_pid(procs, "111") is None  # tracked pid gone
        e = A.Engine(False)
        coll = _drift_coll(flags=["stuck_process"], gpu=_gpu(), procs=procs)
        coll["gpu"]["baseline"] = {"last_pid": "111"}
        events, rem = A.gpu_drift_actions(coll, {"gpu_drift": ["stuck_process"]}, 0.95, e, {})
        assert not any(r.get("verb") == "gpu_kill" for r in e.executed)

    def test_stuck_no_none_pid_string(self, base_config):
        # procs entry without a pid key must not yield the string "None"
        procs = [{"name": "x", "mem_mb": "5"}]
        e = A.Engine(False)
        coll = _drift_coll(flags=["stuck_process"], gpu=_gpu(), procs=procs)
        events, rem = A.gpu_drift_actions(coll, {"gpu_drift": ["stuck_process"]}, 0.95, e, {})
        assert not any(r.get("verb") == "gpu_kill" for r in e.executed)

    def test_stuck_lowconf_notify_only(self, base_config):
        base_config["actions"]["qwen_confidence_floor"] = 0.6
        e = A.Engine(False)
        qwen = {"gpu_drift": ["stuck_process"], "confidence": 0.7}
        events, rem = A.gpu_drift_actions(_drift_coll(), qwen, 0.7, e, {})
        # conf 0.7 <= 0.8 -> NO kill dispatched
        assert not any(isinstance(r, dict) and r.get("verb") == "gpu_kill" for r in rem)
        assert not any(r.get("verb") == "gpu_kill" for r in e.executed)

    def test_vram_overload_notify_only(self, base_config):
        e = A.Engine(False)
        qwen = {"gpu_drift": ["vram_overload"]}
        events, rem = A.gpu_drift_actions(_drift_coll(), qwen, 0.3, e, {})
        assert not any(r.get("verb") == "gpu_kill" for r in e.executed)
        # pure overload must not attempt a kill, only notify
        assert not any(isinstance(r, dict) and r.get("verb") == "gpu_kill" for r in rem)

    def test_flag_union_of_qwen_and_deterministic(self, base_config):
        # qwen silent, collector deterministic says vram_drift -> handled
        e = A.Engine(False)
        coll = _drift_coll(flags=["vram_drift"])
        events, rem = A.gpu_drift_actions(coll, {"gpu_drift": []}, 0.9, e, {})
        assert events and "vram_drift" in events[0]["flags"]


class TestOllamaGuard:
    def test_never_restarts_ollama(self, base_config):
        base_config["actions"]["allow_restart_containers"] = ["ollama"]  # even if allow-listed
        e = A.Engine(False)
        rec = e.dispatch("docker_restart", "ollama", "x")
        assert rec["state"] == "blocked"
        assert not e.executed

    def test_allowlisted_other_container_ok(self, base_config):
        e = A.Engine(False)
        rec = e.dispatch("docker_restart", "homepage", "x")
        assert rec["state"] in ("executed",)  # dry_run False, allow-listed