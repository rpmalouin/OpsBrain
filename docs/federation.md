# Federation layer (multi-node)

Ops Brain can reason about **multiple nodes as a unified cluster**. Each node must expose
a collector snapshot as JSON over HTTP — the easiest way is to run Ops Brain on that node
too and use its dashboard endpoint (`http://<node>:9120/api/status`, which includes the
`collector` doc). The federation collector also tolerates a raw `collector.json` document.

## Configure nodes

```yaml
federation:
  enabled: true
  nodes:
    - name: dockervm
      type: linux
      collector_endpoint: "http://dockervm:8099/api/status"
    - name: truenas
      type: storage
      collector_endpoint: "http://truenas:8099/api/status"
  cluster_stability_weights:   # weights in the stability formula
    confidence: 0.4
    drift: 0.3
    anomalies: 0.2
    restarts: 0.1
  poll_interval_cycles: 2      # run federation every N scheduler cycles (4 min at 120s cadence)
```

Replace the endpoints with your nodes' actual snapshot URLs, then restart the daemon.

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
  stability with online/offline health dots.
- Daily report gains a **Cluster Summary** section.

## Degradation

If a node is unreachable it is marked offline, the cluster score drops, and a health
warning is emitted — no automation is triggered.