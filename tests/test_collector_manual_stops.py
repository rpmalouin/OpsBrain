"""Ops Brain - tests for collector manual-stop transition detection."""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "common"))

from common import Cfg  # noqa: E402
from manual_stops import ManualStops  # noqa: E402
from collector import collector as C  # noqa: E402


def _cfg(tmp):
    Cfg.load()
    Cfg.data["manual_stop_protection"] = {
        "enabled": True, "track_manual_stops": True, "block_restart_for_manual_stops": True}
    Cfg.data["actions"]["manual_stop_sigkill_protect"] = True
    Cfg.data["paths"]["manual_stops"] = os.path.join(tmp, "manual_stops.json")
    Cfg.data["paths"]["prev_running"] = os.path.join(tmp, "prev_running.json")


def _cont(name, state, exit_code=0, oom=False, cid=None):
    return {"name": name, "id": cid or name, "state": state,
            "exit_code": exit_code, "oom_killed": oom, "finished_at": "t"}


def test_first_run_seeds_no_protection(tmp_path):
    _cfg(str(tmp_path))
    containers = [_cont("web", "running", cid="c1")]
    meta = C.update_manual_stops(containers, Cfg)
    assert meta["count"] == 0            # no transition observed yet


def test_manual_stop_detected_after_transition(tmp_path):
    _cfg(str(tmp_path))
    # seed prev_running as if web was running last cycle
    C.write_json(C.Cfg.resolve("paths.prev_running") if C.Cfg.get("paths.prev_running")
                 else os.path.join(str(tmp_path), "prev_running.json"),
                 {"ids": ["c1"], "names": ["web"]})
    containers = [_cont("web", "exited", exit_code=0, cid="c1")]
    meta = C.update_manual_stops(containers, Cfg)
    assert "web" in meta["names"]


def test_crash_exit_not_manual(tmp_path):
    _cfg(str(tmp_path))
    C.write_json(os.path.join(str(tmp_path), "prev_running.json"),
                 {"ids": ["c1"], "names": ["web"]})
    containers = [_cont("web", "exited", exit_code=1, cid="c1")]   # app crash
    meta = C.update_manual_stops(containers, Cfg)
    assert "web" not in meta["names"]


def test_oom_not_manual(tmp_path):
    _cfg(str(tmp_path))
    C.write_json(os.path.join(str(tmp_path), "prev_running.json"),
                 {"ids": ["c1"], "names": ["web"]})
    containers = [_cont("web", "exited", exit_code=137, oom=True, cid="c1")]
    meta = C.update_manual_stops(containers, Cfg)
    assert "web" not in meta["names"]


def test_protection_persists_and_survives(tmp_path):
    _cfg(str(tmp_path))
    C.write_json(os.path.join(str(tmp_path), "prev_running.json"),
                 {"ids": ["c1"], "names": ["web"]})
    containers = [_cont("web", "exited", exit_code=0, cid="c1")]
    meta = C.update_manual_stops(containers, Cfg)
    assert "web" in meta["names"]
    # next cycle: container still gone from snapshot -> protection persists
    meta2 = C.update_manual_stops([], Cfg)
    assert "web" in meta2["names"]     # never auto-forgets


def test_rearm_on_rerun_clears(tmp_path):
    _cfg(str(tmp_path))
    C.write_json(os.path.join(str(tmp_path), "prev_running.json"),
                 {"ids": ["c1"], "names": ["web"]})
    containers = [_cont("web", "exited", exit_code=0, cid="c1")]
    meta = C.update_manual_stops(containers, Cfg)
    assert "web" in meta["names"]
    # user restarts web -> running again, same id -> re-arm clears protection
    containers2 = [_cont("web", "running", cid="c1")]
    meta2 = C.update_manual_stops(containers2, Cfg)
    assert "web" not in meta2["names"]


# --------------------------------------------------------------------------- restart policy from inspect
# The Dockhand ingest compares restart policy; it used to have to infer one from
# container state because the collector record carried no policy. Carry the real
# HostConfig value instead (immich, 2026-10-08: `restart policy ${RESTART} vs always`).


class _FakeInspect:
    returncode = 0
    stdout = ("abc123|1|false|2026-10-08T13:00:00Z|2026-10-08T14:00:00Z|29"
              "|unless-stopped\n")


def _stub_inspect(monkeypatch):
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: _FakeInspect())


def test_inspect_state_carries_the_real_restart_policy(monkeypatch):
    _stub_inspect(monkeypatch)
    st = C._inspect_state("immich_server")
    assert st["restart_policy"] == "unless-stopped"
    assert st["restart_count"] == 29
    assert st["exit_code"] == 1


def test_inspect_state_handles_a_container_with_no_policy(monkeypatch):
    class R:
        returncode = 0
        stdout = "abc|0|false|t|t|0|\n"
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: R())
    assert C._inspect_state("x")["restart_policy"] == ""


def test_inspect_state_policy_is_none_when_inspect_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("no docker")
    monkeypatch.setattr(C.subprocess, "run", boom)
    assert C._inspect_state("x")["restart_policy"] is None