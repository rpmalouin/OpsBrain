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
import http.client
import json
import os
import socket
import sys
import threading
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
HOSTS = ("dockervm", "macstudio", "ollamabox")
# The ollama box costs one SSH round trip (~0.5 s) per collect, so it is cached longer and
# only fetched while its tab is the one being viewed (see static/app.js).
CACHE_TTL = {"dockervm": 3.0, "macstudio": 3.0, "ollamabox": 45.0}

# Names the front door may proxy here with. The bind stays loopback; caddy is
# host-networked and dials 127.0.0.1, so the ONLY thing that changes when a route
# exists is which Host header is legitimate. Everything else is still 421.
PUBLIC_HOSTS = tuple(
    h.strip().lower()
    for h in os.environ.get("HERMES_DASH_PUBLIC_HOSTS", "hermesjobs.home").split(",")
    if h.strip()
)
# The Home tab embeds the OpsBrain page. Served over http://127.0.0.1 the iframe must
# be http; served through the route (https) any http iframe is blocked as mixed content.
OPSBRAIN_HTTP = os.environ.get("HERMES_DASH_OPSBRAIN_HTTP", "http://127.0.0.1:9120/")
OPSBRAIN_HTTPS = os.environ.get("HERMES_DASH_OPSBRAIN_HTTPS", "https://opsbrain.home/")

# ---- the embedded OpsBrain page, served SAME-ORIGIN -------------------------------
# The Home tab used to iframe https://opsbrain.home, a second origin, so the browser asked
# for the same door credential a second time inside the page (the "double login", measured
# 2026-11-25). A path route at the door cannot fix it: CaddyUI owns srv0 and rebuilds the
# Caddyfile from its own rows, which carry host+port only, so a `handle_path /fleet/*` added
# by hand would be erased on the next caddyui start. The proxy therefore lives here: /fleet/*
# is the OpsBrain app, /stream is its websocket, and both are served from the origin the page
# already authenticated against. Nothing is added to the door and no second name is needed.
OPSBRAIN_UPSTREAM = (
    os.environ.get("HERMES_DASH_OPSBRAIN_HOST", "127.0.0.1"),
    int(os.environ.get("HERMES_DASH_OPSBRAIN_PORT", "9120")),
)
FLEET_PREFIX = "/fleet"
FLEET_MAX_BYTES = 8 * 1024 * 1024     # one page or one JSON body; a runaway is cut, not buffered
FLEET_TIMEOUT = 8.0                   # connect + read for a plain request
FLEET_WS_IDLE = 900.0                 # a quiet websocket is kept, a dead one is not
# The upstream page and its script address their own assets and API root-absolutely
# ("/static/app.js", fetch("/api/groups")), because at its own origin it IS the root.
# Under /fleet those would resolve to this server's own /static and /api, so the two
# prefixes are rewritten once per text response. Only quoted forms are touched (a bare
# substring would also hit prose and paths inside strings); JSON is left byte-identical,
# because it is data and a rewritten string in it would be a lie.
_FLEET_REWRITES = (
    (b'"/static/', b'"/fleet/static/'),
    (b"'/static/", b"'/fleet/static/"),
    (b'"/api/', b'"/fleet/api/'),
    (b"'/api/", b"'/fleet/api/"),
    (b'href="/report"', b'href="/fleet/report"'),
    (b"href='/report'", b"href='/fleet/report'"),
)
_FLEET_REWRITABLE = ("text/html", "application/javascript", "text/javascript")

_cache: dict[str, tuple[float, dict]] = {}


def state_for(host: str) -> dict:
    """Collector output, cached for a few seconds so a fast refresh is cheap."""
    key = host
    hit = _cache.get(key)
    now = time.monotonic()
    ttl = CACHE_TTL.get(host, 3.0) if isinstance(CACHE_TTL, dict) else CACHE_TTL
    if hit and now - hit[0] < ttl:
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

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
              csp: bool = True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if csp:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';"
                " frame-src 'self' http://127.0.0.1:9120 http://localhost:9120"
                f" {OPSBRAIN_HTTP.rstrip('/')} {OPSBRAIN_HTTPS.rstrip('/')}",
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

    @property
    def srv(self) -> "Server":
        """Typed view of the running server (BaseHTTPRequestHandler types it as BaseServer)."""
        return self.server  # type: ignore[return-value]

    def _host_ok(self) -> bool:
        port = self.srv.server_address[1]
        allowed = {
            f"{self.srv.bind_address}:{port}".lower(),
            f"localhost:{port}".lower(),
            f"[::1]:{port}".lower(),
        }
        for name in PUBLIC_HOSTS:
            # caddy sends the bare host on 443; accept the explicit-port forms too
            allowed.update({name, f"{name}:443", f"{name}:{port}"})
        host = (self.headers.get("Host") or "").strip().lower()
        return host in allowed

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
        elif route == "/stream":
            self._fleet_ws(parsed.query)
        elif route == FLEET_PREFIX or route.startswith(FLEET_PREFIX + "/"):
            self._fleet_proxy(route, parsed.query)
        elif route == "/api/health":
            self._json({"ok": True, "hosts": list(HOSTS), "bind": self.srv.bind_address, "public_hosts": list(PUBLIC_HOSTS)})
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
            if name == "index.html":
                # one substitution point; the static snapshot keeps the raw file and
                # app.js falls back to the defaults when the placeholders survive.
                body = (
                    body.decode()
                    .replace("{{OPSBRAIN_HTTP}}", OPSBRAIN_HTTP)
                    .replace("{{OPSBRAIN_HTTPS}}", OPSBRAIN_HTTPS)
                    .encode()
                )
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

    # -------------------------------------------------- OpsBrain, same-origin (fleet)
    @staticmethod
    def _fleet_rewrite(body: bytes | str) -> bytes | str:
        """Re-point the upstream's root-absolute /static, /api and /report at /fleet."""
        for old, new in _FLEET_REWRITES:
            if isinstance(body, bytes):
                body = body.replace(old, new)
        return body

    def _fleet_proxy(self, route: str, query: str):
        """Serve the OpsBrain dashboard under /fleet so the Home tab shares this origin.

        GET only (the do_GET table is the whole surface), read-only, and the upstream is a
        loopback app with no auth of its own — the door's basic_auth on this host is the gate,
        exactly as it was for the iframe it replaces.
        """
        upstream_path = route[len(FLEET_PREFIX):] or "/"
        if not upstream_path.startswith("/"):
            upstream_path = "/" + upstream_path
        target = upstream_path + (f"?{query}" if query else "")
        conn = None
        try:
            conn = http.client.HTTPConnection(*OPSBRAIN_UPSTREAM, timeout=FLEET_TIMEOUT)
            conn.request("GET", target, headers={
                "Host": f"{OPSBRAIN_UPSTREAM[0]}:{OPSBRAIN_UPSTREAM[1]}",
                "Accept": self.headers.get("Accept") or "*/*",
                # identity: a text body may be rewritten, so a compressed one is useless here
                "Accept-Encoding": "identity",
            })
            resp = conn.getresponse()
            body = resp.read(FLEET_MAX_BYTES + 1)
            if len(body) > FLEET_MAX_BYTES:
                self._text(f"502 the OpsBrain response is larger than {FLEET_MAX_BYTES} bytes\n",
                           code=502)
                return
            ctype = resp.getheader("Content-Type") or "application/octet-stream"
            extra = {}
            location = resp.getheader("Location")
            if location:
                extra["Location"] = self._fleet_rewrite(location)
            if ctype.split(";")[0].strip().lower() in _FLEET_REWRITABLE:
                body = self._fleet_rewrite(body)
            # csp=False: that page is its own app — inline styles and a CDN script — and this
            # server's policy would blank it. Passing the upstream's status through unchanged
            # keeps a 404 or a 500 visible as one instead of becoming a quiet 200.
            self._send(resp.status, body, ctype, extra=extra, csp=False)
        except (OSError, http.client.HTTPException) as exc:
            self._text(
                f"502 the OpsBrain dashboard is not answering on "
                f"{OPSBRAIN_UPSTREAM[0]}:{OPSBRAIN_UPSTREAM[1]} ({type(exc).__name__}: {exc})\n",
                code=502,
            )
        finally:
            if conn is not None:
                conn.close()

    def _fleet_ws(self, query: str):
        """Relay the OpsBrain live stream. After the 101 this is bytes, not HTTP.

        The page renders only on a websocket message, so without this relay the embedded
        Home tab would be permanently blank — which is why the route exists rather than a
        note saying the stream is unavailable.
        """
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        if "websocket" not in (self.headers.get("Upgrade") or "").lower() or not key:
            self._text("400 this route relays a websocket upgrade only\n", code=400)
            return
        upstream = None
        try:
            upstream = socket.create_connection(OPSBRAIN_UPSTREAM, timeout=FLEET_TIMEOUT)
            upstream.settimeout(FLEET_WS_IDLE)
            version = (self.headers.get("Sec-WebSocket-Version") or "13").strip()
            upstream.sendall(
                (f"GET /stream{'?' + query if query else ''} HTTP/1.1\r\n"
                 f"Host: {OPSBRAIN_UPSTREAM[0]}:{OPSBRAIN_UPSTREAM[1]}\r\n"
                 "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: {version}\r\n\r\n"
                 ).encode()
            )
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = upstream.recv(4096)
                if not chunk:
                    raise OSError("upstream closed before the handshake finished")
                head += chunk
                if len(head) > 65536:
                    raise OSError("upstream handshake larger than 64 KiB")
            status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 101" not in status:
                self._text(f"502 the OpsBrain live stream was refused: {status}\n", code=502)
                return
            self.close_connection = True
            self.wfile.write(head)
            self.wfile.flush()
            self._relay(self.connection, upstream)
        except OSError as exc:
            self._text(
                f"502 the OpsBrain live stream is not answering ({type(exc).__name__}: {exc})\n",
                code=502,
            )
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass

    @staticmethod
    def _relay(client: socket.socket, upstream: socket.socket):
        """Copy bytes both ways until either side closes; no frame parsing, no interpretation."""
        def pump(src: socket.socket, dst: socket.socket):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        thread = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        thread.start()
        pump(client, upstream)
        thread.join(timeout=5.0)


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
