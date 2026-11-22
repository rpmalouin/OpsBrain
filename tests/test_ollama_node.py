"""Ops Brain - tests for the ollama box node source (kind: ollama).

The states that matter here are the ones a naive poller gets wrong:
  * HTTP 200 with an empty body — the 2026-11-06 Go-panic wedge: TCP accepts, nothing answers;
  * a timeout — same verdict, different mechanism;
  * a model that is partly in host RAM (`size_vram < size`), i.e. spilled off the card;
  * a card at the VRAM ceiling or over the temperature line;
  * SSH failing while ollama itself is perfectly healthy — a partial failure that must not
    read as "the node is down".
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from federation import ollama_node as ON  # noqa: E402

PS_OK = {"models": [{"name": "qwen3.5:27b", "size": 19_944_366_404,
                     "size_vram": 19_944_366_404, "context_length": 65536,
                     "expires_at": "2026-11-22T23:03:39Z"}]}
TAGS_OK = {"models": [{"name": "qwen3.5:27b"}]}
VERSION_OK = {"version": "0.40.16"}
GPUS_OK = [{"index": 0, "name": "NVIDIA GeForce RTX 5070 Ti", "mem_total_mib": 16303.0,
            "mem_used_mib": 10564.0, "util_percent": 0.0, "temp_c": 39.0, "power_w": 19.0},
           {"index": 1, "name": "NVIDIA GeForce RTX 5070 Ti", "mem_total_mib": 16303.0,
            "mem_used_mib": 9928.0, "util_percent": 0.0, "temp_c": 41.0, "power_w": 11.0}]
CFG = {"name": "ollamabox", "type": "gpu", "kind": "ollama",
       "ollama_url": "http://10.1.10.30:11434", "ssh_host": "ollama"}


def test_healthy_box():
    block = ON.evaluate(VERSION_OK, TAGS_OK, PS_OK, GPUS_OK, CFG)
    assert block["version"] == "0.40.16"
    assert block["tags"] == ["qwen3.5:27b"]
    assert block["models_resident"][0]["size_vram_gb"] == block["models_resident"][0]["size_gb"]
    assert block["context_length"] == 65536
    assert block["alerts"] == []


def test_model_spilled_off_the_gpu_is_an_alert():
    ps = {"models": [dict(PS_OK["models"][0], size_vram=12_000_000_000)]}
    block = ON.evaluate(VERSION_OK, TAGS_OK, ps, GPUS_OK, CFG)
    assert any("not fully on the GPU" in a for a in block["alerts"])


def test_model_on_cpu_only_is_an_alert():
    ps = {"models": [dict(PS_OK["models"][0], size_vram=0)]}
    block = ON.evaluate(VERSION_OK, TAGS_OK, ps, GPUS_OK, CFG)
    assert any("CPU only" in a for a in block["alerts"])


def test_no_model_resident_is_not_an_alert():
    # An idle box (keep_alive expired) is healthy, not broken.
    block = ON.evaluate(VERSION_OK, TAGS_OK, {"models": []}, GPUS_OK, CFG)
    assert block["models_resident"] == []
    assert block["alerts"] == []


def test_gpu_vram_ceiling_and_temperature():
    gpus = [dict(GPUS_OK[0], mem_used_mib=16_000.0, temp_c=88.0)]
    block = ON.evaluate(VERSION_OK, TAGS_OK, PS_OK, gpus, CFG)
    assert any("VRAM used" in a for a in block["alerts"])
    assert any("88 C" in a for a in block["alerts"])


def test_wedged_ollama_reads_offline_with_its_own_wording():
    block = ON.evaluate(None, None, None, GPUS_OK, CFG, http_error="wedged: HTTP 200 with an empty body")
    assert any("unreachable" in a and "wedged" in a for a in block["alerts"])


def test_empty_body_is_the_wedge_signature(monkeypatch):
    """_get_json must not accept a 200 with nothing in it."""
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    monkeypatch.setattr(ON.urllib.request, "urlopen", lambda url, timeout=None: Resp())
    try:
        ON._get_json("http://10.1.10.30:11434/api/version", 1)
    except ON.EmptyBody:
        pass
    else:  # pragma: no cover
        raise AssertionError("an empty 200 body must raise EmptyBody")


def test_collect_survives_a_dead_box(monkeypatch):
    """Every transport failure collapses into one offline node block, never an exception."""
    def boom(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(ON.urllib.request, "urlopen", boom)
    monkeypatch.setattr(ON, "read_gpus", lambda host, timeout: (_ for _ in ()).throw(RuntimeError("ssh refused")))
    node = ON.collect(CFG)
    assert node["online"] is False
    assert node["err"]
    assert node["anomalies"] == 2          # ollama unreachable + gpu query failed
    assert node["type"] == "gpu"


def test_collect_healthy(monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return {"/api/version": VERSION_OK, "/api/tags": TAGS_OK, "/api/ps": PS_OK}[
            "/api/" + url.rsplit("/api/", 1)[1]]

    monkeypatch.setattr(ON, "_get_json", fake_get)
    monkeypatch.setattr(ON, "read_gpus", lambda host, timeout: GPUS_OK)
    node = ON.collect(CFG)
    assert node["online"] is True
    assert node["ollama"]["version"] == "0.40.16"
    assert len(node["gpu"]) == 2
    assert node["anomalies"] == 0
    assert node["raw"]["collector"]["gpu"]["drift_flags"] == []
    assert [c for c in calls] == ["http://10.1.10.30:11434/api/version",
                                  "http://10.1.10.30:11434/api/tags",
                                  "http://10.1.10.30:11434/api/ps"]


def test_partial_failure_is_online_with_one_alert(monkeypatch):
    """ollama fine, SSH broken: the LLM is serving, so the node is UP and the alert says why."""
    monkeypatch.setattr(ON, "_get_json", lambda url, timeout: {
        "/api/version": VERSION_OK, "/api/tags": TAGS_OK, "/api/ps": PS_OK}[
        "/api/" + url.rsplit("/api/", 1)[1]])
    monkeypatch.setattr(ON, "read_gpus", lambda host, timeout: (_ for _ in ()).throw(RuntimeError("ssh timeout")))
    node = ON.collect(CFG)
    assert node["online"] is True
    assert node["gpu"] == []
    assert node["anomalies"] == 1
    assert any("gpu query failed" in a for a in node["ollama"]["alerts"])


def test_expected_model_missing_is_named(monkeypatch):
    monkeypatch.setattr(ON, "_get_json", lambda url, timeout: {
        "/api/version": VERSION_OK, "/api/tags": {"models": [{"name": "other:1b"}]},
        "/api/ps": {"models": []}}["/api/" + url.rsplit("/api/", 1)[1]])
    monkeypatch.setattr(ON, "read_gpus", lambda host, timeout: GPUS_OK)
    node = ON.collect(dict(CFG, expect_model="qwen3.5:27b"))
    assert any("not installed" in a for a in node["ollama"]["alerts"])


def test_read_gpus_parses_real_nvidia_smi_output(monkeypatch):
    class Proc:
        returncode = 0
        stdout = ("0, NVIDIA GeForce RTX 5070 Ti, 16303, 10564, 0, 39, 19.05\n"
                  "1, NVIDIA GeForce RTX 5070 Ti, 16303, 9928, 0, 41, 10.53\n")
        stderr = ""

    monkeypatch.setattr(ON.subprocess, "run", lambda *a, **k: Proc())
    gpus = ON.read_gpus("ollama", 5)
    assert [g["index"] for g in gpus] == [0, 1]
    assert gpus[0]["mem_total_mib"] == 16303.0
    assert gpus[0]["power_w"] == 19.05


def test_read_gpus_raises_on_ssh_failure(monkeypatch):
    class Proc:
        returncode = 255
        stdout = ""
        stderr = "ssh: connect to host 10.1.10.30 port 22: Connection refused\n"

    monkeypatch.setattr(ON.subprocess, "run", lambda *a, **k: Proc())
    try:
        ON.read_gpus("ollama", 5)
    except RuntimeError as exc:
        assert "Connection refused" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a failed ssh must raise")
