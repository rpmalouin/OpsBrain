"""Ops Brain - Mac node-snapshot federation adapter.

Reduces the JSON document the Mac Studio's own Hermes cron writes every 15 minutes
(``logs/macstudio_node.json``, schema 1, produced by ``hermes-mac``) into the same node
telemetry block ``federation_collector._collect_node`` emits for an OpsBrain node. It is the
richer successor to the Netdata-sourced node (``federation/netdata_node.py``): Netdata can see
disks and alarms, but only the Mac itself can see Time Machine health, the Hermes cron set, the
gateway heartbeat and its own curated alert list.

Document contract (schema 1), read at ingest:

    schema, node, kind, label, hostname, dns_name, address,
    generated_at, generated_at_utc, producer, interval_minutes,
    host{model, os, kernel, arch, cpu_cores, load_avg[3], uptime_hours},
    time_machine{latest_backup, latest_backup_age_hours, destinations[]},
    disks[{path, total_gib, used_gib, free_gib, used_percent}],
    mounts[{path, mounted, read_only, source, fstype}],
    volumes_watched[], volumes_skipped[],
    services[], gateway{heartbeat_file, present, age_seconds, pid},
    hermes_cron{jobs_total, jobs_enabled, jobs_paused, jobs_failing[], oldest_last_run_hours, unreadable},
    local_model{...}, netdata{...}, alerts[], alert_count, build_seconds

Three rules this module exists to enforce (do not "simplify" them away):

1. **Fail closed.** A missing / unreadable / non-JSON document, or ``schema != 1``, is an
   OFFLINE node (``_failed_node``) - an absent document must never be presented as a healthy
   node. An unparseable ``generated_at_utc`` is treated as stale, never fresh.
2. **The document's age is the signal.** ``age_s = now(UTC) - generated_at_utc``; the document
   is stale past ``interval_minutes * stale_intervals`` (default 3 -> 45 min). A stale document
   keeps the node ``online`` (the host is probably fine; the DOCUMENT is blind) but raises a
   synthesised alert, so a dead producer is visible instead of reading as an all-clear.
3. **Surface, do not judge, capacity.** The Mac's steady state includes volumes at ~82 %; a
   near-full volume reads clean on purpose. ``used_percent`` is reported, never turned into an
   alert. Likewise, alerts come from the producer's curated ``alerts[]`` plus synthesised
   freshness/mount/heartbeat issues - not from a re-interpretation of raw numbers.

The node's ``alerts`` are attached to ``raw`` in the exact shape
``federation_reasoner._alarm_names()`` reads
(``raw.collector.netdata.alarms_active[].name``), so cross-node correlation sees them.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from common import REPO

DEFAULT_INTERVAL_MINUTES = 15
DEFAULT_STALE_INTERVALS = 3
DEFAULT_TM_STALE_HOURS = 48
DEFAULT_GATEWAY_STALE_S = 300
SCHEMA = 1


def _f(value):
    """Coerce a JSON numeric (often a numeric string) to float, else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def _i(value):
    n = _f(value)
    return int(n) if n is not None else None


def _parse_utc(value):
    """Parse an ISO-8601 UTC timestamp into an aware datetime, else None.

    Python 3.10's ``fromisoformat`` rejects the trailing ``Z`` the producer writes
    (``2026-10-10T18:22:24Z``), so it is normalised first. A naive timestamp is assumed UTC.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_path(raw):
    """Resolve a configured snapshot path; relative paths are repo-relative."""
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else REPO / path


def _failed_node(node_cfg, err):
    """Offline node block, mirroring netdata_node._failed_node."""
    return {"online": False, "type": node_cfg.get("type") or "macos",
            "confidence": None, "drift_events": 0, "anomalies": 0, "restart_events": 0,
            "disk_used_percent": None, "containers": {"running": 0, "total": 0},
            "alerts_count": 0, "err": err}


def _build_node(node_cfg, doc):
    """Assemble the federation node block from a schema-1 snapshot document."""
    # --- host facts -------------------------------------------------------
    h = doc.get("host") or {}
    uptime_hours = _f(h.get("uptime_hours"))
    uptime_s = round(uptime_hours * 3600, 1) if uptime_hours is not None else None
    host = {
        "os": h.get("os"),
        "cpu": h.get("model"),          # the document's hardware model (e.g. Mac17,14)
        "cores": _i(h.get("cpu_cores")),
        "uptime_s": uptime_s,
        "load_avg": h.get("load_avg"),
        # the document has no memory reading; None means "not observed", and the UI shows it
        "ram_used_percent": None,
    }

    # --- watched volumes --------------------------------------------------
    watched = doc.get("volumes_watched") or node_cfg.get("volumes") or []
    watched = [str(p) for p in watched]
    used_by_path = {}
    for disk in doc.get("disks") or []:
        if isinstance(disk, dict):
            used_by_path[disk.get("path")] = _f(disk.get("used_percent"))
    # a watched path missing from `disks` stays None (we know it exists, we could not read it)
    volumes = {path: used_by_path.get(path) for path in watched}
    readable = [pct for pct in volumes.values() if pct is not None]
    disk_used_percent = max(readable) if readable else None

    # --- document freshness ----------------------------------------------
    interval = _f(doc.get("interval_minutes"))
    interval = int(interval) if interval is not None else DEFAULT_INTERVAL_MINUTES
    stale_intervals = _f(node_cfg.get("stale_intervals"))
    stale_intervals = stale_intervals if stale_intervals is not None else DEFAULT_STALE_INTERVALS
    stale_after_s = interval * stale_intervals * 60

    generated = doc.get("generated_at_utc")
    gen_dt = _parse_utc(generated)
    unparseable = gen_dt is None
    if unparseable:
        age_s = None
        stale = True
    else:
        age_s = round(max(0.0, (datetime.now(timezone.utc) - gen_dt).total_seconds()), 1)
        stale = age_s > stale_after_s
    snapshot = {
        "schema": doc.get("schema"),
        "producer": doc.get("producer"),
        "generated_at_utc": generated,
        "age_s": age_s,
        "interval_minutes": interval,
        "stale_after_s": stale_after_s,
        "stale": stale,
    }

    # --- Time Machine -----------------------------------------------------
    tm = doc.get("time_machine") or {}
    tm_age = _f(tm.get("latest_backup_age_hours"))
    tm_stale_hours = _f(node_cfg.get("tm_stale_hours"))
    tm_stale_hours = tm_stale_hours if tm_stale_hours is not None else DEFAULT_TM_STALE_HOURS
    tm_stale = tm_age is not None and tm_age > tm_stale_hours
    time_machine = {
        "latest_backup_age_hours": tm_age,
        "latest_backup": tm.get("latest_backup"),
        "destinations": tm.get("destinations") or [],
        "stale": tm_stale,
    }

    # --- mounts -----------------------------------------------------------
    # Iterate the producer's mount table rather than volumes_watched: the `mounts` list omits
    # "/" (root is always mounted) but carries the SMB shares, so scanning watched paths would
    # falsely report "/" not mounted. Filtering this list to watched paths is the right scope.
    mounts = doc.get("mounts") or []
    mount_problems = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        path = mount.get("path")
        if path not in watched:
            continue
        if not mount.get("mounted"):
            mount_problems.append(f"{path} not mounted")
        elif mount.get("read_only"):
            mount_problems.append(f"{path} read-only")

    # --- Hermes cron ------------------------------------------------------
    hc = doc.get("hermes_cron") or {}
    failing_jobs = [j.get("name") for j in (hc.get("jobs_failing") or []) if isinstance(j, dict)]
    failing_jobs = [name for name in failing_jobs if name is not None]
    hermes_cron = {
        "jobs_total": _i(hc.get("jobs_total")),
        "jobs_enabled": _i(hc.get("jobs_enabled")),
        "jobs_paused": _i(hc.get("jobs_paused")),
        "failing": failing_jobs,
        "failing_count": len(failing_jobs),
        "oldest_last_run_hours": _f(hc.get("oldest_last_run_hours")),
        "unreadable": hc.get("unreadable"),
    }

    # --- gateway heartbeat ------------------------------------------------
    gw = doc.get("gateway") or {}
    gw_present = gw.get("present")
    gw_age = _f(gw.get("age_seconds"))
    gateway_stale_s = _f(node_cfg.get("gateway_stale_s"))
    gateway_stale_s = gateway_stale_s if gateway_stale_s is not None else DEFAULT_GATEWAY_STALE_S
    # a present:false heartbeat is stale; a missing reading is unknown and raises nothing
    gw_stale = (gw_present is False) or (gw_age is not None and gw_age > gateway_stale_s)
    gateway = {
        "present": gw_present,
        "age_seconds": gw_age,
        "pid": gw.get("pid"),
        "stale": gw_stale,
    }

    # --- Netdata info (informational only) --------------------------------
    nd = doc.get("netdata") or {}
    netdata = {
        "up": nd.get("up"),
        "version": nd.get("version"),
        "alarms": nd.get("alarms"),
        "alarms_non_normal": nd.get("alarms_non_normal") or [],
        "charts": nd.get("charts"),
    }

    # --- alerts = the producer's curated list + synthesised issues ---------
    alerts = [str(a) for a in (doc.get("alerts") or [])]
    if unparseable:
        alerts.append("node snapshot stale: generated_at_utc "
                      f"unparseable ({generated if generated is not None else 'missing'})")
    elif stale:
        alerts.append(f"node snapshot stale: last write {int(age_s // 60)} min ago "
                      f"(interval {interval} min)")
    alerts.extend(mount_problems)
    if tm_stale:
        alerts.append(f"time machine backup stale: newest backup {tm_age:g}h old")
    if gw_stale:
        if gw_age is not None:
            alerts.append(f"gateway heartbeat stale: last beat {int(gw_age)}s ago")
        else:
            alerts.append("gateway heartbeat not present")

    return {
        "online": True,
        "type": node_cfg.get("type") or "macos",
        # no reasoner on the Mac: null confidence earns FULL credit in federation_reasoner
        "confidence": None,
        "drift_events": 0,
        "anomalies": len(alerts),
        "restart_events": 0,
        "disk_used_percent": disk_used_percent,
        "containers": {"running": 0, "total": 0},
        "alerts_count": len(alerts),
        "host": host,
        "uptime_s": uptime_s,
        "volumes": volumes,
        "alarms": list(alerts),
        "alerts": list(alerts),
        "snapshot": snapshot,
        "time_machine": time_machine,
        "mounts": list(mounts),
        "mount_problems": mount_problems,
        "hermes_cron": hermes_cron,
        "gateway": gateway,
        "netdata": netdata,
        # federation_reasoner correlates alarm NAMES from exactly this path
        "raw": {"collector": {"netdata": {
            "alarms_active": [{"name": alert} for alert in alerts]}}},
    }


def collect_node_snapshot(node_cfg, timeout=6):
    """Read + reduce one node-snapshot document. Never raises: a failure is an offline node.

    ``timeout`` is accepted for signature parity with the other adapters (``collect_cluster``
    calls every kind the same way); reading a local JSON file has nothing to time out, so it is
    deliberately unused. Fail-closed: a missing / unreadable / non-JSON file, a non-dict payload,
    or a schema other than 1 returns an offline ``_failed_node`` with a precise ``err``.
    """
    raw = node_cfg.get("snapshot_path")
    if not raw:
        return _failed_node(node_cfg, "missing snapshot_path")
    try:
        text = _resolve_path(raw).read_text(encoding="utf-8")
    except OSError as exc:
        return _failed_node(node_cfg, f"{type(exc).__name__}: {exc}")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return _failed_node(node_cfg, f"invalid JSON: {exc}")
    if not isinstance(doc, dict):
        return _failed_node(node_cfg, "snapshot is not a JSON object")
    if doc.get("schema") != SCHEMA:
        return _failed_node(node_cfg, f"unsupported schema: {doc.get('schema')!r}")
    return _build_node(node_cfg, doc)
