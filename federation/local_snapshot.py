"""Ops Brain - local node snapshot merge (the Phase 2 producer half).

A node may be described by more than one source: the live poll (Netdata / OpsBrain) plus a
**producer snapshot file** written by the node itself for the things the poll cannot see
(Time Machine's last successful backup, mount health, cron health, loopback-only services).

The producer writes it ATOMICALLY (tmp + rename) into this host's `logs/` — the Mac's
`~/.hermes/scripts/mac-node-snapshot.py` writes `/Volumes/appdata/OpsBrain/logs/macstudio_node.json`,
which is this repo's `logs/macstudio_node.json`. **OpsBrain is the CONSUMER only** (same ownership
rule as the vault-drift report): never write that file from here.

Anomaly semantics, chosen so the merge cannot cry wolf and cannot hide a dead producer:

* `ok`      — the file is fresh; its own `alert_count` (the node's real findings) counts.
* `stale`   — older than `snapshot_max_age_s`: **one** anomaly. A producer that stopped while the
              live poll stayed green is exactly the silent failure this pipeline exists to catch.
* `missing` — no fault: the producer is optional, and a node without one is still a valid node.
              Surfaced as `snapshot.state: missing` so the dashboard can say so out loud.
* `unreadable` — half-written/foreign file: treated like missing for anomalies, with the error kept.
"""
import json
import time
from pathlib import Path

DEFAULT_MAX_AGE_S = 3600          # 4 missed ticks of a 15-minute producer
SNAPSHOT_KEYS = ("time_machine", "mounts", "services", "gateway", "hermes_cron",
                 "local_model", "disks", "volumes_watched", "volumes_skipped", "netdata")


def read_snapshot(path, max_age_s=DEFAULT_MAX_AGE_S, now=None):
    """Read one producer snapshot; never raises. Returns {state, age_s, data, error?}."""
    p = Path(path)
    if not p.is_file():
        return {"state": "missing", "age_s": None, "data": None}
    try:
        mtime = p.stat().st_mtime
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a bad file is a degraded source, not a crash
        return {"state": "unreadable", "age_s": None, "data": None,
                "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"state": "unreadable", "age_s": None, "data": None,
                "error": "snapshot payload is not an object"}
    age = float(now if now is not None else time.time()) - mtime
    limit = float(max_age_s)
    state = "ok" if (limit <= 0 or age <= limit) else "stale"
    return {"state": state, "age_s": round(age, 1), "data": data}


def merge_snapshot(node, snap, source=None):
    """Attach a producer snapshot to a collected node block (mutates and returns it).

    The live poll keeps ownership of what it measures (online/offline, alarms, per-volume disk);
    the snapshot only ADDS the telemetry the poll cannot reach, and its own findings become
    anomalies. A node that has no `host` block of its own gets the producer's.
    """
    data = snap.get("data") or {}
    alerts = [str(a) for a in (data.get("alerts") or [])]
    block = {
        "state": snap.get("state"),
        "age_s": snap.get("age_s"),
        "source": source,
        "generated_at": data.get("generated_at_utc") or data.get("generated_at"),
        "interval_minutes": data.get("interval_minutes"),
        "label": data.get("label"),
        "dns_name": data.get("dns_name"),
        "producer": data.get("producer"),
        "alert_count": 0,
        "alerts": alerts,
    }
    if snap.get("error"):
        block["error"] = snap["error"]

    if snap.get("state") == "ok":
        count = data.get("alert_count")
        block["alert_count"] = int(count) if isinstance(count, (int, float)) else len(alerts)
        extra = block["alert_count"]
    elif snap.get("state") == "stale":
        # a producer that stopped while the live poll stayed green is a real fault, and exactly
        # the kind the reasoner should see: one anomaly, not one per missed tick
        extra = 1
        block["note"] = "producer snapshot is stale"
    else:
        extra = 0

    for key in SNAPSHOT_KEYS:
        if key in data:
            block[key] = data[key]

    prod_host = data.get("host")
    if isinstance(prod_host, dict) and prod_host:
        host = node.setdefault("host", {})
        if not isinstance(host, dict):
            node["host"] = host = {}
        for key, value in prod_host.items():
            host.setdefault(key, value)      # the live poll's own host facts win on conflict

    node["snapshot"] = block
    if extra:
        node["anomalies"] = int(node.get("anomalies") or 0) + extra
        node["alerts_count"] = int(node.get("alerts_count") or 0) + extra
    return node
