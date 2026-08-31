"""Tests for collector/vault_drift_ingest.py + hermes_actions vault_drift_actions.

Mirrors the style of test_dockhand_ingest.py / the dockhand tests in
test_opsbrain.py. Vault drift is NOTIFY-ONLY: these tests assert it never
proposes docker_restart / any remediation.
"""
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hermes_actions.actions as A
from common import Cfg
from collector import vault_drift_ingest as V


def _report(findings=None, timestamp="2026-08-31T22:00:00+00:00", up=True):
    return {
        "up": up,
        "timestamp": timestamp,
        "attention": "actionable" if up else "none",
        "findings": findings if findings is not None else [],
    }


def _write(path, doc):
    path.write_text(json.dumps(doc))
    return path


_CFG = {"sources": {"vault_drift": {"enabled": True}}}


@pytest.fixture(autouse=True)
def base_config(monkeypatch):
    monkeypatch.setattr(Cfg, "data", deepcopy(_CFG))


# --------------------------------------------------------------------------- pull_report


def test_pull_report_reads(tmp_path, base_config):
    p = _write(tmp_path / "r.json", _report([
        {"path": "A.md", "issue": "broken link to [[X]]"},
    ]))
    r = V.pull_report(str(p))
    assert r["up"] is True
    assert len(r["findings"]) == 1


def test_pull_report_missing(tmp_path, base_config):
    r = V.pull_report(str(tmp_path / "nope.json"))
    assert r["up"] is False
    assert "read failed" in r["err"]


def test_pull_report_corrupt(tmp_path, base_config):
    p = tmp_path / "bad.json"
    p.write_text("{corrupt")
    r = V.pull_report(str(p))
    assert r["up"] is False
    assert "parse failed" in r["err"]


def test_pull_report_staleness(tmp_path, base_config):
    p = _write(tmp_path / "r.json", _report([], timestamp="2020-01-01T00:00:00+00:00"))
    r = V.pull_report(str(p), max_age_s=100)
    assert r["stale"] is True
    assert r["age_s"] is not None


# --------------------------------------------------------------------------- classify / context / dashboard


def test_classify_buckets_and_counts(base_config):
    cls = V.classify(_report([
        {"path": "A.md", "issue": "broken link to [[X]]"},
        {"path": "B.md", "issue": "broken wikilink"},
        {"path": "C.md", "issue": "orphan note"},
        {"path": "D.md", "issue": "stale metadata"},
        {"path": "E.md", "issue": "something ambiguous"},
    ]))
    assert cls["counts"] == {"broken_links": 2, "orphans": 1,
                             "stale_metadata": 1, "needs_review": 1}
    assert len(cls["broken_links"]) == 2


def test_classify_skips_non_dict(base_config):
    cls = V.classify(_report([42, "x", None]))
    assert cls["counts"] == {"broken_links": 0, "orphans": 0,
                             "stale_metadata": 0, "needs_review": 0}


def test_context_attention_actionable(base_config):
    rep = _report([{"path": "A.md", "issue": "broken link"}])
    ctx = V.create_context_nodes(rep, V.classify(rep))
    assert ctx["drift_count"] == 1
    assert ctx["attention"] in ("actionable", "low")


def test_dashboard_summary(base_config):
    rep = _report([
        {"path": "A.md", "issue": "broken link"},
        {"path": "B.md", "issue": "orphan note"},
    ])
    dash = V.update_dashboard_snapshot(rep, V.classify(rep))
    assert dash["up"] is True
    assert dash["count"] == 2
    assert "broken links" in dash["summary"]


def test_collect_vault_drift_disabled(base_config, monkeypatch):
    monkeypatch.setattr(Cfg, "data", {"sources": {"vault_drift": {"enabled": False}}})
    assert V.collect_vault_drift(Cfg) == {"enabled": False}
    monkeypatch.setattr(Cfg, "data", {"sources": {"vault_drift": {"enabled": True}}})


def test_collect_vault_drift_graceful_missing(tmp_path, base_config, monkeypatch):
    monkeypatch.setattr(Cfg, "data", {"sources": {"vault_drift": {"report_path": str(tmp_path / "no.json")}}})
    out = V.collect_vault_drift(Cfg)
    assert out["up"] is False
    assert out["dashboard"]["up"] is False


# --------------------------------------------------------------------------- vault_drift_actions (notify-only)


def _vd_coll(up=True, findings=None, stale=False):
    report = _report(findings or [{"path": "A.md", "issue": "broken link"}], up=up)
    cls = V.classify(report)
    if up:
        report = dict(report)
        report["classify"] = cls
        report["context_nodes"] = V.create_context_nodes(report, cls)
        report["dashboard"] = V.update_dashboard_snapshot(report, cls)
        report["stale"] = stale
    return {"vault_drift": report}


def test_vault_drift_notify_only_when_findings(base_config):
    e = A.Engine(True)  # dry run, still notifies
    recs = A.vault_drift_actions(_vd_coll(), e)
    assert recs
    assert all(r["verb"] == "notify_vault_drift" for r in recs)
    assert all(r["state"] == "notified" for r in recs)
    assert all(r["verb"] != "docker_restart" for r in recs)


def test_vault_drift_noop_when_down(base_config):
    e = A.Engine(True)
    assert A.vault_drift_actions(_vd_coll(up=False), e) == []


def test_vault_drift_notifies_stale(base_config):
    e = A.Engine(True)
    recs = A.vault_drift_actions(_vd_coll(findings=[], stale=True), e)
    assert any("stale" in (r.get("reason") or "").lower() for r in recs)


def test_engine_accepts_vault_drift_verb(base_config):
    e = A.Engine(True)
    e.dispatch("notify_vault_drift", "3", "vault drift: 3 finding(s)")
    assert e.executed and e.blocked == []
    assert e.executed[0]["verb"] == "notify_vault_drift"