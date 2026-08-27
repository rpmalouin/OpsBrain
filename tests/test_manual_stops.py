"""Ops Brain - tests for the Manual Stop Protection (HARD INVARIANT):
"manually stopped containers must stay stopped"."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "common"))

from manual_stops import ManualStops, classify_manual_stop  # noqa: E402
from hermes_actions import actions as A  # noqa: E402
from common import Cfg  # noqa: E402


def _cfg():
    Cfg.load()
    Cfg.data["manual_stop_protection"] = {
        "enabled": True, "track_manual_stops": True, "block_restart_for_manual_stops": True}
    Cfg.data["actions"]["manual_stop_sigkill_protect"] = True


def _reg(tmp=None):
    d = tempfile.mkdtemp()
    return ManualStops(os.path.join(d, "manual_stops.json"))


@pytest.fixture(autouse=True)
def _stub_run(monkeypatch):
    """Stub subprocess execution so tests never touch docker/systemctl."""
    monkeypatch.setattr(A, "run", lambda *a, **k: {"ok": True, "rc": 0, "out": "ok", "err": ""})
    monkeypatch.setattr(A, "notify", lambda *a, **k: None)


# ------------------------------------------------- classify_manual_stop
def test_classify_manual_clean_exit():
    assert classify_manual_stop(0, False) is True       # graceful
    assert classify_manual_stop(143, False) is True     # SIGTERM docker stop
    assert classify_manual_stop(137, False) is True     # SIGKILL, not OOM -> protect


def test_classify_crash_not_manual():
    assert classify_manual_stop(137, True) is False     # OOM killed -> crash
    assert classify_manual_stop(1, False) is False      # app error
    assert classify_manual_stop(139, False) is False    # segfault
    assert classify_manual_stop(2, False) is False


def test_classify_sigkill_protect_disabled():
    assert classify_manual_stop(137, False, sigkill_protect=False) is False


# ------------------------------------------------- ManualStops persistence
def test_registry_add_and_protect():
    r = _reg()
    r.add("abc123", "worldmonitor", 0, False, "2026-01-01", "2026-08-27T00:00:00")
    assert r.is_protected("worldmonitor") is True
    assert r.is_protected("WORLDMONITOR") is True    # case-insensitive
    assert r.is_protected("abc123") is True          # by id works too
    assert r.is_protected("jellyfin") is False


def test_registry_never_overwrites_and_persists(tmp_path):
    p = tmp_path / "ms.json"
    r = ManualStops(p)
    r.add("a", "one", 0, False, "t", "2026-08-27T01:00:00")
    r.add("a", "one", 1, False, "t", "2026-08-27T02:00:00")   # second detect ignored
    rec = r.stops()["a"]
    assert rec["detected_at"] == "2026-08-27T01:00:00"  # not overwritten
    # persisted
    r2 = ManualStops(p)
    assert r2.is_protected("one") is True


def test_registry_rearm_clears():
    r = _reg()
    r.add("cid", "web", 0, False, "t", "2026-08-27T00:00:00")
    assert r.is_protected("web")
    r.rearm("cid", "web")
    assert r.is_protected("web") is False


def test_corrupt_read_fails_closed(tmp_path):
    p = tmp_path / "ms.json"
    p.write_text("{ broken json !!")
    r = ManualStops(p)
    # corrupt -> no protected names, but must not crash
    assert r.protected_names() == []
    assert r.corrupt is True


# ------------------------------------------------- Engine hard gate
def test_dispatch_blocks_manual_stop_before_other_gates():
    _cfg()
    r = _reg()
    r.add("cid", "worldmonitor", 0, False, "t", "2026-08-27T00:00:00")
    e = A.Engine(False, manual_stops=r)
    rec = e.dispatch("docker_restart", "worldmonitor", "cpu high")
    assert rec["state"] == "blocked"
    assert rec["reason"] == "blocked_manual_stop"
    assert rec["proposed_reason"] == "cpu high"
    assert e.manual_block_count == 1
    # NOT counted toward executed - hard invariant bypasses cap/allow
    assert e.executed == []


def test_dispatch_allows_non_protected():
    _cfg()
    r = _reg()
    r.add("cid", "worldmonitor", 0, False, "t", "2026-08-27T00:00:00")
    # put homepage in allow list so it clears that gate; dry-run
    Cfg.data["actions"]["allow_restart_containers"] = ["homepage"]
    e = A.Engine(True, manual_stops=r)
    rec = e.dispatch("docker_restart", "homepage", "x")
    assert rec["state"] == "skipped"   # not blocked by manual stop


def test_dispatch_blocks_prune_when_protected_stopped():
    _cfg()
    r = _reg()
    r.add("cid", "worldmonitor", 0, False, "t", "2026-08-27T00:00:00")
    e = A.Engine(False, manual_stops=r, protected_stopped={"worldmonitor"})
    rec = e.dispatch("docker_prune", "", "disk high")
    assert rec["state"] == "blocked"
    assert rec["reason"] == "blocked_manual_stop"


def test_dispatch_allows_prune_when_no_protected_stopped():
    _cfg()
    r = _reg()
    e = A.Engine(False, manual_stops=r, protected_stopped=set())
    Cfg.data["actions"]["allow_prune"] = True
    rec = e.dispatch("docker_prune", "", "disk high")
    assert rec["state"] == "executed"


def test_invariant_disabled_does_not_block():
    _cfg()
    Cfg.data["manual_stop_protection"]["block_restart_for_manual_stops"] = False
    r = _reg()
    r.add("cid", "worldmonitor", 0, False, "t", "2026-08-27T00:00:00")
    e = A.Engine(True, manual_stops=r)
    rec = e.dispatch("docker_restart", "worldmonitor", "x")
    # dry-run path -> skipped (worldmonitor not in allow list? add it)
    Cfg.data["actions"]["allow_restart_containers"] = ["worldmonitor"]
    assert rec["state"] != "blocked" or rec["reason"] != "blocked_manual_stop"
    _cfg()  # restore