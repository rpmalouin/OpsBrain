"""Tests for collector/gpu_reconcile_ingest.py + hermes_actions gpu_reconcile_actions.

Mirrors test_vault_drift_ingest.py. The source is NOTIFY-ONLY: a driver/library
mismatch is a host condition a human fixes (stop the GPU containers, reload the
module), so these tests assert it never proposes docker_restart / gpu_kill / prune.

Fixtures are per-file (no tests/conftest.py): the autouse fixture patches Cfg.data and
redirects A.NOTIF_FILE / A.STATE_FILE into tmp_path so `notify` never appends to the
live logs/notifications.jsonl.
"""
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hermes_actions.actions as A
from common import Cfg
from collector import gpu_reconcile_ingest as G

# A log in exactly the format /usr/local/sbin/nvidia-module-reconcile writes.
_REAL_LOG = """2026-10-10T13:11:30Z MISMATCH loaded=595.91.07 disk=595.99.02 — reload DEFERRED (GPU held: processes=none containers=/ollama)
2026-10-10T13:11:30Z   remedy: stop the GPU containers (docker stop /ollama), then run /usr/local/sbin/nvidia-module-reconcile --force
2026-10-10T13:40:02Z MISMATCH loaded=595.91.07 disk=595.99.02 — reloading (processes=none containers=/ollama force=1)
2026-10-10T13:40:03Z   /usr/sbin/rmmod nvidia_uvm failed (still in use?)
2026-10-10T13:40:04Z reloaded: loaded module now 595.99.02 (disk 595.99.02)
2026-10-10T13:40:04Z CDI spec regenerated at /var/run/cdi/nvidia.yaml for 595.99.02
"""

_FAILED_LOG = """2026-10-10T14:00:00Z MISMATCH loaded=595.91.07 disk=595.99.02 — reloading (processes=none containers=none force=1)
2026-10-10T14:00:01Z   modprobe nvidia FAILED — reload by hand or reboot
"""

# A deferral the hook never resolved (no later successful reload line).
_DEFERRED_ONLY_LOG = """2026-10-10T13:11:30Z MISMATCH loaded=595.91.07 disk=595.99.02 — reload DEFERRED (GPU held: processes=none containers=/ollama)
2026-10-10T13:11:30Z   remedy: stop the GPU containers (docker stop /ollama), then run /usr/local/sbin/nvidia-module-reconcile --force
"""

# Fixed "now" for age calculations: the fixtures are stamped the same day, and a real
# clock earlier than a fixture would make ages negative and flip the staleness verdict.
NOW = datetime(2026, 10, 10, 14, 30, 0, tzinfo=timezone.utc)

_CFG = {"sources": {"gpu_reconcile": {"enabled": True}}}


class _NullLog:
    """Silence the actions logger: patching NOTIF_FILE keeps the ticker clean but
    A.notify() still does log.info -> the LIVE logs/opsbrain.log (documented pitfall)."""

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def base_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Cfg, "data", deepcopy(_CFG))
    monkeypatch.setattr(A, "NOTIF_FILE", tmp_path / "notifications.jsonl", raising=False)
    monkeypatch.setattr(A, "STATE_FILE", tmp_path / "action_state.json", raising=False)
    monkeypatch.setattr(A, "log", _NullLog())


def _empty_ko_root(tmp_path):
    """A ko_root with no module for this kernel (live verdict unknown)."""
    return tmp_path / "lib-modules"


# --------------------------------------------------------------------------- parse_log

def test_parse_log_reads_real_format():
    evs = G.parse_log(_REAL_LOG)
    assert [e["kind"] for e in evs] == [G.KIND_DEFERRED, G.KIND_RELOADING, G.KIND_RELOADED]
    d = evs[0]
    assert d["loaded"] == "595.91.07" and d["disk"] == "595.99.02"
    assert d["containers"] == ["/ollama"] and d["processes"] == []
    assert d["remedy"] and "--force" in d["remedy"]
    assert evs[1]["containers"] == ["/ollama"] and evs[1]["processes"] == []
    # an rmmod that failed mid-attempt is a warning on that attempt, not a failed reload
    assert any("still in use" in w for w in evs[1]["warnings"])
    assert evs[2]["loaded"] == "595.99.02"


def test_parse_log_reads_failed_and_failure_lines():
    evs = G.parse_log(_FAILED_LOG)
    assert [e["kind"] for e in evs] == [G.KIND_RELOADING, G.KIND_FAILED]


def test_parse_log_skips_handwritten_and_truncated_lines():
    evs = G.parse_log("a hand-added note\n2026-10-10T00:00:00Z\nMISMATCH loaded=x disk=y\n")
    assert evs == []


def test_parse_log_keeps_events_without_holders():
    evs = G.parse_log("2026-10-10T14:00:00Z MISMATCH loaded=1 disk=2 — reloading (processes=none containers=none force=1)\n")
    assert evs[0]["kind"] == G.KIND_RELOADING
    assert evs[0]["containers"] == [] and evs[0]["processes"] == []


# --------------------------------------------------------------------------- live_versions

def _fake_ko(root, release, name="nvidia.ko"):
    p = Path(root) / release / "updates" / "dkms"
    p.mkdir(parents=True, exist_ok=True)
    ko = p / name
    ko.write_text("")
    return ko


def test_live_versions_match(tmp_path):
    vf = tmp_path / "version"
    vf.write_text("595.99.02\n")
    ko_root = tmp_path / "lib-modules"
    _fake_ko(ko_root, "5.15.0-test")
    out = G.live_versions(version_file=vf, ko_root=ko_root, kernel_release="5.15.0-test",
                          modinfo=lambda p: "595.99.02")
    assert out["match"] is True and out["ko"].endswith("nvidia.ko")


def test_live_versions_mismatch(tmp_path):
    vf = tmp_path / "version"
    vf.write_text("595.91.07")
    ko_root = tmp_path / "lib-modules"
    _fake_ko(ko_root, "5.15.0-test")
    out = G.live_versions(version_file=vf, ko_root=ko_root, kernel_release="5.15.0-test",
                          modinfo=lambda p: "595.99.02")
    assert out["match"] is False and out["loaded"] == "595.91.07" and out["disk"] == "595.99.02"


def test_live_versions_unknown_when_module_absent(tmp_path):
    vf = tmp_path / "version"
    vf.write_text("595.99.02")
    out = G.live_versions(version_file=vf, ko_root=tmp_path / "nope",
                          kernel_release="5.15.0-test", modinfo=lambda p: "x")
    assert out["match"] is None and "no nvidia.ko" in out["err"]


def test_live_versions_missing_version_file(tmp_path):
    out = G.live_versions(version_file=tmp_path / "absent", ko_root=tmp_path)
    assert out["match"] is None and "unreadable" in out["err"]


# --------------------------------------------------------------------------- classify

def test_classify_counts_and_last_mismatch():
    cls = G.classify(G.parse_log(_REAL_LOG))
    assert cls["counts"][G.KIND_DEFERRED] == 1
    assert cls["counts"][G.KIND_RELOADED] == 1
    assert cls["last"]["kind"] == G.KIND_RELOADED
    assert cls["last_mismatch"]["kind"] == G.KIND_RELOADED


def test_classify_marks_stale_deferred_event():
    cls = G.classify(G.parse_log(_REAL_LOG), max_age_s=1, now=NOW)
    assert cls["stale_event"] is True


def test_classify_knows_a_deferral_the_hook_later_resolved():
    resolved = G.classify(G.parse_log(_REAL_LOG), now=NOW)
    assert resolved["needs_human_unresolved"] is False
    assert resolved["last_needs_human"]["kind"] == G.KIND_DEFERRED
    assert resolved["last_reloaded"]["loaded"] == "595.99.02"
    open_case = G.classify(G.parse_log(_DEFERRED_ONLY_LOG), now=NOW)
    assert open_case["needs_human_unresolved"] is True


# --------------------------------------------------------------------------- context + dashboard

def _report(text, live, max_age_s=60 * 60 * 24 * 7):
    evs = G.parse_log(text)
    cls = G.classify(evs, max_age_s=max_age_s, now=NOW)
    rep = {"up": True, "live": live, "classify": cls, "events": len(evs)}
    rep["context_nodes"] = G.create_context_nodes(rep, cls)
    rep["dashboard"] = G.update_dashboard_snapshot(rep, cls)
    return rep


def test_context_actionable_on_live_mismatch():
    rep = _report(_REAL_LOG, {"loaded": "595.91.07", "disk": "595.99.02", "match": False})
    ctx = rep["context_nodes"]
    assert ctx["state"] == "mismatch" and ctx["attention"] == "actionable"
    assert ctx["holders"] == ["/ollama"] and "--force" in ctx["remedy"]


def test_context_low_when_deferral_still_open():
    """Hook deferred and never reloaded; the live driver is fine again (a human fixed it)
    -> informational, not an alarm."""
    ctx = _report(_DEFERRED_ONLY_LOG, {"loaded": "595.99.02", "disk": "595.99.02", "match": True})["context_nodes"]
    assert ctx["state"] == "ok" and ctx["attention"] == "low"
    assert ctx["holders"] == ["/ollama"] and "--force" in ctx["remedy"]


def test_context_none_when_hook_already_reloaded_it():
    ctx = _report(_REAL_LOG, {"loaded": "595.99.02", "disk": "595.99.02", "match": True})["context_nodes"]
    assert ctx["state"] == "ok" and ctx["attention"] == "none"


def test_context_none_when_no_events_and_in_sync():
    ctx = _report("", {"loaded": "595.99.02", "disk": "595.99.02", "match": True})["context_nodes"]
    assert ctx["state"] == "ok" and ctx["attention"] == "none"


def test_context_unknown_live_verdict_falls_back_to_log():
    ctx = _report(_FAILED_LOG, {"loaded": None, "disk": None, "match": None})["context_nodes"]
    assert ctx["state"] == G.KIND_FAILED and ctx["attention"] == "low"


def test_dashboard_summary_names_versions_and_remedy():
    rep = _report(_REAL_LOG, {"loaded": "595.91.07", "disk": "595.99.02", "match": False})
    s = rep["dashboard"]["summary"]
    assert "595.91.07" in s and "595.99.02" in s and "--force" in s and "/ollama" in s


# --------------------------------------------------------------------------- orchestrator

def test_collect_disabled():
    Cfg.data = {"sources": {"gpu_reconcile": {"enabled": False}}}
    assert G.collect_gpu_reconcile(Cfg) == {"enabled": False}


def test_collect_graceful_when_log_missing(tmp_path):
    log = tmp_path / "absent.log"
    Cfg.data = {"sources": {"gpu_reconcile": {
        "enabled": True, "log_path": str(log),
        "module_version_file": str(tmp_path / "version"),
        "ko_root": str(_empty_ko_root(tmp_path)),
    }}}
    out = G.collect_gpu_reconcile(Cfg)
    assert out["enabled"] is True and out["up"] is False
    assert "read failed" in out["err"]
    assert out["context_nodes"]["state"] == "unknown"          # never invents a verdict


def test_collect_reads_log_and_live_state(tmp_path, monkeypatch):
    log = tmp_path / "reconcile.log"
    log.write_text(_REAL_LOG)
    Cfg.data = {"sources": {"gpu_reconcile": {"enabled": True, "log_path": str(log)}}}
    monkeypatch.setattr(G, "live_versions", lambda **kw: {
        "loaded": "595.91.07", "disk": "595.99.02", "match": False, "ko": "/x/nvidia.ko", "err": None})
    out = G.collect_gpu_reconcile(Cfg)
    assert out["up"] is True and out["events"] == 3
    assert out["context_nodes"]["attention"] == "actionable"
    assert out["dashboard"]["state"] == "mismatch"
    assert out["classify"]["counts"][G.KIND_DEFERRED] == 1


def test_collect_live_mismatch_without_log_is_still_actionable(tmp_path, monkeypatch):
    """A mismatch with no hook log (bare dpkg -i, hand-copied module) must still alarm."""
    Cfg.data = {"sources": {"gpu_reconcile": {"enabled": True, "log_path": str(tmp_path / "none.log")}}}
    monkeypatch.setattr(G, "live_versions", lambda **kw: {
        "loaded": "595.91.07", "disk": "595.99.02", "match": False, "ko": "/x/nvidia.ko", "err": None})
    out = G.collect_gpu_reconcile(Cfg)
    assert out["up"] is False and out["context_nodes"]["attention"] == "actionable"


def test_collect_against_real_host_paths_is_graceful():
    """Default config paths on the live host: never raises, always a dict block."""
    out = G.collect_gpu_reconcile({})
    assert isinstance(out, dict) and out.get("enabled") is True
    assert "context_nodes" in out and "dashboard" in out


# --------------------------------------------------------------------------- actions (notify-only)

def _gr_coll(attention="actionable", state="mismatch", up=True, summary="driver mismatch: loaded a vs b"):
    return {"gpu_reconcile": {
        "enabled": True, "up": up,
        "context_nodes": {"attention": attention, "state": state},
        "dashboard": {"summary": summary, "state": state},
    }}


def test_gpu_reconcile_notify_only():
    e = A.Engine(True)                                   # dry-run: still notifies
    st = {}
    recs = A.gpu_reconcile_actions(_gr_coll(), e, st)
    assert recs and all(r["verb"] == "notify_gpu_reconcile" for r in recs)
    assert all(r["state"] == "notified" for r in recs)
    assert all(r["verb"] not in ("docker_restart", "gpu_kill", "docker_prune") for r in recs)


def test_gpu_reconcile_notifies_once_per_utc_day():
    e = A.Engine(True)
    st = {}
    assert A.gpu_reconcile_actions(_gr_coll(), e, st)
    assert A.gpu_reconcile_actions(_gr_coll(), e, st) == []      # same UTC day
    assert st.get("gpu_reconcile_notified")


def test_gpu_reconcile_noop_when_not_actionable():
    e = A.Engine(True)
    assert A.gpu_reconcile_actions(_gr_coll(attention="low", state="ok"), e, {}) == []
    assert A.gpu_reconcile_actions(_gr_coll(attention="none", state="ok"), e, {}) == []


def test_gpu_reconcile_noop_on_empty_or_bad_collector():
    e = A.Engine(True)
    assert A.gpu_reconcile_actions({}, e, {}) == []
    assert A.gpu_reconcile_actions({"gpu_reconcile": "nonsense"}, e, {}) == []


def test_engine_accepts_gpu_reconcile_verb():
    e = A.Engine(True)
    e.dispatch("notify_gpu_reconcile", "mismatch", "driver mismatch")
    assert e.executed and e.blocked == []
    assert e.executed[0]["verb"] == "notify_gpu_reconcile"


# --------------------------------------------------------------------------- notification text

def _entries():
    """The notification entries written to the (tmp) ticker file."""
    txt = A.NOTIF_FILE.read_text().strip()
    return [json.loads(l) for l in txt.splitlines()] if txt else []


def test_dispatch_notify_prefers_reason_text_over_target():
    """NOTIFY[...] used to read "3"/"sonarr": the target (a count, a list, a name) was used as
    the ticker message and the informative reason thrown away."""
    e = A.Engine(True)
    e.dispatch("notify_vault_drift", 3, "Vault drift (3): 3 vault finding(s)")
    assert _entries()[-1]["message"] == "Vault drift (3): 3 vault finding(s)"
    e.dispatch("notify_dockhand_drift", "sonarr", "Dockhand restart storm sonarr: 4 in 30m")
    assert _entries()[-1]["message"] == "Dockhand restart storm sonarr: 4 in 30m"


def test_dispatch_notify_falls_back_to_target_when_no_reason():
    e = A.Engine(True)
    e.dispatch("notify_vault_drift", "stale", "")
    assert _entries()[-1]["message"] == "stale"


def test_generic_notify_verb_keeps_its_category_stable():
    """dispatch("notify", msg, explanation): the explanation is NOT the category (an LLM
    sentence there left the ticker entry unclassifiable)."""
    e = A.Engine(True)
    e.dispatch("notify", "Vault drift: 10 orphaned notes need review", "explains the finding")
    entry = _entries()[-1]
    assert entry["message"] == "Vault drift: 10 orphaned notes need review"
    assert entry["category"] == "action"


def test_vault_drift_notifies_once_per_event_with_readable_text():
    e = A.Engine(True)
    coll = {"vault_drift": {
        "up": True, "stale": True,
        "classify": {"counts": {"broken_links": 1, "orphans": 1}},
        "dashboard": {"summary": "2 vault finding(s): 1 broken links; 1 orphans"},
        "context_nodes": {"drift_count": 2},
    }}
    recs = A.vault_drift_actions(coll, e)
    entries = _entries()
    assert len(recs) == 2 and len(entries) == len(recs)      # no dispatch+notify duplication
    msgs = [x["message"] for x in entries]
    assert all(m and not m.isdigit() for m in msgs)
    assert any("stale" in m.lower() for m in msgs)


def test_dockhand_notifies_once_per_event_and_names_the_service():
    e = A.Engine(True)
    coll = {"dockhand": {
        "up": True,
        "classify": {"drift_count": 0},
        "correlate": {"restart_storms": [{"service": "sonarr", "count": 4, "window_min": 30}],
                      "health_flaps": [{"service": "immich_server", "toggle_count": 3, "window_min": 30}]},
        "dashboard": {"orphaned_containers": ["Firefox"]},
    }}
    recs = A.dockhand_drift_actions(coll, e)
    entries = _entries()
    assert len(entries) == len(recs) == 3
    joined = " ".join(x["message"] for x in entries)
    for name in ("sonarr", "immich_server", "Firefox"):
        assert name in joined
