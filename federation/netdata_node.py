"""Ops Brain - Netdata-sourced federation node adapter.

Reduces a remote host's Netdata HTTP API into the same node telemetry block
``federation_collector._collect_node`` emits for an OpsBrain node, so a host that runs
Netdata but NOT OpsBrain (the Mac Studio) can join the cluster with nothing installed on it.

Endpoints used (measured working against Netdata v2.11.1 / macOS, 2026-10-10):

    GET /api/v1/info                        -> version, os_*, cores_total, ram_total, host_labels
    GET /api/v1/alarms?all                  -> {alarms: {name: {status, name, chart, value, ...}}}
    GET /api/v1/data?chart=disk_space.<mount>   -> dims avail/used/reserved for root, units GiB
    GET /api/v1/data?chart=system.ram           -> dims active/wired/.../free, units MiB
    GET /api/v1/data?chart=system.uptime        -> dim "uptime", units seconds

Three traps this module exists to avoid (each measured on the Mac, do not "simplify" them away):

1. **847 alarms, and the only CRITICAL is a transient mounted disk image** — a naive ingest makes
   a permanent cluster anomaly and a ``notify_cluster`` recommendation every federation cycle.
   Filter on status AND on the mount behind the alarm (``alarm_skip_mounts``).
2. **3288 charts**, dominated by Time Machine snapshot mounts — never enumerate ``disk_space.*``;
   watch an explicit ``volumes`` list.
3. **disk dims are GiB, not percent** — the root volume reports ``used=315.3`` for a 34 % full
   disk, so a percentage must be computed. Returning ``used`` here would report 315 %.
"""
import json
import urllib.request
from urllib.parse import quote

KEEP = ("CRITICAL", "WARNING", "ERROR")
ALARMS_MAX_BYTES = 3_000_000


def _f(value):
    """Coerce a Netdata numeric (often a numeric string) to float, else None."""
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


def _get_json(url, timeout, max_bytes=None):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read(max_bytes) if max_bytes else resp.read()
    return json.loads(raw.decode("utf-8"))


def parse_info(doc):
    """Host facts from /api/v1/info (works for hosts that report little in `host_labels`)."""
    doc = doc or {}
    labels = doc.get("host_labels") or {}
    ram_bytes = _f(doc.get("ram_total")) or _f(labels.get("_system_ram_total"))
    os_name = doc.get("os_name") or labels.get("_os_name") or ""
    os_version = doc.get("os_version") or labels.get("_os_version") or ""
    return {
        "hostname": labels.get("_hostname"),
        "os": f"{os_name} {os_version}".strip() or None,
        "cpu": labels.get("_system_cpu_model"),
        "cores": _i(doc.get("cores_total") or labels.get("_system_cores")),
        "arch": labels.get("_architecture"),
        "ram_total_gb": round(ram_bytes / (1024 ** 3), 1) if ram_bytes else None,
        "netdata": doc.get("version"),
    }


def chart_mount(chart):
    """'disk_space./Volumes/Data' -> '/Volumes/Data'; None when the chart is not a disk."""
    chart = str(chart or "")
    return chart[len("disk_space."):] if chart.startswith("disk_space.") else None


def dimension_value(doc, dim):
    """Read one named dimension from a single-point Netdata data payload."""
    labels = (doc or {}).get("labels") or []
    rows = (doc or {}).get("data") or []
    if not labels or not rows:
        return None
    try:
        return _f(rows[0][labels.index(dim)])
    except (ValueError, IndexError, TypeError):
        return None


def select_alarms(doc, keep=KEEP, skip_names=(), skip_mounts=()):
    """Keep only alarms worth alarming about, minus the configured skip-lists.

    `keep` mirrors collector.collect_netdata's CRITICAL/WARNING/ERROR filter (Netdata's
    UNDEFINED status is noise — 318 entries on the Mac). `skip_names` drops alarm names,
    `skip_mounts` drops any alarm whose chart points at a transient mount (mounted disk
    images, Time Machine local-snapshot mounts).
    """
    alarms = (doc or {}).get("alarms") or {}
    if not isinstance(alarms, dict):  # tolerate a list payload
        alarms = {str(a.get("name")): a for a in alarms if isinstance(a, dict)}
    keep = tuple(keep or KEEP)
    skip_names = tuple(skip_names or ())
    skip_mounts = tuple(skip_mounts or ())
    out = []
    for alarm in alarms.values():
        if not isinstance(alarm, dict):
            continue
        status = str(alarm.get("status") or "")
        if status not in keep:
            continue
        if alarm.get("name") in skip_names:
            continue
        mount = chart_mount(alarm.get("chart"))
        if mount and any(mount.startswith(str(s)) for s in skip_mounts):
            continue
        out.append({"name": alarm.get("name"), "status": status,
                    "mount": mount, "value": _f(alarm.get("value"))})
    return out


def volume_percent(doc):
    """Used percent for one disk_space.<mount> payload.

    The dimensions (avail, used, reserved for root) are GiB — measured on the Mac 2026-10-10:
    the root volume reports used=315.3 for a 34 % full disk — so a percentage is computed.
    """
    labels = (doc or {}).get("labels") or []
    rows = (doc or {}).get("data") or []
    if not labels or not rows:
        return None
    try:
        used = _f(rows[0][labels.index("used")])
        avail = _f(rows[0][labels.index("avail")])
    except (ValueError, IndexError, TypeError):
        return None
    if used is None or avail is None or (used + avail) <= 0:
        return None
    return round(used / (used + avail) * 100, 1)


RAM_USED_DIMS = ("active", "wired", "compressor")


def ram_used_percent(doc):
    """Memory in use, as a percentage of the sum of the reported dimensions.

    macOS splits RAM differently from Linux (active/wired/throttled/compressor/inactive/
    purgeable/speculative/free), and `inactive`/`purgeable` are reclaimable cache — so the naive
    `100 * (total - free) / total` reports ~98 % on a healthy 64 GB Studio. That is the kind of
    number that reads as an emergency and is not one, so "in use" here is
    active + wired + compressor (what Activity Monitor calls Memory Used), falling back to
    not-free only when a build does not report those dimensions at all.
    """
    labels = (doc or {}).get("labels") or []
    rows = (doc or {}).get("data") or []
    if not labels or not rows:
        return None
    try:
        values = {label: _f(rows[0][i]) for i, label in enumerate(labels) if label != "time"}
    except (ValueError, IndexError, TypeError):
        return None
    total = sum(v for v in values.values() if v is not None)
    if total <= 0:
        return None
    present = [v for v in (values.get(d) for d in RAM_USED_DIMS) if v is not None]
    if present:
        return round(sum(present) / total * 100, 1)
    free = values.get("free")
    if free is None:
        return None
    return round((total - free) / total * 100, 1)


def build_node(node_cfg, host, alarms, volumes, ram_used_percent=None, uptime_s=None):
    """Assemble the federation node block from reduced Netdata data.

    Carries the same contract keys `federation_collector._collect_node` produces for an OpsBrain
    node (online/type/confidence/drift_events/anomalies/restart_events/disk_used_percent/
    containers/alerts_count) plus display-only extras (host/volumes/alarms/uptime_s) and `raw`,
    which is what `federation_reasoner` correlates alarm names across nodes with.
    """
    vols = dict(volumes or {})
    readable = [pct for pct in vols.values() if pct is not None]
    return {
        "online": True,
        "type": node_cfg.get("type") or "macos",
        # no reasoner on the node: null confidence earns FULL credit in federation_reasoner,
        # and cluster_confidence ignores it rather than averaging a fake 0
        "confidence": None,
        "drift_events": 0,
        "anomalies": len(alarms or []),
        "restart_events": 0,
        "disk_used_percent": max(readable) if readable else None,
        "containers": {"running": 0, "total": 0},   # no Docker on the Mac
        "alerts_count": len(alarms or []),
        "host": dict(host or {}, ram_used_percent=ram_used_percent),
        "uptime_s": uptime_s,
        "volumes": vols,
        "alarms": list(alarms or []),
        "raw": {"collector": {"netdata": {
            "alarms_active": [{"name": a.get("name")} for a in (alarms or [])]}}},
    }


def _failed_node(node_cfg, err):
    return {"online": False, "type": node_cfg.get("type") or "macos",
            "confidence": None, "drift_events": 0, "anomalies": 0, "restart_events": 0,
            "disk_used_percent": None, "containers": {"running": 0, "total": 0},
            "alerts_count": 0, "err": err}


def collect_netdata_node(node_cfg, timeout=6):
    """Fetch + reduce one Netdata-sourced node. Never raises: any failure is an offline node.

    Required endpoints are info and alarms (a failure there marks the node offline). The per-volume
    disk reads, uptime and RAM are best-effort — a missing chart leaves a null field, not an
    offline node, because Netdata builds differ in what they collect.
    """
    base = str(node_cfg.get("collector_endpoint") or "").rstrip("/")
    if not base:
        return _failed_node(node_cfg, "missing collector_endpoint")
    try:
        info = _get_json(f"{base}/api/v1/info", timeout)
        alarms = select_alarms(
            _get_json(f"{base}/api/v1/alarms?all", timeout, max_bytes=ALARMS_MAX_BYTES),
            keep=tuple(node_cfg.get("alarm_keep") or KEEP),
            skip_names=tuple(node_cfg.get("alarm_skip_names") or ()),
            skip_mounts=tuple(node_cfg.get("alarm_skip_mounts") or ()),
        )
    except Exception as exc:  # noqa: BLE001 - a node must degrade, not crash the cluster poll
        return _failed_node(node_cfg, f"{type(exc).__name__}: {exc}")

    volumes = {}
    for mount in node_cfg.get("volumes") or []:
        # fully percent-encode the mount: the chart id carries slashes, and only the encoded
        # form is a valid URI query value (Netdata accepts both; verified 2026-10-10)
        chart = "disk_space." + quote(str(mount), safe="")
        try:
            volumes[mount] = volume_percent(
                _get_json(f"{base}/api/v1/data?chart={chart}&after=-10&points=2", timeout))
        except Exception:  # noqa: BLE001 - one unreadable volume must not blind the node
            volumes[mount] = None

    uptime_s = ram = None
    for chart, setter in (("system.uptime", "uptime"), ("system.ram", "ram")):
        try:
            doc = _get_json(f"{base}/api/v1/data?chart={chart}&after=-10&points=2", timeout)
        except Exception:  # noqa: BLE001
            continue
        if setter == "uptime":
            uptime_s = dimension_value(doc, "uptime")
        else:
            ram = ram_used_percent(doc)

    return build_node(node_cfg, parse_info(info), alarms, volumes,
                      ram_used_percent=ram, uptime_s=uptime_s)
