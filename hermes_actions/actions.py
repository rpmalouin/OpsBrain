"""
Ops Brain - hermes_actions remediation module.

Reads Qwen's structured decisions (logs/reasoner_result.json) and the collector
data, then executes SAFE remediation via the deterministic ACTION RULES.
Nothing runs for real unless actions.dry_run=false in config.

Deterministic RULES (thresholds in config/ops_brain.yaml):
  - container CPU   > 80% sustained 5 min   -> docker restart
  - container memory creep > 20% over base  -> docker restart
  - GPU memory > 90% with resident process  -> gpu_kill   (needs allow_gpu_kill)
  - container restart loop                  -> docker restart + notify
  - Netdata ACTIVE alarms                    -> surfaced as warnings
  - disk usage > 85%                            -> docker prune + notify
  - Qwen confidence < 0.6                    -> do nothing, log only

Qwen's own actions run only when confidence >= floor, and every verb goes
through SAFETY gates: allow-lists, restart cap, dry-run.

Usage:
    python3 hermes_actions/actions.py [--config <path>] [--dry-run] [--no-rules]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, REPO, get_logger, read_json, write_json  # noqa: E402

log = get_logger("actions")

STATE_FILE = REPO / "logs" / "action_state.json"
NOTIF_FILE = REPO / "logs" / "notifications.jsonl"
ALLOWED_VERBS = {"docker_restart", "docker_prune", "service_restart", "gpu_kill", "notify"}


def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()}
    except Exception as e:
        return {"ok": False, "rc": -1, "out": "", "err": str(e)}


def pct(v):
    try:
        return float(str(v).strip().rstrip("%"))
    except Exception:
        return None


def load_state():
    return read_json(STATE_FILE, {}) or {}


def save_state(st):
    write_json(STATE_FILE, st)


def notify(message, category="action"):
    entry = {"ts": time.time(), "category": category, "message": message}
    NOTIF_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTIF_FILE, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    log.info("NOTIFY[%s] %s", category, message)
    url = Cfg.get("actions.notify_webhook", "") or ""
    if url:
        try:
            import urllib.request
            req = urllib.request.Request(url, data=json.dumps(entry).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
        except Exception as e:
            log.warning("notify webhook failed: %s", e)
    return entry


def allow_container(name):
    wl = Cfg.get("actions.allow_restart_containers", []) or []
    return (not wl) or name in wl


def allow_service(unit):
    wl = Cfg.get("actions.allow_service_restart", []) or []
    return unit in wl


class Engine:
    """Per-run executor with safety gates."""

    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.cap = int(Cfg.get("actions.restart_limit_per_run", 3))
        self.used = 0
        self.executed = []
        self.skipped = []
        self.blocked = []

    def _rec(self, verb, target, reason):
        return {"verb": verb, "target": target, "reason": reason}

    def dispatch(self, verb, target, reason):
        rec = self._rec(verb, target, reason)
        if verb == "docker_restart":
            if not allow_container(target):
                rec["state"] = "blocked"
                rec["detail"] = "container not in allow list"
                self.blocked.append(rec)
                return rec
            if self.used >= self.cap:
                rec["state"] = "blocked"
                rec["detail"] = "restart cap reached"
                self.blocked.append(rec)
                return rec
            self.used += 1
            if self.dry_run:
                rec["state"] = "skipped"
                rec["detail"] = f"[dry-run] docker restart {target}"
                self.skipped.append(rec)
                return rec
            r = run(["docker", "restart", target])
            rec["state"] = "executed"
            rec["detail"] = r["out"] or ("ERR: " + r["err"]) if not r["ok"] else r["out"]
            self.executed.append(rec)
            return rec
        if verb == "service_restart":
            if not allow_service(target):
                rec["state"] = "blocked"
                rec["detail"] = "service not in allow list"
                self.blocked.append(rec)
                return rec
            if self.used >= self.cap:
                rec["state"] = "blocked"
                rec["detail"] = "restart cap reached"
                self.blocked.append(rec)
                return rec
            self.used += 1
            if self.dry_run:
                rec["state"] = "skipped"
                rec["detail"] = f"[dry-run] systemctl restart {target}"
                self.skipped.append(rec)
                return rec
            r = run(["systemctl", "restart", target])
            rec["state"] = "executed"
            rec["detail"] = r["out"] if r["out"] else r["err"]
            self.executed.append(rec)
            return rec
        if verb == "gpu_kill":
            if not Cfg.get("actions.allow_gpu_kill", False):
                rec["state"] = "blocked"
                rec["detail"] = "gpu kill disabled"
                self.blocked.append(rec)
                return rec
            if self.dry_run:
                rec["state"] = "skipped"
                rec["detail"] = f"[dry-run] kill -9 {target}"
                self.skipped.append(rec)
                return rec
            r = run(["kill", "-9", str(target)])
            rec["state"] = "executed"
            rec["detail"] = r["out"] if r["out"] else r["err"]
            self.executed.append(rec)
            return rec
        if verb == "docker_prune":
            if not Cfg.get("actions.allow_prune", True):
                rec["state"] = "blocked"
                rec["detail"] = "prune disabled"
                self.blocked.append(rec)
                return rec
            if self.dry_run:
                rec["state"] = "skipped"
                rec["detail"] = "[dry-run] docker system prune -af"
                self.skipped.append(rec)
                return rec
            r = run(["docker", "system", "prune", "-af"])
            rec["state"] = "executed"
            rec["detail"] = r["out"] if r["out"] else r["err"]
            self.executed.append(rec)
            return rec
        if verb == "notify":
            notify(target, reason)
            rec["state"] = "notified"
            rec["detail"] = target
            self.executed.append(rec)
            return rec
        rec["state"] = "blocked"
        rec["detail"] = f"unknown verb {verb}"
        self.blocked.append(rec)
        return rec


def deterministic_rules(coll, st, baseline):
    """Return (rule_actions, rule_warnings). Thresholds from config; state persisted in st."""
    rules = []
    warnings = []
    cpu_th = float(Cfg.get("actions.cpu_restart_threshold_percent", 80))
    cpu_mins = float(Cfg.get("actions.cpu_restart_minutes", 5))
    mem_th = float(Cfg.get("actions.mem_creep_threshold_percent", 20))
    gpu_th = float(Cfg.get("actions.gpu_mem_threshold_percent", 90))
    disk_th = float(Cfg.get("actions.disk_threshold_percent", 85))

    containers = coll.get("docker", {}).get("containers", [])

    # 1) sustained CPU (persisted across runs)
    now_cpu = {}
    for co in containers:
        name = co["name"]
        cpu = pct((co.get("stats") or {}).get("cpu_percent"))
        if cpu is None or co.get("state") != "running":
            continue
        now_cpu[name] = cpu
        key = "cpu_high:" + name
        if cpu > cpu_th:
            st[key] = st.get(key, 0) + int(Cfg.get("interval_seconds", 120))
            if st[key] >= cpu_mins * 60:
                rules.append({"type": "docker_restart", "target": name,
                              "reason": f"CPU {cpu:.0f}% sustained >{cpu_th:.0f}% for {cpu_mins:.0f}min"})
                st.pop(key, None)
        else:
            st.pop(key, None)

    # 2) memory creep over per-container baseline
    for co in containers:
        name = co["name"]
        mem = pct((co.get("stats") or {}).get("mem_percent"))
        if mem is None or co.get("state") != "running":
            continue
        base = baseline.get(name, {}).get("mem_percent")
        if base:
            creep = 100.0 * (mem - base) / max(base, 1e-3)
            if creep > mem_th:
                rules.append({"type": "docker_restart", "target": name,
                              "reason": f"memory creep {creep:.0f}% over baseline {base:.1f}%"})
        else:
            baseline.setdefault(name, {})["mem_percent"] = mem

    # 3) GPU memory high + resident process
    for g in coll.get("gpu", {}).get("gpus", []):
        try:
            mem = float(g.get("mem_used_mb", 0)) / float(g.get("mem_total_mb", 1)) * 100
        except Exception:
            continue
        if mem > gpu_th:
            procs = coll.get("gpu", {}).get("compute_processes", [])
            if procs and procs[0].get("pid"):
                rules.append({"type": "gpu_kill", "target": str(procs[0]["pid"]),
                              "reason": f"GPU mem {mem:.0f}% > {gpu_th:.0f}% with resident process"})

    # 4) restart loops
    for name in coll.get("docker", {}).get("restarting", []) or []:
        rules.append({"type": "docker_restart", "target": name, "reason": "restart loop detected"})
        warnings.append(f"container {name} in restart loop")

    # 5) Netdata alarms -> warnings
    for a in (coll.get("netdata", {}).get("alarms_active", []) or [])[:10]:
        warnings.append(f"netdata[{a.get('status')}] {a.get('name')} ({a.get('component')})")

    # 6) disk -> prune + notify warning
    try:
        disk = float(coll.get("vm", {}).get("disk_used_percent"))
    except (TypeError, ValueError):
        disk = None
    if disk is not None and disk > disk_th:
        rules.append({"type": "docker_prune", "target": "", "reason": f"disk {disk:.0f}% > {disk_th:.0f}%"})
        warnings.append(f"disk usage {disk:.0f}% over {disk_th:.0f}% threshold")

    return rules, warnings


def _merge_warnings(qwen_warnings, rule_warnings):
    # dedupe: combine qwen and rule warnings in stable order
    seen = set()
    merged = []
    for item in list(qwen_warnings) + list(rule_warnings):
        key = str(item)[:160]
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true", help="force no-op")
    args = ap.parse_args()
    Cfg.load(args.config)
    dry = args.dry_run or bool(Cfg.get("actions.dry_run", True))

    coll = read_json(Cfg.resolve("paths.collector_json"), {})
    qwen = read_json(Cfg.resolve("paths.reasoner_json"),
                     {"warnings": [], "actions": [], "summary": "", "confidence": 0.0})
    st = load_state()
    baseline = read_json(Cfg.resolve("paths.baseline_json"), {}) or {}

    conf = float(qwen.get("confidence", 0.0))
    floor = float(Cfg.get("actions.qwen_confidence_floor", 0.6))

    rule_actions, rule_warnings = deterministic_rules(coll, st, baseline)

    engine = Engine(dry)

    if conf >= floor:
        proposed = list(qwen.get("actions", [])) or []
        proposed += rule_actions
    else:
        # confidence below floor: skip Qwen's decisions, still honour the
        # deterministic safety rules (restart loops, disk prune, GPU) and log.
        log.info("confidence %.2f < floor %.2f -> Qwen actions suppressed (log only)", conf, floor)
        proposed = rule_actions

    # exec all proposed (dedupe identical container restarts)
    seen = set()
    for a in proposed:
        verb = str(a.get("type", ""))
        target = str(a.get("target", ""))
        dedup = verb + ":" + target
        if dedup in seen:
            continue
        seen.add(dedup)
        engine.dispatch(verb, target, a.get("reason", ""))

    save_state(st)
    write_json(Cfg.resolve("paths.baseline_json"), baseline)
    write_json(Cfg.resolve("paths.actions_json"), {
        "timestamp": coll.get("timestamp"),
        "dry_run": dry,
        "confidence": conf,
        "confidence_floor": floor,
        "qwen_actions": qwen.get("actions", []),
        "rule_actions": rule_actions,
        "warnings": _merge_warnings(qwen.get("warnings", []), rule_warnings),
        "summary": qwen.get("summary", ""),
        "executed": engine.executed,
        "skipped": engine.skipped,
        "blocked": engine.blocked,
    })
    log.info("actions: executed=%d skipped=%d blocked=%d (dry_run=%s)",
             len(engine.executed), len(engine.skipped), len(engine.blocked), dry)
    return 0


if __name__ == "__main__":
    main()