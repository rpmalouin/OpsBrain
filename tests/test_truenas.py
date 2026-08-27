"""Ops Brain - tests for the TrueNAS collector (mocked transport; no live network)."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "common"))

from common import Cfg  # noqa: E402
from collector import collector as C  # noqa: E402


def _cfg_sample():
    Cfg.load()
    Cfg.data.setdefault("sources", {})["truenas"] = {
        "enabled": True, "base_url": "http://truenas/api/v2.0", "creds_file": "~/.smbcred"}
    return Cfg


def test_collect_truenas_parses_pools(tmp_path, monkeypatch):
    _cfg_sample()
    # mock creds to a temp file
    creds = tmp_path / "creds"
    creds.write_text("username=ron\npassword=sekret\n")
    monkeypatch.setattr(C, "_truenas_creds", lambda cfg: ("ron", "sekret"))

    def fake_get(url, user, passwd, timeout=8, max_bytes=2_000_000):
        if url.endswith("/pool"):
            return {"ok": True, "json": [{
                "name": "malouin_data", "status": "ONLINE", "healthy": True,
                "size": 20_000_000_000_000, "allocated": 11_000_000_000_000,
                "free": 9_000_000_000_000,
                "topology": {"data": [{"name": "raidz1-0", "type": "RAIDZ1"}]}}]}
        if url.endswith("/system/info"):
            return {"ok": True, "json": {"version": "25.10.3.1", "model": "AMD Ryzen 9",
                                         "cores": 32, "physmem": 134979616768,
                                         "uptime_seconds": 86400, "hostname": "truenas"}}
        if url.endswith("/alert/list"):
            return {"ok": True, "json": [
                {"level": "WARNING", "klass": "SnapshotTotalCount",
                 "formatted": "too many snapshots"}]}
        if url.endswith("/disk"):
            return {"ok": True, "json": [{"name": "nvme1n1"}, {"name": "nvme2n1"}]}
        return {"ok": False, "err": "unhandled"}

    monkeypatch.setattr(C, "truenas_get", fake_get)
    out = C.collect_truenas(Cfg)
    assert out["up"] is True
    assert out["pool_count"] == 1
    assert out["pools"][0]["name"] == "malouin_data"
    assert out["pools"][0]["status"] == "ONLINE"
    assert out["pools_healthy"] == 1
    assert out["version"] == "25.10.3.1"
    assert out["alerts_count"] == 1
    assert out["disk_count"] == 2


def test_collect_truenas_unreachable(tmp_path, monkeypatch):
    _cfg_sample()
    monkeypatch.setattr(C, "_truenas_creds", lambda cfg: ("ron", "sekret"))
    monkeypatch.setattr(C, "truenas_get",
                        lambda *a, **k: {"ok": False, "err": "HTTP 401", "code": 401})
    out = C.collect_truenas(Cfg)
    assert out["up"] is False
    assert out["pool_count"] == 0
    assert out["alerts_count"] == 0


def test_collect_truenas_disabled():
    Cfg.load()
    Cfg.data.setdefault("sources", {})["truenas"] = {"enabled": False}
    out = C.collect_truenas(Cfg)
    assert out == {"enabled": False}


def test_truenas_creds_reads_smbcred(tmp_path):
    creds = tmp_path / ".smbcred"
    creds.write_text("username=ron\npassword=hunter2\n\n")
    Cfg.load()
    Cfg.data.setdefault("sources", {})["truenas"] = {"creds_file": str(creds)}
    u, p = C._truenas_creds(Cfg)
    assert u == "ron"
    assert p == "hunter2"