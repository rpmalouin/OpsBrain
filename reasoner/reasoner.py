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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, REPO, get_logger, read_json, write_json  # noqa: E402

log = get_logger("reasoner")

TEMPLATE_PATH = REPO / "reasoner" / "prompt.txt"
ALLOWED_VERBS = {"docker_restart", "docker_prune", "service_restart", "gpu_kill", "notify"}


def resolve_model():
    """Pick qwen3:14b (requested), else fallback, else any qwen2.5 model."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode())
        avail = {m["name"] for m in tags.get("models", [])}
        prim = Cfg.get("ollama.model", "qwen3:14b")
        fallback = Cfg.get("ollama.fallback_model", "qwen2.5-coder:14b")
        if prim in avail:
            return prim
        if fallback in avail:
            log.info("primary model %s absent, using fallback %s", prim, fallback)
            return fallback
        for m in tags.get("models", []):
            if "qwen" in m["name"].lower():
                log.info("using available qwen model %s", m["name"])
                return m["name"]
        raise RuntimeError("no usable qwen model in ollama")
    except Exception as e:
        log.warning("model resolution failed: %s", e)
        return None


def render_prompt(collector_json):
    tpl = Path(TEMPLATE_PATH).read_text()
    return tpl.replace("{{ collector_json }}", collector_json) \
              .replace("{host}", Cfg.get("hostname", "dockerVM"))


def call_ollama(prompt, model):
    import urllib.request
    url = str(Cfg.get("ollama.url", "http://localhost:11434/api/generate"))
    opts = dict(Cfg.get("ollama.options", {}) or {})
    keep = opts.pop("keep_alive", None) or "5m"
    # NOTE: do NOT pass format=json alongside think=true for qwen3 — it makes the
    # model short-circuit to an empty {} . The prompt already forces JSON output.
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": opts, "keep_alive": keep, "think": bool(Cfg.get("ollama.think", True))}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(Cfg.get("ollama.timeout", 120))) as r:
        return json.loads(r.read().decode())


def extract_json(raw):
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.M)
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    cand = s[start:end + 1]
    try:
        return json.loads(cand)
    except Exception:
        raise ValueError("could not parse JSON from model output")


def sanitize(obj):
    out = {
        "warnings": list(obj.get("warnings", [])) if isinstance(obj.get("warnings", []), list) else [],
        "summary": str(obj.get("summary", ""))[:500],
        "confidence": min(1.0, max(0.0, float(obj.get("confidence", 0.0)))),
        "actions": [],
    }
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
    model = args.model or os.environ.get("OPSBRAIN_MODEL") or resolve_model()
    if not model:
        log.error("no ollama model available; cannot reason")
        return 3

    prompt = render_prompt(json.dumps(risk_summary, indent=1))
    log.info("invoking %s via ollama (prompt %s chars)", model, len(prompt))
    try:
        resp = call_ollama(prompt, model)
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
                    "actions": [], "summary": f"parse error: {e}", "confidence": 0.0}
    decision["model"] = model
    decision["timestamp"] = collector.get("timestamp")
    out = Cfg.resolve("paths.reasoner_json")
    write_json(out, decision)
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
    vm = c.get("vm", {})
    out["vm"] = {"uptime_load": vm.get("uptime_load"), "disk_used_percent": vm.get("disk_used_percent"),
                 "memory": (vm.get("memory") or "")[:160],
                 "top_by_mem_top5": vm.get("top_by_mem_top5", [])[:5],
                 "syslog_error_count_2min": vm.get("syslog_error_count_2min"),
                 "syslog_last2min_error_lines": (vm.get("syslog_last2min_error_lines") or [])[:20]}
    return out


if __name__ == "__main__":
    sys.exit(main())