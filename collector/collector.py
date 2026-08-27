"""
Ops Brain - collector.

Polls Netdata, Dozzle, Dockpeek(Docker socket), nvidia-smi, and VM/OS state,
merges everything into a single unified JSON document and writes it to
<repo>/logs/collector.json.

Usage:
    python3 collector/collector.py [--config <path>] [--out <path>] [--loop]
"""
import argparse
import json
import shlex
import subprocess
import time
import urllib.error
import urllib.request

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, REPO, get_logger, now_iso  # noqa: E402
from common import read_json, write_json  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from manual_stops import ManualStops, classify_manual_stop, _norm_name  # noqa: E402

log = get_logger("collector")


def run(cmd, timeout=30, max_bytes=200000):
    """Run a shell command list, return dict {ok, rc, out, err}."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "rc": p.returncode,
                "out": p.stdout[:max_bytes], "err": p.stderr[:max_bytes]}
    except Exception as e:
        return {"ok": False, "rc": -1, "out": "", "err": str(e)}


def http_get(url, timeout=10, max_bytes=200000):
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "opsbrain/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
            body = data[:max_bytes]
            if isinstance(body, bytes):
                try:
                    return {"ok": True, "json": json.loads(body.decode("utf-8"))}
                except Exception:
                    return {"ok": True, "text": body.decode("utf-8", "replace")}
            return {"ok": False, "err": "no data"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": f"HTTP {e.code}", "code": e.code}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def _truenas_creds(cfg):
    """Read username/password from the configured creds file (username= / password= lines)."""
    raw = cfg.get("sources.truenas.creds_file", "~/.smbcred")
    path = Path(raw).expanduser()
    user = passwd = ""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "username":
                user = v.strip()
            elif k.strip() == "password":
                passwd = v.strip()
    except Exception:
        pass
    return user, passwd


def truenas_get(url, user, passwd, timeout=8, max_bytes=2_000_000):
    """Authed GET to the TrueNAS REST API. Returns {ok, json} on success."""
    import base64
    import urllib.request
    try:
        token = base64.b64encode(f"{user}:{passwd}".encode()).decode()
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "opsbrain/1.0",
                     "Authorization": f"Basic {token}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes + 1)[:max_bytes]
            try:
                return {"ok": True, "json": json.loads(body.decode("utf-8"))}
            except Exception:
                return {"ok": True, "json": None}
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": f"HTTP {e.code}", "code": e.code}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def collect_truenas(cfg):
    """Collect TrueNAS SCALE status: pools, system info, active alerts, disk summary.
    Degrades gracefully (returns up:false) if the array is unreachable or auth fails."""
    t = cfg.get("sources.truenas", {})
    if not t.get("enabled", True):
        return {"enabled": False}
    base = str(t.get("base_url", "http://truenas/api/v2.0")).rstrip("/")
    timeout = float(t.get("timeout_s", 8))
    user, passwd = _truenas_creds(cfg)
    out = {"enabled": True, "up": False, "hostname": "truenas"}

    pool = truenas_get(f"{base}/pool", user, passwd, timeout)
    sysi = truenas_get(f"{base}/system/info", user, passwd, timeout)
    alerts = truenas_get(f"{base}/alert/list", user, passwd, timeout)
    disks = truenas_get(f"{base}/disk", user, passwd, timeout)

    # pools
    pools = []
    if pool.get("ok") and isinstance(pool.get("json"), list):
        for p in pool["json"]:
            if isinstance(p, dict):
                pools.append({
                    "name": p.get("name"),
                    "status": p.get("status"),
                    "healthy": p.get("healthy"),
                    "size": p.get("size"),
                    "allocated": p.get("allocated"),
                    "free": p.get("free"),
                    "topology": (p.get("topology") or {}).get("data"),
                })
    out["pools"] = pools
    out["pool_count"] = len(pools)
    out["pools_healthy"] = sum(1 for p in pools if p.get("healthy"))
    out["up"] = bool(pools)  # pool data -> array reachable & authed

    # system info
    if sysi.get("ok") and isinstance(sysi.get("json"), dict):
        s = sysi["json"]
        out["version"] = s.get("version")
        out["model"] = s.get("model")
        out["cores"] = s.get("cores")
        out["physical_cores"] = s.get("physical_cores")
        out["physmem"] = s.get("physmem")
        out["uptime_seconds"] = s.get("uptime_seconds")
        out["hostname"] = s.get("hostname", out.get("hostname"))

    # active alerts
    act = []
    if alerts.get("ok") and isinstance(alerts.get("json"), list):
        for a in alerts["json"]:
            if isinstance(a, dict) and not a.get("dismissed"):
                act.append({"level": a.get("level"), "klass": a.get("klass"),
                            "formatted": (a.get("formatted") or "")[:160]})
    out["alerts_active"] = act[:30]
    out["alerts_count"] = len(act)

    # disk summary (count + first few for visibility)
    if disks.get("ok") and isinstance(disks.get("json"), list):
        ds = disks["json"]
        out["disk_count"] = len(ds)
    return out


def collect_netdata(cfg):
    if not Cfg.get("sources.netdata.enabled", True):
        return {"enabled": False}
    base = Cfg.get("sources.netdata.base_url", "http://localhost:19999")
    out = {"enabled": True, "hostname": "docker", "alarms": [], "api": {}, "up": False}
    info = http_get(f"{base}/api/v1/info")
    if info.get("ok"):
        out["api"] = {"version": info["json"].get("version"), "hosts": info["json"].get("hosts-available")}
        out["up"] = True
    alarms = http_get(f"{base}/api/v1/alarms?all", max_bytes=3_000_000)
    active = []
    _keep = {"CRITICAL", "WARNING", "ERROR"}
    if alarms.get("ok") and isinstance(alarms.get("json", {}).get("alarms"), dict):
        al = alarms["json"]["alarms"]
        for a in al.values():
            status = a.get("status")
            if status in _keep:
                active.append({
                    "name": a.get("name"), "status": status, "class": a.get("class"),
                    "component": a.get("component"), "value": a.get("value"),
                    "units": a.get("units"), "info": a.get("info", "")[:120]})
    out["alarms_active"] = active[:60]     # cap for model context
    out["alarms_count"] = len(active)
    # light host overview
    chart = http_get(f"{base}/api/v1/data?chart=system.cpu&after=-2&before=0&points=1&format=json")
    if chart.get("ok") and isinstance(chart["json"].get("data"), list) and chart["json"]["data"]:
        vals = chart["json"]["data"][0]
        cols = chart["json"].get("labels", [])
        if vals and len(vals) == len(cols):
            out["system_cpu_user"] = vals[cols.index("user")] if "user" in cols else None
    return out


def collect_dozzle(cfg):
    base = Cfg.get("sources.dozzle.base_url", "http://localhost:8080")
    r = http_get(f"{base}/api/config")
    if not r.get("ok"):
        return {"enabled": True, "up": False, "err": r.get("err")}
    return {"enabled": True, "up": True,
            "image": r["json"].get("name"), "version": r["json"].get("version"),
            "auth": r["json"].get("features", {}).get("auth")}


def collect_dockpeek(cfg):
    base = Cfg.get("sources.dockpeek.base_url", "http://localhost:8081")
    # Dockpeek proxies the Docker API; probe several common routes.
    r = http_get(f"{base}/api/v1/containers?all=1")
    status = "ok" if r.get("ok") else f"err:{r.get('err')}"
    n = len(r.get("json", [])) if r.get("ok") and isinstance(r.get("json"), list) else None
    try:
        vitals = run(["docker", "inspect", "dockpeek", "--format", "{{.State.Running}}"], timeout=8)
        running = vitals.get("out", "").strip() == "true"
    except Exception:
        running = None
    return {"enabled": True, "up": r.get("ok", False), "api_http_ok": r.get("ok", False),
            "containers_seen": n, "err": r.get("err"), "container_running": running}


def collect_docker_socket(cfg):
    """Primary container-state source via the docker CLI (same data as dockpeek UI)."""
    ps = run(["docker", "ps", "-a", "--no-trunc",
              "--format", "{{json .}}"])
    containers = []
    if ps["ok"]:
        for line in ps["out"].splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                j = json.loads(line)
                name = j.get("Names", "").lstrip("/")
                inst = _inspect_state(name)
                containers.append({
                    "id": inst.get("id"),
                    "name": name,
                    "image": j.get("Image"),
                    "state": j.get("State"),
                    "status": j.get("Status"),
                    "restarting": (j.get("State") == "restarting"),
                    "labels": j.get("Labels", ""),
                    "exit_code": inst.get("exit_code"),
                    "oom_killed": inst.get("oom_killed"),
                    "finished_at": inst.get("finished_at"),
                    "started_at": inst.get("started_at"),
                })
            except Exception:
                continue
    # live stats (CPU / MEM)
    stats = run(["docker", "stats", "--no-stream", "--format",
                 "{{json .}}"], timeout=30)
    stats_map = {}
    if stats.get("ok"):
        for line in stats["out"].splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                j = json.loads(line)
                stats_map[j.get("Name", "").lstrip("/")] = {
                    "cpu_percent": j.get("CPUPerc"),
                    "mem_percent": j.get("MemPerc"),
                    "mem_usage": j.get("MemUsage"),
                    "net_io": j.get("NetIO"),
                    "pids": j.get("PIDs"),
                }
            except Exception:
                continue
    for c in containers:
        c["stats"] = stats_map.get(c["name"], {})
        c["restart_count"] = _restart_count(c["name"])

    # ---- Manual Stop Protection: detect newly stopped containers ----
    manual_stops_meta = update_manual_stops(containers, cfg)
    doc = {
        "containers_count": len(containers),
        "running": sum(1 for c in containers if c["state"] == "running"),
        "restarting": [c["name"] for c in containers if c["restarting"]],
        "unhealthy": [c["name"] for c in containers
                      if c["state"] == "running" and "unhealthy" in (c["status"] or "").lower()],
        "containers": containers,
        "manual_stops": manual_stops_meta.get("names", []),
        "manual_stop_protected_count": manual_stops_meta.get("count", 0),
    }
    # mark each container's protected flag (for the dashboard)
    protected = set(manual_stops_meta.get("names", []))
    for c in containers:
        c["manual_stop_protected"] = c["name"].lower() in protected
    doc["containers"] = containers
    return doc


def _inspect_state(name):
    """Get a container's exit fingerprint via docker inspect.
    Returns {id, exit_code, oom_killed, finished_at, started_at, restart_count}."""
    fmt = "{{.Id}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.FinishedAt}}|{{.State.StartedAt}}|{{.RestartCount}}"
    try:
        r = subprocess.run(["docker", "inspect", name, "--format", fmt],
                           capture_output=True, text=True, timeout=6)
        parts = r.stdout.strip().split("|")
        while len(parts) < 6:
            parts.append("")
        return {
            "id": parts[0],
            "exit_code": _to_int(parts[1]),
            "oom_killed": str(parts[2]).strip().lower() == "true",
            "finished_at": parts[3],
            "started_at": parts[4],
            "restart_count": _to_int(parts[5]),
        }
    except Exception:
        return {"id": "", "exit_code": None, "oom_killed": False,
                "finished_at": None, "started_at": None, "restart_count": 0}


def _restart_count(name):
    try:
        r = subprocess.run(["docker", "inspect", name, "--format", "{{.RestartCount}}"],
                           capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip() or 0)
    except Exception:
        return 0


def _manual_stops_path():
    if Cfg.get("paths.manual_stops"):
        return Cfg.resolve("paths.manual_stops")
    return REPO / "logs" / "manual_stops.json"


def _prev_running_path():
    if Cfg.get("paths.prev_running"):
        return Cfg.resolve("paths.prev_running")
    return REPO / "logs" / "prev_running.json"


def update_manual_stops(containers, cfg):
    """Detect newly-manually-stopped containers and persist manual_stops.json.

    Transition-based detection: a container that WAS running last cycle and is now
    stopped (exited) with a clean/manual exit signature qualifies as a manual stop.
    First run (no prev_running) seeds and classifies nothing to avoid false protects.

    Returns {names: [..], count: N} for embedding in collector.json.
    """
    if not Cfg.get("manual_stop_protection.track_manual_stops", True):
        return {"names": [], "count": 0}

    reg = ManualStops(_manual_stops_path())
    if not _manual_stops_path().exists():
        reg.save()  # create an empty registry file so consumers can rely on it
    prev_path = _prev_running_path()
    prev_running = read_json(prev_path, {}) or {}
    prev_ids = set(prev_running.get("ids", []))
    prev_names = {str(n).lower() for n in prev_running.get("names", [])}

    now_run_ids = set()
    now_run_names = set()
    for c in containers:
        cid = (c.get("id") or "").strip()
        name = (c.get("name") or "").strip()
        is_running = (c.get("state") == "running")
        if cid and is_running:
            now_run_ids.add(cid)
        if name and is_running:
            now_run_names.add(name.lower())

    sigkill_protect = bool(Cfg.get("actions.manual_stop_sigkill_protect", True))

    newly_stopped = []
    if prev_ids or prev_names:
        # containers that were running before and are not running now
        for c in containers:
            cid = (c.get("id") or "").strip()
            name = (c.get("name") or "").strip()
            if c.get("state") == "exited":
                was_running = (cid and cid in prev_ids) or (name and name.lower() in prev_names)
                if was_running:
                    newly_stopped.append(c)

    detected_at = now_iso()
    for c in newly_stopped:
        cid = (c.get("id") or "").strip()
        name = (c.get("name") or "").strip()
        manual = classify_manual_stop(c.get("exit_code"), c.get("oom_killed"),
                                      sigkill_protect)
        if manual and cid:
            reg.add(cid, name, c.get("exit_code"), c.get("oom_killed"),
                    c.get("finished_at"), detected_at)
        elif manual and name:
            reg.add(name, name, c.get("exit_code"), c.get("oom_killed"),
                    c.get("finished_at"), detected_at)

    # User re-arm: a protected container now RUNNING again clears protection.
    # A container is "re-run" if its ID is currently running, OR its recorded name
    # is currently running under a (possibly new) ID. Still-stopped/absent persist.
    for cid, rec in list(reg.stops().items()):
        name_key = _norm_name(rec.get("name", ""))
        running_now = (cid in now_run_ids) or (name_key and name_key in now_run_names)
        if running_now:
            reg.rearm(cid, rec.get("name", ""))

    # persist prev_running (RUNNING ids + lowercased names) for the next cycle
    write_json(prev_path, {"ids": sorted(now_run_ids), "names": sorted(now_run_names)})

    names = reg.protected_names()
    return {"names": names, "count": len(names)}


def collect_gpu(cfg):
    """Query nvidia-smi (now incl. power draw), track a persistent baseline, and
    compute deterministic GPU-drift flags against that baseline."""
    gp = run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,name",
              "--format=csv,noheader,nounits"])
    gpus = []
    if gp.get("ok"):
        for idx, line in enumerate(gp["out"].strip().splitlines()):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append({"index": idx, "name": parts[5],
                             "mem_used_mb": _to_int(parts[0]),
                             "mem_total_mb": _to_int(parts[1]),
                             "util_gpu_percent": _to_float(parts[2]),
                             "temp_c": _to_int(parts[3]),
                             "power_w": _to_float(parts[4])})
    procs = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                 "--format=csv,noheader,nounits"])
    processes = []
    if procs.get("ok"):
        for line in procs["out"].strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 3:
                processes.append({"pid": p[0], "name": p[1], "mem_mb": p[2]})

    # 1) load existing baseline
    baseline_path = _gpu_baseline_path()
    b = read_json(baseline_path, {}) or {}

    # 2) compare current stats to baseline and compute drift flags
    flags, baseline = evaluate_gpu_drift(gpus, processes, b, cfg)

    # 3) persist updated baseline
    write_json(baseline_path, baseline)

    return {"gpus": gpus, "compute_processes": processes,
            "baseline": baseline, "drift_flags": flags}


GPU_BASELINE = REPO / "logs" / "gpu_baseline.json"


def _gpu_baseline_path():
    return Cfg.resolve("paths.gpu_baseline") if Cfg.get("paths.gpu_baseline") else GPU_BASELINE


def _to_int(v):
    try:
        return int(round(float(str(v).strip())))
    except Exception:
        return 0


def _to_float(v):
    try:
        return float(str(v).strip())
    except Exception:
        return 0.0


def _reset_baseline(prev):
    """Return a fresh baseline when GPU identity changed or none existed."""
    return {"last_vram": 0, "last_pid": "0", "last_power": 0, "last_temp": 0,
            "cycles_with_same_pid": 0, "gpu_name": (prev or {}).get("gpu_name"),
            "total_vram": (prev or {}).get("total_vram")}


def evaluate_gpu_drift(gpus, processes, prev_baseline, cfg):
    """Deterministic GPU-drift evaluation. Pure function (testable).

    Returns (flags:list[str], new_baseline:dict) using the documented baseline shape.
    Operates on the project's primary GPU (index 0)."""
    dr = Cfg.get("gpu_drift", {}) if cfg is None else (cfg.get("gpu_drift", {}) or {})
    vram_creep = int(dr.get("vram_creep_mb", 250))
    vram_max_pct = float(dr.get("vram_max_percent", 90))
    stuck_cycles = int(dr.get("stuck_pid_cycles", 5))
    util_thr = float(dr.get("util_threshold", 10))
    power_idle = float(dr.get("power_idle_max_watts", 40))
    temp_idle = float(dr.get("temp_idle_max_c", 55))

    if not gpus:
        return [], _reset_baseline(prev_baseline)
    g = gpus[0]
    vram = g.get("mem_used_mb", 0)
    total = g.get("mem_total_mb", 1) or 1
    util = g.get("util_gpu_percent", 0)
    temp = g.get("temp_c", 0)
    power = g.get("power_w", 0)

    pid = str(processes[0].get("pid", "0")) if processes else "0"
    # Reset baseline when GPU identity changed (name or total VRAM) — avoids a
    # spurious vram_drift after a driver/GPU swap.
    identity_change = (
        prev_baseline.get("gpu_name") not in (None, "") and prev_baseline.get("gpu_name") != g.get("name")
    ) or ("total_vram" in prev_baseline and prev_baseline.get("total_vram") != total)
    base = _reset_baseline(prev_baseline) if (identity_change or not prev_baseline) else prev_baseline
    new = dict(base)
    new["gpu_name"] = g.get("name")
    new["total_vram"] = total

    # cycles_with_same_pid bookkeeping
    if base.get("last_pid") == pid and pid != "0":
        new["cycles_with_same_pid"] = int(base.get("cycles_with_same_pid", 0)) + 1
    else:
        new["cycles_with_same_pid"] = 1 if pid != "0" else 0

    flags = []
    vram_pct = 100.0 * vram / total if total else 0.0

    # VRAM drift
    if base.get("last_vram") and vram - int(base["last_vram"]) > vram_creep:
        flags.append("vram_drift")
    # VRAM overload
    if vram_pct > vram_max_pct:
        flags.append("vram_overload")
    # Stuck process: same PID persisted + util above threshold
    if new["cycles_with_same_pid"] >= stuck_cycles and util > util_thr:
        flags.append("stuck_process")
    # Power drift: high draw while strictly "idle" util (< threshold)
    if power > power_idle and util < util_thr:
        flags.append("power_drift")
    # Temp drift: high temp while strictly "idle" util (< threshold)
    if temp > temp_idle and util < util_thr:
        flags.append("temp_drift")

    # persist current as new baseline
    new["last_vram"] = vram
    new["last_pid"] = pid
    new["last_power"] = power
    new["last_temp"] = temp

    return flags, new


def collect_vm(cfg):
    uptime = run(["uptime"])
    df = run(["df", "-h", "/"])
    dfmap = {}
    disk_pct = None
    if df.get("ok"):
        lines = df["out"].strip().splitlines()
        if len(lines) >= 2:
            cols = lines[0].split()
            vals = lines[1].split()
            for c, v in zip(cols, vals):
                dfmap[c] = v
            if "Use%" in dfmap:
                disk_pct = dfmap["Use%"].rstrip("%")
    top = run(["top", "-b", "-n", "1", "-o", "%MEM"], timeout=25)
    top_slices = [l for l in top.get("out", "").splitlines() if l.strip()][6:11]
    journal = run(["journalctl", "--since", Cfg.get("sources.journalctl_since", "2 min ago"),
                   "--no-pager", "-p", "err"], timeout=20)
    jlines = [l for l in journal.get("out", "").splitlines() if l.strip()]
    mem = run(["free", "-h"])
    mem_line = mem.get("out", "").replace("\n", "; ")[:400]
    load = {}
    if uptime.get("ok") and "load average:" in uptime["out"]:
        load["load"] = uptime["out"].split("load average:")[1].strip()
    return {
        "uptime_load": load,
        "disk_root": dfmap,
        "disk_used_percent": disk_pct,
        "top_by_mem_top5": top_slices,
        "memory": mem_line,
        "syslog_last2min_error_lines": jlines[:50],
        "syslog_error_count_2min": len(jlines),
    }


def collect_all(cfg):
    doc = {"timestamp": now_iso(), "collector_version": "1.0", "host": Cfg.get("hostname", "dockerVM")}
    doc["netdata"] = collect_netdata(cfg)
    doc["dozzle"] = collect_dozzle(cfg)
    doc["dockpeek"] = collect_dockpeek(cfg)
    doc["docker"] = collect_docker_socket(cfg)
    doc["gpu"] = collect_gpu(cfg)
    doc["vm"] = collect_vm(cfg)
    doc["truenas"] = collect_truenas(cfg)
    doc["sources_healthy"] = sum(1 for k in ("netdata", "dozzle", "dockpeek", "docker", "gpu", "vm", "truenas")
                                 if doc.get(k, {}).get("up") or k in ("docker", "gpu", "vm"))
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--loop", action="store_true", help="run continuously every interval")
    args = ap.parse_args()
    Cfg.load(args.config)
    interval = Cfg.get("interval_seconds", 120)
    outpath = Path(args.out) if args.out else Cfg.resolve("paths.collector_json")
    while True:
        doc = collect_all(Cfg)
        write_json(outpath, doc)
        log.info("collector wrote %s  (%s containers, %s alarms)"
                 % (outpath, doc["docker"]["containers_count"], doc["netdata"].get("alarms_count", 0)))
        if not args.loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()