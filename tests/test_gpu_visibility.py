"""
Ops Brain - GPU visibility: an invisible GPU must never read as "all clear".

Sep 2026: a host driver/NVML version mismatch (userspace 595.91 vs loaded kernel module
595.84) made `nvidia-smi` fail on this VM. The collector wrote `gpus: []`, GPU drift
detection silently compared nothing, the dashboard raised IndexError and crash-looped.

Covered here:
  - collector.collect_gpu        -> available:false + the nvidia-smi error text
  - actions.gpu_unavailable_notice -> one notify per UTC day, only for a known GPU
  - (dashboard history guard: tests/test_ui_refinements.py)

No live nvidia-smi / docker / ollama is touched.

Run:  python3 -m pytest -q   (from /appdata/OpsBrain)
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from common import Cfg                       # noqa: E402
import collector.collector as C              # noqa: E402
import hermes_actions.actions as A           # noqa: E402

NVML_ERR = "Failed to initialize NVML: Driver/library version mismatch"


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(Cfg, "data", {
        "hostname": "testvm",
        "paths": {"gpu_baseline": str(tmp_path / "gpu_baseline.json")},
        "gpu_drift": {"vram_creep_mb": 250, "vram_max_percent": 90, "stuck_pid_cycles": 5,
                      "util_threshold": 10, "power_idle_max_watts": 40,
                      "temp_idle_max_c": 55},
    })


def _fake_run(gpu_ok, procs_ok=True):
    def run(cmd, timeout=30, max_bytes=200000):
        if "--query-gpu" in " ".join(cmd):
            if not gpu_ok:
                return {"ok": False, "rc": 9, "out": NVML_ERR + "\n", "err": ""}
            return {"ok": True, "rc": 0, "out": "14046, 16303, 0, 39, 11.28, "
                                                "NVIDIA GeForce RTX 5070 Ti\n", "err": ""}
        if not procs_ok:
            return {"ok": False, "rc": 9, "out": NVML_ERR, "err": ""}
        return {"ok": True, "rc": 0, "out": "2380123, /usr/bin/ollama, 14000 MiB\n", "err": ""}
    return run


# --------------------------------------------------------------- collector
def test_collect_gpu_flags_unavailable_and_keeps_the_error(monkeypatch, tmp_path):
    # a GPU that used to be visible: seed the baseline identity first
    from common import write_json
    write_json(Path(Cfg.get("paths.gpu_baseline")),
               {"last_vram": 14046, "last_pid": "2380123", "last_power": 11.2,
                "last_temp": 39, "cycles_with_same_pid": 1,
                "gpu_name": "NVIDIA GeForce RTX 5070 Ti", "total_vram": 16303})
    monkeypatch.setattr(C, "run", _fake_run(gpu_ok=False))
    out = C.collect_gpu(Cfg.data)
    assert out["gpus"] == []
    assert out["available"] is False
    assert "Driver/library version mismatch" in out["error"]
    # baseline keeps the known GPU identity so "it vanished" stays detectable
    assert out["baseline"].get("gpu_name") == "NVIDIA GeForce RTX 5070 Ti"


def test_collect_gpu_reports_available_when_smi_works(monkeypatch):
    monkeypatch.setattr(C, "run", _fake_run(gpu_ok=True))
    out = C.collect_gpu(Cfg.data)
    assert out["available"] is True
    assert out["error"] is None
    assert out["gpus"][0]["mem_used_mb"] == 14046
    assert out["compute_processes"][0]["pid"] == "2380123"


def test_collect_gpu_available_without_compute_processes(monkeypatch):
    """A readable GPU with nothing on it is normal — not a visibility failure."""
    monkeypatch.setattr(C, "run", _fake_run(gpu_ok=True, procs_ok=False))
    out = C.collect_gpu(Cfg.data)
    assert out["available"] is True and out["compute_processes"] == []


# --------------------------------------------------------------- actions notify
def _invisible_gpu(**over):
    gpu = {"gpus": [], "available": False, "error": NVML_ERR,
           "baseline": {"gpu_name": "NVIDIA GeForce RTX 5070 Ti", "total_vram": 16303}}
    gpu.update(over)
    return {"timestamp": "2026-09-14T16:09:00Z", "gpu": gpu}


def test_gpu_unavailable_notifies_once_per_day(monkeypatch):
    sent = []
    monkeypatch.setattr(A, "notify", lambda msg, category="action": sent.append((category, msg)))
    st = {}
    assert A.gpu_unavailable_notice(_invisible_gpu(), st) is True
    assert sent and sent[0][0] == "gpu_unavailable" and NVML_ERR in sent[0][1]
    assert st.get("gpu_unavailable_notified")
    # same day, again -> silent
    assert A.gpu_unavailable_notice(_invisible_gpu(), st) is False
    assert len(sent) == 1


def test_gpu_unavailable_stays_silent_when_the_gpu_is_visible(monkeypatch):
    sent = []
    monkeypatch.setattr(A, "notify", lambda msg, category="action": sent.append(msg))
    coll = _invisible_gpu(available=True, gpus=[{"mem_used_mb": 14046}], error=None)
    assert A.gpu_unavailable_notice(coll, {}) is False
    assert sent == []


def test_gpu_unavailable_silent_without_a_known_gpu(monkeypatch):
    """A machine that never had a GPU (no baseline name) must not be nagged about one."""
    sent = []
    monkeypatch.setattr(A, "notify", lambda msg, category="action": sent.append(msg))
    coll = _invisible_gpu(baseline={})
    assert A.gpu_unavailable_notice(coll, {}) is False
    assert sent == []


def test_gpu_unavailable_backfills_availability_for_old_collector_docs(monkeypatch):
    """Older collector.json has no `available` key — derive it from the gpu list."""
    sent = []
    monkeypatch.setattr(A, "notify", lambda msg, category="action": sent.append(msg))
    coll = {"gpu": {"gpus": [{"mem_used_mb": 1}], "baseline": {"gpu_name": "x"}}}
    assert A.gpu_unavailable_notice(coll, {}) is False
    assert sent == []
