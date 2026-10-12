# Federation layer (multi-node)

Ops Brain can reason about **multiple nodes as a unified cluster**. Every node reduces to the same node
block; how its telemetry is obtained is selected by the node's `kind` (absent = `opsbrain`).

| `kind` | Source | Use for |
|---|---|---|
| `opsbrain` (default) | this host's dashboard envelope `http://<node>:9120/api/status` (which embeds the `collector` doc), or a raw `collector.json` | a host running Ops Brain |
| `snapshot` | **no endpoint at all** — the node's own producer snapshot file, written into this host's `logs/` (see *Snapshot-sourced nodes* below) | a host that runs no Ops Brain and needs no agent: it writes one file, we read it |

## Configure nodes

```yaml
federation:
  enabled: true
  nodes:
    - name: dockervm
      type: linux
      collector_endpoint: "http://127.0.0.1:9120/api/status"
    - name: macstudio                # snapshot-sourced: no endpoint, nothing to poll
      type: macos
      kind: snapshot
      snapshot_file: "logs/macstudio_node.json"
      snapshot_max_age_s: 2700       # soft: 3 missed ticks (45 min) -> one anomaly, node stays online
      snapshot_dead_after_s: 7200    # hard: 8 missed ticks (2 h)  -> the node reads OFFLINE
      snapshot_tm_stale_hours: 48
      snapshot_gateway_stale_s: 300
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

Replace the endpoints with your nodes' actual snapshot URLs. A config-only change needs no restart: the
scheduler shells out to the federation scripts per run, so the next federation cycle picks it up.

**For a polled node the endpoint is the node's dashboard port (`:9120`), not an arbitrary one.** A wrong
port does not fail loudly — the node just reads offline forever: on dockerVM, `:8099` is *caddy-editor*
(HTTP 401/404), so a `http://dockervm:8099/api/status` endpoint produced a permanent "0/2 online,
stability 0" Cluster Overview and a critical `cluster_health_warning` plus `escalate_cluster` on **every**
federation cycle. Verify a candidate endpoint by hand before wiring it in — it must return HTTP 200 with
the `/api/status` envelope (or a raw `collector.json`):

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://<node>:9120/api/status
```

A node you cannot serve yet is better commented out than left configured: an offline node degrades
gracefully at the collector level but still raises a standing critical recommendation. Note also that the
actions stage consumes the PREVIOUS cycle's cluster result (stage order is collector → reasoner → actions
→ federation), so a fix shows up one cycle later.

## Snapshot-sourced nodes (`kind: snapshot`)

A node with no Ops Brain and no endpoint to poll can still be a cluster member: it writes ONE JSON file
into this host's `logs/`, and for it **that file IS the node**. `federation_collector._collect_node`
returns an empty shell for such a node (no endpoint attempt, and deliberately no `err` — nothing was
polled, so nothing failed) and `federation/local_snapshot.py` merges it with `primary=True`, which
decides `online` as well as supplying the panel's display fields.

The Mac Studio is the reference case (2026-10-12): its 15-minute cron
(`~/.hermes/scripts/mac-node-snapshot.py`) writes over SMB, and the file carries what no poll can reach —
Time Machine's last successful backup, per-mount health (mounted / **read-only** / absent), Hermes cron
health, the gateway heartbeat, the local model router, the **loopback-only** services (Hermes Portal 8087,
Hindsight 9177, llama.cpp 18434), the six watched volumes with total/used/free, and the SoC telemetry from
`macmon` in its `soc` block.

**Ownership follows the vault-drift rule: OpsBrain is the CONSUMER only, never the writer.**

| `snapshot.state` | meaning | `online` | effect on `anomalies` |
|---|---|---|---|
| `ok` | fresh file | **online** | the producer's own `alert_count` **plus** `derived_alerts` |
| `stale` | older than `snapshot_max_age_s` (default **2700 s = 45 min = three missed ticks**, the producer's own contract) | **online** | **+1** — a missed tick is a transient, and the file's old facts are NOT re-judged |
| `dead` | older than `snapshot_dead_after_s` (default **7200 s = 2 h**) | **offline** | **+1** — a producer that stopped for hours is a fault, and it must not look like a missed tick |
| `missing` | no file | **offline** | **+1** — for a primary source the file IS the node |
| `unreadable` | half-written / foreign payload | **offline** | **+1**, error kept in the block |

**Two tiers, not one, and not zero.** A single 45-minute threshold turns a reboot or an SMB remount into a
standing critical (`the truenas lesson`: a node that is permanently red gets disabled, and then it is not
watched at all); a single infinite one lets a producer die for a week without the board going dark. The
cluster score is computed from ONLINE nodes only, so an offline node appears as a **dark node plus a
critical `cluster_health_warning`**, never as a lower score. Note that a node's faults are also excluded
from `cluster_metrics` while it is offline — the dark node and its recommendation ARE the signal.

Two kinds of finding arrive from the producer, kept apart on purpose:

- **Its `alerts[]`** — the host's own curated judgments (e.g. a Hermes cron job failing every 15 minutes
  for days). Counted as-is: the collector must never re-derive a threshold the host already applied, and
  `alerts[]` is authoritative (a cleared alert simply disappears from the array).
- **`derived_alerts[]`** — the few *state* checks the collector makes itself, because the producer reports
  facts rather than verdicts: a mount **not mounted**, a mount mounted **read-only**, the Hermes
  **gateway heartbeat** missing or older than `snapshot_gateway_stale_s` (300 s), a **Time Machine**
  backup older than `snapshot_tm_stale_hours` (48 h), **memory pressure** (`ram_available` under 10 % of
  RAM, heavy under 5 %) and a **fan at 0 rpm**. These run on FRESH data only.
- **Capacity is never judged.** `disks[].used_percent` is surfaced for every watched volume and never
  alerted: a volume at 82 % is the Mac's steady state, and re-scoring it here is how a node becomes noise.

### The `soc` block (macmon)

`macmon` (Ron's fork `rpmalouin/macmon`: a menu bar app + a C ABI over upstream `vladkens/macmon`) reaches
the node as one verbatim sample in the snapshot's `soc` block — `metrics` is macmon's own `Metrics`
serialization, `soc_info` its `SocInfo`, plus `sampled_at` / `source` / `available` / `error`. **The
producer reports; the collector derives** every percentage, pressure reading and verdict. (The Homebrew
`macmon` is upstream's older CLI without `ram_available`; read the fork, not its docs.)

- **`ram_usage` is NOT a pressure figure.** It is Activity Monitor's "Memory Used" and it counts
  reclaimable inactive pages, so a machine holding a resident ~35 GB model reads 87-92 % while nothing is
  wrong. It is rendered and never thresholded. `ram_available` (free + inactive + purgeable + speculative)
  is what the kernel can still hand out, it is what macmon's own menu bar judges, and it is the only one
  this collector judges — heavy below 5 %, moderate below 10 %, the fork's own lines. `ram_available` is 0
  when the kernel does not report it, and 0 must read as UNKNOWN, never as an out-of-memory machine.
- **A failed read must never look like 0 W.** `soc.available: false` + `soc.error` are rendered; on macOS
  27 the CPU/ANE IOReport counters can stop updating without a private entitlement, and the fork's CLPC
  path is what covers it. If the watts are unavailable the node shows temperatures, memory and usage and
  the power row is dropped.
- **Per-tier CPU aggregates render (P 12 c / S 6 c with frequency and scaled ratio); the 18 per-core rows
  do not** — they would swamp a two-node table, and the per-core detail stays in the fixture and the
  reasoner digest.

**Do not poll a `macmon serve` endpoint instead.** A scrape would sample a finer source more coarsely than
the file already does (farmore than the poll's 2-hour cadence versus the producer's 15 minutes) and would
reintroduce a pinned address, which is the failure mode this kind exists to delete. If a host runs
`macmon serve` for its own tools, keep it on loopback — it has no auth and no TLS.

`raw` is deliberately **compact** (alert tokens + GPU drift flags, nothing else): it exists so the
reasoner can correlate signals across nodes, and attaching the whole collector document there once grew
the cluster snapshot from ~0.5 KB to ~255 KB per cycle for no added signal. A snapshot-sourced node
attaches `raw.collector.snapshot.alert_keys` — stable tokens (`time_machine`, `cron`, `mount_read_only`,
`memory_pressure`, `fan_stopped`, …) — because it has no Netdata alarm names to correlate on, and a node
that reports nothing comparable makes the whole cluster's correlation read empty, silently.

## What it computes

Every `poll_interval_cycles`, the federation collector merges each node into
`logs/cluster_snapshot.json` and the reasoner writes `logs/cluster_reasoner_result.json`.

- **Cluster stability score (0–100):**
  `(confidence*0.4 + drift*0.3 + anomalies*0.2 + restarts*0.1) * 100`, where
  drift/anomalies/restarts are reverse-normalized (`1 − n/max_bad`, budgets 20/20/10).
  An online node with null confidence gets full credit; an offline node scores 0.
- **Node ranking** by per-node stability.
- **Cross-node correlations:** nodes sharing an alarm token — `netdata.alarms_active[].name` for a polled
  node, `snapshot.alert_keys` for a snapshot-sourced one (`anomaly_correlations`) — or the same GPU drift
  flag (`drift_correlations`).
- **Recommendations:** offline node → `cluster_health_warning`; score < 60 →
  `escalate_cluster`; any anomalies/drift → `notify_cluster`. Each reason **names the nodes behind it** —
  a bare count is unactionable in the ticker.

## Safety

All federation actions are **notify-only** — the cluster layer never performs cross-node
remediation. Per-node remediation still obeys confidence gating, manual-stop protection,
and restart caps.

## Dashboard + report

- **Cluster Overview** panel: stability score, nodes online, avg confidence, totals,
  recommendations.
- **Node Comparison** panel: per-node confidence / drift / anomalies / restarts /
  stability with online/offline health dots, plus sub-lines for a node that reports a `host` block
  instead of containers: host facts (OS, chip, cores, RAM, **RAM used AND available**), the macmon row
  (temperatures, power, per-tier CPU, fans), **every watched volume** with its percentage, and the
  snapshot line — its state and age, the file it came from, cron health, and **all** of its findings.
- Daily report gains a **Cluster Summary** section.

## Degradation

If a node is unreachable it is marked offline, the cluster score drops, and a health
warning is emitted — no automation is triggered. A live producer beside a dead poll is a different fault
from a dead machine, and the two are distinguishable in the snapshot block.
