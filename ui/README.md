# OpsBrain UI — real-time dashboard

Serves a single-page operational dashboard for the OpsBrain pipeline. It streams
the live JSON log files (including the manual-stop registry and the federation
cluster snapshot/reasoner) over a WebSocket and renders the panels, refreshed every
2 seconds on change.

## Layout

```
ui/
  server.py           FastAPI app + WebSocket + file-watch background task
  config.yaml         host / port / refresh interval / watched files
  templates/dashboard.html
  static/app.js       WS client + panel rendering (TailwindCSS via CDN)
  static/ui_refinements.js   recovery / drift-decay / restart-impact panels
  README.md
```

## Panels

1. **System Overview** — GPU util, disk, running containers, **confidence score**,
   **last cycle age**, **next scheduled cycle**.
2. **Cluster Overview** — cluster stability score (0–100), nodes online, avg
   confidence, total anomalies / drift / restart events, cluster recommendations.
3. **Node Comparison** — per-node confidence / drift / anomalies / restarts /
   stability table with online/offline health dots.
4. **TrueNAS** — pool status + health, system version/model/RAM/uptime, disk count,
   active alerts.
5. **Container Health** — running/exited, per-container CPU/RAM, restart-loop badge,
   restart count.
6. **GPU Drift** — VRAM used/total + %, power draw, temperature, drift flags
   (color-coded: `stuck_process`/`vram_overload` = red, others = yellow), stuck-PID +
   same-pid cycle count.
7. **OpsBrain Decisions** — Qwen `warnings[]`, `actions[]`, confidence, **dry-run /
   live** badge.
8. **Manual Stop Protection** — protected (manually stopped) containers; red
   "MANUALLY STOPPED" tag on rows.
9. **Daily Report Preview** — last-24h summary, anomaly/remediation/drift-event counts,
   link to the full report at `/report`.
10. **Refinements** (from `ui_refinements.js`) — confidence recovery pulse, GPU drift
    decay graph, container restart-impact bars.

## Files streamed (WebSocket, every 2s on change)

- `logs/collector.json`
- `logs/reasoner_result.json`
- `logs/actions_result.json`
- `logs/gpu_baseline.json`
- `logs/manual_stops.json`
- `logs/cluster_snapshot.json`
- `logs/cluster_reasoner_result.json`

The server reads these only when they change (mtime+size), merges them into a single
document `{_meta, sources:{...}, collector, reasoner, actions, gpu_baseline,
manual_stops, cluster_snapshot, cluster_reasoner}`, and broadcasts to every connected
client.

## Run

Requires `fastapi`, `uvicorn`, `websockets`, `pyyaml` (`pip install fastapi uvicorn websockets pyyaml`).

```bash
python3 ui/server.py                       # uvicorn on 0.0.0.0:9120
# or
uvicorn ui.server:app --host 0.0.0.0 --port 9120
```

### systemd (persistent)

Pre-registered as `opsbrain-ui.service`:

```bash
systemctl daemon-reload
systemctl enable --now opsbrain-ui
tail -f /var/log/opsbrain-ui.log   # optional: see service file
```

## Config

`config.yaml` → `server.port` (default 9120), `server.refresh_seconds` (default 2),
`server.host` (default 0.0.0.0), and `watch[]` (the log files). Change and
`systemctl restart opsbrain-ui`.

## Behind Caddy

Published as **https://opsbrain.home** via the homelab `caddy` container
(`network_mode: host`). Route in `/appdata/caddy/Caddyfile`:

```
opsbrain.home {
    reverse_proxy 192.0.2.10:9120 {
        flush_interval -1   # keep the 2s WS /stream alive through the proxy
    }
    tls internal
}
```

WS upgrade is proxied automatically. **Gotcha:** the caddy container bind-mounts a single
file, so editing the Caddyfile with a write-replacing tool (patch/write_file) creates a new
inode the running container won't see — `caddy reload` will silently apply the stale config.
Run `docker restart caddy` after editing, then confirm
`docker exec caddy grep -c <sitename> /etc/caddy/Caddyfile` shows the change.

## Notes

- The dashboard binds `0.0.0.0` for LAN access. If it's exposed beyond a trusted
  network, put a reverse proxy (Caddy/nginx) + auth in front — there is no auth
  layer yet.
- TailwindCSS is loaded from the CDN; for a fully offline box, vendor `tailwind.min.css`
  into `ui/static/` and reference it instead.