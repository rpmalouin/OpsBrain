"""Loopback-only, read-only HTTP server for the Hermes cron dashboards.

Safety properties, each enforced here rather than promised in docs:

* binds ``127.0.0.1`` only (``HERMES_DASH_BIND`` can move it, deliberately not default)
* answers ``GET`` only — every other method gets 405 with an ``Allow: GET`` header
* refuses any request whose ``Host`` header is not the address it listens on (421),
  which is what makes DNS-rebinding and a stray proxy useless against it
* serves a linked file only when its REAL path is inside one of the collector's
  allowed folders (``collector.is_servable``) — so ``.env``, ``config.yaml``,
  ``auth.json``, ``state.db`` and every credential file are refused with a reason
* no request path writes anything: the only writer in this project is
  ``snapshot.py``, run by its own systemd timer
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from hermes_dash import collector as C  # noqa: E402

STATIC = HERE / "static"
STATIC_FILES = {"app.js", "app.css", "index.html"}
HOSTS = ("dockervm", "macstudio")
CACHE_TTL = 3.0
_cache: dict[str, tuple[float, dict]] = {}


def state_for(host: str) -> dict:
    """Collector output, cached for a few seconds so a fast refresh is cheap."""
    key = host
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    doc = C.collect(host) if host in HOSTS else C.collect_all()
    _cache[key] = (now, doc)
    return doc


class Handler(BaseHTTPRequestHandler):
    server_version = "hermes-dash/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- plumbing
    def log_message(self, format, *args):  # noqa: A002 - matches the stdlib signature
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), format % args))

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';"
            " frame-src 'self' http://127.0.0.1:9120 http://localhost:9120",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def _text(self, text: str, code: int = 200, ctype: str = "text/plain; charset=utf-8"):
        self._send(code, text.encode("utf-8", errors="replace"), ctype)

    def _host_ok(self) -> bool:
        allowed = {
            f"{self.server.bind_address}:{self.server.server_address[1]}",
            f"localhost:{self.server.server_address[1]}",
            f"[::1]:{self.server.server_address[1]}",
        }
        host = (self.headers.get("Host") or "").strip().lower()
        return host in {a.lower() for a in allowed}

    # ---------------------------------------------------------------- routes
    def do_GET(self):  # noqa: N802 - stdlib signature
        if not self._host_ok():
            self._text(
                f"421 Misdirected Request: Host {self.headers.get('Host')!r} is not the "
                f"address this server listens on\n",
                code=421,
            )
            return
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if route in ("/", "/index.html"):
            self._serve_static("index.html")
        elif route.startswith("/static/"):
            self._serve_static(route[len("/static/"):])
        elif route == "/api/state":
            host = (query.get("host") or ["dockervm"])[0]
            if host not in HOSTS and host != "all":
                self._json({"error": f"unknown host {host!r}", "hosts": list(HOSTS)}, 400)
                return
            self._json(state_for(host))
        elif route == "/api/file":
            self._serve_file((query.get("path") or [""])[0])
        elif route == "/api/health":
            self._json({"ok": True, "hosts": list(HOSTS), "bind": self.server.bind_address})
        else:
            self._text("404 not found\n", code=404)

    def _method_not_allowed(self):
        self._text("405 method not allowed — this server answers GET only\n", code=405,
                   ctype="text/plain; charset=utf-8")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815

    # ---------------------------------------------------------------- handlers
    def _serve_static(self, name: str):
        name = name or "index.html"
        if name not in STATIC_FILES:
            self._text("404 not found\n", code=404)
            return
        path = STATIC / name
        try:
            body = path.read_bytes()
        except OSError as exc:
            self._text(f"500 cannot read {name}: {exc}\n", code=500)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, body, ctype)

    def _serve_file(self, raw_path: str):
        ok, reason = C.is_servable(raw_path)
        if not ok:
            # Deliberately specific: the point is to show the refusal was a decision.
            self._text(f"403 refused: {reason}\n", code=403)
            return
        text, reason = C.read_servable(raw_path)
        if text is None:
            self._text(f"403 refused: {reason}\n", code=403)
            return
        self._text(text)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    bind_address: str

    def __init__(self, address: tuple[str, int], handler, bind_address: str):
        self.bind_address = bind_address
        super().__init__(address, handler)


def build(bind: str = "127.0.0.1", port: int = 9130) -> Server:
    return Server((bind, port), Handler, bind)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hermes cron dashboard (loopback, read-only)")
    ap.add_argument("--bind", default=os.environ.get("HERMES_DASH_BIND", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("HERMES_DASH_PORT", "9130")))
    args = ap.parse_args(argv)
    if args.bind not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"refusing to bind {args.bind}: this server is loopback-only by design\n"
            "ask before exposing it — see hermes_dash/README.md",
            file=sys.stderr,
        )
        return 2
    srv = build(args.bind, args.port)
    print(f"hermes-dash on http://{args.bind}:{args.port}/ (GET only, loopback)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
