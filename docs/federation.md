# Federation layer (multi-node)

Ops Brain can reason about **multiple nodes as a unified cluster**. A node is polled over HTTP and
must serve JSON in one of two shapes, selected by the node's `kind` (absent = `opsbrain`):

| `kind` | What the endpoint must serve | Use for |
|---|---|---|
| `opsbrain` (default) | this host's dashboard envelope `http://<node>:9120/api/status` (which embeds the `collector` doc), or a raw `collector.json` | a host running Ops Brain |
| `netdata` | a **Netdata HTTP API** (`http://<node>:19999`) | a host running Netdata but **not** Ops Brain — reduced by `federation/netdata_node.py` |

## Configure nodes

```yaml
federation:
  enabled: true
  nodes:
    - name: dockervm
      type: linux
      collector_endpoint: "http://127.0.0.1:9120/api/status"
    - name: macstudio                # Netdata-sourced: nothing is installed on the Mac
      type: macos
      kind: netdata
      collector_endpoint: "http://10.1.10.34:19999"
      timeout_s: 6
      volumes: ["/", "/Volumes/Data", "/Volumes/Data2", "/Volumes/Data3",
                "/Volumes/Data4", "/Volumes/TimeMachine"]
      alarm_keep: ["CRITICAL", "WARNING", "ERROR"]
      alarm_skip_names: ["disk_fill_rate", "disk_inode_rate"]
      alarm_skip_mounts: ["/Volumes/makemkv", "/Volumes/Gemini", "/Volumes/Install",
                          "/Volumes/MusicBrainz", "/Volumes/documents", "/Volumes/mystuff",
                          "/Volumes/appdata", "/Volumes/hermes", "/Volumes/downloads",
                          "/Volumes/music", "/Volumes/media",
                          "/Volumes/.timemachine", "com.apple.TimeMachine.localsnapshots",
                          "/Volumes/Recovery", "/System/Volumes/VM", "/System/Volumes/Preboot",
                          "/System/Volumes/Update", "/System/Volumes/xarts",
                          "/System/Volumes/iSCPreboot", "/System/Volumes/Hardware",
                          "/System/Volumes/Data/home"]
    # - name: truenas
    #   type: storage
    #   collector_endpoint: "http://truenas:9120/api/status"
  cluster_stability_weights:   # weights in the stability formula
    confidence: 0.4
    drift: 0.3
    anomalies: 0.2
    restarts: 0.1
  poll_interval_cycles: 2      # run federation every N scheduler cycles (2 h at 3600s cadence)
```

Replace the endpoints with your nodes' actual snapshot URLs, then restart the daemon (a config-only
change needs no restart: the scheduler shells out to the federation scripts per run, so the next
federation cycle picks it up).

**The endpoint is the node's dashboard port (`:9120`), not an arbitrary one.** A wrong port
does not fail loudly — the node just reads offline forever: on dockerVM, `:8099` is
*caddy-editor* (HTTP 401/404), so a `http://dockervm:8099/api/status` endpoint produced a
permanent "0/2 online, stability 0" Cluster Overview and a critical `cluster_health_warning`
plus `escalate_cluster` on **every** federation cycle. Verify a candidate endpoint by hand
before wiring it in — it must return HTTP 200 with the `/api/status` envelope (or a raw
`collector.json`):

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://<node>:9120/api/status
```

A node you cannot serve yet is better commented out than left configured: an offline node
degrades gracefully at the collector level but still raises a standing critical recommendation.
Note also that the actions stage consumes the PREVIOUS cycle's cluster result (stage order is
collector → reasoner → actions → federation), so a fix shows up one cycle later.

## Netdata-sourced nodes (`kind: netdata`)

`federation/netdata_node.py` reduces a remote Netdata API into the same node block an OpsBrain node
produces, so a host that runs Netdata but not Ops Brain (the Mac Studio) joins the cluster with
**nothing installed on it** — no agent, no Ops Brain, no writes. Per cycle it reads:

```
GET /api/v1/info                            -> host facts (os, cpu model, cores, RAM, uptime)
GET /api/v1/alarms?all                      -> active alarms, filtered (see below)
GET /api/v1/data?chart=disk_space.<mount>   -> per-configured-volume usage
GET /api/v1/data?chart=system.ram           -> RAM in use
```

Node block: `confidence: null` (there is no reasoner on that host — the federation reasoner gives an
online node with null confidence **full credit**, and `cluster_confidence` ignores it rather than
averaging a fake 0), `anomalies` = kept alarm count, `drift_events`/`restart_events` = 0,
`containers` = `0/0` (no Docker there), plus display-only `host`, `volumes`, `alarms`, `uptime_s`.

Three traps this adapter exists to absorb — each measured on the Mac, 2026-10-10:

1. **The only CRITICAL there was a mounted installer disk image — and a remote server's capacity is
   not this node's anomaly.** Per-mount alarms are the host's own verdicts, but three classes of mount
   must never reach the cluster: transient installer images and personal archives (the standing
   CRITICAL was `disk_space_usage` on `/Volumes/makemkv_v2.0.0` at 98.8 %, a MakeMKV installer DMG
   that was **not even mounted** when it was checked — Netdata serves the stale chart, so the alarm
   sticks), **SMB mounts belonging to another machine** (`/Volumes/appdata` and `/Volumes/hermes` are
   this host's own shares, `/Volumes/media` is TrueNAS — reporting their fullness as a Mac anomaly is
   a false-alarm generator), and Time Machine / macOS system views, which appear and disappear by
   design. Hence `alarm_skip_mounts`; `alarm_skip_names` separately drops the `disk_fill_rate` /
   `disk_inode_rate` alarms that are UNINITIALIZED on every Time Machine snapshot mount.
2. **Never enumerate disk charts.** The Mac exposes 3288 charts, most of them Time Machine
   snapshot and DMG mounts, so the worst-volume metric would be garbage. `volumes:` is an explicit
   list.
3. **`disk_space.*` dimensions are GiB, not percent.** The root volume reports `used=315.3` on a
   34 % full disk; the percentage is computed from `used / (used + avail)`. RAM is
   `active + wired + compressor` (Activity Monitor's "Memory Used") — the naive
   `total - free` reports ~98 % on a healthy macOS box, because macOS counts cache as occupied.

Alarms and per-volume disk are best-effort: a chart that 404s leaves a null field, never an offline
node. Only `/info` and `/alarms` failing marks the node offline.

**Pin the address, and make the pin durable.** `collector_endpoint` carries the literal IP because
mDNS does not resolve from dockerVM (`Mac-Studio.local` fails). If that address comes from DHCP — the
Mac's en0 is a 24 h lease from `10.1.10.1` — put a DHCP reservation on the router: a moved lease reads
as an **offline node**, which is a standing critical `cluster_health_warning` on every federation
cycle, not a quiet degradation.

`raw` is deliberately **compact** (alarm names + GPU drift flags, nothing else): it exists so the
reasoner can correlate signals across nodes, and attaching the whole collector document there once
grew the cluster snapshot from ~0.5 KB to ~255 KB per cycle for no added signal.

## Node snapshot files (Phase 2 — what the live poll cannot see)

A node entry may also carry `snapshot_file` (and `snapshot_max_age_s`), pointing at a JSON file the
**node itself** writes into this host's `logs/`. Ownership follows the vault-drift rule: **OpsBrain is
the consumer only, never the writer.** The Mac's 15-minute cron
(`~/.hermes/scripts/mac-node-snapshot.py`, writing over the SMB share) produces
`logs/macstudio_node.json` with what a Netdata poll cannot reach: Time Machine's last successful
backup, per-mount health (mounted / read-only / absent), Hermes cron health, the gateway heartbeat,
the local model router, and the **loopback-only** services (Hermes Portal 8087, Hindsight 9177/19177,
llama.cpp 18434) that no host off the machine can poll at all.

`federation/local_snapshot.py` reads and merges it into the same node block:

| `snapshot.state` | meaning | effect on `anomalies` |
|---|---|---|
| `ok` | fresh file | the producer's own `alert_count` **plus** the collector's own state checks |
| `stale` | older than `snapshot_max_age_s` (default **2700 s = 45 min, three missed ticks** — the producer's own contract) | **+1** — a producer that stopped while the live poll stayed green is a silent failure; its old data is NOT re-judged |
| `missing` | no file | +0 — the producer is optional; recorded so the dashboard can say so |
| `unreadable` | half-written/foreign payload | +0, error kept in the block |

Two kinds of finding arrive from the producer, and they are kept apart on purpose:

- **Its `alerts[]`** — the Mac's own curated judgments (e.g. a Hermes cron job failing every 15
  minutes for days). Counted as-is: the collector must never re-derive a threshold the Mac already
  applied.
- **`derived_alerts[]`** — the few *state* checks the collector makes itself, because the producer
  reports facts rather than verdicts: a mount that is **not mounted**, the Hermes **gateway heartbeat**
  missing or older than `snapshot_gateway_stale_s` (default 300 s), and a **Time Machine** backup older
  than `snapshot_tm_stale_hours` (default 48 h). These run on FRESH data only.
- **Capacity is never judged.** `disks[].used_percent` is surfaced, never alerted: a volume at 82 % is
  the Mac's steady state, and re-scoring it here is how a node becomes noise.

The live poll keeps ownership of what it measures (online/offline, alarms, per-volume disk); the
snapshot only adds. `host` facts merge with the **live values winning**, so the panel can show
`Mac17,14`, `load_avg` and `uptime_hours` from the producer beside Netdata's `cpu`/`cores`/`RAM`.
The merge runs for offline nodes too — a live producer beside a dead poll is a different fault from a
dead machine.

## What it computes

Every `poll_interval_cycles`, the federation collector merges each node into
`logs/cluster_snapshot.json` and the reasoner writes `logs/cluster_reasoner_result.json`.

- **Cluster stability score (0–100):**
  `(confidence*0.4 + drift*0.3 + anomalies*0.2 + restarts*0.1) * 100`, where
  drift/anomalies/restarts are reverse-normalized (`1 − n/max_bad`, budgets 20/20/10).
  An online node with null confidence gets full credit; an offline node scores 0.
- **Node ranking** by per-node stability.
- **Cross-node correlations:** nodes sharing the same Netdata alarm `name`
  (`anomaly_correlations`) or the same GPU drift flag (`drift_correlations`).
- **Recommendations:** offline node → `cluster_health_warning`; score < 60 →
  `escalate_cluster`; any anomalies/drift → `notify_cluster`.

## Safety

All federation actions are **notify-only** — the cluster layer never performs cross-node
remediation. Per-node remediation still obeys confidence gating, manual-stop protection,
and restart caps.

## Dashboard + report

- **Cluster Overview** panel: stability score, nodes online, avg confidence, totals,
  recommendations.
- **Node Comparison** panel: per-node confidence / drift / anomalies / restarts /
  stability with online/offline health dots, plus a sub-line of host facts (OS, CPU, cores, RAM,
  worst watched disk) for nodes that report a `host` block instead of containers.
- Daily report gains a **Cluster Summary** section.

## Degradation

If a node is unreachable it is marked offline, the cluster score drops, and a health
warning is emitted — no automation is triggered.