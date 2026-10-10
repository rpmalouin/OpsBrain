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
   table with online/offline health dots, plus a host-facts sub-line (OS, CPU, cores, RAM,
   worst watched disk) for nodes that report a `host` block instead of containers.
4. **TrueNAS** — pool status + health, system version/model/RAM/uptime, disk count,
   active alerts.
5. **Container Health** — running/exited, per-container CPU/RAM, restart-loop badge,
   restart count.
6. **GPU Drift** — VRAM used/total + %, power draw, temperature, drift flags, plus a driver-reconcile strip: red when the loaded nvidia module does not match the one on disk (naming the deferred apt-hook reload and the exact fix command), info when a deferral was since resolved
   (color-coded), stuck-PID + cycle count.
7. **OpsBrain Decisions** — Qwen warnings/actions, confidence, **dry-run / live** badge.
8. **Manual Stop Protection** — protected (manually stopped) containers; red
   "MANUALLY STOPPED" tag on rows.
9. **Dockhand** — desired-state drift: drift count (colour-coded), attention level,
   one-line summary, and counts/listings for stacks drifting, restart storms, health
   flaps, orphaned containers, and missing resources. Status reflects `dockhand.up` /
   `enabled`, so a down DB is visible immediately. (Source: the `dockhand` key in
   `logs/collector.json`.)
10. **Daily Report Preview** — last-24h summary, anomaly/remediation/drift-event counts,
    link to the full report at `/report`.
11. **Refinements** (from `ui_refinements.js`) — confidence recovery pulse, GPU drift
    decay graph, container restart-impact bars.

## Notification ticker

The **Live notifications** panel renders the most recent 8 entries, newest first, with the
level chip and the event's own UTC time (`HH:MMZ`). Two producers feed it:

1. conditions the dashboard itself detects from the current collector/reasoner/actions docs
   (below-floor confidence, GPU drift flags, restart loops, packet drops, restarts OpsBrain
   performed), which carry levels straight from `ui/server.py::_check_notifications`;
2. the tail of `logs/notifications.jsonl`, level-mapped by category
   (`_category_level`: gpu_*/manual_stop/restart/cluster_health_warning/reasoner_stale → critical,
   dockhand/vault/cluster_*/disk/low_confidence/packet_drop → warning, else info).

The panel is LIVE-ONLY: the JSONL ring is a byte offset seeded at EOF on the first read after a
UI start, so a restart never replays historical records (it used to dump the last 100 of a
34k-line log into the ticker). The ring is read on EVERY refresh — `notifications.jsonl` is not a
watched file and the pipeline writes it after the collector, so an ingest tied to a collector
change showed each event up to an hour late — and a new alert is itself a reason to broadcast to
clients. The header chip counts what is wrong (`⚠ N critical`, else `N warn`), not the length of
the in-memory list.

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

`/api/gpu` returns the GPU block plus `available` and `error` (from
`collector.gpu.available`/`collector.gpu.error`), so a host that can no longer read the
card reports that instead of empty metrics.

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