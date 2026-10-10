"""
Ops Brain - Federation Collector.

Polls each configured node's collector endpoint and reduces the cluster into a
single unified JSON snapshot consumed by the federation reasoner and dashboard.

Schema (logs/cluster_snapshot.json):
{
  "timestamp": "<ISO UTC>",
  "nodes": {
    "<name>": {online, type, confidence, drift_events, anomalies,
               restart_events, disk_used_percent, containers{running,total},
               alerts_count, pools?[healthy,total], err?},
    ...
  },
  "cluster_metrics": {avg_confidence, total_anomalies, drift_events, restart_events}
}

Usage:
    python3 federation/federation_collector.py [--config <path>] [--out <path>]

Node source: federation.nodes[] in ops_brain.yaml. Each node's collector_endpoint
is expected to serve the same shape as this OpsBrain's /api/status snapshot.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, REPO, get_logger, write_json  # noqa: E402

log = get_logger("federation_collector")


def _get(data, *path):
    cur = data
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _cnt(value):
    return len(value) if isinstance(value, (list, tuple, set, dict)) else None


def _int_or_cnt(value):
    n = _num(value)
    if n is not None:
        return int(n)
    c = _cnt(value)
    return int(c) if c is not None else 0


def _failed_node(node_type, err):
    return {"online": False, "type": node_type, "confidence": None,
            "drift_events": 0, "anomalies": 0, "restart_events": 0,
            "disk_used_percent": None, "containers": {"running": 0, "total": 0},
            "alerts_count": 0, "err": err}


def _collect_node(node, timeout):
    """Fetch one node's snapshot and reduce it to a compact telemetry block.

    `kind` selects the node source (absent = the OpsBrain path):
      - `netdata` -> federation/netdata_node.py reduces a remote Netdata HTTP API
        (for a host that runs Netdata but not OpsBrain, e.g. the Mac Studio)
      - otherwise -> this host's snapshot shape, tolerant of two payloads:
        the raw OpsBrain collector.json {netdata, gpu, docker, vm, truenas, ...} and
        an OpsBrain /api/status envelope {collector:{...}, reasoner, ...}
    """
    if str(node.get("kind") or "opsbrain").lower() == "netdata":
        from federation.netdata_node import collect_netdata_node
        return collect_netdata_node(node, timeout)
    node_type = node.get("type")
    endpoint = node.get("collector_endpoint")
    if not endpoint:
        return _failed_node(node_type, "missing collector_endpoint")
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return _failed_node(node_type, f"{type(exc).__name__}: {exc}")

    if not isinstance(payload, dict):
        return _failed_node(node_type, "non-dict snapshot payload")

    # Normalize: if this is a /api/status envelope, pull the embedded collector doc;
    # also look under sources.collector.json for the raw path.
    coll = None
    for key in ("collector", "sources"):
        cand = payload.get(key)
        cand = cand.get("collector.json") if isinstance(cand, dict) and key == "sources" else cand
        if isinstance(cand, dict):
            coll = cand
            break
    if coll is None:
        coll = payload  # assume the payload IS the collector doc
    reasoner = payload.get("reasoner") if isinstance(payload.get("reasoner"), dict) else None

    confidence = _num(reasoner and reasoner.get("confidence"))
    if confidence is None:
        confidence = _num(coll.get("reasoner", {}).get("confidence") if isinstance(coll.get("reasoner"), dict) else None)
    drift_events = _cnt(_get(coll, "gpu", "drift_flags")) or 0
    anomalies = _cnt(_get(coll, "netdata", "alarms_active")) or 0
    alerts_count = _int_or_cnt(_get(coll, "netdata", "alarms_count"))
    disk_used_percent = _num(_get(coll, "vm", "disk_used_percent"))
    if disk_used_percent is None:
        disk_used_percent = _num(_get(coll, "netdata", "disk_used_percent"))
    restart_events = _cnt(_get(coll, "docker", "restarting")) or 0
    running = _num(_get(coll, "docker", "running"))
    total = _num(_get(coll, "docker", "containers_count"))
    running = int(running) if running is not None else (_cnt(_get(coll, "docker", "running")) or 0)
    total = int(total) if total is not None else (_cnt(_get(coll, "docker", "containers_count")) or 0)

    node_dict = {"online": True, "type": node_type, "confidence": confidence,
                 "drift_events": drift_events, "anomalies": anomalies,
                 "restart_events": restart_events,
                 "disk_used_percent": disk_used_percent,
                 "containers": {"running": running, "total": total},
                 "alerts_count": alerts_count}
    # federation_reasoner correlates alarm names / GPU drift flags across nodes by reading
    # node["raw"]; without this key cross-node correlation could never fire for ANY node.
    # COMPACT on purpose: the shapes it reads are the only ones kept — attaching the whole
    # collector doc here pushed the cluster snapshot from ~0.5 KB to ~255 KB per cycle.
    node_dict["raw"] = {"collector": {
        "netdata": {"alarms_active": [
            {"name": a.get("name")}
            for a in (_get(coll, "netdata", "alarms_active") or []) if isinstance(a, dict)]},
        "gpu": {"drift_flags": [str(f) for f in (_get(coll, "gpu", "drift_flags") or [])]},
    }}

    pools_healthy = _num(_get(coll, "truenas", "pools_healthy"))
    pool_count = _num(_get(coll, "truenas", "pool_count"))
    if pools_healthy is None:
        pools_healthy = _cnt(_get(coll, "truenas", "pools_healthy"))
    if pool_count is None:
        pool_count = _cnt(_get(coll, "truenas", "pool_count"))
    if pools_healthy is not None or pool_count is not None:
        node_dict["pools"] = [pools_healthy, pool_count]
    return node_dict


def _merge_local_snapshot(node_dict, node_cfg):
    """Merge a node's producer snapshot file (Phase 2) into its collected block.

    Never raises: an absent/foreign/old file leaves a `snapshot` block that says so. The producer
    owns the file; this host only reads it (same rule as the vault-drift report).
    """
    from federation.local_snapshot import read_snapshot, merge_snapshot
    try:
        path = node_cfg.get("snapshot_file")
        if not str(path).startswith("/"):
            path = str(REPO / path)
        snap = read_snapshot(path, max_age_s=node_cfg.get("snapshot_max_age_s") or 3600)
        return merge_snapshot(node_dict, snap, source=str(node_cfg.get("snapshot_file")))
    except Exception as exc:  # noqa: BLE001 - a snapshot must never take a cluster poll down
        log.warning("local snapshot merge failed for %s: %s", node_cfg.get("name"), exc)
        return node_dict


def collect_cluster(node_list, timeout=6):
    """Collect each node's snapshot and roll up cluster metrics.

    A node may override the poll default with its own `timeout_s` (a Netdata-sourced node does
    several HTTP reads per cycle, so it can be slower than a single /api/status fetch), and may
    carry a `snapshot_file` — a producer snapshot written by the node itself for whatever the live
    poll cannot see (see federation/local_snapshot.py). The merge runs for offline nodes too, so a
    dead poll beside a live producer is distinguishable from a dead node.
    """
    timeout = float(timeout)  # urllib accepts float seconds
    nodes = {}
    for node in node_list:
        name = node.get("name") or node.get("collector_endpoint") or "unknown"
        node_timeout = _num(node.get("timeout_s")) or timeout
        node_dict = _collect_node(node, node_timeout)
        if node.get("snapshot_file"):
            node_dict = _merge_local_snapshot(node_dict, node)
        nodes[name] = node_dict

    online = [n for n in nodes.values() if n.get("online")]
    confidences = [n["confidence"] for n in online if n.get("confidence") is not None]
    avg_confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0

    cluster_metrics = {
        "avg_confidence": avg_confidence,
        "total_anomalies": sum(n["anomalies"] for n in online),
        "drift_events": sum(n["drift_events"] for n in online),
        "restart_events": sum(n["restart_events"] for n in online),
    }
    return {"timestamp": datetime.now(timezone.utc).isoformat(),
            "nodes": nodes, "cluster_metrics": cluster_metrics}


def snapshot_path():
    if Cfg.get("paths.cluster_snapshot"):
        return Cfg.resolve("paths.cluster_snapshot")
    return REPO / "logs" / "cluster_snapshot.json"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="federation_collector")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    Cfg.load(args.config)
    fed = Cfg.get("federation", {}) or {}
    if not fed.get("enabled", False):
        log.info("federation disabled; skipping cluster collection")
        return 0
    node_list = fed.get("nodes", []) or []
    timeout = float(fed.get("poll_timeout_s", 6) or 6)
    if not node_list:
        log.warning("federation enabled but no nodes configured")
        return 0

    doc = collect_cluster(node_list, timeout=timeout)
    out = Path(args.out) if args.out else snapshot_path()
    write_json(out, doc)
    log.info("federation snapshot written: %s (nodes=%d online=%d)",
             out, len(node_list),
             sum(1 for n in doc["nodes"].values() if n.get("online")))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())