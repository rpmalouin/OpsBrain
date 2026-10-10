"""
Ops Brain - GPU driver-reconcile ingest module.

Reads the log written by the host's NVIDIA driver-reconcile apt hook
(`/usr/local/sbin/nvidia-module-reconcile`, wired as `DPkg::Post-Invoke` from
`/etc/apt/apt.conf.d/99-nvidia-module-reconcile`) and merges it with the LIVE
module state, so a driver/library mismatch becomes a first-class signal instead
of a silent blindness.

Background: an unattended `apt-daily-upgrade` installs a newer nvidia userspace
and `nvidia-dkms` rebuilds the module for the running kernel, but the module
ALREADY LOADED stays at the old version. NVML then refuses to talk to it,
`nvidia-smi` fails with "Driver/library version mismatch", the collector writes
`gpus: []`, and GPU-drift detection compares nothing and reports healthy — the
failure looks exactly like a quiet GPU (Sep 2026 stayed blind for days; the same
thing recurred 2026-10-10 on the 595.91.07 -> 595.99.02 step).

The hook reloads the module automatically, EXCEPT when a GPU container holds the
device (rmmod cannot succeed under a live `llama-server`): it then logs the
mismatch plus the remedy and DEFERS. That deferred case is the gap this module
closes — it is the state a human has to act on, and nothing else in the pipeline
surfaces it (the hook's log is a host file no dashboard reads).

Two inputs, deliberately:
  * the hook's log  -> what the hook decided and WHEN (deferred / reloaded / failed),
    which containers held the GPU, and the exact remedy it printed;
  * a LIVE comparison (loaded `nvidia.ko` version vs the module on disk for the
    running kernel) -> whether the mismatch is still true NOW. The log alone would
    go stale: a deferred mismatch a human fixed by hand would alarm forever, so
    `state` is driven by the live comparison and the log supplies the context.

Self-contained, mirroring vault_drift_ingest.py: the component functions are PURE
(parse -> classify -> context -> dashboard) and only `collect_gpu_reconcile(cfg)`
reads config / the host. Degrades gracefully: a missing or unreadable log yields
`{"enabled": true, "up": false, "err": ...}` — never raises.

NOTIFY-ONLY: a driver mismatch is a host condition that must be fixed by a human
(stopping GPU containers and reloading the module). This module never proposes a
restart, a kill, or a prune.

Usage (standalone, same import pattern as collector.py):
    sys.path.insert(0, <parent>)
    from common import Cfg
    from collector.gpu_reconcile_ingest import collect_gpu_reconcile
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, now_iso  # noqa: E402

DEFAULT_LOG = Path("/var/log/nvidia-module-reconcile.log")
DEFAULT_MODULE_VERSION_FILE = Path("/sys/module/nvidia/version")
DEFAULT_KO_ROOT = Path("/lib/modules")
DEFAULT_MAX_AGE_S = 60 * 60 * 24 * 7          # a deferred event older than a week is history

# Event kinds, in the order the hook writes them.
KIND_DEFERRED = "deferred"      # mismatch found, GPU held -> needs a human
KIND_RELOADING = "reloading"   # mismatch found, reload attempted
KIND_RELOADED = "reloaded"     # reload succeeded
KIND_FAILED = "failed"         # reload attempted and failed (still stuck)
KIND_NOOP = "noop"             # hook ran, nothing to reconcile
EVENT_KINDS = (KIND_DEFERRED, KIND_RELOADING, KIND_RELOADED, KIND_FAILED, KIND_NOOP)
# A kind that means somebody has to act (or did): these get an attention level.
NEEDS_HUMAN = (KIND_DEFERRED, KIND_FAILED)

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?)\s+(.*)$")
_VERS_RE = re.compile(r"loaded=([^\s]+)\s+disk=([^\s]+)")
_HOLDERS_RE = re.compile(r"processes=([^\s)]*)\s+containers=([^\s)]*)")


# --------------------------------------------------------------------------- helpers


def _cfg_get(cfg, dotted, default=None):
    """Read a dotted config key from either the Cfg class (Cfg.get) or a plain
    config dict — mirrors vault_drift_ingest._cfg_get."""
    if cfg is None:
        return default
    getter = getattr(cfg, "get", None)
    if getter is not None and not isinstance(cfg, dict):
        try:
            return getter(dotted, default)
        except TypeError:
            return default
    node = cfg if isinstance(cfg, dict) else {}
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _log_path(cfg):
    p = _cfg_get(cfg, "sources.gpu_reconcile.log_path")
    return Path(p).expanduser() if p else DEFAULT_LOG


def _max_age_s(cfg):
    return int(_cfg_get(cfg, "sources.gpu_reconcile.max_age_s", DEFAULT_MAX_AGE_S))


def _split(v):
    """'a,b' -> ['a','b']; '' / 'none' -> []."""
    v = (v or "").strip()
    if not v or v == "none":
        return []
    return [x for x in (p.strip() for p in v.split(",")) if x]


def _age_s(ts, now=None):
    """Seconds since an ISO-8601 (possibly Z-suffixed) stamp, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    ref = now or datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return int((ref - created).total_seconds())
    except Exception:
        return None


# --------------------------------------------------------------------------- 1) pull


def parse_log(text):
    """Parse the reconcile log into a list of events (oldest first).

    Recognizes only the lines the hook writes; anything else (a truncated line, a
    hand-added note, a rotated fragment) is skipped rather than guessed at. Never
    raises. Each event: {ts, kind, loaded, disk, processes, containers, remedy, raw}.
    """
    events = []
    pending = None                      # the last mismatch, to attach its "remedy:" line to
    for line in (text or "").splitlines():
        m = _TS_RE.match(line.strip())
        if not m:
            continue
        ts, body = m.group(1), m.group(2).strip()
        low = body.lower()
        if "remedy:" in low:
            remedy = body.split("remedy:", 1)[1].strip()
            if pending is not None:
                pending["remedy"] = remedy
            continue
        if "mismatch" in low:
            vers = _VERS_RE.search(body)
            holders = _HOLDERS_RE.search(body)
            if "defer" in low:
                kind = KIND_DEFERRED
            elif "reloading" in low:
                kind = KIND_RELOADING
            else:
                kind = KIND_RELOADING
            ev = {
                "ts": ts,
                "kind": kind,
                "loaded": vers.group(1) if vers else None,
                "disk": vers.group(2) if vers else None,
                "processes": _split(holders.group(1)) if holders else [],
                "containers": _split(holders.group(2)) if holders else [],
                "remedy": None,
                "warnings": [],
                "raw": body[:300],
            }
            events.append(ev)
            pending = ev
        elif "reloaded: loaded module now" in low:
            vers = _VERS_RE.search(body)
            now_v = None
            m2 = re.search(r"loaded module now\s+([^\s]+)", body)
            if m2:
                now_v = m2.group(1)
            events.append({"ts": ts, "kind": KIND_RELOADED, "loaded": now_v,
                           "disk": vers.group(2) if vers else None, "processes": [],
                           "containers": [], "remedy": None, "warnings": [], "raw": body[:300]})
            pending = None
        elif "failed" in low:
            # An rmmod that failed inside a reload attempt is a WARNING on that attempt, not
            # the attempt's verdict — the hook still goes on to modprobe and may succeed. Only
            # modprobe's terminal failure line means the reload FAILED.
            if "rmmod" in low and pending is not None:
                pending.setdefault("warnings", []).append(body[:160])
                continue
            events.append({"ts": ts, "kind": KIND_FAILED, "loaded": None, "disk": None,
                           "processes": [], "containers": [], "remedy": None,
                           "warnings": [], "raw": body[:300]})
            pending = None
        elif "nothing to do" in low:
            events.append({"ts": ts, "kind": KIND_NOOP, "loaded": None, "disk": None,
                           "processes": [], "containers": [], "remedy": None,
                           "warnings": [], "raw": body[:300]})
    return events


def live_versions(version_file=DEFAULT_MODULE_VERSION_FILE, ko_root=DEFAULT_KO_ROOT,
                  kernel_release=None, modinfo=None):
    """Compare the LOADED nvidia module with the one on disk for this kernel.

    Returns {"loaded": str|None, "disk": str|None, "match": bool|None, "ko": path|None,
    "err": str|None}. `match` is None when either side is unknown (no GPU driver, no
    kernel module for this release) — never guess a verdict from half the picture.
    `modinfo` is injectable so tests need no real kernel tree.
    """
    out = {"loaded": None, "disk": None, "match": None, "ko": None, "err": None}
    try:
        raw = Path(version_file).read_text().strip()
        out["loaded"] = raw or None
    except Exception as e:
        out["err"] = f"loaded version unreadable: {e}"
        return out
    release = kernel_release or os.uname().release
    ko = None
    try:
        hits = sorted(Path(ko_root).glob(f"{release}/**/nvidia.ko*"))
        if hits:
            ko = str(hits[0])
    except Exception as e:
        out["err"] = f"module search failed: {e}"
        return out
    if not ko:
        out["err"] = f"no nvidia.ko for kernel {release} under {ko_root}"
        return out
    out["ko"] = ko
    modinfo_fn = modinfo
    if modinfo_fn is None:
        def modinfo_fn(path):  # noqa: E306 - default implementation
            p = subprocess.run(["/usr/sbin/modinfo", "-F", "version", path],
                               capture_output=True, text=True, timeout=30)
            return p.stdout.strip() if p.returncode == 0 else None
    try:
        disk = modinfo_fn(ko)
    except Exception as e:
        out["err"] = f"modinfo failed: {e}"
        return out
    out["disk"] = disk or None
    if out["loaded"] and out["disk"]:
        out["match"] = out["loaded"] == out["disk"]
    return out


# --------------------------------------------------------------------------- 2) classify


def classify(events, max_age_s=DEFAULT_MAX_AGE_S, now=None):
    """Collapse the event stream into counts + the entries that matter.

    `last` is the newest event of any kind (what the hook last did); `last_mismatch`
    is the newest mismatch-bearing event (deferred/reloading/reloaded/failed). Lists
    are capped for the digest. A `deferred`/`failed` event older than `max_age_s` is
    still reported but flagged `stale_event`, so a long-resolved incident stops
    reading as news.
    """
    evs = [e for e in (events or []) if isinstance(e, dict)]
    counts = {k: 0 for k in EVENT_KINDS}
    for e in evs:
        if e.get("kind") in counts:
            counts[e["kind"]] += 1
    mismatch_evs = [e for e in evs if e.get("kind") in (KIND_DEFERRED, KIND_RELOADING,
                                                        KIND_RELOADED, KIND_FAILED)]
    needs_human_evs = [e for e in evs if e.get("kind") in NEEDS_HUMAN]
    reloaded_evs = [e for e in evs if e.get("kind") == KIND_RELOADED]
    last = evs[-1] if evs else None
    last_mismatch = mismatch_evs[-1] if mismatch_evs else None
    last_needs_human = needs_human_evs[-1] if needs_human_evs else None
    last_reloaded = reloaded_evs[-1] if reloaded_evs else None
    # A deferral the hook later reloaded its way out of is history, not an open item. The
    # log is append-only and never rewritten, so the timestamps decide.
    unresolved = bool(last_needs_human) and (
        last_reloaded is None
        or str(last_needs_human.get("ts") or "") > str(last_reloaded.get("ts") or ""))
    ref_ev = last_needs_human or last_mismatch
    age = _age_s(ref_ev.get("ts"), now=now) if ref_ev else None
    return {
        "counts": counts,
        "last": last,
        "last_mismatch": last_mismatch,
        "last_needs_human": last_needs_human,
        "last_reloaded": last_reloaded,
        "needs_human_unresolved": unresolved,
        "deferred": [e for e in evs if e.get("kind") == KIND_DEFERRED][-10:],
        "failed": [e for e in evs if e.get("kind") == KIND_FAILED][-10:],
        "reloaded": reloaded_evs[-10:],
        "last_event_age_s": age,
        "stale_event": bool(age is not None and age > max_age_s),
        "events": len(evs),
    }


# --------------------------------------------------------------------------- 3) context nodes + dashboard


def create_context_nodes(report, classified):
    """Reasoner/dashboard context: the LIVE verdict plus the last hook event.

    `state`:
      "mismatch" — loaded != disk right now: NVML is broken and the GPU is blind.
      "ok"       — loaded == disk right now (any hook event is history).
      "deferred" / "failed" / "reloaded" / "unknown" — the live verdict is unavailable
                   (no driver / no module on disk), so the log is the only evidence.
    `attention`: "actionable" only while the mismatch is live; a deferral/failure the hook
    has NOT resolved (and that is not stale) is "low"; everything else "none".
    """
    live = report.get("live") or {}
    last = (classified.get("last_needs_human")
            or classified.get("last_mismatch") or {})
    counts = classified.get("counts") or {}
    if live.get("match") is True:
        state = "ok"
    elif live.get("match") is False:
        state = "mismatch"
    elif classified.get("last_mismatch"):
        state = (classified.get("last_mismatch") or {}).get("kind") or "unknown"
    else:
        state = "unknown"

    if state == "mismatch":
        attention = "actionable"
    elif classified.get("needs_human_unresolved") and not classified.get("stale_event"):
        attention = "low"
    else:
        attention = "none"

    return {
        "attention": attention,
        "state": state,
        "loaded": live.get("loaded") or last.get("loaded"),
        "disk": live.get("disk") or last.get("disk"),
        "live_match": live.get("match"),
        "last_event_kind": last.get("kind"),
        "last_event_ts": last.get("ts"),
        "last_event_age_s": classified.get("last_event_age_s"),
        "stale_event": bool(classified.get("stale_event")),
        "holders": sorted(set((last.get("containers") or []) + (last.get("processes") or []))),
        "remedy": last.get("remedy"),
        "deferred_count": counts.get(KIND_DEFERRED, 0),
        "failed_count": counts.get(KIND_FAILED, 0),
        "reloaded_count": counts.get(KIND_RELOADED, 0),
    }


def _summary(ctx):
    state, loaded, disk = ctx.get("state"), ctx.get("loaded"), ctx.get("disk")
    vers = f"{loaded} vs {disk}" if (loaded and disk) else (loaded or disk or "?")
    if state == "mismatch":
        s = f"driver mismatch: loaded {vers}"
        if ctx.get("last_event_kind") == KIND_DEFERRED:
            held = ", ".join(ctx.get("holders") or []) or "a GPU consumer"
            s += f"; reload DEFERRED by the apt hook ({held} holds the GPU)"
        if ctx.get("remedy"):
            s += f" — {ctx['remedy']}"
        return s
    if state == "ok":
        s = f"driver in sync ({loaded or '?'})"
        if ctx.get("last_event_kind") in NEEDS_HUMAN:
            age = ctx.get("last_event_age_s")
            when = f"{age / 3600:.1f} h ago" if isinstance(age, int) else "earlier"
            s += f"; a reload was {ctx['last_event_kind']} {when} (since resolved)"
        return s
    if state in (KIND_DEFERRED, KIND_FAILED):
        s = f"hook {state} a reload ({vers}); live module version unavailable to confirm"
        if ctx.get("remedy"):
            s += f" — {ctx['remedy']}"
        return s
    if state == KIND_RELOADED:
        return f"hook reloaded the module ({vers}); live module version unavailable to confirm"
    return "no driver-reconcile events; module versions unavailable"


def update_dashboard_snapshot(report, classified):
    """Dashboard view: state + a short human summary line + the remedy."""
    ctx = report.get("context_nodes") or {}
    if not report.get("up") and ctx.get("state") == "unknown":
        return {"up": False, "state": "unknown"}
    return {
        "up": bool(report.get("up")),
        "state": ctx.get("state"),
        "attention": ctx.get("attention"),
        "loaded": ctx.get("loaded"),
        "disk": ctx.get("disk"),
        "remedy": ctx.get("remedy"),
        "last_event_ts": ctx.get("last_event_ts"),
        "source": "apt-hook",
        "summary": _summary(ctx),
    }


# --------------------------------------------------------------------------- orchestrator


def collect_gpu_reconcile(cfg, now=None):
    """Read the hook log + the live module state; produce the collector doc block."""
    src = _cfg_get(cfg, "sources.gpu_reconcile", {}) or {}
    if not src.get("enabled", True):
        return {"enabled": False}
    log_path = _log_path(cfg)
    max_age = _max_age_s(cfg)
    version_file = _cfg_get(cfg, "sources.gpu_reconcile.module_version_file",
                            str(DEFAULT_MODULE_VERSION_FILE))
    ko_root = _cfg_get(cfg, "sources.gpu_reconcile.ko_root", str(DEFAULT_KO_ROOT))

    out = {"enabled": True, "up": False, "err": None, "log_path": str(log_path)}
    try:
        text = Path(log_path).read_text()
    except Exception as e:
        text = ""
        out["err"] = f"read failed: {e}"
    try:
        events = parse_log(text)
        classified = classify(events, max_age_s=max_age, now=now)
        out["up"] = bool(text) or Path(log_path).exists()
    except Exception as e:                                    # pragma: no cover - defensive
        events, classified = [], classify([])
        out["err"] = f"parse failed: {e}"

    try:
        live = live_versions(version_file=version_file, ko_root=ko_root)
    except Exception as e:                                    # pragma: no cover - defensive
        live = {"loaded": None, "disk": None, "match": None, "ko": None, "err": str(e)}
    out["live"] = live
    if out["err"] is None and live.get("err") and live.get("match") is None:
        out["err"] = live["err"]

    out["events"] = classified.get("events", 0)
    out["classify"] = classified
    out["context_nodes"] = create_context_nodes(out, classified)
    out["dashboard"] = update_dashboard_snapshot(out, classified)
    out["collected_at"] = now_iso()
    return out


if __name__ == "__main__":
    # Standalone smoke test: load config and print the block.
    Cfg.load(None)
    import json as _json
    print(_json.dumps(collect_gpu_reconcile(Cfg), indent=2, default=str))
