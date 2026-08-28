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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from manual_stops import ManualStops  # noqa: E402

log = get_logger("actions")

STATE_FILE = REPO / "logs" / "action_state.json"
NOTIF_FILE = REPO / "logs" / "notifications.jsonl"


def _manual_stops_path():
    if Cfg.get("paths.manual_stops"):
        return Cfg.resolve("paths.manual_stops")
    return REPO / "logs" / "manual_stops.json"


def load_manual_stops() -> ManualStops:
    return ManualStops(_manual_stops_path())


def manual_stop_enabled() -> bool:
    return bool(Cfg.get("manual_stop_protection.block_restart_for_manual_stops", True))


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
    if not wl:
        return True
    low = str(name).lower()
    return any(str(w).lower() == low for w in wl)


def allow_service(unit):
    wl = Cfg.get("actions.allow_service_restart", []) or []
    return unit in wl


class Engine:
    """Per-run executor with safety gates."""

    def __init__(self, dry_run, manual_stops=None, protected_stopped=None):
        self.dry_run = dry_run
        self.cap = int(Cfg.get("actions.restart_limit_per_run", 3))
        self.used = 0
        self.executed = []
        self.skipped = []
        self.blocked = []
        self.manual_stops = manual_stops   # ManualStops registry or None
        self.manual_stop_enabled = manual_stop_enabled()
        self.manual_block_count = 0
        # names of protected containers that are CURRENTLY stopped (avoids prune
        # deleting them — docker prune has no per-name exclusion)
        self.protected_stopped = set(protected_stopped or [])

    def _rec(self, verb, target, reason):
        return {"verb": verb, "target": target, "reason": reason}

    def _is_manual_stop(self, target):
        if not self.manual_stop_enabled or self.manual_stops is None:
            return False
        return self.manual_stops.is_protected(target)

    def dispatch(self, verb, target, reason):
        rec = self._rec(verb, target, reason)
        if verb == "docker_restart":
            # HARD INVARIANT (step 1, before every other gate): never restart a
            # container the user manually stopped. Overrides allow-list, cap,
            # confidence gating, and Qwen/deterministic recommendations.
            if self._is_manual_stop(target):
                rec["state"] = "blocked"
                rec["reason"] = "blocked_manual_stop"
                rec["proposed_reason"] = str(reason or "")
                rec["detail"] = f"manually stopped container {target} is protected — restart suppressed"
                self.blocked.append(rec)
                self.manual_block_count += 1
                notify(f"Manual-stop protection BLOCKED restart of {target}", "manual_stop_protection")
                return rec
            # Hard guard: never auto-restart ollama (would drop local inference).
            if str(target).lower() == "ollama":
                rec["state"] = "blocked"
                rec["detail"] = "ollama restart forbidden by policy"
                self.blocked.append(rec)
                return rec
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
            # HARD INVARIANT: `docker system prune -af` deletes stopped containers,
            # including manually-stopped (protected) ones. Docker prune has no
            # per-name exclusion, so block when any protected container is stopped.
            if self.manual_stop_enabled and self.protected_stopped:
                rec["state"] = "blocked"
                rec["reason"] = "blocked_manual_stop"
                rec["detail"] = (f"prune blocked: {len(self.protected_stopped)} manually-stopped "
                                 f"container(s) protected: {', '.join(sorted(self.protected_stopped))}")
                self.blocked.append(rec)
                self.manual_block_count += 1
                notify(f"Prune blocked — {len(self.protected_stopped)} manually-stopped "
                       f"container(s) protected", "manual_stop_protection")
                return rec
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
        # ---- federation / cluster-level verbs (NOTIFY ONLY, no remediation) ----
        if verb in ("notify_cluster", "escalate_cluster", "cluster_health_warning"):
            if not Cfg.get("federation.enabled", False):
                rec["state"] = "blocked"
                rec["detail"] = f"federation disabled; cannot {verb}"
                self.blocked.append(rec)
                return rec
            catmap = {"notify_cluster": "cluster_notify",
                      "escalate_cluster": "cluster_escalate",
                      "cluster_health_warning": "cluster_health_warning"}
            notify(str(target or reason), catmap.get(verb, "cluster"))
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
            procs = coll.get("gpu", {}).get("compute_processes", []) or []
            if procs and procs[0].get("pid"):
                name = str(procs[0].get("name") or "").lower()
                # never auto-kill the ollama/llama-server runner
                if not any(m in name for m in OLLAMA_MARKERS):
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


def _gpu_drift_events_path():
    return Cfg.resolve("paths.gpu_drift_events") if Cfg.get("paths.gpu_drift_events") else REPO / "logs" / "gpu_drift_events.jsonl"


def _gpu_daily_stats_path():
    return Cfg.resolve("paths.gpu_daily_stats") if Cfg.get("paths.gpu_daily_stats") else REPO / "logs" / "gpu_daily_stats.json"


def record_gpu_event(flags, gpu, pid="", coll_ts=None):
    """Append a GPU-drift event to the rolling 24h log. Returns the event dict."""
    ev = {
        "ts": time.time(),
        "timestamp": coll_ts,
        "flags": list(flags),
        "vram_mb": (gpu or {}).get("mem_used_mb"),
        "temp_c": (gpu or {}).get("temp_c"),
        "power_w": (gpu or {}).get("power_w"),
        "pid": pid or "",
    }
    path = _gpu_drift_events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(ev) + "\n")
    return ev


OLLAMA_MARKERS = ("ollama", "llama-server", "llama_server")


def _ollama_owned(procs, pid):
    """True if `pid` belongs to ollama's runner (a permanent GPU resident)."""
    try:
        pid = str(pid)
    except Exception:
        return False
    for p in procs or []:
        if str(p.get("pid", "")) == pid:
            name = str(p.get("name", "")).lower()
            return any(m in name for m in OLLAMA_MARKERS)
    return False


def _killable_stuck_pid(procs, last_pid):
    """Return the pid to safely kill for a stuck GPU process, or None.

    Safety: only returns a pid that (a) is the STUCK pid tracked by the baseline
    (last_pid) — not blindly procs[0] — and (b) is still present in compute
    processes, and (c) is NOT an ollama/llama-server process (never kill the local
    inference runner)."""
    try:
        last_pid = str(last_pid)
    except Exception:
        last_pid = ""
    for p in procs or []:
        if str(p.get("pid", "")) == last_pid:
            name = str(p.get("name", "")).lower()
            if any(m in name for m in OLLAMA_MARKERS):
                return None
            return last_pid
    return None


def update_gpu_daily_stats(gpu, drift_event_count, remediations, stuck_pids=None):
    """Roll a per-day max-vram/temp + remediations + stuck-pid accumulator."""
    path = _gpu_daily_stats_path()
    stats = read_json(path, {}) or {}
    today = time.strftime("%Y-%m-%d")
    if stats.get("date") != today:
        stats = {"date": today, "max_vram_mb": 0, "max_temp_c": 0,
                 "drift_event_count": 0, "stuck_pids": [], "remediations": []}
    if gpu:
        try:
            stats["max_vram_mb"] = max(stats.get("max_vram_mb", 0), int(gpu.get("mem_used_mb") or 0))
            stats["max_temp_c"] = max(stats.get("max_temp_c", 0), int(gpu.get("temp_c") or 0))
        except (TypeError, ValueError):
            pass
    stats["drift_event_count"] = int(stats.get("drift_event_count", 0)) + drift_event_count
    if stuck_pids:
        merged = list(dict.fromkeys(list((stats.get("stuck_pids") or [])) + list(stuck_pids)))
        stats["stuck_pids"] = merged
    for r in remediations:
        stats.setdefault("remediations", []).append(r)
    write_json(path, stats)
    return stats


def gpu_drift_actions(coll, qwen, conf, engine, st):
    """GPU-drift remediation per policy.

    - stuck_process + confidence>0.8  -> kill the GPU PID + notify
    - vram_drift | power_drift | temp_drift | vram_overload -> notify only
    - ollama is never restarted (enforced in Engine.dispatch).

    Returns (events logged this cycle, remediations applied)."""
    NOTIFY_ONLY = {"vram_drift", "power_drift", "temp_drift", "vram_overload"}

    # Union of Qwen's reasoned flags and the collector's deterministic flags.
    flags = set()
    for f in list(qwen.get("gpu_drift", [])) or []:
        flags.add(str(f))
    for f in (coll.get("gpu", {}).get("drift_flags", []) or []):
        flags.add(str(f))
    # drop unknown flags defensively
    flags = {f for f in flags if f in NOTIFY_ONLY | {"stuck_process"}}

    if not flags:
        return [], []

    gpu = (coll.get("gpu", {}).get("gpus") or [{}])[0]
    procs = coll.get("gpu", {}).get("compute_processes", []) or []
    baseline = coll.get("gpu", {}).get("baseline", {}) or {}
    pid = str(procs[0].get("pid") or "0") if procs else "0"
    events, remediations = [], []
    stuck_pids = list(st.get("gpu_stuck_pids") or [])

    stuck = "stuck_process" in flags
    tracked = pid
    if stuck:
        # Kill ONLY the baseline-tracked stuck pid, and only if it's not ollama's
        # runner and still present (see _killable_stuck_pid).
        kill_pid = _killable_stuck_pid(procs, baseline.get("last_pid"))
        tracked = kill_pid or pid
        if _ollama_owned(procs, tracked):
            # Ollama's llama-server is a PERMANENT GPU resident (the local
            # inference backend) — never a stuck process. Defensive drop: a stale
            # collector or a baseline predating the collector-side exclusion can
            # still surface the flag; suppress it here so we never notify/spam or
            # accumulate its pid as "stuck".
            flags.discard("stuck_process")
            stuck = False
        elif conf > 0.8 and kill_pid:
            rec = engine.dispatch("gpu_kill", kill_pid, "stuck GPU process (drift)")
            remediations.append(rec)
            notify(f"GPU stuck_process: killed pid {kill_pid} (conf {conf:.2f})", "gpu_drift")
        else:
            notify(f"GPU stuck_process detected (pid {tracked or '?'}, conf {conf:.2f}) "
                   "-> notify only", "gpu_drift")

    for f in sorted(flags & NOTIFY_ONLY):
        notify(f"GPU drift flag: {f}", "gpu_drift")
        remediations.append({"verb": "notify", "target": f})

    if flags:  # all flags may have been dropped (e.g. ollama-owned stuck_process)
        event = record_gpu_event(sorted(flags), gpu, tracked if stuck else pid, coll.get("timestamp"))
        events.append(event)
    if stuck and tracked and tracked != "0":
        stuck_pids = list(dict.fromkeys(stuck_pids + [tracked]))
        st["gpu_stuck_pids"] = stuck_pids
    update_gpu_daily_stats(gpu, len(events), remediations, stuck_pids)
    return events, remediations


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


def federation_decisions(engine, rec):
    """Dispatch cluster-level recommendations (NOTIFY-ONLY: never cross-node
    remediation). Loads cluster_reasoner_result.json and enqueues every
    recommendation via the cluster verbs. Federation must be enabled."""
    if not Cfg.get("federation.enabled", False):
        return []
    path = Cfg.resolve("paths.cluster_reasoner") if Cfg.get("paths.cluster_reasoner") else REPO / "logs" / "cluster_reasoner_result.json"
    if not path.exists():
        return []
    try:
        cr = read_json(path, {})
    except Exception:
        return []
    results = []
    for r in cr.get("recommendations", []) or []:
        typ = str(r.get("type", "cluster_health_warning"))
        if typ not in ("notify_cluster", "escalate_cluster", "cluster_health_warning"):
            continue
        target = str(r.get("target") or cr.get("cluster_stability_score") or "cluster")
        reason = str(r.get("reason") or cr.get("summary") or "cluster signal")
        results.append(engine.dispatch(typ, target, reason))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true", help="force no-op")
    args = ap.parse_args()
    Cfg.load(args.config)
    dry = args.dry_run or bool(Cfg.get("actions.dry_run", True))

    coll = read_json(Cfg.resolve("paths.collector_json"), {})
    qwen = read_json(Cfg.resolve("paths.reasoner_json"),
                     {"warnings": [], "actions": [], "summary": "", "confidence": 0.0, "manual_stops": []})
    st = load_state()
    baseline = read_json(Cfg.resolve("paths.baseline_json"), {}) or {}

    conf = float(qwen.get("confidence", 0.0))
    floor = float(Cfg.get("actions.qwen_confidence_floor", 0.6))

    rule_actions, rule_warnings = deterministic_rules(coll, st, baseline)

    # ---- Manual Stop Protection: load the protected set once for this run ----
    reg = load_manual_stops()
    protected_names = reg.protected_names()
    # which protected containers are currently stopped (for the prune guard)?
    now_containers = coll.get("docker", {}).get("containers", [])
    protected_stopped = {c["name"] for c in now_containers
                         if c.get("manual_stop_protected") and c.get("state") == "exited"}

    engine = Engine(dry, manual_stops=reg, protected_stopped=protected_stopped)

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

    # GPU drift handling (gated internally: kill needs conf>0.8; else notify only)
    gpu_events, gpu_remediations = gpu_drift_actions(coll, qwen, conf, engine, st)

    # Federation: dispatch cluster-level recommendations (notify-only, gated).
    cluster_recs = federation_decisions(engine, None)

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
        "gpu_drift": {
            "flags": gpu_events[0]["flags"] if gpu_events else [],
            "events": gpu_events,
            "remediations": gpu_remediations,
            "killed_pids": [r["target"] for r in gpu_remediations
                            if isinstance(r, dict) and r.get("verb") == "gpu_kill"
                            and r.get("state") == "executed"],
        },
        "manual_stops": {
            "names": protected_names,
            "count": len(protected_names),
            "blocked_this_run": engine.manual_block_count,
        },
        "cluster": {
            "enabled": Cfg.get("federation.enabled", False),
            "recommendations": cluster_recs,
        },
    })
    log.info("actions: executed=%d skipped=%d blocked=%d gpu_events=%d manual_stop_blocks=%d (dry_run=%s)",
             len(engine.executed), len(engine.skipped), len(engine.blocked),
             len(gpu_events), engine.manual_block_count, dry)
    return 0


if __name__ == "__main__":
    main()