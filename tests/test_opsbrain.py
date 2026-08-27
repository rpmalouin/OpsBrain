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
            "allow_gpu_kill": False,
            "allow_prune": True,
        },
        "paths": {"collector_json": str(tmp_path / "collector.json")},
    }
    monkeypatch.setattr(Cfg, "data", cfg)
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
        assert out == {"warnings": [], "summary": "", "confidence": 0.0, "actions": []}


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