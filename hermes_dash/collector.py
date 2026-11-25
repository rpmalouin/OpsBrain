"""Read-only collector for the Hermes cron dashboards (dockerVM + Mac Studio).

Design rules this module exists to keep:

* **No model in the loop.** Everything here is file/database reading with the
  standard library, so a page refresh costs no tokens and no API call.
* **Read-only on every source.** SQLite files are opened with ``mode=ro``; the
  Hermes profile directories, its job store, its logs and the Mac's producer
  document are only ever READ. The only path this project writes to is
  ``state/hermes_runs.jsonl`` (a rollup of run history, written by
  ``snapshot.py``, never by an HTTP request).
* **No invented data.** A source that cannot be read is reported as missing or
  unreadable with its age, never as a healthy zero. ``not_connected`` names what
  the dashboard cannot see and why.

The two hosts are deliberately different shapes:

``dockervm`` reads the live Hermes install on this machine (jobs.json,
cron/executions.db, cron/output, logs, gateway state).
``macstudio`` reads the producer document the Mac writes every 15 minutes
(``logs/macstudio_node.json``); per-job detail for that node is not reachable
from here and is listed in ``not_connected``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- paths
# Every root is env-overridable so the tests (and a future move) can point at a
# fixture tree without touching the live install.
HERMES_HOME = Path(os.environ.get("HERMES_DASH_HERMES_HOME", "/root/.hermes"))
OPSBRAIN = Path(os.environ.get("HERMES_DASH_OPSBRAIN", "/appdata/OpsBrain"))
STATE_DIR = Path(os.environ.get("HERMES_DASH_STATE", str(OPSBRAIN / "state")))
ROLLUP_FILE = Path(os.environ.get("HERMES_DASH_ROLLUP", str(STATE_DIR / "hermes_runs.jsonl")))
MAC_SNAPSHOT = Path(
    os.environ.get("HERMES_DASH_MAC_SNAPSHOT", str(OPSBRAIN / "logs" / "macstudio_node.json"))
)

JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"
EXEC_DB = HERMES_HOME / "cron" / "executions.db"
OUTPUT_ROOT = HERMES_HOME / "cron" / "output"
GATEWAY_STATE = HERMES_HOME / "gateway_state.json"
GATEWAY_HEARTBEAT = HERMES_HOME / "state" / "gateway.heartbeat"
TICKER_HEARTBEAT = HERMES_HOME / "cron" / "ticker_heartbeat"
TICKER_SUCCESS = HERMES_HOME / "cron" / "ticker_last_success"
LOGS_DIR = HERMES_HOME / "logs"

MISSING = "missing"
STALE = "stale"
DEAD = "dead"
OK = "ok"

# ---------------------------------------------------------------- serving allow-list
# /api/file refuses anything whose REAL path is not inside one of these. That is
# what keeps /root/.hermes/.env, config.yaml, auth.json, state.db, job prompts as
# files, SSH keys and the vault off the wire: none of them live in a readable dir.
ALLOWED_DIRS = [
    OUTPUT_ROOT,
    LOGS_DIR,
    HERMES_HOME / "state",
    OPSBRAIN / "reports",
]
ALLOWED_SUFFIXES = {".md", ".log", ".txt"}
# Belt and braces on top of the directory allow-list.
DENY_NAME_PARTS = (
    ".env",
    "config.yaml",
    "auth.json",
    "credential",
    "secret",
    "token",
    "password",
    "passwd",
    "id_rsa",
    "id_ed25519",
    "smbcred",
    ".db",
    ".sqlite",
    ".key",
    ".pem",
)

MAX_FILE_BYTES = 1_000_000
MAX_LOG_TAIL_BYTES = 300_000   # a log link opens the last 300 kB, never the whole file


# ---------------------------------------------------------------- small helpers
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value) -> datetime | None:
    """Parse the several timestamp shapes Hermes writes. Never raises."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d_%H-%M-%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_seconds(dt: datetime | None, ref: datetime | None = None) -> float | None:
    if dt is None:
        return None
    ref = ref or now_utc()
    return round((ref - dt).total_seconds(), 1)


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _file_freshness(path: Path, ref: datetime) -> dict:
    info = {"path": str(path), "present": path.exists(), "mtime": None, "age_s": None, "size": None}
    if not path.exists():
        return info
    try:
        st = path.stat()
    except OSError as exc:
        info["error"] = str(exc)
        return info
    dt = datetime.fromtimestamp(st.st_mtime, timezone.utc)
    info.update(mtime=iso(dt), age_s=age_seconds(dt, ref), size=st.st_size)
    return info


def _read_text_tail(path: Path, max_bytes: int = 250_000) -> str:
    """Last ``max_bytes`` of a text file, decoded leniently. Empty on any error."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # drop the partial first line
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def is_servable(path: str | os.PathLike) -> tuple[bool, str]:
    """True only for a real, non-symlinked-out file inside an allowed folder.

    Returns ``(ok, reason)`` so the server can log/refuse with a reason instead
    of a bare boolean.
    """
    raw = str(path or "")
    if not raw or "\x00" in raw:
        return False, "empty or malformed path"
    try:
        real = Path(os.path.realpath(raw))
    except OSError as exc:
        return False, f"unresolvable: {exc}"
    lowered = real.name.lower()
    if any(part in lowered for part in DENY_NAME_PARTS):
        return False, "denied filename"
    if real.suffix.lower() not in ALLOWED_SUFFIXES:
        return False, "extension not served"
    for allowed in ALLOWED_DIRS:
        try:
            base = Path(os.path.realpath(allowed))
        except OSError:
            continue
        if real == base:
            continue
        if base in real.parents:
            if not real.is_file():
                return False, "not a regular file"
            try:
                # Logs are served as a tail by read_servable, so size never blocks one.
                if real.suffix.lower() != ".log" and real.stat().st_size > MAX_FILE_BYTES:
                    return False, "file too large to serve"
            except OSError as exc:
                return False, f"stat failed: {exc}"
            return True, "ok"
    return False, "outside the served folders"


def read_servable(path: str | os.PathLike) -> tuple[str | None, str]:
    """Read an allowed file. Logs are served as their TAIL, never refused for size.

    errors.log and gateway.log pass 1 MB within a day on this host, and a link that
    answers "too large" is a link that never worked. The tail is the part anyone
    reads; the reply says how much was omitted so it is never mistaken for the whole
    file.
    """
    ok, reason = is_servable(path)
    if not ok:
        return None, reason
    real = os.path.realpath(str(path))
    budget = MAX_LOG_TAIL_BYTES if Path(real).suffix.lower() == ".log" else MAX_FILE_BYTES
    try:
        size = os.path.getsize(real)
        with open(real, "rb") as fh:
            omitted = 0
            if size > budget:
                if Path(real).suffix.lower() != ".log":
                    return None, f"file too large to serve ({size} bytes)"
                omitted = size - budget
                fh.seek(omitted)
                fh.readline()
            body = fh.read(budget)
    except OSError as exc:
        return None, str(exc)
    text = body.decode("utf-8", errors="replace")
    if omitted:
        text = f"[showing the last {budget} of {size} bytes; {omitted} bytes omitted]\n" + text
    return text, "ok"


# ---------------------------------------------------------------- dockerVM sources
def load_jobs() -> tuple[list[dict], dict]:
    freshness = _file_freshness(JOBS_FILE, now_utc())
    try:
        doc = json.loads(JOBS_FILE.read_text())
    except FileNotFoundError:
        return [], freshness | {"error": "jobs.json not found"}
    except (OSError, json.JSONDecodeError) as exc:
        return [], freshness | {"error": str(exc)}
    jobs = doc.get("jobs") if isinstance(doc, dict) else doc
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    if not isinstance(jobs, list):
        return [], freshness | {"error": "jobs.json has no job list"}
    freshness["updated_at"] = doc.get("updated_at") if isinstance(doc, dict) else None
    return jobs, freshness


def load_executions(limit: int = 4000) -> tuple[list[dict], dict]:
    freshness = _file_freshness(EXEC_DB, now_utc())
    if not EXEC_DB.exists():
        return [], freshness | {"error": "executions.db not found"}
    rows: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{EXEC_DB}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "select id, job_id, source, status, claimed_at, started_at, finished_at,"
                " error, delivery_outcome, scheduled_instant"
                " from executions order by claimed_at desc limit ?",
                (limit,),
            )
            rows = [dict(r) for r in cur]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [], freshness | {"error": str(exc)}
    freshness["rows"] = len(rows)
    return rows, freshness


def load_incidents() -> tuple[list[dict], dict]:
    freshness = _file_freshness(EXEC_DB, now_utc())
    if not EXEC_DB.exists():
        return [], freshness | {"error": "executions.db not found"}
    rows: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{EXEC_DB}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "select id, job_id, state, failure_type, first_seen_at, last_seen_at,"
                " acked_at, closed_at, error, output_file from cron_incidents"
                " order by last_seen_at desc limit 200"
            )
            rows = [dict(r) for r in cur]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [], freshness | {"error": str(exc)}
    freshness["rows"] = len(rows)
    return rows, freshness


def _output_ts(name: str) -> datetime | None:
    stem = Path(name).stem
    return parse_ts(stem)


def load_outputs(per_job: int = 12) -> tuple[list[dict], list[dict], dict]:
    """Per-job output files plus a flat most-recent-first index."""
    freshness = _file_freshness(OUTPUT_ROOT, now_utc())
    per_job_rows: list[dict] = []
    flat: list[dict] = []
    if not OUTPUT_ROOT.is_dir():
        return [], [], freshness | {"error": "output folder not found"}
    try:
        job_dirs = sorted(p for p in OUTPUT_ROOT.iterdir() if p.is_dir())
    except OSError as exc:
        return [], [], freshness | {"error": str(exc)}
    for job_dir in job_dirs:
        try:
            files = sorted((p for p in job_dir.iterdir() if p.is_file()), reverse=True)
        except OSError:
            continue
        for path in files[:per_job]:
            try:
                st = path.stat()
            except OSError:
                continue
            row = {
                "job_id": job_dir.name,
                "name": path.name,
                "path": str(path),
                "mtime": iso(datetime.fromtimestamp(st.st_mtime, timezone.utc)),
                "size": st.st_size,
                "run_ts": iso(_output_ts(path.name)),
            }
            per_job_rows.append(row)
            flat.append(row)
    flat.sort(key=lambda r: r["mtime"] or "", reverse=True)
    freshness["job_dirs"] = len(job_dirs)
    # The folder's own mtime barely moves (files land in job subfolders), so age the
    # source off the NEWEST output file instead — otherwise a busy job reads 2 days stale.
    if flat:
        stamps = [parse_ts(r["mtime"]) for r in flat if r.get("mtime")]
        newest = max((s for s in stamps if s is not None), default=None)
        if newest is not None:
            freshness["mtime"] = iso(newest)
            freshness["age_s"] = age_seconds(newest, now_utc())
            freshness["newest_file"] = flat[0]["name"]
    return per_job_rows, flat, freshness


def load_log_files() -> list[dict]:
    """The log files the dashboard may link: top-level *.log plus <state>/*.log."""
    found: list[dict] = []
    for folder in (LOGS_DIR, HERMES_HOME / "state"):
        if not folder.is_dir():
            continue
        try:
            entries = sorted(folder.glob("*.log"))
        except OSError:
            continue
        for path in entries:
            try:
                st = path.stat()
            except OSError:
                continue
            found.append(
                {
                    "name": path.name,
                    "folder": folder.name,
                    "path": str(path),
                    "size": st.st_size,
                    "mtime": iso(datetime.fromtimestamp(st.st_mtime, timezone.utc)),
                }
            )
    found.sort(key=lambda r: r["mtime"] or "", reverse=True)
    return found


_LOG_LINE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})\s+(?P<lvl>[A-Z]+)\s+(?P<msg>.*)$")
_CRON_TAG = re.compile(r"\[cron_([0-9a-f]{6,32})_")


def _signature(msg: str) -> str:
    """Collapse a log message to a stable signature so repeats group."""
    sig = re.sub(r"\b[0-9a-f]{16,}\b", "<hash>", msg)
    sig = re.sub(r"\b\d+(\.\d+)?\b", "<n>", sig)
    sig = re.sub(r"\s+", " ", sig).strip()
    return sig[:180]


def load_warnings(job_names: dict[str, str], limit_each: int = 4000) -> list[dict]:
    """Grouped WARNING/ERROR/CRITICAL lines from errors.log + gateway.log."""
    groups: dict[tuple[str, str], dict] = {}
    for name in ("errors.log", "gateway.log"):
        path = LOGS_DIR / name
        if not path.exists():
            continue
        lines = _read_text_tail(path).splitlines()[-limit_each:]
        for line in lines:
            m = _LOG_LINE.match(line)
            if not m or m.group("lvl") not in ("WARNING", "ERROR", "CRITICAL"):
                continue
            msg = m.group("msg")
            key = (m.group("lvl"), _signature(msg))
            row = groups.get(key)
            ts = parse_ts(m.group("ts"))
            cron_job = _CRON_TAG.search(msg)
            job_id = None
            if cron_job:
                job_id = next((jid for jid in job_names if jid.startswith(cron_job.group(1))), None)
            if row is None:
                groups[key] = {
                    "level": m.group("lvl"),
                    "message": msg[:400],
                    "count": 1,
                    "first_seen": iso(ts),
                    "last_seen": iso(ts),
                    "source": name,
                    "job_id": job_id,
                    "job_name": job_names.get(job_id) if job_id else None,
                }
            else:
                row["count"] += 1
                row["last_seen"] = iso(ts)
                if ts and row["first_seen"] and ts.isoformat() < row["first_seen"]:
                    row["first_seen"] = iso(ts)
    order = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2}
    rows = sorted(
        groups.values(),
        key=lambda r: (order.get(r["level"], 9), -(r["count"])),
    )
    return rows[:40]


def load_rollup(max_rows: int = 20000) -> list[dict]:
    """Extra run history written by snapshot.py (executions.db is a 1000-row ring)."""
    if not ROLLUP_FILE.exists():
        return []
    rows: list[dict] = []
    try:
        with open(ROLLUP_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-max_rows:]


def append_rollup(rows: list[dict]) -> int:
    """Append executions not already in the rollup. Called by snapshot.py only."""
    existing = {r.get("id") for r in load_rollup(max_rows=200_000) if r.get("id")}
    fresh = [r for r in rows if r.get("id") and r["id"] not in existing]
    if not fresh:
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    keep = [
        "id", "job_id", "source", "status", "claimed_at", "started_at",
        "finished_at", "error", "delivery_outcome", "scheduled_instant",
    ]
    tmp = ROLLUP_FILE.with_suffix(".jsonl.tmp")
    with open(ROLLUP_FILE, "a", encoding="utf-8") as fh:
        # append in chronological order so the tail is the newest
        for row in sorted(fresh, key=lambda r: r.get("claimed_at") or ""):
            fh.write(json.dumps({k: row.get(k) for k in keep}, default=str) + "\n")
    if tmp.exists():
        tmp.unlink()
    return len(fresh)


# ---------------------------------------------------------------- classification
def job_period_seconds(job: dict) -> int | None:
    sched = job.get("schedule") or {}
    kind = sched.get("kind")
    if kind == "interval":
        minutes = _num(sched.get("minutes"), 1)
        return max(60, int(minutes * 60))
    if kind == "cron":
        return 3600
    return None


def overdue_grace(job: dict) -> int:
    """How late a job may be before it counts as overdue.

    Interval jobs are sampled tightly, so three missed ticks is a miss; a calendar job
    gets ten minutes because the ticker itself is a ~60 s cycle and a clock fire may
    land just past its minute. A once job is never "overdue" — it either fired or it
    is still waiting.
    """
    sched = job.get("schedule") or {}
    if sched.get("kind") == "interval":
        return max(120, int(_num(sched.get("minutes"), 1) or 1) * 60 * 3)
    return 600


def classify_job(job: dict, ref: datetime) -> dict:
    """One job row with the three verdicts the page needs: failing, overdue, next-in."""
    last_status = (job.get("last_status") or "").lower()
    streak = int(_num(job.get("failure_streak"), 0) or 0)
    failing = last_status not in ("", "ok", "success", "completed") or streak > 0
    next_run = parse_ts(job.get("next_run_at"))
    last_run = parse_ts(job.get("last_run_at"))
    next_age = age_seconds(next_run, ref) if next_run else None
    next_in = -next_age if next_age is not None else None
    overdue = False
    if job.get("enabled") and next_run is not None:
        overdue = next_in is not None and next_in < -overdue_grace(job)
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "enabled": bool(job.get("enabled")),
        "state": job.get("state"),
        "paused": bool(job.get("paused_at")),
        "paused_reason": job.get("paused_reason"),
        "no_agent": bool(job.get("no_agent")),
        "script": job.get("script"),
        "skills": job.get("skills") or [],
        "schedule_display": job.get("schedule_display"),
        "schedule_kind": (job.get("schedule") or {}).get("kind"),
        "period_s": job_period_seconds(job),
        "next_run_at": iso(next_run),
        "next_in_s": None if next_in is None else round(next_in, 1),
        "last_run_at": iso(last_run),
        "last_status": job.get("last_status"),
        "last_error": job.get("last_error"),
        "last_failure": job.get("last_failure"),
        "failure_streak": streak,
        "deliver": job.get("deliver"),
        "failure_deliver": job.get("failure_deliver"),
        "completed_runs": ((job.get("repeat") or {}).get("completed")),
        "last_dispatch": job.get("last_dispatch"),
        "failing": failing,
        "overdue": overdue,
        "grace_s": overdue_grace(job),
    }


def attach_outputs(runs: list[dict], outputs: list[dict]) -> list[dict]:
    """Attach the output file whose timestamp lands within 2 min of the run."""
    by_job: dict[str, list[dict]] = {}
    for out in outputs:
        ts = parse_ts(out.get("run_ts"))
        if ts is None:
            continue
        by_job.setdefault(out["job_id"], []).append({"ts": ts, "row": out})
    for run in runs:
        run_ts = parse_ts(run.get("started_at")) or parse_ts(run.get("claimed_at"))
        if run_ts is None:
            continue
        best = None
        candidates = by_job.get(str(run.get("job_id") or ""), [])
        for cand in candidates:
            delta = abs((cand["ts"] - run_ts).total_seconds())
            if delta <= 120 and (best is None or delta < best[0]):
                best = (delta, cand["row"])
        if best:
            run["output"] = {
                "path": best[1]["path"],
                "name": best[1]["name"],
                "size": best[1]["size"],
            }
    return runs


def _merge_runs(ring: list[dict], rollup: list[dict], limit: int = 300) -> list[dict]:
    seen: dict[str, dict] = {}
    for row in sorted(rollup, key=lambda r: r.get("claimed_at") or ""):
        if row.get("id"):
            seen[row["id"]] = row
    for row in ring:  # the live DB wins on status
        if row.get("id"):
            seen[row["id"]] = row
    merged = sorted(seen.values(), key=lambda r: r.get("claimed_at") or "", reverse=True)
    return merged[:limit]


# ---------------------------------------------------------------- dockerVM document
def collect_dockervm(ref: datetime | None = None) -> dict:
    ref = ref or now_utc()
    jobs_raw, jobs_fresh = load_jobs()
    jobs = [classify_job(j, ref) for j in jobs_raw]
    names = {j["id"]: j["name"] for j in jobs}
    ring, exec_fresh = load_executions()
    rollup = load_rollup()
    incidents_raw, inc_fresh = load_incidents()
    outputs_per_job, outputs_flat, out_fresh = load_outputs()
    log_files = load_log_files()
    warnings = load_warnings(names)

    runs = attach_outputs(_merge_runs(ring, rollup), outputs_flat)
    for run in runs:
        run["job_name"] = names.get(run.get("job_id"))
        started = parse_ts(run.get("started_at"))
        finished = parse_ts(run.get("finished_at"))
        run["duration_s"] = (
            round((finished - started).total_seconds(), 2) if started and finished else None
        )

    incidents = []
    for inc in incidents_raw:
        row = dict(inc)
        row["job_name"] = names.get(inc.get("job_id"))
        row["error"] = (inc.get("error") or "")[:2000]
        incidents.append(row)
    open_incidents = [
        i for i in incidents if (i.get("state") or "").lower() not in ("resolved", "closed")
    ]

    outputs_by_job: dict[str, list[dict]] = {}
    for out in outputs_per_job:
        outputs_by_job.setdefault(out["job_id"], []).append(out)
    for job in jobs:
        job["outputs"] = outputs_by_job.get(job["id"], [])
        job_runs = [r for r in runs if r.get("job_id") == job["id"]]
        job["recent_runs"] = job_runs[:12]
        job["runs_seen"] = len(job_runs)
        job["last_run"] = job_runs[0] if job_runs else None

    # gateway / scheduler liveness
    gateway: dict = {}
    try:
        state = json.loads(GATEWAY_STATE.read_text())
        platforms = {
            name: (p or {}).get("state") for name, p in (state.get("platforms") or {}).items()
        }
        gateway = {
            "pid": state.get("pid"),
            "state": state.get("gateway_state"),
            "version": state.get("code_version"),
            "platforms": platforms,
            "platforms_bad": [n for n, s in platforms.items() if s != "connected"],
            "served_profiles": state.get("served_profiles") or [],
            "updated_at": state.get("updated_at"),
            "updated_age_s": age_seconds(parse_ts(state.get("updated_at")), ref),
        }
    except FileNotFoundError:
        gateway = {"error": "gateway_state.json not found"}
    except (OSError, json.JSONDecodeError) as exc:
        gateway = {"error": str(exc)}
    hb = _file_freshness(GATEWAY_HEARTBEAT, ref)
    gateway["heartbeat_age_s"] = hb.get("age_s")
    gateway["heartbeat_present"] = hb.get("present")

    ticker = _file_freshness(TICKER_HEARTBEAT, ref)
    ticker_success = _file_freshness(TICKER_SUCCESS, ref)

    upcoming = sorted(
        (j for j in jobs if j["enabled"] and j["next_run_at"]),
        key=lambda j: j["next_in_s"] if j["next_in_s"] is not None else 1e12,
    )[:12]

    counts = {
        "jobs": len(jobs),
        "enabled": sum(1 for j in jobs if j["enabled"]),
        "paused": sum(1 for j in jobs if j["paused"] or not j["enabled"]),
        "failing": sum(1 for j in jobs if j["failing"]),
        "overdue": sum(1 for j in jobs if j["overdue"]),
        "open_incidents": len(open_incidents),
        "runs_seen": len(runs),
        "outputs_seen": len(outputs_flat),
        "warnings": len(warnings),
        "warnings_error_or_worse": sum(1 for w in warnings if w["level"] in ("ERROR", "CRITICAL")),
    }

    # profile cron dirs are listed honestly even when empty
    profiles = []
    for profile_dir in sorted((HERMES_HOME / "profiles").glob("*")) if (HERMES_HOME / "profiles").is_dir() else []:
        cron = profile_dir / "cron"
        if not cron.is_dir():
            continue
        n_exec = None
        try:
            conn = sqlite3.connect(f"file:{cron / 'executions.db'}?mode=ro", uri=True, timeout=1.0)
            try:
                n_exec = conn.execute("select count(*) from executions").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            n_exec = None
        profiles.append({"name": profile_dir.name, "executions": n_exec})

    not_connected = []
    if not OUTPUT_ROOT.is_dir():
        not_connected.append("cron/output folder not found — job outputs cannot be linked")

    return {
        "host_id": "dockervm",
        "kind": "hermes",
        "label": "dockerVM — Hermes",
        "as_of": iso(ref),
        "hostname": os.uname().nodename,
        "gateway": gateway,
        "ticker": {
            "heartbeat": ticker,
            "last_success": ticker_success,
            "healthy": (ticker.get("age_s") is not None and ticker["age_s"] < 180),
        },
        "counts": counts,
        "jobs": jobs,
        "upcoming": upcoming,
        "runs": runs,
        "incidents": incidents,
        "open_incidents": open_incidents,
        "outputs": outputs_flat[:60],
        "logs": log_files,
        "warnings": warnings,
        "profiles": profiles,
        "sources": {
            "jobs": jobs_fresh,
            "executions": exec_fresh,
            "incidents": inc_fresh,
            "outputs": out_fresh,
            "rollup": _file_freshness(ROLLUP_FILE, ref),
        },
        "not_connected": not_connected,
    }


# ---------------------------------------------------------------- Mac Studio document
def collect_macstudio(ref: datetime | None = None) -> dict:
    ref = ref or now_utc()
    fresh = _file_freshness(MAC_SNAPSHOT, ref)
    base = {
        "host_id": "macstudio",
        "kind": "snapshot",
        "label": "Mac Studio — Hermes",
        "as_of": None,
        "file": fresh,
        "counts": {},
        "not_connected": [],
    }
    try:
        doc = json.loads(MAC_SNAPSHOT.read_text())
    except FileNotFoundError:
        base["state"] = MISSING
        base["error"] = "producer document not found"
        base["not_connected"].append(
            "the Mac's producer never wrote its snapshot; nothing on that node is visible"
        )
        return base
    except (OSError, json.JSONDecodeError) as exc:
        base["state"] = "unreadable"
        base["error"] = str(exc)
        base["not_connected"].append("the producer document could not be parsed")
        return base

    age = fresh.get("age_s")
    if age is None:
        state = MISSING
    elif age > 7200:
        state = DEAD
    elif age > 2700:
        state = STALE
    else:
        state = OK

    cron = doc.get("hermes_cron") or {}
    alerts = doc.get("alerts") or []
    gateway = doc.get("gateway") or {}

    base.update(
        state=state,
        as_of=doc.get("generated_at_utc"),
        generated_at=doc.get("generated_at_utc"),
        produced_by=doc.get("producer"),
        interval_minutes=doc.get("interval_minutes"),
        host=doc.get("host") or {},
        hostname=doc.get("hostname"),
        address=doc.get("address"),
        cron={
            "jobs_total": cron.get("jobs_total"),
            "jobs_enabled": cron.get("jobs_enabled"),
            "jobs_paused": cron.get("jobs_paused"),
            "failing": cron.get("failing") or [],
            "overdue": cron.get("overdue") or [],
            "oldest_last_run_hours": cron.get("oldest_last_run_hours"),
            "oldest_overdue_hours": cron.get("oldest_overdue_hours"),
            "max_age_period_ratio": cron.get("max_age_period_ratio"),
            "unreadable": cron.get("unreadable"),
        },
        gateway={
            "present": gateway.get("present"),
            "age_seconds": gateway.get("age_seconds"),
            "pid": gateway.get("pid"),
        },
        alerts=alerts,
        time_machine=doc.get("time_machine") or {},
        mounts=doc.get("mounts") or [],
        disks=doc.get("disks") or [],
        services=doc.get("services") or [],
    )
    base["counts"] = {
        "jobs": cron.get("jobs_total"),
        "enabled": cron.get("jobs_enabled"),
        "paused": cron.get("jobs_paused"),
        "failing": len(cron.get("failing") or []),
        "overdue": len(cron.get("overdue") or []),
        "alerts": len(alerts),
        "open_incidents": len([a for a in alerts if (a.get("severity") or "").upper() in ("CRITICAL", "ERROR")]),
    }
    base["not_connected"] = [
        "per-job names, run history and job output files live in /Users/ron/.hermes on the Mac"
        " and are not reachable from dockerVM (no SSH key, no share of that folder)",
        "log file links are not available for this node for the same reason",
    ]
    return base


def collect(host: str, ref: datetime | None = None) -> dict:
    if host == "dockervm":
        return collect_dockervm(ref)
    if host == "macstudio":
        return collect_macstudio(ref)
    raise ValueError(f"unknown host: {host}")


def collect_all(ref: datetime | None = None) -> dict:
    ref = ref or now_utc()
    return {
        "generated_at": iso(ref),
        "hosts": {
            "dockervm": collect_dockervm(ref),
            "macstudio": collect_macstudio(ref),
        },
    }
