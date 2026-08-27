"""
OpsBrain real-time dashboard server.

Streams the live OpsBrain JSON logs over WebSockets and serves a single-page
dashboard.

Endpoints:
    GET /              -> dashboard HTML (ui/templates/dashboard.html)
    GET /static/*      -> ui/static assets
    WS  /stream        -> live merged JSON, pushed every refresh_seconds

How it works:
    A background asyncio task wakes every `refresh_seconds`, re-reads the four
    watched log files ONLY when a file's mtime/size changed, merges them into one
    document, and broadcasts to all connected WebSocket clients. On a fresh
    client connect we push the current snapshot immediately so the page isn't
    blank while waiting for the first tick.

Run:
    uvicorn ui.server:app --host 0.0.0.0 --port 9120
    (or via systemd unit opsbrain-ui.service)
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

REPO = Path(__file__).resolve().parent.parent
UI = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("OPSBRAIN_UI_CONFIG", UI / "config.yaml"))


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


@asynccontextmanager
async def lifespan(app):
    refresh_snapshot()
    task = asyncio.create_task(watcher_loop())
    try:
        yield
    finally:
        _clients.clear()
        task.cancel()


app = FastAPI(title="OpsBrain Dashboard", lifespan=lifespan)
templates = Jinja2Templates(directory=str(UI / "templates"))
app.mount("/static", StaticFiles(directory=str(UI / "static")), name="static")

# ---------------------------------------------------------------- state
# {path: (mtime_ns, size, parsed_json_or_None)}
_last_state = {}
_snapshot: dict = {}
_clients = set()
_watcher_task = None


def _sig_and_load(path):
    """Return (mtime_ns, size, parsed) for a file, or (None,None,None) if missing."""
    try:
        st = path.stat()
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return st.st_mtime_ns, st.st_size, data
    except FileNotFoundError:
        return None, None, None
    except (json.JSONDecodeError, OSError):
        # mid-write / corrupt: keep last good value for this file
        prev = _last_state.get(str(path))
        return (prev[0] if prev else None), (prev[1] if prev else None), (prev[2] if prev else None)


def refresh_snapshot():
    """Re-read changed watched files and rebuild the merged snapshot. Returns True if changed."""
    changed = False
    merged = {"_meta": {"refresh_seconds": CFG["refresh_seconds"]}, "sources": {}}

    for path in CFG["watch"]:
        key = str(path)
        mtime, size, data = _sig_and_load(path)
        if mtime is None:
            merged["sources"][path.name] = None
            continue
        prev = _last_state.get(key)
        is_new = prev is None or prev[0] != mtime or prev[1] != size
        good = (data if mtime is not None and data is not None else (prev[2] if prev else None))
        if is_new and good is not None:
            _last_state[key] = (mtime, size, good)
            changed = True
        if good is not None:
            merged["sources"][path.name] = good
        elif _last_state.get(key) and _last_state[key][2] is not None:
            merged["sources"][path.name] = _last_state[key][2]

    if not changed and not _snapshot:
        changed = True  # first ever snapshot

    # Top-level convenience keys (a merged view of the four files)
    merged["collector"] = merged["sources"].get("collector.json")
    merged["reasoner"] = merged["sources"].get("reasoner_result.json")
    merged["actions"] = merged["sources"].get("actions_result.json")
    merged["gpu_baseline"] = merged["sources"].get("gpu_baseline.json")

    if changed:
        _snapshot.clear()
        if "_meta" not in merged:
            merged["_meta"] = {"refresh_seconds": CFG["refresh_seconds"]}
        _snapshot.update(merged)
    return changed


async def broadcast(payload):
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_text(json.dumps(payload, default=str))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


async def watcher_loop():
    """Background task: push merged snapshot to every WS client on a tick (or change)."""
    while True:
        changed = refresh_snapshot()
        if changed and _clients:
            await broadcast(_snapshot)
        await asyncio.sleep(CFG["refresh_seconds"])


# ---------------------------------------------------------------- routes
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/report", response_class=HTMLResponse)
async def report_md(request: Request):
    """Serve the latest daily ops report as text."""
    import glob as _glob
    files = sorted(REPO.glob("reports/*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return HTMLResponse("no report yet", status_code=404)
    return HTMLResponse(files[0].read_text(), media_type="text/plain")


@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    # push the current snapshot immediately so the page renders without waiting
    refresh_snapshot()
    if _snapshot:
        try:
            await ws.send_text(json.dumps(_snapshot, default=str))
        except Exception:
            pass
    try:
        while True:
            msg = await ws.receive_text()
            # any client message is treated as a ping; reply with the latest snapshot
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