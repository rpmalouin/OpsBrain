"""
Ops Brain - per-model ollama selection + the `think` capability gate.

Regression cover for the Sep 2026 model swap: the operator replaced the ollama model
set, the old resolver substituted an unconfigured 7B ("any qwen"), and ollama answered
think=true with HTTP 400 "does not support thinking" — so the hourly reasoner stage
failed while actions kept replaying a stale decision doc.

  - reasoner.pick_model / resolve_model    (only config-named models, never a guess)
  - reasoner.fetch_capabilities            (/api/show -> "thinking")
  - reasoner.decide_think / build_body     (think per model, never format=json)
  - reasoner.call_ollama                   (400 -> retry once without think)
  - actions.reasoner_decision_state        (stale snapshot -> LLM half suppressed)

No live ollama, docker, or GPU is touched.

Run:  python3 -m pytest -q   (from /appdata/OpsBrain)
"""
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from common import Cfg                      # noqa: E402
import reasoner.reasoner as R               # noqa: E402
import hermes_actions.actions as A          # noqa: E402

PRIMARY = "qwen3:14b-64k"
FALLBACK = "deepseek-r1:7b"
ALT = "qwen2.5-7b-128k:latest"


class _NullLog:
    """The suite must not write into the LIVE logs/opsbrain.log (R.log is the shared logger)."""

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    """Config identical in shape to config/ops_brain.yaml (ollama + floor)."""
    monkeypatch.setattr(R, "log", _NullLog())
    monkeypatch.setattr(A, "log", _NullLog())
    monkeypatch.setattr(A, "NOTIF_FILE", Path("/dev/null"), raising=False)
    monkeypatch.setattr(A, "STATE_FILE", Path("/dev/null"), raising=False)
    monkeypatch.setattr(Cfg, "data", {
        "hostname": "testvm",
        "ollama": {
            "url": "http://localhost:11434/api/generate",
            "model": PRIMARY,
            "fallback_model": FALLBACK,
            "alt_models": [ALT],
            "think": True,
            "timeout": 120,
            "options": {"temperature": 0.2, "num_ctx": 65536, "keep_alive": "5m"},
        },
        "actions": {"qwen_confidence_floor": 0.6},
    })
    R._CAP_CACHE.clear()
    R._LAST_CALL.clear()
    yield


# ------------------------------------------------------------------ model selection
def test_pick_model_prefers_primary():
    avail = {PRIMARY, FALLBACK, ALT}
    assert R.pick_model(avail, PRIMARY, FALLBACK, [ALT]) == PRIMARY


def test_pick_model_falls_back_to_configured_secondary():
    assert R.pick_model({FALLBACK, ALT}, PRIMARY, FALLBACK, [ALT]) == FALLBACK


def test_pick_model_uses_configured_alt_last():
    assert R.pick_model({ALT}, PRIMARY, FALLBACK, [ALT]) == ALT


def test_pick_model_never_substitutes_unconfigured_model():
    """The exact Sep 2026 failure: primary+fallback gone, other models installed."""
    avail = {"qwen2.5-7b-128k:latest", "deepseek-r1:7b", "qwen3:8b", "llama3:8b"}
    assert R.pick_model(avail, PRIMARY, "qwen2.5-coder:14b", []) is None


def test_pick_model_normalizes_latest_suffix():
    assert R.pick_model({"qwen3:14b-64k:latest"}, PRIMARY, None, []) == "qwen3:14b-64k:latest"
    assert R.pick_model({"deepseek-r1:7b:latest"}, PRIMARY, FALLBACK, []) == "deepseek-r1:7b:latest"


def test_pick_model_ignores_empty_config_entries():
    assert R.pick_model({ALT}, PRIMARY, None, [None, "", ALT]) == ALT


def test_resolve_model_accepts_declared_alt(monkeypatch):
    monkeypatch.setattr(R, "fetch_available_models",
                        lambda timeout=5: {"qwen2.5-7b-128k:latest", "deepseek-r1-7b-128k:latest"})
    # alt_models also declares qwen2.5-7b-128k, so this config DOES accept it
    assert R.resolve_model() == ALT


def test_resolve_model_none_when_nothing_configured_matches(monkeypatch):
    monkeypatch.setattr(R, "fetch_available_models", lambda timeout=5: {"llama3:8b"})
    assert R.resolve_model() is None


def test_resolve_model_none_when_tags_unreachable(monkeypatch):
    def boom(timeout=5):
        raise OSError("connection refused")
    monkeypatch.setattr(R, "fetch_available_models", boom)
    assert R.resolve_model() is None


# ------------------------------------------------------------- thinking capability
def test_decide_think_thinking_model():
    send, why = R.decide_think(PRIMARY, True, {"completion", "tools", "thinking"})
    assert send is True and "thinking model" in why


def test_decide_think_non_thinking_model_is_omitted():
    """qwen2.5 has no thinking capability -> ollama 400s if we send think."""
    send, why = R.decide_think("qwen2.5-7b-128k:latest", True, {"completion", "tools"})
    assert send is False and "thinking" in why


def test_decide_think_unknown_capability_keeps_config_behaviour():
    send, _ = R.decide_think(PRIMARY, True, None)
    assert send is True


def test_decide_think_config_can_disable():
    send, why = R.decide_think(PRIMARY, False, {"thinking"})
    assert send is False and "disabled" in why


def test_build_body_sends_think_but_never_format_json():
    body = R.build_body(PRIMARY, "p", {"num_ctx": 65536}, "5m", True)
    assert body["think"] is True
    assert "format" not in body          # format=json + think=true -> bare {}
    assert body["options"] == {"num_ctx": 65536}
    assert body["keep_alive"] == "5m" and body["stream"] is False


def test_build_body_omits_think_for_non_thinking():
    assert "think" not in R.build_body(ALT, "p", {}, "5m", False)


# ------------------------------------------------------------------- call_ollama
class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, detail):
    return urllib.error.HTTPError("http://localhost:11434/api/generate", code,
                                  "Bad Request", {}, io.BytesIO(detail.encode()))


def test_call_ollama_sends_think_for_thinking_model(monkeypatch):
    bodies = []

    def urlopen(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        return _Resp({"response": "{}", "eval_count": 2})

    monkeypatch.setattr(R.urllib.request, "urlopen", urlopen)
    R.call_ollama("prompt", PRIMARY, supports_thinking={"completion", "thinking"})
    assert bodies[0]["think"] is True
    assert R._LAST_CALL["thinking"] is True


def test_call_ollama_omits_think_for_non_thinking_model(monkeypatch):
    bodies = []

    def urlopen(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        return _Resp({"response": '{"confidence": 0.9}'})

    monkeypatch.setattr(R.urllib.request, "urlopen", urlopen)
    R.call_ollama("prompt", ALT, supports_thinking={"completion", "tools"})
    assert "think" not in bodies[0]
    assert R._LAST_CALL["thinking"] is False


def test_call_ollama_retries_once_without_think_on_400(monkeypatch):
    """Belt & braces for ollama builds that don't advertise capabilities."""
    bodies = []

    def urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        bodies.append(body)
        if body.get("think"):
            raise _http_error(400, '{"error":"\\"m:latest\\" does not support thinking"}')
        return _Resp({"response": '{"confidence": 0.8}'})

    monkeypatch.setattr(R.urllib.request, "urlopen", urlopen)
    resp = R.call_ollama("prompt", "m:latest", supports_thinking=None)  # capability unknown
    assert len(bodies) == 2 and bodies[0]["think"] is True and "think" not in bodies[1]
    assert resp["response"].startswith("{")
    assert R._LAST_CALL["thinking"] is False


def test_call_ollama_propagates_unrelated_400(monkeypatch):
    def urlopen(req, timeout=None):
        raise _http_error(400, '{"error":"invalid options"}')

    monkeypatch.setattr(R.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        R.call_ollama("prompt", PRIMARY, supports_thinking={"thinking"})


# ------------------------------------------------- stale reasoner decision guard
def _coll(ts="2026-09-14T15:39:00Z"):
    return {"timestamp": ts, "docker": {"containers": []}}


def test_reasoner_decision_state_fresh_snapshot():
    ok, why = A.reasoner_decision_state({"timestamp": "2026-09-14T15:39:00Z", "confidence": 0.9},
                                        _coll())
    assert ok is True and why == ""


def test_reasoner_decision_state_detects_stale_snapshot():
    ok, why = A.reasoner_decision_state({"timestamp": "2026-09-14T12:39:00Z", "confidence": 0.85},
                                        _coll("2026-09-14T15:39:00Z"))
    assert ok is False and "12:39" in why and "15:39" in why


def test_reasoner_decision_state_rejects_missing_or_empty():
    assert A.reasoner_decision_state({}, _coll())[0] is False
    assert A.reasoner_decision_state({"confidence": 0.9}, _coll())[0] is False
    assert A.reasoner_decision_state(None, _coll())[0] is False


def test_reasoner_decision_state_tolerates_missing_collector_timestamp():
    """A collector doc without a timestamp must not disable the LLM half outright."""
    assert A.reasoner_decision_state({"timestamp": "2026-09-14T15:39:00Z"}, {})[0] is True


# ------------------------------------------------- extract_json resilience
def test_extract_json_plain_object():
    assert R.extract_json('{"confidence": 0.9}')["confidence"] == 0.9


def test_extract_json_fenced_block():
    raw = 'Here you go:\n```json\n{"confidence": 0.7, "warnings": ["a"]}\n```\n'
    assert R.extract_json(raw)["warnings"] == ["a"]


def test_extract_json_recovers_from_prose_wrapped_objects():
    """deepseek-r1 intermittently emits a scratch object before the real answer."""
    raw = ('Reviewing {"note": "scratch"}\nFinal answer:\n'
           '{"confidence": 0.82, "summary": "ok", "actions": []}')
    assert R.extract_json(raw)["confidence"] == 0.82


def test_extract_json_prefers_latest_decision_object():
    raw = ('{"confidence": 0.1, "summary": "first"}\n{"confidence": 0.9, "summary": "second"}')
    assert R.extract_json(raw)["summary"] == "second"


def test_extract_json_raises_without_object():
    with pytest.raises(ValueError):
        R.extract_json("no json here at all")
