"""
Ops Brain - reasoner.

Loads the latest collector output, renders the Qwen prompt template, calls
Ollama (http://localhost:11434/api/generate) for Qwen3 14B local inference,
and normalizes the response into a strict structured decision doc.

Writes <repo>/logs/reasoner_result.json.

Usage:
    python3 reasoner/reasoner.py [--config <path>] [--collector <path>] [--raw]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, REPO, get_logger, read_json, write_json  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from manual_stops import ManualStops  # noqa: E402

log = get_logger("reasoner")

TEMPLATE_PATH = REPO / "reasoner" / "prompt.txt"
ALLOWED_VERBS = {"docker_restart", "docker_prune", "service_restart", "gpu_kill", "notify"}
GPU_DRIFT_FLAGS = {"vram_drift", "vram_overload", "stuck_process", "power_drift", "temp_drift"}
LOW_CONF_FILE = "reasoner_low_conf.jsonl"
RAW_KEEP_CHARS = 4000        # model text kept per below-floor record


def capture_low_confidence(resp, decision, model, snapshot, floor):
    """Keep the raw model text + eval count for a below-floor decision.

    A decision under the action floor suppresses the LLM half for that cycle, and the only
    artefact it left behind was `confidence: 0.0` — the raw text was never kept (only the
    manual ``--raw`` flag saved it, and the scheduler never passes it), so a degenerate
    answer could not be read back afterwards. Measured 2026-10-10: the live reasoner
    answered `0.00` with an "all healthy, no findings" summary on a digest that yields
    0.75-0.85 in fresh runs on the same input, and across 6,243 logged cycles 7.2 % sat
    below the floor (1-5/day since the hourly retune). This appends one record per such
    cycle — the parse-failure path (also confidence 0.0) included, which is what makes the
    difference between "the model said nothing useful" and "we could not read it"
    decidable later. Diagnostic only: it changes no decision and notifies nobody.
    """
    raw = str(resp.get("response", "") or "")
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "snapshot": snapshot,
        "model": model,
        "thinking": bool(_LAST_CALL.get("thinking")),
        "confidence": decision.get("confidence"),
        "floor": floor,
        "eval_count": resp.get("eval_count"),
        "warnings": len(decision.get("warnings") or []),
        "actions": len(decision.get("actions") or []),
        "summary": str(decision.get("summary") or "")[:500],
        "raw_chars": len(raw),
        "raw": raw[:RAW_KEEP_CHARS],
    }
    path = REPO / "logs" / LOW_CONF_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError as e:
        log.warning("could not append %s: %s", LOW_CONF_FILE, e)
    return rec


def load_manual_stop_names():
    path = Cfg.resolve("paths.manual_stops") if Cfg.get("paths.manual_stops") else REPO / "logs" / "manual_stops.json"
    if not path.exists():
        return []
    try:
        return ManualStops(path).protected_names()
    except Exception:
        return []


# ------------------------------------------------------------ model / ollama plumbing
# Model choice and the `think` flag are decided PER MODEL, from ollama itself:
#   GET /api/tags  -> which configured model is installed
#   POST /api/show -> capabilities, incl. "thinking"
# Two hard-won facts this replaces (Sep 2026 model swap):
#  1. think=true is MANDATORY for thinking models here (without it this ollama
#     short-circuits a large prompt to a bare {}), and FATAL for non-thinking ones
#     (HTTP 400 "does not support thinking") — so it cannot be a global switch.
#  2. A loose "any qwen" resolution silently ran the pipeline on an unconfigured 7B
#     after the operator removed the primary model. Only config-named models are ever
#     used now; otherwise the stage fails loudly (rc=3).
_CAP_CACHE = {}
_LAST_CALL = {}


def _api_base():
    url = str(Cfg.get("ollama.url", "http://localhost:11434/api/generate"))
    return url.rsplit("/api/", 1)[0] if "/api/" in url else "http://localhost:11434"


def fetch_available_models(timeout=5):
    """Names installed in ollama (GET /api/tags)."""
    with urllib.request.urlopen(f"{_api_base()}/api/tags", timeout=timeout) as r:
        return {m["name"] for m in json.loads(r.read().decode()).get("models", [])}


def fetch_capabilities(model, timeout=5):
    """Capability set from /api/show — {'completion','tools','thinking',...}.

    None means ollama could not tell us (older build, missing model); callers then
    keep the configured behaviour and rely on the 400 retry rather than guessing.
    """
    if model in _CAP_CACHE:
        return _CAP_CACHE[model]
    caps = None
    try:
        req = urllib.request.Request(f"{_api_base()}/api/show",
                                     data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read().decode()).get("capabilities")
        caps = set(raw) if isinstance(raw, list) else None
    except Exception as e:
        log.warning("capability probe failed for %s: %s", model, e)
    _CAP_CACHE[model] = caps
    return caps


def _norm(name):
    """Normalize a tag for comparison: 'qwen3:14b:latest' == 'qwen3:14b'."""
    n = str(name or "").strip()
    return n[:-len(":latest")] if n.endswith(":latest") else n


def pick_model(avail, primary, fallback=None, alts=()):
    """Pick ONLY from models the config names, in priority order; else None.

    Never substitute an unvetted model: reasoning with a model nobody chose is worse
    than failing the stage where it is visible in the log.
    """
    by_norm = {}
    for name in avail or []:
        by_norm.setdefault(_norm(name), name)
    for idx, want in enumerate([primary, fallback, *list(alts or [])]):
        if not want:
            continue
        hit = by_norm.get(_norm(want))
        if hit:
            if idx == 1:
                log.info("primary model %s absent, using configured fallback %s", primary, hit)
            elif idx > 1:
                log.info("primary/fallback absent, using configured alt model %s", hit)
            return hit
    return None


def resolve_model():
    """Configured primary, else configured fallback/alt, else None (loud failure)."""
    try:
        avail = fetch_available_models()
    except Exception as e:
        log.warning("model resolution failed: %s", e)
        return None
    primary = Cfg.get("ollama.model", "qwen3:14b")
    fallback = Cfg.get("ollama.fallback_model")
    alts = Cfg.get("ollama.alt_models", []) or []
    picked = pick_model(avail, primary, fallback, alts)
    if not picked:
        log.error("no configured ollama model installed (primary=%s fallback=%s alts=%s); "
                  "installed=%s", primary, fallback, list(alts), sorted(avail))
    return picked


def decide_think(model, cfg_think=True, supports_thinking=None):
    """(send_think, why) — send `think` only when the model can accept it.

    supports_thinking is either None (unknown -> keep sending, the 400 retry protects),
    a bool, or the capability set returned by /api/show.
    """
    if not cfg_think:
        return False, "think disabled in config"
    if supports_thinking is None:
        return True, "thinking capability unknown; sending think (400-retry protects)"
    if isinstance(supports_thinking, bool):
        supports = supports_thinking
    else:
        supports = "thinking" in set(supports_thinking)
    if not supports:
        return False, f"{model} has no 'thinking' capability (ollama would 400)"
    return True, "thinking model"


def build_body(model, prompt, opts, keep, send_think):
    """Body for /api/generate.

    Never add format=json: alongside think=true it makes this ollama short-circuit to a
    bare {} (prompt already mandates JSON). See MEMORY.md.
    """
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": opts, "keep_alive": keep}
    if send_think:
        body["think"] = True
    return body


def _post_ollama(url, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def call_ollama(prompt, model, supports_thinking=None):
    url = str(Cfg.get("ollama.url", "http://localhost:11434/api/generate"))
    opts = dict(Cfg.get("ollama.options", {}) or {})
    keep = opts.pop("keep_alive", None) or "5m"
    timeout = int(Cfg.get("ollama.timeout", 120))
    send_think, why = decide_think(model, bool(Cfg.get("ollama.think", True)),
                                   supports_thinking)
    body = build_body(model, prompt, opts, keep, send_think)
    log.info("ollama %s: think=%s (%s)", model, send_think, why)
    try:
        resp = _post_ollama(url, body, timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        # Belt & braces for ollama builds that don't advertise capabilities: the only
        # 400 we may safely reinterpret is "does not support thinking".
        if send_think and e.code == 400 and "think" in detail.lower():
            log.warning("model %s rejected think=true (%s); retrying without it",
                        model, detail.strip())
            body.pop("think", None)
            send_think = False
            resp = _post_ollama(url, body, timeout)
        else:
            log.error("ollama HTTP %s: %s", e.code, detail.strip())
            raise
    _LAST_CALL.clear()
    _LAST_CALL.update({"model": model, "thinking": send_think, "reason": why})
    return resp


def render_prompt(collector_json):
    tpl = Path(TEMPLATE_PATH).read_text()
    return tpl.replace("{{ collector_json }}", collector_json) \
              .replace("{host}", Cfg.get("hostname", "dockerVM"))


def _json_objects(s):
    """Yield balanced-brace substrings of s, in order of appearance."""
    depth, start = 0, None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield s[start:i + 1]


DECISION_KEYS = ("confidence", "summary", "actions", "warnings")


def extract_json(raw):
    """Recover the decision object from model output.

    Models fence their JSON, wrap it in prose, or emit a couple of objects in a row
    (deepseek-r1 does all three at different times). Try the widest {...} span first,
    then balanced objects — preferring one that carries a decision key, latest first.
    """
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    span = s[start:end + 1]
    objs = [o for o in _json_objects(s) if o != span]

    def _tries():
        yield span
        for o in reversed(objs):
            if any(k in o for k in DECISION_KEYS):
                yield o
        yield from reversed(objs)

    for cand in _tries():
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("could not parse JSON from model output")


def sanitize(obj):
    if not isinstance(obj, dict):
        obj = {}
    conf_raw = obj.get("confidence", 0.0)
    try:
        conf = float(conf_raw) if conf_raw is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    out = {
        "warnings": list(obj.get("warnings", [])) if isinstance(obj.get("warnings", []), list) else [],
        "summary": str(obj.get("summary", ""))[:500],
        "confidence": min(1.0, max(0.0, conf)),
        "gpu_drift": [],
        "manual_stops": [],
        "actions": [],
    }
    # carry only known GPU-drift flags
    gd = obj.get("gpu_drift", [])
    if isinstance(gd, list):
        out["gpu_drift"] = [str(f) for f in gd if str(f) in GPU_DRIFT_FLAGS]
    # carry the manual-stop list if the model echoed it (list of names)
    ms = obj.get("manual_stops", [])
    if isinstance(ms, list):
        out["manual_stops"] = [str(n) for n in ms if str(n).strip()]
    for a in obj.get("actions", []) or []:
        if not isinstance(a, dict):
            continue
        typ = str(a.get("type") or a.get("action") or "").strip()   # accept both keys
        if typ not in ALLOWED_VERBS:
            log.warning("dropping unknown action verb: %r", typ)
            continue
        out["actions"].append({
            "type": typ,
            "target": str(a.get("target") or a.get("arg") or a.get("message") or ""),
            "reason": str(a.get("reason", ""))[:200],
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--collector", default=None)
    ap.add_argument("--raw", action="store_true", help="also save raw model text")
    ap.add_argument("--model", default=None, help="override ollama model")
    args = ap.parse_args()

    Cfg.load(args.config)
    if args.model:
        os.environ["OPSBRAIN_MODEL"] = args.model

    collector_path = Path(args.collector) if args.collector else \
        Cfg.resolve("paths.collector_json")
    collector = read_json(collector_path, {})
    if not collector:
        log.error("collector data missing at %s — run collector first", collector_path)
        return 2

    risk_summary = summarize_collector(collector)
    # HARD INVARIANT: surface protected (manually-stopped) containers to Qwen so it
    # never proposes restarting them. (Enforcement is authoritative in hermes_actions.)
    manual_names = load_manual_stop_names()
    risk_summary["manual_stops"] = manual_names

    model = args.model or os.environ.get("OPSBRAIN_MODEL") or resolve_model()
    if not model:
        log.error("no configured ollama model available; cannot reason "
                  "(set ollama.model / fallback_model / alt_models in config)")
        return 3

    caps = fetch_capabilities(model)
    prompt = render_prompt(json.dumps(risk_summary, indent=1))
    log.info("invoking %s via ollama (prompt %s chars)", model, len(prompt))
    try:
        resp = call_ollama(prompt, model, supports_thinking=caps)
    except Exception as e:
        log.error("ollama call failed: %s", e)
        return 4
    raw = resp.get("response", "")
    if args.raw:
        write_json(REPO / "logs" / "reasoner_raw.json", {"raw": raw, "model": model,
                                                         "eval_count": resp.get("eval_count")})
    try:
        decision = sanitize(extract_json(raw))
    except Exception as e:
        log.error("decision parse failed: %s", e)
        decision = {"warnings": ["reasoner failed to parse Qwen output"],
                    "actions": [], "gpu_drift": [],
                    "summary": f"parse error: {e}", "confidence": 0.0}

    # HARD INVARIANT (defense-in-depth): never let Qwen propose a restart of a
    # manually-stopped container. The authoritative block lives in hermes_actions,
    # but self-censoring here keeps the decision doc clean too.
    if manual_names:
        protected = {n.lower() for n in manual_names}
        kept = []
        for a in decision.get("actions", []):
            tgt = str(a.get("target", "")).lower()
            if a.get("type") == "docker_restart" and tgt in protected:
                continue  # drop the proposal
            kept.append(a)
        decision["actions"] = kept

    decision["manual_stops"] = sorted(manual_names)
    decision["model"] = model
    # Which model actually answered and whether `think` was usable — the field that
    # would have made the Sep 2026 model-swap failure obvious in one glance.
    decision["thinking"] = bool(_LAST_CALL.get("thinking"))
    decision["timestamp"] = collector.get("timestamp")
    out = Cfg.resolve("paths.reasoner_json")
    write_json(out, decision)
    # Below the floor the LLM half is suppressed for this cycle, so nothing downstream will
    # show WHY: keep the raw text (and the eval count) for the after-the-fact read.
    floor_raw = Cfg.get("actions.qwen_confidence_floor", 0.6)
    floor = float(floor_raw) if isinstance(floor_raw, (int, float, str)) else 0.6
    conf = float(decision.get("confidence") or 0.0)
    if conf < floor:
        rec = capture_low_confidence(resp, decision, model, decision["timestamp"], floor)
        log.warning("confidence %.2f < floor %.2f — raw model text kept in logs/%s "
                    "(eval_count=%s, %s chars)", conf, floor,
                    LOW_CONF_FILE, rec["eval_count"], rec["raw_chars"])
    log.info("decision: conf=%.2f warnings=%s actions=%s",
             decision["confidence"], len(decision["warnings"]), len(decision["actions"]))
    return 0


def trim(v, n=None):
    if isinstance(v, list):
        return v[:n] if n else v
    return v


def pct(v):
    """'2.31%' -> 2.31 or None."""
    try:
        return float(str(v).strip().rstrip("%"))
    except Exception:
        return None


def summarize_collector(c):
    """Build a compact risk digest so Qwen's context stays small (avoids the 9k-token short-circuit)."""
    out = {"host": c.get("host"), "timestamp": c.get("timestamp")}
    net = c.get("netdata", {})
    out["netdata"] = {"up": net.get("up"), "alarms_count": net.get("alarms_count", 0),
                      "alarms_active": net.get("alarms_active", [])[:30]}
    out["dozzle"] = {"up": c.get("dozzle", {}).get("up", False)}
    out["dockpeek"] = {"api_up": c.get("dockpeek", {}).get("up", False),
                       "container_running": c.get("dockpeek", {}).get("container_running")}

    dock = c.get("docker", {})
    containers = dock.get("containers", [])
    restarting = [co["name"] for co in containers if co.get("restarting")]
    unhealthy = [co["name"] for co in containers
                 if co.get("state") == "running" and "unhealthy" in (co.get("status") or "").lower()]
    with_loops = [co["name"] for co in containers if (co.get("restart_count") or 0) >= 3]
    # threshold anomalies
    hot = []
    for co in containers:
        st = co.get("stats") or {}
        cpu, mem = pct(st.get("cpu_percent")), pct(st.get("mem_percent"))
        if (cpu is not None and cpu > 80) or (mem is not None and mem > 90):
            hot.append({"name": co["name"], "cpu_percent": cpu, "mem_percent": mem})
    out["docker"] = {
        "containers_count": dock.get("containers_count"), "running": dock.get("running"),
        "restarting": restarting, "unhealthy": unhealthy,
        "restart_loops (>=3)": with_loops,
        "over_dev_containers": hot[:10],
        # only flag containers in a problematic state, not the full fleet
        "flagged_containers": [{"name": co["name"], "state": co.get("state"),
                                "restart_count": co.get("restart_count")}
                               for co in containers
                               if co.get("restarting") or co.get("state") == "exited"
                               or (co.get("restart_count") or 0) >= 2][:30],
    }
    out["gpu"] = c.get("gpu", {})
    # Dockhand desired-state drift — compact so it stays out of the token budget.
    dh = c.get("dockhand", {})
    if isinstance(dh, dict) and dh.get("up"):
        ctx = dh.get("context_nodes", {}) or {}
        dash = dh.get("dashboard", {}) or {}
        out["dockhand"] = {
            "up": True,
            "attention": ctx.get("attention"),
            "drift_summary": ctx.get("drift_summary"),
            "drift_count": dh.get("classify", {}).get("drift_count", 0),
            "state_drift": (dh.get("classify", {}) or {}).get("state_drift_items", [])[:10],
            "image_drift": (dh.get("classify", {}) or {}).get("image_drift_items", [])[:5],
            "restart_storms": (dh.get("correlate", {}) or {}).get("restart_storms", [])[:5],
            "health_flaps": (dh.get("correlate", {}) or {}).get("health_flaps", [])[:5],
            "orphaned": dash.get("orphaned_containers", [])[:10],
        }
    else:
        out["dockhand"] = {"up": bool(isinstance(dh, dict) and dh.get("up"))}
    # Hatchdoor vault drift — compact so it stays out of the token budget.
    vd = c.get("vault_drift", {})
    if isinstance(vd, dict) and vd.get("up"):
        ctx = vd.get("context_nodes", {}) or {}
        out["vault_drift"] = {
            "up": True,
            "attention": ctx.get("attention"),
            "drift_count": ctx.get("drift_count", 0),
            "broken_links": ctx.get("broken_links", [])[:6],
            "orphans": ctx.get("orphans", [])[:4],
            "stale": bool(vd.get("stale")),
        }
    else:
        out["vault_drift"] = {"up": bool(isinstance(vd, dict) and vd.get("up"))}
    # GPU driver reconcile: the apt hook's log + the LIVE module comparison. Carries the
    # state even when the log is unreadable, because a live mismatch is the actionable half.
    gr = c.get("gpu_reconcile", {})
    grc = (gr.get("context_nodes") or {}) if isinstance(gr, dict) else {}
    out["gpu_reconcile"] = {
        "up": bool(isinstance(gr, dict) and gr.get("up")),
        "state": grc.get("state"),
        "attention": grc.get("attention"),
        "loaded": grc.get("loaded"),
        "disk": grc.get("disk"),
        "last_event": grc.get("last_event_kind"),
        "holders": (grc.get("holders") or [])[:4],
        "remedy": (grc.get("remedy") or "")[:200],
    }
    vm = c.get("vm", {})
    out["vm"] = {"uptime_load": vm.get("uptime_load"), "disk_used_percent": vm.get("disk_used_percent"),
                 "memory": (vm.get("memory") or "")[:160],
                 "top_by_mem_top5": vm.get("top_by_mem_top5", [])[:5],
                 "syslog_error_count_2min": vm.get("syslog_error_count_2min"),
                 "syslog_last2min_error_lines": (vm.get("syslog_last2min_error_lines") or [])[:20]}
    return out


if __name__ == "__main__":
    sys.exit(main())