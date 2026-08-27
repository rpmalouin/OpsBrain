# Dashboard

A FastAPI + WebSocket single-page dashboard served at `http://<host>:9120/`.

## Run

```bash
# via the opsbrain-ui service (see deployment.md), or manually:
uvicorn server:app --app-dir /appdata/OpsBrain/ui --host 0.0.0.0 --port 9120
```

Config: `ui/config.yaml` — `server.port`, `server.refresh_seconds`, and `watch[]` (the
list of log files streamed on change).

## Panels

1. **System Overview** — GPU util, disk, running containers, confidence score, last cycle
   age, next scheduled cycle.
2. **Cluster Overview** — cluster stability score (0–100), nodes online, avg confidence,
   total anomalies / drift / restart events, recommendations.
3. **Node Comparison** — per-node confidence / drift / anomalies / restarts / stability
   table with online/offline health dots.
4. **TrueNAS** — pool status + health, system version/model/RAM/uptime, disk count,
   active alerts.
5. **Container Health** — running/exited, per-container CPU/RAM, restart-loop badge,
   restart count.
6. **GPU Drift** — VRAM used/total + %, power draw, temperature, drift flags
   (color-coded), stuck-PID + cycle count.
7. **OpsBrain Decisions** — Qwen warnings/actions, confidence, **dry-run / live** badge.
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

Merged into one doc `{_meta, sources, collector, reasoner, actions, gpu_baseline,
manual_stops, cluster_snapshot, cluster_reasoner}` and broadcast to every client.

## Reverse proxy (recommended for LAN exposure)

Example **Caddy** route (the dashboard has no auth layer — front it with auth if exposed
beyond a trusted LAN):

```
opsbrain.home {
    reverse_proxy 127.0.0.1:9120 {
        flush_interval -1    # keep the 2s WebSocket stream alive through the proxy
    }
    tls internal
}
```

> **PITFALL — bind-mount inode staleness.** If the reverse-proxy container bind-mounts a
> single config file, editing it with a write-replacing tool (patch/write) creates a NEW
> inode the running container won't see — `caddy reload` silently applies the STALE
> config. Run `docker restart caddy` after editing, then confirm the change landed inside
> the container.