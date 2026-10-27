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
import re
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


def gpu_unavailable_notice(coll, st):
    """Notify (at most once per UTC day) when the collector can no longer see the GPU.

    GPU drift detection reads zeros when nvidia-smi fails, which looks exactly like a
    healthy GPU — the Sep 2026 driver/NVML mismatch kept this pipeline blind for days
    without a single signal. Only notify if a baseline proves a GPU used to be there.
    """
    g = coll.get("gpu", {}) or {}
    if g.get("available", bool(g.get("gpus"))):
        return False
    if not (g.get("baseline") or {}).get("gpu_name"):
        return False
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if st.get("gpu_unavailable_notified") == today:
        return False
    notify(f"GPU invisible to nvidia-smi ({(g.get('error') or 'no GPUs reported')[:180]}); "
           f"GPU drift detection is blind", "gpu_unavailable")
    st["gpu_unavailable_notified"] = today
    return True


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

    def __init__(self, dry_run, manual_stops=None, protected_stopped=None, storming=None,
                 gpu_kill_corroborated=None, prune_corroborated=None):
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
        # containers currently in a Dockhand restart storm (case-insensitive) —
        # restarting one of these would feed the very loop being detected
        self.storming = {str(s).lower() for s in (storming or [])}
        # Does the COLLECTOR's deterministic detector corroborate `stuck_process`
        # this cycle? `gpu_kill` is dangerous and the reasoner's LLM half has
        # proposed it off an idle, VRAM-holding model server; None means "not
        # supplied" (unit tests constructing an Engine directly stay permissive —
        # the allow_gpu_kill gate still applies).
        self.gpu_kill_corroborated = gpu_kill_corroborated
        # Does the DETERMINISTIC disk-threshold rule fire this cycle? `docker_prune`
        # ignores its target and runs `docker system prune -af` on the whole host —
        # every stopped container, every unused image, the build cache — so a proposal
        # made from model prose must not reach it. Measured 2026-10-27: the reasoner
        # read "Dockhand orphan: clamav not in any stack" and proposed
        # `docker_prune`/`clamav` at conf 0.95; the engine pruned the host. None means
        # "not supplied" (a directly constructed Engine stays permissive).
        self.prune_corroborated = prune_corroborated

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
            # Storm guard: a container already bouncing (die/destroy/restart
            # storms detected from dockhand events) must not be restarted again —
            # each restart re-arms the storm detector and the system would loop
            # restarting it every cycle (observed: qwen restarted Firefox for
            # "4 restarts in 30m" + high CPU, which produced 5 more restarts).
            if str(target).lower() in self.storming:
                rec["state"] = "blocked"
                rec["reason"] = "blocked_restart_storm"
                rec["detail"] = ("container currently in a detected restart storm — "
                                 "autonomous restart suppressed (would feed the loop)")
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
            # Second layer (the first is in gpu_drift_actions): a kill must rest on
            # the collector's deterministic `stuck_process` flag. Qwen proposes
            # `gpu_kill` directly from its own action list, and an LLM-only verdict
            # is not grounds to SIGKILL a healthy GPU process.
            if self.gpu_kill_corroborated is False:
                rec["state"] = "blocked"
                rec["reason"] = "blocked_no_stuck_evidence"
                rec["detail"] = ("collector did not flag stuck_process this cycle "
                                 "(LLM-only proposal)")
                self.blocked.append(rec)
                return rec
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
            # A prune must rest on the deterministic disk-threshold rule: it is
            # fleet-wide, ignores `target`, and deletes stopped containers + unused
            # images + the build cache. An LLM proposal (typically reading an orphan
            # notice as "prune this") is not evidence of disk pressure.
            if self.prune_corroborated is False:
                rec["state"] = "blocked"
                rec["reason"] = "blocked_no_prune_evidence"
                rec["detail"] = ("disk usage did not cross the deterministic threshold "
                                 "this cycle (LLM-only proposal)")
                self.blocked.append(rec)
                return rec
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
            # Generic notify: target IS the message the caller wants shown, and the reason is
            # its explanation — so it must not be used as the CATEGORY (an LLM sentence there
            # made every such entry unclassified in the ticker).
            notify(str(target) or str(reason) or "notify", "action")
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
            notify(str(reason) or str(target) or "cluster", catmap.get(verb, "cluster"))
            rec["state"] = "notified"
            rec["detail"] = target
            self.executed.append(rec)
            return rec
        # ---- Dockhand desired-state drift (NOTIFY ONLY, no remediation) ----
        if verb == "notify_dockhand_drift":
            notify(str(reason) or str(target) or "Dockhand drift", "dockhand_drift")
            rec["state"] = "notified"
            rec["detail"] = target
            self.executed.append(rec)
            return rec
        # ---- Vault drift (NOTIFY ONLY, never remediates the vault) ----
        if verb == "notify_vault_drift":
            notify(str(reason) or str(target) or "Vault drift", "vault_drift")
            rec["state"] = "notified"
            rec["detail"] = target
            self.executed.append(rec)
            return rec
        # ---- GPU driver/library reconcile (NOTIFY ONLY, never touches the driver) ----
        if verb == "notify_gpu_reconcile":
            notify(str(reason) or str(target) or "GPU driver reconcile", "gpu_reconcile")
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
                              "reason": f"CPU {cpu:.0f}% sustained >{cpu_th:.0f}% for {cpu_mins:.0f}min",
                              # Tags the rule's own verdict so downstream filters can
                              # treat it as the ONLY deterministic sustained-CPU evidence
                              # (see filter_llm_notices).
                              "evidence": "sustained_cpu"})
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
            # Floor the baseline at 1% of RAM: sub-1% baselines (e.g. dozzle at
            # 0.01%) amplify tiny fluctuations into noise restarts (0.02% vs 0.01%
            # = 100% "creep" → docker restart every 2-min cycle). With the floor,
            # creep means "growth of at least N percentage points of RAM", which is
            # the meaningful signal on a 94 GiB box. Baselines >= 1% are unaffected.
            creep = 100.0 * (mem - base) / max(base, 1.0)
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
        # `evidence` is what the Engine's prune gate looks for: without it a
        # `docker_prune` is an LLM proposal and is blocked (blocked_no_prune_evidence).
        rules.append({"type": "docker_prune", "target": "", "evidence": "disk_threshold",
                      "reason": f"disk {disk:.0f}% > {disk_th:.0f}%"})
        warnings.append(f"disk usage {disk:.0f}% over {disk_th:.0f}% threshold")

    return rules, warnings


# --------------------------------------------------------------- container-CPU notices
# A container's CPU reading is a SINGLE instantaneous `docker stats` sample per cycle —
# it says nothing about duration. Qwen read one 634% sample of the ollama container
# (a 20-45 s inference burst) as "consuming excessive CPU resources ... may require
# optimization or scaling", which is a fault claim the data cannot support. Only the
# deterministic sustained-CPU rule (cpu_restart_threshold_percent held for
# cpu_restart_minutes, which is exactly what tags evidence="sustained_cpu") can assert
# that, so an LLM notice about container CPU is dropped unless that rule fired for the
# same container in the same cycle.

CPU_TOKENS = ("cpu", "processor")
CPU_FAULT_TOKENS = ("excessive", "excessive usage", "high", "consuming", "consumption",
                    "utilization", "utilisation", "usage", "overload", "load",
                    "optimization", "optimisation", "scaling", "pegged", "spike",
                    "saturated", "saturation", "hogging", "throttl")


def container_named_in(message, containers):
    """Longest container name mentioned in `message`, or None.

    Word-boundary match on the container names present in the snapshot, so a notice
    naming an app the collector does not run cannot be gated (it also cannot be
    verified — Qwen is told to use the state)."""
    msg = str(message or "").lower()
    best = None
    for co in containers or []:
        name = str((co or {}).get("name") or "").lower()
        if len(name) < 3:
            continue
        if re.search(r"(?<![a-z0-9_.-])" + re.escape(name) + r"(?![a-z0-9_.-])", msg):
            if best is None or len(name) > len(best):
                best = name
    return best


def is_container_cpu_notice(message, containers):
    """True if `message` claims a container CPU problem (name + CPU token + fault token)."""
    msg = str(message or "").lower()
    if not any(t in msg for t in CPU_TOKENS):
        return False
    if not any(t in msg for t in CPU_FAULT_TOKENS):
        return False
    return container_named_in(msg, containers) is not None


def sustained_cpu_evidence(rule_actions):
    """Containers the deterministic sustained-CPU rule flagged THIS cycle."""
    return {str(a.get("target")) for a in (rule_actions or [])
            if a.get("evidence") == "sustained_cpu"}


# Tokens that make a notice an ORPHAN claim — `notify_dockhand_drift` already reports every
# orphan every cycle, so a model notice repeating one is a duplicate, not new information.
ORPHAN_CLAIM_TOKENS = ("orphan", "unused", "not in any stack", "missing container", "no longer")


def filter_llm_notices(actions, coll, sustained):
    """Drop LLM-authored notices that restate a condition the deterministic rules already report.

    Two claim classes so far:
      * a container-CPU fault with no sustained-CPU evidence (a single `docker stats` sample),
      * an orphan/unused-container notice — `dockhand_drift_actions` dispatches
        `notify_dockhand_drift` for each orphan every cycle, and the model re-reporting it just
        doubles the ticker (measured 2026-10-27: 2 of 3 runs still did, after the prompt told it
        not to).

    Returns (kept, dropped). Everything else passes through untouched: this gate is about claims
    the pipeline already owns, not about Qwen's freedom to report."""
    containers = coll.get("docker", {}).get("containers", []) or []
    dash = (coll.get("dockhand") or {}).get("dashboard") or {}
    orphaned = {str(n) for n in (dash.get("orphaned_containers") or [])}
    kept, dropped = [], []
    for a in actions or []:
        if str(a.get("type") or "") == "notify":
            msg = str(a.get("target") or "")
            name = container_named_in(msg, containers)
            if name and is_container_cpu_notice(msg, containers) and name not in sustained:
                dropped.append(a)
                continue
            # ...and the same orphan the dockhand rule already notified this cycle
            low = msg.lower()
            if name and name in orphaned and any(t in low for t in ORPHAN_CLAIM_TOKENS):
                dropped.append(a)
                continue
        kept.append(a)
    return kept, dropped


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


def _resident_pids(coll):
    """Configured permanent GPU residents recorded by the collector
    (`gpu_drift.resident_containers`) — model servers that hold VRAM forever and
    idle at 0% util between jobs. Never stuck candidates."""
    try:
        return {str(p) for p in (coll.get("gpu", {}).get("resident_pids") or [])}
    except Exception:
        return set()


def _process_name_of(procs, pid):
    try:
        pid = str(pid)
    except Exception:
        return ""
    for p in procs or []:
        if str(p.get("pid", "")) == pid:
            return str(p.get("name") or "")
    return ""


def _ollama_owned(procs, pid):
    """True if `pid` belongs to ollama's runner (a permanent GPU resident)."""
    name = _process_name_of(procs, pid).lower()
    return bool(name) and any(m in name for m in OLLAMA_MARKERS)


def _resident_owned(procs, pid, resident_pids):
    """True if `pid` belongs to a configured permanent GPU resident."""
    try:
        return str(pid) in {str(p) for p in (resident_pids or ())}
    except Exception:
        return False


def stuck_process_corroborated(coll):
    """True when the COLLECTOR's deterministic detector flagged `stuck_process`.

    `stuck_process` needs same-written-pid for `gpu_drift.stuck_pid_cycles` AND
    util > `gpu_drift.util_threshold`; the reasoner's LLM half has been observed to
    assert the flag off the alone (a 62-cycle-old idle GPU process holding VRAM was
    quoted as "stuck"), which would otherwise reach a kill proposal. The
    detector is the single source of truth for this flag — do not duplicate its
    thresholds here."""
    return "stuck_process" in (coll.get("gpu", {}).get("drift_flags") or [])


def _killable_stuck_pid(procs, last_pid, resident_pids=()):
    """Return the pid to safely kill for a stuck GPU process, or None.

    Safety: only returns a pid that (a) is the STUCK pid tracked by the baseline
    (last_pid) — not blindly procs[0] — and (b) is still present in compute
    processes, and (c) is NOT an ollama/llama-server process and NOT a configured
    permanent GPU resident (never kill a model server)."""
    try:
        last_pid = str(last_pid)
    except Exception:
        last_pid = ""
    for p in procs or []:
        if str(p.get("pid", "")) == last_pid:
            name = str(p.get("name", "")).lower()
            if any(m in name for m in OLLAMA_MARKERS):
                return None
            if _resident_owned(procs, last_pid, resident_pids):
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


def gpu_flags_corroborated(coll):
    """The collector's DETERMINISTIC drift flags — the only authority for drift claims.

    `gpu_drift_actions` used to union the reasoner's own `gpu_drift` list with this and
    notify on the union, so every flag the LLM asserted became a critical alert. The 7B
    re-derives flags from the digest's raw numbers with thresholds it invents (measured
    2026-10-27: "power draw is 8.93 W, which exceeds the idle max of 8.5 W" against a
    configured 40 W; "842 MB, which is 5.15% higher than the baseline of 800 MB" against
    a 250 MB creep rule and a real baseline of 842) — 106 of 249 event flag occurrences
    were impossible on their own recorded numbers, and 31 of 41 Telegram alerts in three
    weeks were the model's claim alone. Same rule as `stuck_process`: the detector
    decides, the model may only relay it.
    """
    try:
        return {str(f) for f in (coll.get("gpu", {}).get("drift_flags") or [])}
    except Exception:
        return set()


def _flag_measure(coll, flag):
    """The numbers behind a drift flag, so the alert is worth reading.

    A bare "GPU drift flag: vram_drift" says nothing actionable; the point of the flag
    is the size of the move, which needs the PREVIOUS baseline (`baseline_prev` — the
    collector's `baseline` block is already advanced to this cycle's values).
    """
    g = (coll.get("gpu", {}).get("gpus") or [{}])[0]
    prev = coll.get("gpu", {}).get("baseline_prev") or {}
    used, total = g.get("mem_used_mb"), g.get("mem_total_mb")
    util, temp, power = g.get("util_gpu_percent"), g.get("temp_c"), g.get("power_w")
    if flag == "vram_drift":
        was = prev.get("last_vram")
        if was in (None, "", 0) or used is None:
            return "vram %s MB" % used
        try:
            return "vram %s MB (was %s, +%s)" % (used, was, int(used) - int(was))
        except (TypeError, ValueError):
            return "vram %s MB (was %s)" % (used, was)
    if flag == "vram_overload":
        pct = ""
        if used is not None and total:
            try:
                pct = ", %.0f%%" % (100.0 * float(used) / float(total))
            except (TypeError, ValueError):
                pct = ""
        return "vram %s/%s MB%s" % (used, total, pct)
    if flag == "power_drift":
        return "%s W while util %s%%" % (power, util)
    if flag == "temp_drift":
        return "%s C while util %s%%" % (temp, util)
    return ""


def gpu_drift_actions(coll, qwen, conf, engine, st):
    """GPU-drift remediation per policy.

    - stuck_process + confidence>0.8  -> kill the GPU PID + notify
    - vram_drift | power_drift | temp_drift | vram_overload -> notify only
    - ollama is never restarted (enforced in Engine.dispatch).

    EVERY flag must be corroborated by the collector's own `drift_flags`: the reasoner's
    `gpu_drift` list is advisory and a flag that only the model asserts is logged and
    dropped (no notify, no event) — see gpu_flags_corroborated().

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

    # ...and drop anything the deterministic detector did not itself flag. Only
    # `stuck_process` used to be gated; the four notify-only flags were not, which is
    # how a healthy 842 MB / 9 W card produced hours of critical alerts. Note this also
    # keeps the suffix rule honest: a second card's `vram_overload:gpu1` never reaches
    # here, so a non-primary flag needs an explicit opt-in before it can act.
    corroborated = gpu_flags_corroborated(coll)
    llm_only = sorted(f for f in flags if f in NOTIFY_ONLY and f not in corroborated)
    if llm_only:
        log.info("dropping reasoner-only GPU drift flag(s) without deterministic evidence: "
                 "%s (collector drift_flags=%s)", llm_only, sorted(corroborated))
        flags -= set(llm_only)

    if not flags:
        return [], []


    gpu = (coll.get("gpu", {}).get("gpus") or [{}])[0]
    procs = coll.get("gpu", {}).get("compute_processes", []) or []
    baseline = coll.get("gpu", {}).get("baseline", {}) or {}
    resident_pids = _resident_pids(coll)
    pid = str(procs[0].get("pid") or "0") if procs else "0"
    events, remediations = [], []
    stuck_pids = list(st.get("gpu_stuck_pids") or [])

    stuck = "stuck_process" in flags
    tracked = pid
    if stuck:
        # The flag may arrive from the reasoner's LLM half alone. `stuck_process`
        # is a DETERMINISTIC verdict (same pid for stuck_pid_cycles AND util >
        # util_threshold) — Qwen asserting it off an idle, VRAM-holding process is
        # model noise, so drop it here (as with an ollama-owned flag): no notify,
        # no event, no state accumulation.
        if not stuck_process_corroborated(coll):
            log.info("stuck_process proposed by reasoner without deterministic evidence "
                     "(collector drift_flags=%s) -> dropped",
                     (coll.get("gpu", {}).get("drift_flags") or []))
            flags.discard("stuck_process")
            stuck = False
        else:
            # Kill ONLY the baseline-tracked stuck pid, and only if it's not
            # ollama's runner nor a configured resident (see _killable_stuck_pid).
            kill_pid = _killable_stuck_pid(procs, baseline.get("last_pid"), resident_pids)
            tracked = kill_pid or pid
            if _ollama_owned(procs, tracked) or _resident_owned(procs, tracked, resident_pids):
                # Ollama's llama-server and configured residents (e.g.
                # docling-serve) are PERMANENT GPU residents — the local inference
                # backend and the document model server. Never a stuck process.
                # Defensive drop: a stale collector or a baseline predating the
                # collector-side exclusion can still surface the flag; suppress it
                # so we never notify/spam or accumulate its pid as "stuck".
                flags.discard("stuck_process")
                stuck = False
            elif conf > 0.8 and kill_pid:
                rec = engine.dispatch("gpu_kill", kill_pid, "stuck GPU process (drift)")
                remediations.append(rec)
                # Report what the ENGINE did, not what we asked for: gpu_kill is
                # gated (allow_gpu_kill) and a refused kill must not be announced
                # as one that happened.
                if rec.get("state") == "executed":
                    notify(f"GPU stuck_process: killed pid {kill_pid} (conf {conf:.2f})", "gpu_drift")
                else:
                    why = rec.get("detail") or rec.get("reason") or rec.get("state")
                    notify(f"GPU stuck_process: kill pid {kill_pid} NOT applied "
                           f"({why}; conf {conf:.2f})", "gpu_drift")
            else:
                notify(f"GPU stuck_process detected (pid {tracked or '?'}, conf {conf:.2f}) "
                       f"-> notify only", "gpu_drift")

    for f in sorted(flags & NOTIFY_ONLY):
        detail = _flag_measure(coll, f)
        notify(f"GPU drift flag: {f}" + (f" ({detail})" if detail else ""), "gpu_drift")
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


def dockhand_drift_actions(coll, engine):
    """Surface Dockhand desired-state drift as NOTIFY-ONLY events.

    Dockhand tells us the desired state vs what's actually running (image /
    state / volume / policy drift, restart storms, health flaps, orphans). These
    are informational — the module ends at dashboard/reasoner context and never
    proposes a docker_restart, so Manual-Stop Protection and the allow-list are
    not circumnavigated. Returns a list of {verb, target, reason} records.
    """
    dh = coll.get("dockhand", {}) if isinstance(coll, dict) else {}
    if not isinstance(dh, dict) or not dh.get("up"):
        return []
    classified = dh.get("classify", {}) or {}
    correlated = dh.get("correlate", {}) or {}
    dash = dh.get("dashboard", {}) or {}
    results = []
    drift_count = int(classified.get("drift_count") or 0)
    if drift_count > 0:
        items = []
        for kind in ("state", "health", "image", "volume", "network", "dependency", "policy", "replica"):
            for it in classified.get(f"{kind}_drift_items") or []:
                svc = str(it.get("service") or "")
                cause = str(it.get("cause") or kind)
                items.append(f"{svc}:{cause}" if svc else cause)
        msg = f"Dockhand drift ({drift_count}): " + "; ".join(items[:6])
        # dispatch() owns the notification and uses the REASON as the ticker text, so every
        # reason below is self-contained — the target stays a name/list/count for the record
        # and must not be relied on as the message (NOTIFY[...] used to read "3"/"sonarr").
        results.append(engine.dispatch("notify_dockhand_drift", dash.get("stack_drift", []), msg))
    for s in correlated.get("restart_storms") or []:
        results.append(engine.dispatch(
            "notify_dockhand_drift", s.get("service"),
            f"Dockhand restart storm {s.get('service')}: {s.get('count')} in {s.get('window_min')}m"))
    for f in correlated.get("health_flaps") or []:
        results.append(engine.dispatch(
            "notify_dockhand_drift", f.get("service"),
            f"Dockhand health flap {f.get('service')}: {f.get('toggle_count')} toggles in {f.get('window_min')}m"))
    for name in (dash.get("orphaned_containers") or [])[:5]:
        results.append(engine.dispatch(
            "notify_dockhand_drift", name,
            f"Dockhand orphan: {name} not in any stack"))
    return results


def vault_drift_actions(coll, engine):
    """Surface Hatchdoor vault drift as NOTIFY-ONLY events.

    The vault-drift report is produced by Hermes' weekly cron; OpsBrain only
    consumes it and notifies. It NEVER remediates the vault (no file writes, no
    docker actions) — the vault lives behind Hatchdoor's sync engine and must
    not be touched here. Even with active findings, this only dispatches
    ``notify_vault_drift``. Returns a list of {verb, target, reason} records.
    """
    vd = coll.get("vault_drift", {}) if isinstance(coll, dict) else {}
    if not isinstance(vd, dict) or not vd.get("up"):
        return []
    context = vd.get("context_nodes", {}) or {}
    dash = vd.get("dashboard", {}) or {}
    total = int(context.get("drift_count") or 0)
    results = []
    if total > 0:
        summary = str(dash.get("summary") or f"{total} vault finding(s)")
        # Only report actionable categories; needs_review is by definition a
        # human decision and is surfaced in the digest, not notified.
        actionable = int(vd.get("classify", {}).get("counts", {}).get("broken_links", 0)) \
            + int(vd.get("classify", {}).get("counts", {}).get("orphans", 0))
        kind = "vault drift" if actionable else "vault notes"
        # dispatch() notifies with the reason (one ticker entry per event) — do not notify
        # again here, and keep the reason human-readable on its own.
        results.append(engine.dispatch("notify_vault_drift", total, f"Vault drift ({total}): {summary}"))
    if vd.get("stale"):
        results.append(engine.dispatch("notify_vault_drift", "stale",
                                       "Vault-drift report is stale; the weekly cron may not have run"))
    return results


def gpu_reconcile_actions(coll, engine, st):
    """Surface a LIVE GPU driver/library mismatch as NOTIFY-ONLY.

    The host's apt hook (`nvidia-module-reconcile`) repairs the loaded module after a
    driver upgrade UNLESS a GPU container holds the device — it then logs the mismatch
    plus the remedy and DEFERS, which is the state a human must act on. This overlaps
    `gpu_unavailable_notice` deliberately: that one names the nvidia-smi error, this one
    names the versions, the holder, the hook's verdict and the exact command. Guarded to
    at most one notification per UTC day. NEVER proposes a restart/kill/prune — a driver
    mismatch is fixed by stopping GPU containers and reloading, not by OpsBrain.
    """
    gr = coll.get("gpu_reconcile", {}) if isinstance(coll, dict) else {}
    if not isinstance(gr, dict):
        return []
    ctx = gr.get("context_nodes", {}) or {}
    if ctx.get("attention") != "actionable":
        return []
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if st.get("gpu_reconcile_notified") == today:
        return []
    summary = (gr.get("dashboard", {}) or {}).get("summary") or "GPU driver mismatch"
    # dispatch() notifies with the reason; no second notify() here (one entry per event).
    results = [engine.dispatch("notify_gpu_reconcile", ctx.get("state"), summary)]
    st["gpu_reconcile_notified"] = today
    return results


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


def reasoner_decision_state(qwen, coll):
    """(usable, why) — is the LLM decision doc produced for THIS collector snapshot?

    The scheduler treats a failed reasoner stage as non-fatal and runs actions anyway,
    which silently replays the previous decision doc (Sep 2026: an ollama model swap
    400'd the reasoner while actions kept re-firing a 4-hour-old decision). Reasoning
    over a stale snapshot is worse than not reasoning: compare snapshots and skip the
    LLM half loudly instead.
    """
    if not qwen:
        return False, "no reasoner decision on disk"
    dts = str(qwen.get("timestamp") or "")
    cts = str(coll.get("timestamp") or "")
    if not dts:
        return False, "reasoner decision carries no snapshot timestamp"
    if cts and dts != cts:
        return False, f"decision is for snapshot {dts}, collector is {cts}"
    return True, ""


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

    reasoner_fresh, reasoner_note = reasoner_decision_state(qwen, coll)
    if not reasoner_fresh:
        # Deterministic rules still run on the fresh collector data; only the LLM half
        # is dropped. Notify at most once per UTC day so a broken reasoner cannot spam.
        log.warning("reasoner decision unusable -> LLM actions suppressed: %s", reasoner_note)
        qwen = {"warnings": [], "actions": [], "gpu_drift": [], "summary": "",
                "confidence": 0.0, "manual_stops": []}
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if st.get("reasoner_stale_notified") != today:
            notify(f"Reasoner produced no fresh decision ({reasoner_note}); "
                   f"LLM-driven actions suppressed this cycle", "reasoner_stale")
            st["reasoner_stale_notified"] = today

    conf = float(qwen.get("confidence", 0.0))
    floor = float(Cfg.get("actions.qwen_confidence_floor", 0.6))

    rule_actions, rule_warnings = deterministic_rules(coll, st, baseline)
    if not reasoner_fresh:
        rule_warnings.append(f"reasoner stage produced no fresh decision ({reasoner_note})")

    # ---- Manual Stop Protection: load the protected set once for this run ----
    reg = load_manual_stops()
    protected_names = reg.protected_names()
    # which protected containers are currently stopped (for the prune guard)?
    now_containers = coll.get("docker", {}).get("containers", [])
    protected_stopped = {c["name"] for c in now_containers
                         if c.get("manual_stop_protected") and c.get("state") == "exited"}
    # containers currently flagged in a Dockhand restart storm: never restart
    # these (storm guard in Engine.dispatch) — restarting feeds the loop.
    storming = [str(s.get("service") or "") for s in
                ((coll.get("dockhand", {}) or {}).get("correlate", {}) or {}).get("restart_storms", [])]

    engine = Engine(dry, manual_stops=reg, protected_stopped=protected_stopped,
                    storming=storming,
                    gpu_kill_corroborated=stuck_process_corroborated(coll),
                    prune_corroborated=any(a.get("type") == "docker_prune"
                                           and a.get("evidence") == "disk_threshold"
                                           for a in rule_actions))

    if conf >= floor:
        # Qwen's actions, minus any container-CPU fault claim the deterministic
        # sustained-CPU rule does not corroborate this cycle (a single `docker stats`
        # sample cannot support "excessive CPU ... requires optimization or scaling").
        sustained = sustained_cpu_evidence(rule_actions)
        qwen_actions = qwen.get("actions")
        if not isinstance(qwen_actions, list):
            qwen_actions = []
        kept, dropped = filter_llm_notices(qwen_actions, coll, sustained)
        for d in dropped:
            log.info("dropped LLM notice without deterministic evidence: %s", d.get("target"))
        proposed = kept + rule_actions
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

    # A GPU the collector cannot see (driver/NVML mismatch) reads as all-clear — say so.
    gpu_unavailable_notice(coll, st)

    # GPU driver/library mismatch the apt hook could not repair on its own (notify-only).
    gpu_reconcile_recs = gpu_reconcile_actions(coll, engine, st)

    # Federation: dispatch cluster-level recommendations (notify-only, gated).
    cluster_recs = federation_decisions(engine, None)

    # Dockhand desired-state drift (notify-only, never remediate).
    dockhand_recs = dockhand_drift_actions(coll, engine)

    # Hatchdoor vault drift (notify-only, never remediate the vault).
    vault_drift_recs = vault_drift_actions(coll, engine)

    save_state(st)
    write_json(Cfg.resolve("paths.baseline_json"), baseline)
    write_json(Cfg.resolve("paths.actions_json"), {
        "timestamp": coll.get("timestamp"),
        "dry_run": dry,
        "confidence": conf,
        "confidence_floor": floor,
        "reasoner": {"fresh": reasoner_fresh, "reason": reasoner_note},
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
        "gpu": {"available": (coll.get("gpu", {}) or {}).get("available",
                                                             bool((coll.get("gpu", {}) or {}).get("gpus"))),
                "error": (coll.get("gpu", {}) or {}).get("error")},
        "manual_stops": {
            "names": protected_names,
            "count": len(protected_names),
            "blocked_this_run": engine.manual_block_count,
        },
        "cluster": {
            "enabled": Cfg.get("federation.enabled", False),
            "recommendations": cluster_recs,
        },
        "dockhand": {
            "enabled": bool((coll.get("dockhand", {}) or {}).get("up")),
            "notifications": dockhand_recs,
        },
        "vault_drift": {
            "enabled": bool((coll.get("vault_drift", {}) or {}).get("up")),
            "notifications": vault_drift_recs,
            "drift_count": (coll.get("vault_drift", {}) or {}).get("context_nodes", {}).get("drift_count", 0),
        },
        "gpu_reconcile": {
            "enabled": bool((coll.get("gpu_reconcile", {}) or {}).get("up")),
            "state": ((coll.get("gpu_reconcile", {}) or {}).get("context_nodes", {}) or {}).get("state"),
            "notifications": gpu_reconcile_recs,
        },
    })
    log.info("actions: executed=%d skipped=%d blocked=%d gpu_events=%d manual_stop_blocks=%d (dry_run=%s)",
             len(engine.executed), len(engine.skipped), len(engine.blocked),
             len(gpu_events), engine.manual_block_count, dry)
    return 0


if __name__ == "__main__":
    main()