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