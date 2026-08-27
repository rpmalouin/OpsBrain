"""
OpsBrain real-time dashboard server (event-driven).

Streams the live OpsBrain JSON logs over WebSockets and serves a single-page
dashboard with a 10-cycle history for sparkline/trend panels.

Endpoints:
    GET  /                   -> dashboard HTML (ui/templates/dashboard.html)
    GET  /static/*           -> ui/static assets
    GET  /report             -> latest daily ops report (text)
    GET  /api/status         -> merged OpsBrain state (JSON)
    GET  /api/containers     -> container health + per-container history (JSON)
    GET  /api/gpu            -> GPU drift + baseline + history (JSON)
    GET  /api/confidence     -> confidence trend (JSON)
    WS   /stream             -> live merged JSON (incl. history[]), pushed on change

How it works:
    A `watchdog` Observer watches the OpsBrain logs directory and, on ANY
    create/modify event, re-reads the changed file, rebuilds the merged snapshot,
    updates the 10-cycle in-memory history, collects notification events, and
    broadcasts to all WebSocket clients in real time (event-driven). A slow
    fallback tick (every ~30s) guards against a missed fs event.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

REPO = Path(__file__).resolve().parent.parent
UI = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("OPSBRAIN_UI_CONFIG", UI / "config.yaml"))

HISTORY_LEN = 10       # cycles of sparkline/trend data kept in memory
FALLBACK_TICK = 30     # reliable-delivery net in seconds


def load_config():
    with open(CONFIG_PATH) as fh:
        cfg = yaml.safe_load(fh) or {}
    srv = cfg.get("server", {})
    refresh = int(srv.get("refresh_seconds", 2))
    watch = [REPO / w for w in cfg.get("watch", [])]
    return {
        "host": srv.get("host", "0.0.0.0"),
        "port": int(srv.get("port", 9120)),
        "refresh_seconds": max(1, refresh),
        "watch": watch,
    }


CFG = load_config()

# ---------------------------------------------------------------- state
_last_state = {}          # {path: (mtime_ns, size, parsed)}
_snapshot: dict = {}      # merged doc broadcast to clients
_clients = set()
_watcher_task = None
_observer = None

# 10-cycle trend history (frontend sparklines / trend panels)
_hist = {
    "gpu_vram": deque(maxlen=HISTORY_LEN),
    "gpu_temp": deque(maxlen=HISTORY_LEN),
    "gpu_power": deque(maxlen=HISTORY_LEN),
    "gpu_baseline_vram": deque(maxlen=HISTORY_LEN),
    "confidence": deque(maxlen=HISTORY_LEN),
    "containers": {},          # {name: {"cpu": deque, "mem": deque}}
}
_notifications = deque(maxlen=100)
_ingest_offsets = set()   # which notifications.jsonl line offsets we've already surfaced

# --- confidence recovery + drift decay + restart impact tracking ----
_conf_recovery = {"detected": False, "prev": None, "current": None, "delta": 0.0}
_drift_decay = {"vram": [], "temp": [], "power": [],
                "decay_cycles": 0, "status": "ok"}
# restart impact: {container: {"before": float|None, "after": deque(maxlen=3), "score": float, "done": bool, "last_restart_ts": float}}
_restart_impact: dict = {}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pct(v):
    try:
        return float(str(v).strip().rstrip("%"))
    except Exception:
        return None


# ---------------------------------------------------------------- file i/o
def _sig_and_load(path):
    try:
        st = path.stat()
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return st.st_mtime_ns, st.st_size, data
    except FileNotFoundError:
        return None, None, None
    except (json.JSONDecodeError, OSError):
        prev = _last_state.get(str(path))
        return (prev[0] if prev else None), (prev[1] if prev else None), (prev[2] if prev else None)


# ---------------------------------------------------------------- history + notifications
def _update_history(collector, reasoner):
    """Append one cycle to the 10-cycle rings."""
    g = (collector.get("gpu", {}) or {}).get("gpus", [{}])[0]
    base = (collector.get("gpu", {}) or {}).get("baseline") or {}
    _hist["gpu_vram"].append(int(g.get("mem_used_mb") or 0))
    _hist["gpu_temp"].append(int(g.get("temp_c") or 0))
    _hist["gpu_power"].append(float(g.get("power_w") or 0))
    _hist["gpu_baseline_vram"].append(int(base.get("last_vram") or g.get("mem_used_mb") or 0))
    conf = reasoner_conf(reasoner)
    _hist["confidence"].append(conf)
    # per-container cpu/mem
    for c in (collector.get("docker", {}).get("containers") or []):
        st = c.get("stats") or {}
        cpu, mem = _pct(st.get("cpu_percent")), _pct(st.get("mem_percent"))
        if cpu is not None or mem is not None:
            rec = _hist["containers"].setdefault(
                c["name"], {"cpu": deque(maxlen=HISTORY_LEN), "mem": deque(maxlen=HISTORY_LEN)})
            if cpu is not None:
                rec["cpu"].append(cpu)
            if mem is not None:
                rec["mem"].append(mem)
    # drop containers no longer present (avoid unbounded growth)
    known = {c["name"] for c in (collector.get("docker", {}).get("containers") or [])}
    for name in list(_hist["containers"].keys()):
        if name not in known:
            del _hist["containers"][name]


def reasoner_conf(reasoner):
    c = reasoner.get("confidence")
    return float(c) if c is not None else None


def _history_payload():
    return {
        "gpu_vram": list(_hist["gpu_vram"]),
        "gpu_temp": list(_hist["gpu_temp"]),
        "gpu_power": list(_hist["gpu_power"]),
        "gpu_baseline_vram": list(_hist["gpu_baseline_vram"]),
        "confidence": list(_hist["confidence"]),
        "containers": {n: {"cpu": list(r["cpu"]), "mem": list(r["mem"])}
                       for n, r in _hist["containers"].items()},
    }


# ------------------------------------------------- confidence recovery
def _update_conf_recovery(conf):
    """Detect a strict confidence increase vs the previous known value.
    Sets _conf_recovery.detected for one cycle; the frontend pulses on it."""
    prev = _conf_recovery["current"]
    if conf is None:
        _conf_recovery["detected"] = False
        return _conf_recovery
    if prev is not None and conf > prev:
        _conf_recovery["detected"] = True
        _conf_recovery["prev"] = prev
        _conf_recovery["current"] = conf
        _conf_recovery["delta"] = round(conf - prev, 3)
    else:
        _conf_recovery["detected"] = False
        _conf_recovery["prev"] = prev
        _conf_recovery["current"] = conf
        _conf_recovery["delta"] = 0.0
    return _conf_recovery


# ------------------------------------------------- drift decay
def _update_drift_decay():
    """Compute drift decay from the 10-cycle history vs gpu_baseline.json baselines.
    Returns the decay object for broadcast."""
    vr = list(_hist["gpu_vram"])
    te = list(_hist["gpu_temp"])
    po = list(_hist["gpu_power"])
    # baselines from gpu_baseline.json (already-loaded watched source), fall back to history extrema
    gpu_base = None
    for key in list(_last_state):
        if key.endswith("gpu_baseline.json"):
            gpu_base = _last_state[key][2]
            break
    gpu_base = gpu_base or {}
    base_v = gpu_base.get("last_vram", vr[-1] if vr else 0)
    base_t = gpu_base.get("last_temp", te[-1] if te else 0)
    base_p = gpu_base.get("last_power", po[-1] if po else 0)
    tols = {"vram": 250.0, "temp": 5.0, "power": 40.0}

    def _cycles(vals, b, tol):
        n = 0
        for v in reversed(vals):
            if v is not None and abs(v - b) > tol:
                n += 1
            else:
                break
        return n

    v_cyc = _cycles(vr, base_v, tols["vram"])
    t_cyc = _cycles(te, base_t, tols["temp"])
    p_cyc = _cycles(po, base_p, tols["power"])

    # status = worst of three
    status = "ok"
    if any(v is not None and abs(v - base_v) > tols["vram"] for v in (vr[-1:] or [None])) or \
       any(v is not None and abs(v - base_t) > tols["temp"] for v in (te[-1:] or [None])) or \
       any(v is not None and abs(v - base_p) > tols["power"] for v in (po[-1:] or [None])):
        status = "slow"
    # "bad" = still at/above a drift trigger
    if vr and abs(vr[-1] - base_v) > tols["vram"] * 3:
        status = "bad"
    _drift_decay.update({
        "vram": vr, "temp": te, "power": po,
        "decay_cycles": max(v_cyc, t_cyc, p_cyc), "status": status,
        "baselines": {"vram": base_v, "temp": base_t, "power": base_p},
    })
    return _drift_decay


# ------------------------------------------------- restart impact
def _detect_restarts(actions):
    """Return {container: reason} for restart actions in actions_result.json this cycle.
    Restarts live in qwen_actions/rule_actions (type+target) and executed/skipped/blocked
    (verb+target)."""
    out = {}
    lists = [
        actions.get("actions") or [],
        actions.get("qwen_actions") or [],
        actions.get("rule_actions") or [],
        actions.get("executed") or [],
        actions.get("skipped") or [],
        actions.get("blocked") or [],
    ]
    for lst in lists:
        for a in lst or []:
            if not isinstance(a, dict):
                continue
            typ = a.get("type") or a.get("verb")
            if typ in ("docker_restart", "service_restart"):
                tgt = a.get("target")
                if tgt and tgt not in out:
                    out[tgt] = a.get("reason", "")
    return out


def _register_restart(name, conf_before):
    """Seed an impact tracker for a newly-restarted container.
    conf_before = actions_result.json.confidence at the deciding cycle (may be None)."""
    if name in _restart_impact and not _restart_impact[name]["done"]:
        return
    _restart_impact[name] = {
        "container": name,
        "conf_before": conf_before,
        "conf_1": None, "conf_2": None, "conf_3": None,
        "score": None,
        "done": False,
        "last_restart_ts": time.time(),
    }


def _advance_restart_impact(conf):
    """Each cycle after a restart, fill the next conf_1..3 slot with the observed
    confidence (skipping null readings but keeping slot positions)."""
    for name, imp in _restart_impact.items():
        if imp["done"]:
            continue
        if imp["conf_1"] is None:
            imp["conf_1"] = conf
        elif imp["conf_2"] is None:
            imp["conf_2"] = conf
        elif imp["conf_3"] is None:
            imp["conf_3"] = conf
            imp["done"] = True
            if imp["conf_before"] is not None:
                deltas = [c - imp["conf_before"] for c in
                          (imp["conf_1"], imp["conf_2"], imp["conf_3"]) if c is not None]
                if len(deltas) == 3:
                    imp["score"] = round(sum(deltas) / 3, 3)
            # if less than 3 non-null, score stays None (insufficient data)
    # prune stale entries older than ~2h
    cutoff = time.time() - 7200
    for container in list(_restart_impact.keys()):
        if _restart_impact[container]["last_restart_ts"] < cutoff:
            del _restart_impact[container]


def _restart_impact_payload():
    out = []
    for i in _restart_impact.values():
        samples = [{"conf_before": i.get("conf_before"),
                    "conf_1": i.get("conf_1"), "conf_2": i.get("conf_2"),
                    "conf_3": i.get("conf_3"), "done": i.get("done", False)}]
        out.append({"container": i["container"], "score": i.get("score"),
                    "samples": samples})
    return out


def _check_notifications(collector, reasoner, actions):
    """Detect conditions; append to history; return newly fired (deduped within tick)."""
    fired = []
    conf = reasoner_conf(reasoner)
    if conf is not None and conf < 0.6:
        fired.append({"type": "low_confidence", "severity": "crit", "ts": _now_iso(),
                      "msg": f"confidence {conf:.2f} < 0.6"})
    drift = collector.get("gpu", {}).get("drift_flags") or []
    if drift:
        fired.append({"type": "gpu_drift", "severity": "crit", "ts": _now_iso(),
                      "msg": f"GPU drift: {', '.join(drift)}"})
    restarting = collector.get("docker", {}).get("restarting") or []
    if restarting:
        fired.append({"type": "restart_loop", "severity": "crit", "ts": _now_iso(),
                      "msg": f"restart loop: {', '.join(restarting)}"})
    for a in (collector.get("netdata", {}).get("alarms_active") or []):
        comp = (a.get("component") or "").lower()
        if ("packet" in comp or "drop" in comp) and a.get("status") in ("CRITICAL", "WARNING"):
            fired.append({"type": "packet_drop", "severity": "warning", "ts": _now_iso(),
                          "msg": f"packet drops: {a.get('name')} ({a.get('status')})"})
            break
    for a in (actions.get("actions") or []):
        if a.get("type") in ("docker_restart", "service_restart") or a.get("verb") in ("docker_restart", "service_restart"):
            mode = "dry-run" if actions.get("dry_run") else "live"
            fired.append({"type": "restart_triggered", "severity": "info", "ts": _now_iso(),
                          "msg": f"restart triggered: {a.get('target')} ({mode})"})
    for n in fired:
        _notifications.append(n)
    return fired


# ---------------------------------------------------------------- snapshot
def _read_notifications_tail(max_lines=30):
    """Read the tail of logs/notifications.jsonl (append-only JSONL), normalize it.
    A ring offset is tracked so we only ingest NEW lines each collect."""
    path = REPO / "logs" / "notifications.jsonl"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    seen = set(_ingest_offsets)
    out = []
    for off, line in enumerate(lines):
        if off in seen:
            continue
        _ingest_offsets.add(off)
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        cat = str(rec.get("category") or "info")
        msg = str(rec.get("message") or rec.get("category") or "")
        # map category to a stable level
        if "gpu_drift" in cat or any(f in cat for f in ("vram", "temp", "power", "stuck")):
            level = "critical"
        elif "restart" in cat or "loop" in cat:
            level = "critical"
        else:
            level = "warning"
        out.append({"type": "log", "severity": level, "ts": _now_iso(), "msg": msg or cat,
                    "category": cat})
    return out


def refresh_snapshot():
    """Re-read changed files, rebuild merged snapshot + history. Returns True if anything changed."""
    collector_changed = False
    changed = False
    merged = {"_meta": {"refresh_seconds": CFG["refresh_seconds"]}, "sources": {},
              "history": _history_payload(), "notifications": list(_notifications)[-50:]}

    for path in CFG["watch"]:
        key = str(path)
        mtime, size, data = _sig_and_load(path)
        if mtime is None:
            merged["sources"][path.name] = None
            continue
        prev = _last_state.get(key)
        is_new = prev is None or prev[0] != mtime or prev[1] != size
        good = data if (mtime is not None and data is not None) else (prev[2] if prev else None)
        if is_new and good is not None:
            _last_state[key] = (mtime, size, good)
            changed = True
            if path.name == "collector.json":
                collector_changed = True
        if good is not None:
            merged["sources"][path.name] = good
        elif _last_state.get(key) and _last_state[key][2] is not None:
            merged["sources"][path.name] = _last_state[key][2]

    merged["collector"] = merged["sources"].get("collector.json")
    merged["reasoner"] = merged["sources"].get("reasoner_result.json")
    merged["actions"] = merged["sources"].get("actions_result.json")
    merged["gpu_baseline"] = merged["sources"].get("gpu_baseline.json")
    merged["manual_stops"] = merged["sources"].get("manual_stops.json", {"version": 1, "stops": {}})
    merged["cluster_snapshot"] = merged["sources"].get("cluster_snapshot.json", {"nodes": {}, "cluster_metrics": {}})
    merged["cluster_reasoner"] = merged["sources"].get("cluster_reasoner_result.json", {})

    if changed:
        rea = merged["reasoner"] or {}
        act = merged["actions"] or {}
        if collector_changed:
            col = merged["collector"] or {}
            _update_history(col, rea)
            _check_notifications(col, rea, act)
            # ingest any new agent-side notifications from notifications.jsonl
            log_notifs = _read_notifications_tail()
            for n in log_notifs:
                _notifications.append(n)
            merged["notifications"] = list(_notifications)[-50:]
            merged["history"] = _history_payload()

        # --- refinements: confidence recovery, drift decay, restart impact ---
        conf = reasoner_conf(rea)
        actions_conf = act.get("confidence", conf)
        merged["confidence_recovery"] = _update_conf_recovery(conf)
        merged["drift_decay"] = _update_drift_decay()
        # register newly-detected restarts (conf_before = actions_result.confidence)
        for container in _detect_restarts(act):
            _register_restart(container, actions_conf if actions_conf is not None else conf)
        _advance_restart_impact(conf)
        merged["restart_impact"] = _restart_impact_payload()

        _snapshot.clear()
        if "_meta" not in merged:
            merged["_meta"] = {"refresh_seconds": CFG["refresh_seconds"]}
        _snapshot.update(merged)
    return changed


# ---------------------------------------------------------------- ws helpers
async def broadcast(payload):
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_text(json.dumps(payload, default=str))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


# ---------------------------------------------------------------- watchdog
class LogHandler(FileSystemEventHandler):
    """Bridge a watchdog fs event into the asyncio loop."""

    def __init__(self, loop):
        self._loop = loop
        self._last = 0.0

    def on_any_event(self, event):
        now = time.time()
        if now - self._last < 0.5:
            return
        self._last = now
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(_on_fs_change)


def _on_fs_change():
    """Run off the event loop: refresh + broadcast (async)."""
    async def _do():
        changed = await asyncio.to_thread(refresh_snapshot)
        if changed:
            print(f"[watchdog] collector changed, broadcast to {len(_clients)} clients")
            if _clients:
                await broadcast(_snapshot)
        else:
            print("[watchdog] fs event, no collector change")
    try:
        asyncio.ensure_future(_do())
    except Exception as e:
        print(f"[watchdog] error: {e}")


async def watcher_loop():
    """Fallback reliable-delivery tick when watchdog isn't available (to_thread off-loop)."""
    while True:
        try:
            changed = await asyncio.to_thread(refresh_snapshot)
            if changed and _clients:
                await broadcast(_snapshot)
        except Exception:
            pass
        await asyncio.sleep(FALLBACK_TICK)


@asynccontextmanager
async def lifespan(app):
    global _observer, _watcher_task
    refresh_snapshot()
    # watchdog observer on background thread, bridging to the loop
    try:
        _observer = Observer()
        handler = LogHandler(asyncio.get_event_loop())
        watch_dir = CFG["watch"][0].parent if CFG["watch"] else REPO / "logs"
        _observer.schedule(handler, str(watch_dir), recursive=False)
        _observer.start()
    except Exception as e:
        print(f"watchdog unavailable, using fallback polling: {e}")
        _observer = None
    _watcher_task = asyncio.create_task(watcher_loop())
    try:
        yield
    finally:
        _clients.clear()
        if _observer:
            _observer.stop()
            _observer.join(timeout=2)
        _watcher_task.cancel()


app = FastAPI(title="OpsBrain Dashboard", lifespan=lifespan)
templates = Jinja2Templates(directory=str(UI / "templates"))
app.mount("/static", StaticFiles(directory=str(UI / "static")), name="static")


# ---------------------------------------------------------------- HTTP routes
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/report", response_class=HTMLResponse)
async def report_md(request: Request):
    files = sorted(REPO.glob("reports/*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return HTMLResponse("no report yet", status_code=404)
    return HTMLResponse(files[0].read_text(), media_type="text/plain")


@app.get("/api/status")
async def api_status():
    refresh_snapshot()
    return JSONResponse(_snapshot if _snapshot else {"error": "no data yet"})


@app.get("/api/containers")
async def api_containers():
    refresh_snapshot()
    col = _snapshot.get("collector") or {}
    containers = col.get("docker", {}).get("containers", [])
    # normalize string pct -> number for the frontend (sparkline-ready)
    def _norm(c):
        out = dict(c)
        st = dict(c.get("stats") or {})
        for k in ("cpu_percent", "mem_percent"):
            st[k] = _pct(st.get(k))
        out["stats"] = st
        # per-container manual-stop flag (from collector)
        out["protected"] = bool(c.get("manual_stop_protected"))
        return out
    ms = _snapshot.get("manual_stops") or {}
    ms_names = sorted({r.get("name", k) for k, r in (ms.get("stops", {}) or {}).items()})
    return JSONResponse({
        "containers_count": col.get("docker", {}).get("containers_count"),
        "running": col.get("docker", {}).get("running"),
        "restarting": col.get("docker", {}).get("restarting", []),
        "unhealthy": col.get("docker", {}).get("unhealthy", []),
        "containers": [_norm(c) for c in containers],
        "protected": ms_names,
        "protected_count": len(ms_names),
        "history": {n: {"cpu": list(r["cpu"]), "mem": list(r["mem"])}
                    for n, r in _hist["containers"].items()},
    })


@app.get("/api/gpu")
async def api_gpu():
    refresh_snapshot()
    col = _snapshot.get("collector") or {}
    return JSONResponse({
        "gpus": col.get("gpu", {}).get("gpus", []),
        "compute_processes": col.get("gpu", {}).get("compute_processes", []),
        "drift_flags": col.get("gpu", {}).get("drift_flags", []),
        "baseline": col.get("gpu", {}).get("baseline", {}),
        "history": {"gpu_vram": list(_hist["gpu_vram"]),
                    "gpu_temp": list(_hist["gpu_temp"]),
                    "gpu_power": list(_hist["gpu_power"]),
                    "gpu_baseline_vram": list(_hist["gpu_baseline_vram"])},
    })


@app.get("/api/confidence")
async def api_confidence():
    refresh_snapshot()
    return JSONResponse({
        "current": (_snapshot.get("reasoner") or {}).get("confidence"),
        "history": list(_hist["confidence"]),
        "floor": 0.6,
    })


@app.get("/api/groups")
async def api_groups():
    """Serve the container group filter definitions to the frontend."""
    gpath = REPO / "config" / "container_groups.yaml"
    if not gpath.exists():
        return JSONResponse({})
    with open(gpath) as fh:
        groups = yaml.safe_load(fh) or {}
    return JSONResponse(groups)


# ---------------------------------------------------------------- websocket
@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    refresh_snapshot()
    if _snapshot:
        try:
            await ws.send_text(json.dumps(_snapshot, default=str))
        except Exception:
            pass
    try:
        while True:
            await ws.receive_text()
            refresh_snapshot()
            await ws.send_text(json.dumps(_snapshot, default=str))
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# ---------------------------------------------------------------- entrypoint
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CFG["host"], port=CFG["port"], log_level="info")