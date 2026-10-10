"""Ops Brain - local node snapshot merge (the Phase 2 producer half).

A node may be described by more than one source: the live poll (Netdata / OpsBrain) plus a
**producer snapshot file** written by the node itself for the things the poll cannot see
(Time Machine's last successful backup, mount health, cron health, loopback-only services).

The producer writes it ATOMICALLY (tmp + rename) into this host's `logs/` — the Mac's
`~/.hermes/scripts/mac-node-snapshot.py` writes `/Volumes/appdata/OpsBrain/logs/macstudio_node.json`,
which is this repo's `logs/macstudio_node.json`. **OpsBrain is the CONSUMER only** (same ownership
rule as the vault-drift report): never write that file from here.

Anomaly semantics, chosen so the merge cannot cry wolf and cannot hide a dead producer:

* `ok`      — the file is fresh; its own `alert_count` (the node's real findings) counts, plus the
              collector's own state checks (see `derived_alerts`).
* `stale`   — older than `snapshot_max_age_s`: **one** anomaly. A producer that stopped while the
              live poll stayed green is exactly the silent failure this pipeline exists to catch.
              Its data is NOT re-judged (old facts make bad alerts).
* `missing` — no fault: the producer is optional, and a node without one is still a valid node.
              Surfaced as `snapshot.state: missing` so the dashboard can say so out loud.
* `unreadable` — half-written/foreign file: treated like missing for anomalies, with the error kept.

The producer's contract (2026-10-10, hermes-mac ask `20261010T182237Z-f2b12bf0`) asks for staleness at
three missed intervals: 45 minutes for a 15-minute producer, hence `DEFAULT_MAX_AGE_S = 2700`.
"""
import json
import time
from pathlib import Path

DEFAULT_MAX_AGE_S = 2700          # 3 missed ticks of the 15-minute producer (its own contract)
DEFAULT_TM_STALE_HOURS = 48       # a Time Machine backup older than this is worth saying out loud
DEFAULT_GATEWAY_STALE_S = 300     # the Hermes gateway heartbeat is ~15 s old when healthy
SNAPSHOT_KEYS = ("time_machine", "mounts", "services", "gateway", "hermes_cron",
                 "local_model", "disks", "volumes_watched", "volumes_skipped", "netdata")


def _f(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def derived_alerts(data, tm_stale_hours=DEFAULT_TM_STALE_HOURS,
                   gateway_stale_s=DEFAULT_GATEWAY_STALE_S):
    """State checks the COLLECTOR makes, because the producer reports facts, not verdicts.

    Deliberately narrow, and deliberately never about capacity:

    * a mount that is not mounted — an unmounted share is a fault, not a number
    * the Hermes gateway heartbeat missing or older than `gateway_stale_s` — the Mac's own agent is
      the one service nothing else can see
    * a Time Machine backup older than `tm_stale_hours`

    `disks[].used_percent` is NEVER turned into an alert: a volume sitting at 82 % is the Mac's steady
    state, which is why its own `alerts[]` (its curated verdicts) is the source for judgments. This is
    the "surface, do not judge" rule, and it is what keeps a near-full volume from becoming noise.
    """
    out = []
    for mount in data.get("mounts") or []:
        if isinstance(mount, dict) and not mount.get("mounted", True):
            out.append(f"mount not mounted: {mount.get('path')}")
    gateway = data.get("gateway")
    if isinstance(gateway, dict) and ("present" in gateway or "age_seconds" in gateway):
        if not gateway.get("present"):
            out.append("hermes gateway heartbeat missing")
        else:
            age = _f(gateway.get("age_seconds"))
            limit = _f(gateway_stale_s)
            if age is not None and limit and age > limit:
                out.append(f"hermes gateway heartbeat stale ({int(age)}s)")
    tm = data.get("time_machine")
    if isinstance(tm, dict):
        age_h = _f(tm.get("latest_backup_age_hours"))
        limit_h = _f(tm_stale_hours)
        if age_h is not None and limit_h and age_h > limit_h:
            out.append(f"time machine backup {age_h:.1f}h old (limit {int(limit_h)}h)")
    return out


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


def merge_snapshot(node, snap, source=None, tm_stale_hours=None, gateway_stale_s=None):
    """Attach a producer snapshot to a collected node block (mutates and returns it).

    The live poll keeps ownership of what it measures (online/offline, alarms, per-volume disk);
    the snapshot only ADDS the telemetry the poll cannot reach, and its findings become anomalies.
    A node with no `host` block of its own gets the producer's.
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
        "derived_alerts": [],
    }
    if snap.get("error"):
        block["error"] = snap["error"]

    if snap.get("state") == "ok":
        count = data.get("alert_count")
        block["alert_count"] = int(count) if isinstance(count, (int, float)) else len(alerts)
        # the collector's own state checks run ONLY on fresh data: old facts make bad alerts
        block["derived_alerts"] = derived_alerts(
            data,
            tm_stale_hours=(tm_stale_hours if tm_stale_hours is not None else DEFAULT_TM_STALE_HOURS),
            gateway_stale_s=(gateway_stale_s if gateway_stale_s is not None
                             else DEFAULT_GATEWAY_STALE_S))
        extra = block["alert_count"] + len(block["derived_alerts"])
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
