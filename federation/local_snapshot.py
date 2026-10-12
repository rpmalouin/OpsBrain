"""Ops Brain - node snapshot ingest (the producer half of a node, and for the Mac the ONLY half).

A node may be described by more than one source: a live poll (an OpsBrain `/api/status` envelope) plus
a **producer snapshot file** written by the node itself for the things a poll cannot see (Time Machine's
last successful backup, mount health, cron health, loopback-only services). For the Mac Studio there is
no poll at all any more: its snapshot file IS the node (`kind: snapshot`), because the machine runs no
OpsBrain, its Netdata was removed in 2026-10, and its data is 15-minute fresh while our federation poll
runs every 2 h.

The producer writes it ATOMICALLY (tmp + rename) into this host's `logs/` — the Mac's
`~/.hermes/scripts/mac-node-snapshot.py` writes `/Volumes/appdata/OpsBrain/logs/macstudio_node.json`,
which is this repo's `logs/macstudio_node.json`. **OpsBrain is the CONSUMER only** (same ownership rule
as the vault-drift report): never write that file from here.

Anomaly semantics, chosen so the merge cannot cry wolf and cannot hide a dead producer:

* `ok`      — fresh. Its own `alert_count` (the node's real findings) counts, plus this collector's own
              state checks (`derived_alerts`). A primary source is online.
* `stale`   — older than `max_age_s` (45 min = three missed 15-minute ticks): **one** anomaly. Its data is
              NOT re-judged (old facts make bad alerts). A primary source stays ONLINE: the last known
              facts are still the best information we have, and a reboot or an SMB remount is a
              transient, not a fault.
* `dead`    — older than `dead_after_s` (2 h; primary sources only): **one** anomaly and the node reads
              OFFLINE. A producer that has not ticked for eight intervals is a fault, and it must not
              look like a missed tick.
* `missing` — no file. Not a fault for a node whose poll stands on its own (the producer is optional);
              for a primary source it reads OFFLINE with one anomaly, because for it the file is the node.
* `unreadable` — half-written or foreign file: like `missing`, with the error kept.

The producer's contract (2026-10-10, hermes-mac ask `20261010T182237Z-f2b12bf0`) asks for staleness at
three missed intervals: 45 minutes for a 15-minute producer, hence `DEFAULT_MAX_AGE_S = 2700`.

`macmon` (the Mac's sudoless SoC sampler, `rpmalouin/macmon`) reaches us in the snapshot's `soc` block
as a faithful copy of one sample — the producer reports, we derive: every percentage, pressure reading
and verdict below is computed HERE. Note `ram_usage` is Activity Monitor's "Memory Used" and counts
reclaimable inactive pages, so a machine holding a resident local model reads 87-92 % while nothing is
wrong; `ram_available` is the pressure figure and the only one worth a threshold (the fork's own lines in
`StatusLabel.swift`), which is why both are surfaced and only one is judged.
"""
import json
import re
import time
from pathlib import Path

DEFAULT_MAX_AGE_S = 2700          # 3 missed ticks of the 15-minute producer (its own contract)
DEFAULT_DEAD_AFTER_S = 7200       # 8 missed ticks: for a primary source that is a dead producer
DEFAULT_TM_STALE_HOURS = 48       # a Time Machine backup older than this is worth saying out loud
DEFAULT_GATEWAY_STALE_S = 300     # the Hermes gateway heartbeat is ~15 s old when healthy
DEFAULT_RAM_WARN_PCT = 10.0       # ram_available/total below this reads as pressure (macmon's own line)
DEFAULT_RAM_HEAVY_PCT = 5.0       # ...and below this as heavy (macmon's own line)
SNAPSHOT_KEYS = ("time_machine", "mounts", "services", "gateway", "hermes_cron",
                 "local_model", "disks", "soc", "volumes_watched", "volumes_skipped")

# Stable correlation tokens for this collector's own derived checks. Cross-node correlation matches on
# these, and the reasoner must never compare a name it cannot reproduce, so the vocabulary is fixed here
# and covered by a test.
TOKEN_PREFIXES = (
    ("mount not mounted", "mount_not_mounted"),
    ("mount is read-only", "mount_read_only"),
    ("hermes gateway heartbeat missing", "gateway_missing"),
    ("hermes gateway heartbeat stale", "gateway_stale"),
    ("time machine backup", "time_machine_stale"),
    ("memory pressure", "memory_pressure"),
    ("fan stopped", "fan_stopped"),
)


def _f(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _token(text):
    """One stable token for an alert string, for cross-node correlation.

    Our own derived checks match `TOKEN_PREFIXES`. A producer's prose alert is reduced to its namespace
    before the first colon (`time_machine: no backup visible ...` -> `time_machine`), and a phrase with
    no colon to its first four words. This is a correlation key, not a verdict, and it is deliberately
    lossy: two nodes sharing a token is a hint to look at both, never a diagnosis.
    """
    low = str(text or "").strip().lower()
    for prefix, token in TOKEN_PREFIXES:
        if low.startswith(prefix):
            return token
    head = low.split(":", 1)[0] if ":" in low else " ".join(low.split()[:4])
    head = re.sub(r"[^a-z0-9]+", "_", head).strip("_")
    return head[:48]


def ram_available_pct(metrics):
    """`ram_available` as a percentage of total RAM, or None when it cannot be judged.

    `ram_available` is what the kernel can still hand out without compressing or swapping. macmon
    documents it as 0 when the kernel does not report it, and a 0 must read as unknown rather than as an
    out-of-memory machine.
    """
    mem = ((metrics or {}).get("memory") or {})
    total = _f(mem.get("ram_total"))
    avail = _f(mem.get("ram_available"))
    if not total or total <= 0 or avail is None or avail <= 0:
        return None
    return avail / total * 100


def memory_pressure(metrics, warn_pct=DEFAULT_RAM_WARN_PCT, heavy_pct=DEFAULT_RAM_HEAVY_PCT):
    """`clear` / `moderate` / `heavy` from a macmon metrics block, or None when it cannot be judged."""
    pct = ram_available_pct(metrics)
    if pct is None:
        return None
    if pct < float(heavy_pct):
        return "heavy"
    if pct < float(warn_pct):
        return "moderate"
    return "clear"


def derived_alerts(data, tm_stale_hours=DEFAULT_TM_STALE_HOURS,
                   gateway_stale_s=DEFAULT_GATEWAY_STALE_S,
                   ram_warn_pct=DEFAULT_RAM_WARN_PCT, ram_heavy_pct=DEFAULT_RAM_HEAVY_PCT):
    """State checks the COLLECTOR makes, because the producer reports facts, not verdicts.

    Deliberately narrow, and deliberately never about capacity. In order:

    * a mount that is not mounted — an unmounted share is a fault, not a number
    * a mount that is mounted READ-ONLY — a share silently remounted read-only is a fault too
    * the Hermes gateway heartbeat missing or older than `gateway_stale_s` — the Mac's own agent is
      the one service nothing else can see
    * a Time Machine backup older than `tm_stale_hours`
    * memory pressure — `ram_available` under `ram_warn_pct` of RAM (macmon's own threshold lines)
    * a fan reporting 0 rpm — an unambiguous hardware fault, and nothing else here can see it

    `disks[].used_percent` is NEVER turned into an alert: a volume sitting at 82 % is the Mac's steady
    state, which is why its own `alerts[]` (its curated verdicts) is the source for judgments. This is
    the "surface, do not judge" rule, and it is what keeps a near-full volume from becoming noise.

    Runs ONLY on fresh data (see `merge_snapshot`): old facts make bad alerts.
    """
    out = []
    for mount in data.get("mounts") or []:
        if not isinstance(mount, dict):
            continue
        if not mount.get("mounted", True):
            out.append(f"mount not mounted: {mount.get('path')}")
        elif mount.get("read_only"):
            out.append(f"mount is read-only: {mount.get('path')}")
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
    soc = data.get("soc") if isinstance(data.get("soc"), dict) else {}
    metrics = soc.get("metrics") if isinstance(soc.get("metrics"), dict) else None
    pressure = memory_pressure(metrics, ram_warn_pct, ram_heavy_pct)
    pct = ram_available_pct(metrics)
    if pressure in ("moderate", "heavy") and pct is not None:
        limit = ram_heavy_pct if pressure == "heavy" else ram_warn_pct
        out.append(f"memory pressure: {pct:.1f}% of RAM available (below {float(limit):g}%)")
    for fan in ((metrics or {}).get("fans") or []):
        if isinstance(fan, dict) and _f(fan.get("rpm")) == 0:
            out.append(f"fan stopped: {fan.get('name')}")
    return out


def alert_keys(data, derived=()):
    """Stable tokens for everything a node is reporting, for cross-node correlation.

    `federation_reasoner._alarm_names` reads these out of `node["raw"]`. Composed of the producer's own
    alerts plus our derived checks, de-duplicated, order preserved.
    """
    keys = []
    for text in list(data.get("alerts") or []) + list(derived or ()):
        token = _token(text)
        if token and token not in keys:
            keys.append(token)
    return keys


def read_snapshot(path, max_age_s=DEFAULT_MAX_AGE_S, dead_after_s=None, now=None):
    """Read one producer snapshot; never raises. Returns {state, age_s, data, error?}.

    `dead_after_s` (a primary source) splits the old single `stale` state in two: `stale` is a missed
    tick, `dead` is a producer that has stopped. Leave it None for a node whose live poll stands on its
    own — there, age is informational.
    """
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
    if limit <= 0:
        # age is switched off for this node: neither the stale tier nor the dead tier applies
        return {"state": "ok", "age_s": round(age, 1), "data": data}
    dead_limit = _f(dead_after_s)
    if dead_limit is not None and dead_limit > 0 and age > dead_limit:
        state = "dead"
    else:
        state = "ok" if age <= limit else "stale"
    return {"state": state, "age_s": round(age, 1), "data": data}


def _display_from_snapshot(node, block, ram_warn_pct, ram_heavy_pct):
    """Fill the panel's own display fields from a snapshot (its `disks[]` and the macmon `soc` block).

    The live poll used to own `volumes`, `disk_used_percent` and `host.ram_*`; with no poll the file
    supplies them, and `disks[]` is richer than Netdata's per-mount chart ever was (total, used, free and
    percent for each watched volume). Ratios stay 0-1 in the file and become percentages only here, so
    the file keeps one meaning and the panel does the arithmetic.
    """
    volumes = {}
    for disk in block.get("disks") or []:
        if isinstance(disk, dict) and disk.get("path"):
            volumes[str(disk["path"])] = _f(disk.get("used_percent"))
    if volumes:
        node["volumes"] = volumes
        readable = [pct for pct in volumes.values() if pct is not None]
        if readable:
            node["disk_used_percent"] = max(readable)

    soc = block.get("soc")
    if not isinstance(soc, dict):
        return
    block["soc_available"] = bool(soc.get("available"))
    block["soc_error"] = soc.get("error")
    block["soc_sampled_at"] = soc.get("sampled_at")
    metrics = soc.get("metrics") if isinstance(soc.get("metrics"), dict) else None
    if not metrics:
        return
    host = node.get("host")
    if not isinstance(host, dict):
        node["host"] = host = {}
    info = soc.get("soc_info")
    if not isinstance(info, dict):
        info = {}
    chip = info.get("chip_name")
    if chip:
        host.setdefault("chip_name", chip)
    mem = metrics.get("memory") if isinstance(metrics.get("memory"), dict) else {}
    total = _f(mem.get("ram_total"))
    used = _f(mem.get("ram_usage"))
    avail = _f(mem.get("ram_available"))
    if total and total > 0:
        host["ram_total_gb"] = round(total / (1024 ** 3), 1)
        if used is not None:
            host["ram_used_percent"] = round(used / total * 100, 1)
        if avail and avail > 0:
            host["ram_available_percent"] = round(avail / total * 100, 1)
    pressure = memory_pressure(metrics, ram_warn_pct, ram_heavy_pct)
    if pressure:
        host["memory_pressure"] = pressure
    temp = metrics.get("temp") if isinstance(metrics.get("temp"), dict) else {}
    if temp:
        host["temp"] = {"cpu_c": _f(temp.get("cpu_temp_avg")), "gpu_c": _f(temp.get("gpu_temp_avg"))}
    power = {"cpu_w": _f(metrics.get("cpu_power")), "gpu_w": _f(metrics.get("gpu_power")),
             "ane_w": _f(metrics.get("ane_power")), "sys_w": _f(metrics.get("sys_power"))}
    if any(value is not None for value in power.values()):
        host["power_w"] = power
    fans = metrics.get("fans")
    if isinstance(fans, list) and fans:
        host["fans"] = [{"name": f.get("name"), "rpm": _f(f.get("rpm")), "max_rpm": _f(f.get("max_rpm"))}
                        for f in fans if isinstance(f, dict)]
    cores_by_label = {t.get("label"): t.get("cores") for t in (info.get("cpu_tiers") or [])
                      if isinstance(t, dict)}
    tiers = []
    for tier in metrics.get("cpu_tiers") or []:
        if not isinstance(tier, dict):
            continue
        tiers.append({"label": tier.get("label"), "cores": cores_by_label.get(tier.get("label")),
                      "freq_mhz": _f(tier.get("freq_mhz")),
                      "scaled_ratio": _f(tier.get("scaled_ratio")),
                      "active_ratio": _f(tier.get("active_ratio"))})
    host["cpu"] = {"scaled_ratio": _f(metrics.get("cpu_scaled_ratio")),
                   "active_ratio": _f(metrics.get("cpu_active_ratio")), "tiers": tiers}
    host["gpu"] = {"freq_mhz": _f(metrics.get("gpu_freq_mhz")),
                   "scaled_ratio": _f(metrics.get("gpu_scaled_ratio")),
                   "active_ratio": _f(metrics.get("gpu_active_ratio"))}


def merge_snapshot(node, snap, source=None, primary=False, tm_stale_hours=None, gateway_stale_s=None,
                   ram_warn_pct=None, ram_heavy_pct=None):
    """Attach a producer snapshot to a collected node block (mutates and returns it).

    Without `primary`, the live poll keeps ownership of what it measures (online/offline, alarms,
    per-volume disk); the snapshot only ADDS what the poll cannot reach, and its findings become
    anomalies — a stale file counts once, a missing one is not a fault.

    With `primary=True` the snapshot IS the node (`kind: snapshot`): it decides `online` (see the module
    docstring for the five states), supplies the panel's own display fields, and `raw` is attached so
    cross-node correlation has something to read — without that key the correlation silently reads
    nothing for this node.

    A node with no `host` block of its own gets the producer's; where both have a fact the live poll
    wins (it is the fresher source), which is why the producer's values are `setdefault`ed.
    """
    warn = DEFAULT_RAM_WARN_PCT if ram_warn_pct is None else ram_warn_pct
    heavy = DEFAULT_RAM_HEAVY_PCT if ram_heavy_pct is None else ram_heavy_pct
    data = snap.get("data") or {}
    alerts = [str(a) for a in (data.get("alerts") or [])]
    state = snap.get("state")
    block = {
        "state": state,
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

    if state == "ok":
        count = data.get("alert_count")
        block["alert_count"] = int(count) if isinstance(count, (int, float)) else len(alerts)
        # the collector's own state checks run ONLY on fresh data: old facts make bad alerts
        block["derived_alerts"] = derived_alerts(
            data,
            tm_stale_hours=(tm_stale_hours if tm_stale_hours is not None else DEFAULT_TM_STALE_HOURS),
            gateway_stale_s=(gateway_stale_s if gateway_stale_s is not None
                             else DEFAULT_GATEWAY_STALE_S),
            ram_warn_pct=warn, ram_heavy_pct=heavy)
        extra = block["alert_count"] + len(block["derived_alerts"])
        if primary:
            node["online"] = True
    elif state == "stale":
        # a producer that stopped while the live poll stayed green is a real fault, and exactly the kind
        # the reasoner should see: one anomaly, not one per missed tick. A primary node stays online --
        # a missed tick is not a fault, and the dead tier below is what draws that line.
        extra = 1
        block["note"] = "producer snapshot is stale"
        if primary:
            node["online"] = True
    elif state == "dead":
        extra = 1
        block["note"] = "producer snapshot is dead (no tick for over an hour)"
        if primary:
            node["online"] = False
    elif state == "missing":
        extra = 1 if primary else 0
        block["note"] = "producer snapshot is missing"
        if primary:
            node["online"] = False
    else:
        extra = 1 if primary else 0
        block["note"] = "producer snapshot is unreadable"
        if primary:
            node["online"] = False

    for key in SNAPSHOT_KEYS:
        if key in data:
            block[key] = data[key]

    prod_host = data.get("host")
    if isinstance(prod_host, dict) and prod_host:
        host = node.get("host")
        if not isinstance(host, dict):
            node["host"] = host = {}
        for key, value in prod_host.items():
            host.setdefault(key, value)      # the live poll's own host facts win on conflict

    node["snapshot"] = block
    if extra:
        node["anomalies"] = int(node.get("anomalies") or 0) + extra
        node["alerts_count"] = int(node.get("alerts_count") or 0) + extra

    if primary:
        _display_from_snapshot(node, block, warn, heavy)
        if node.get("online"):
            # federation_reasoner correlates on these; a snapshot-sourced node has no Netdata alarms, so
            # without this key the whole cluster's cross-node correlation reads nothing for it
            node["raw"] = {"collector": {
                "snapshot": {"alert_keys": alert_keys(data, block["derived_alerts"])},
                "gpu": {"drift_flags": []}}}
    return node
