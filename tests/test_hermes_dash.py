"""Tests for the Hermes cron dashboard (collector, server, snapshot).

They run against a fixture Hermes tree built in tmp_path — no real install, no
network, no docker. The safety tests are the point: a wrong Host header, a
non-GET method, and a path pointing at a credential file must all be REFUSED,
and that is asserted, not assumed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_dash import collector as C
from hermes_dash import server as S

EXEC_DDL = """
CREATE TABLE cron_incidents (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, error_sig TEXT NOT NULL,
    state TEXT NOT NULL, failure_type TEXT NOT NULL DEFAULT 'unknown',
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, acked_at TEXT,
    closed_at TEXT, error TEXT NOT NULL, output_file TEXT, alerted_at TEXT
);
CREATE TABLE executions (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
    process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
    status TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT,
    finished_at TEXT, error TEXT, handoff_pending INTEGER NOT NULL DEFAULT 0,
    handoff_started_at REAL, delivery_outcome TEXT, scheduled_instant TEXT,
    progress_at TEXT
);
"""


def ts(minutes_from_now: float = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    ).isoformat(timespec="microseconds")


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    """A miniature /root/.hermes with one healthy, one failing, one overdue job."""
    home = tmp_path / "hermes"
    (home / "cron" / "output").mkdir(parents=True)
    (home / "logs").mkdir()
    (home / "state").mkdir()
    (home / "profiles" / "yamtrack" / "cron").mkdir(parents=True)

    jobs = {
        "jobs": [
            {
                "id": "aaaa11112222",
                "name": "healthy hourly",
                "enabled": True,
                "no_agent": True,
                "script": "ok.sh",
                "schedule": {"kind": "cron", "expr": "13 * * * *"},
                "schedule_display": "13 * * * *",
                "next_run_at": ts(30),
                "last_run_at": ts(-30),
                "last_status": "ok",
                "failure_streak": 0,
                "deliver": "local",
                "repeat": {"times": None, "completed": 41},
            },
            {
                "id": "bbbb33334444",
                "name": "failing minutely",
                "enabled": True,
                "no_agent": True,
                "schedule": {"kind": "interval", "minutes": 1},
                "schedule_display": "every 1m",
                "next_run_at": ts(1),
                "last_run_at": ts(-4),
                "last_status": "error",
                "last_error": "Script exited with code 1",
                "failure_streak": 3,
                "deliver": "bot-chat",
            },
            {
                "id": "cccc55556666",
                "name": "overdue weekly",
                "enabled": True,
                "no_agent": True,
                "schedule": {"kind": "cron", "expr": "0 3 * * 0"},
                "schedule_display": "every sunday 03:00",
                "next_run_at": ts(-600),  # ten hours late, past the 600 s grace
                "last_run_at": ts(-9000),
                "last_status": "ok",
                "failure_streak": 0,
            },
            {
                "id": "dddd77778888",
                "name": "paused reminder",
                "enabled": False,
                "paused_at": "2026-11-01T00:00:00+00:00",
                "paused_reason": "user asked",
                "schedule": {"kind": "once", "run_at": "2027-09-24T14:00:00+00:00"},
                "schedule_display": "once at 2027-09-24",
                "next_run_at": "2027-09-24T14:00:00+00:00",
                "last_status": None,
                "failure_streak": 0,
            },
        ],
        "updated_at": ts(),
    }
    (home / "cron" / "jobs.json").write_text(json.dumps(jobs))

    conn = sqlite3.connect(home / "cron" / "executions.db")
    conn.executescript(EXEC_DDL)
    conn.execute(
        "insert into executions (id, job_id, source, process_id, pid, status, claimed_at,"
        " started_at, finished_at, error, scheduled_instant) values (?,?,?,?,?,?,?,?,?,?,?)",
        ("e1", "aaaa11112222", "builtin", "p1", 1, "completed", ts(-30), ts(-30), ts(-29.9), None, ts(-30)),
    )
    conn.execute(
        "insert into executions (id, job_id, source, process_id, pid, status, claimed_at,"
        " started_at, finished_at, error, scheduled_instant) values (?,?,?,?,?,?,?,?,?,?,?)",
        ("e2", "bbbb33334444", "builtin", "p2", 2, "failed", ts(-4), ts(-4), ts(-3.98),
         "Script exited with code 1", ts(-4)),
    )
    conn.execute(
        "insert into executions (id, job_id, source, process_id, pid, status, claimed_at,"
        " started_at, finished_at, error, scheduled_instant) values (?,?,?,?,?,?,?,?,?,?,?)",
        ("e3", "cccc55556666", "direct", "p3", 3, "missed", ts(-600), None, None, None, ts(-600)),
    )
    conn.execute(
        "insert into cron_incidents (id, job_id, error_sig, state, failure_type, first_seen_at,"
        " last_seen_at, acked_at, closed_at, error) values (?,?,?,?,?,?,?,?,?,?)",
        ("i1", "bbbb33334444", "sig", "detected", "script", ts(-120), ts(-4), None, None,
         "Script exited with code 1\nstdout:\nboom"),
    )
    conn.execute(
        "insert into cron_incidents (id, job_id, error_sig, state, failure_type, first_seen_at,"
        " last_seen_at, acked_at, closed_at, error) values (?,?,?,?,?,?,?,?,?,?)",
        ("i2", "aaaa11112222", "sig2", "resolved", "timeout", ts(-9000), ts(-9000), None,
         ts(-8990), "old failure"),
    )
    conn.commit()
    conn.close()

    out_dir = home / "cron" / "output" / "aaaa11112222"
    out_dir.mkdir(parents=True)
    run_stamp = datetime.now(timezone.utc) - timedelta(minutes=30)
    (out_dir / (run_stamp.strftime("%Y-%m-%d_%H-%M-%S") + ".md")).write_text(
        "# Cron Job: healthy hourly\n\nall good\n"
    )

    (home / "logs" / "errors.log").write_text(
        "2026-11-25 10:00:00,000 WARNING plugin.adapter: Reconnection failed: 502\n"
        "2026-11-25 10:01:00,000 WARNING plugin.adapter: Reconnection failed: 502\n"
        "2026-11-25 10:02:00,000 ERROR cron.scheduler: [cron_bbbb33334444_20261125_100200] boom\n"
        "2026-11-25 10:03:00,000 INFO plugin.adapter: fine\n"
    )
    (home / "logs" / "gateway.log").write_text("2026-11-25 10:00:00,000 WARNING gw: slow\n")

    (home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 42,
                "gateway_state": "running",
                "code_version": "9.9.9",
                "platforms": {"telegram": {"state": "connected"},
                              "homeassistant": {"state": "reconnecting"}},
                "served_profiles": ["default", "yamtrack"],
                "updated_at": ts(),
            }
        )
    )
    (home / "state" / "gateway.heartbeat").write_text("42")
    (home / "cron" / "ticker_heartbeat").write_text("tick")
    (home / "cron" / "ticker_last_success").write_text("ok")
    (home / "profiles" / "yamtrack" / "cron" / "executions.db").write_bytes(b"")

    opsbrain = tmp_path / "opsbrain"
    (opsbrain / "logs").mkdir(parents=True)
    mac = {
        "schema": 2,
        "generated_at_utc": ts(-2),
        "producer": "hermes-mac cron mac-node-snapshot",
        "interval_minutes": 15,
        "hostname": "Mac-Studio.local",
        "address": "192.0.2.34",
        "host": {"model": "Mac17,14", "os": "27.0.1", "cpu_cores": 18, "uptime_hours": 5.0},
        "hermes_cron": {
            "jobs_total": 29,
            "jobs_enabled": 28,
            "jobs_paused": 1,
            "failing": [{"name": "patch watchdog", "last_error": "patch no longer applies"}],
            "overdue": [],
            "oldest_last_run_hours": 575.6,
            "oldest_overdue_hours": None,
            "max_age_period_ratio": 0.98,
        },
        "gateway": {"present": True, "age_seconds": 2.4, "pid": 92302},
        "alerts": [{"severity": "warning", "name": "time_machine"}],
        "time_machine": {"latest_backup_age_hours": 0.2, "mount_point_mounted": True},
        "mounts": [{"path": "/Volumes/Data", "present": True, "mounted": True, "used_percent": 82}],
        "services": [],
    }
    (opsbrain / "logs" / "macstudio_node.json").write_text(json.dumps(mac))

    state = opsbrain / "state"
    state.mkdir()
    monkeypatch.setattr(C, "HERMES_HOME", home)
    monkeypatch.setattr(C, "OPSBRAIN", opsbrain)
    monkeypatch.setattr(C, "STATE_DIR", state)
    monkeypatch.setattr(C, "ROLLUP_FILE", state / "hermes_runs.jsonl")
    monkeypatch.setattr(C, "MAC_SNAPSHOT", opsbrain / "logs" / "macstudio_node.json")
    monkeypatch.setattr(C, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(C, "EXEC_DB", home / "cron" / "executions.db")
    monkeypatch.setattr(C, "OUTPUT_ROOT", home / "cron" / "output")
    monkeypatch.setattr(C, "GATEWAY_STATE", home / "gateway_state.json")
    monkeypatch.setattr(C, "GATEWAY_HEARTBEAT", home / "state" / "gateway.heartbeat")
    monkeypatch.setattr(C, "TICKER_HEARTBEAT", home / "cron" / "ticker_heartbeat")
    monkeypatch.setattr(C, "TICKER_SUCCESS", home / "cron" / "ticker_last_success")
    monkeypatch.setattr(C, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(
        C, "ALLOWED_DIRS", [home / "cron" / "output", home / "logs", home / "state", opsbrain / "reports"]
    )
    return {"home": home, "opsbrain": opsbrain, "state": state}


# ---------------------------------------------------------------- collector
def test_counts_and_verdicts(tree):
    doc = C.collect_dockervm()
    counts = doc["counts"]
    assert counts["jobs"] == 4
    assert counts["enabled"] == 3
    assert counts["paused"] == 1
    assert counts["failing"] == 1, "only the error job counts as failing"
    assert counts["overdue"] == 1, "the weekly job 10 h late must be overdue"
    assert counts["open_incidents"] == 1, "a resolved incident is not open"
    failing = [j for j in doc["jobs"] if j["failing"]]
    assert failing[0]["name"] == "failing minutely"
    overdue = [j for j in doc["jobs"] if j["overdue"]]
    assert overdue[0]["name"] == "overdue weekly"
    # a paused job is never "overdue" even with a far-future next_run
    paused = [j for j in doc["jobs"] if not j["enabled"]][0]
    assert paused["overdue"] is False


def test_overdue_grace_scales_with_interval():
    fast = {"schedule": {"kind": "interval", "minutes": 1}}
    slow = {"schedule": {"kind": "cron", "expr": "0 3 * * 0"}}
    assert C.overdue_grace(fast) == 180
    assert C.overdue_grace(slow) == 600


def test_only_open_incidents_are_open(tree):
    doc = C.collect_dockervm()
    assert [i["id"] for i in doc["open_incidents"]] == ["i1"]
    assert len(doc["incidents"]) == 2
    assert doc["open_incidents"][0]["job_name"] == "failing minutely"


def test_runs_are_merged_and_output_attached(tree):
    doc = C.collect_dockervm()
    by_id = {r["id"]: r for r in doc["runs"]}
    assert set(by_id) == {"e1", "e2", "e3"}
    assert by_id["e1"]["output"]["name"].endswith(".md")
    assert by_id["e2"]["job_name"] == "failing minutely"
    assert by_id["e2"]["duration_s"] is not None
    assert by_id["e3"]["status"] == "missed"


def test_rollup_adds_history_and_dedupes(tree):
    ring, _ = C.load_executions()
    assert C.append_rollup(ring) == 3
    assert C.append_rollup(ring) == 0, "a second run must not duplicate rows"
    rollup = C.load_rollup()
    assert {r["id"] for r in rollup} == {"e1", "e2", "e3"}
    # the live DB wins on status: mark e1 failed in the DB, rollup says completed
    conn = sqlite3.connect(C.EXEC_DB)
    conn.execute("update executions set status='failed' where id='e1'")
    conn.commit()
    conn.close()
    doc = C.collect_dockervm()
    assert [r for r in doc["runs"] if r["id"] == "e1"][0]["status"] == "failed"


def test_warnings_are_grouped_by_signature(tree):
    doc = C.collect_dockervm()
    groups = {(w["level"], w["count"]) for w in doc["warnings"]}
    assert ("WARNING", 2) in groups, "two identical plugin warnings must group"
    assert any(w["level"] == "ERROR" for w in doc["warnings"])
    err = [w for w in doc["warnings"] if w["level"] == "ERROR"][0]
    assert err["job_id"] == "bbbb33334444", "the cron tag must be resolved to a job"


def test_sources_report_their_own_freshness(tree):
    doc = C.collect_dockervm()
    src = doc["sources"]
    assert src["jobs"]["present"] is True
    assert src["executions"]["rows"] == 3
    assert src["outputs"]["age_s"] is not None, "outputs age comes from the newest file"
    assert src["rollup"]["present"] is False


def test_gateway_and_ticker_surface_a_bad_platform(tree):
    doc = C.collect_dockervm()
    assert doc["gateway"]["platforms_bad"] == ["homeassistant"]
    assert doc["gateway"]["heartbeat_age_s"] is not None
    assert doc["ticker"]["healthy"] is True


# ---------------------------------------------------------------- mac node
def test_mac_snapshot_is_ok_and_names_failing_jobs(tree):
    doc = C.collect_macstudio()
    assert doc["state"] == C.OK
    assert doc["counts"]["jobs"] == 29
    assert doc["counts"]["failing"] == 1
    assert doc["cron"]["failing"][0]["name"] == "patch watchdog"
    assert doc["not_connected"], "the gap must be stated, not implied"


@pytest.mark.parametrize(
    "age_hours,expected",
    [(0.05, C.OK), (1.0, C.STALE), (3.0, C.DEAD)],
)
def test_mac_staleness_tiers(tree, age_hours, expected):
    path = C.MAC_SNAPSHOT
    doc = json.loads(path.read_text())
    doc["generated_at_utc"] = (
        datetime.now(timezone.utc) - timedelta(hours=age_hours)
    ).isoformat(timespec="seconds")
    path.write_text(json.dumps(doc))
    old = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).timestamp()
    os.utime(path, (old, old))
    assert C.collect_macstudio()["state"] == expected


def _schema3_mac():
    """The shape asked for on 2026-11-25 (message 20261125T141718Z-c02ab9c4)."""
    return {
        "schema": 3,
        "generated_at_utc": ts(-3),
        "producer": "hermes-mac cron mac-node-snapshot",
        "interval_minutes": 15,
        "hermes_cron": {
            "jobs_total": 2,
            "jobs_enabled": 2,
            "jobs_paused": 0,
            "failing": [],
            "overdue": [],
            "max_age_period_ratio": 0.5,
            "jobs": [
                {"id": "aaaa11112222", "name": "mac hourly", "enabled": True, "no_agent": True,
                 "schedule_display": "13 * * * *", "last_status": "ok", "last_run_at": ts(-30),
                 "next_run_at": ts(30), "failure_streak": 0, "last_error": None},
                {"id": "bbbb33334444", "name": "mac failing", "enabled": True, "no_agent": False,
                 "schedule_display": "every 1m", "last_status": "error", "last_run_at": ts(-4),
                 "next_run_at": ts(1), "failure_streak": 2, "last_error": "Script exited with code 1"},
            ],
            "runs": [
                {"job_id": "bbbb33334444", "status": "failed", "claimed_at": ts(-4),
                 "started_at": ts(-4), "finished_at": ts(-3.9), "source": "builtin",
                 "error": "Script exited with code 1"},
                {"job_id": "aaaa11112222", "status": "completed", "claimed_at": ts(-30),
                 "started_at": ts(-30), "finished_at": ts(-29.9), "source": "builtin", "error": None},
            ],
            "incidents": [
                {"job_id": "bbbb33334444", "state": "detected", "failure_type": "script",
                 "first_seen_at": ts(-120), "last_seen_at": ts(-4), "error": "boom"},
            ],
            "outputs": [
                {"job_id": "aaaa11112222", "name": "2026-11-25_13-00-00.md", "mtime": ts(-30),
                 "size": 812, "text": "# Cron Job: mac hourly\n\nall good\n"},
            ],
            "logs": [
                {"name": "agent.log", "path": "/Users/ron/.hermes/logs/agent.log", "size": 4096,
                 "mtime": ts(-1)},
            ],
        },
        "gateway": {"present": True, "age_seconds": 3.0, "pid": 1},
        "alerts": [],
        "host": {"model": "Mac17,14", "os": "27.0.1", "cpu_cores": 18, "uptime_hours": 5.0},
        "time_machine": {"latest_backup_age_hours": 0.2, "mount_point_mounted": True},
        "mounts": [],
        "disks": [],
    }


def test_mac_schema3_detail_is_carried_through(tree):
    C.MAC_SNAPSHOT.write_text(json.dumps(_schema3_mac()))
    doc = C.collect_macstudio()
    assert doc["detail_present"] is True
    assert doc["schema"] == 3
    assert [j["name"] for j in doc["jobs"]] == ["mac hourly", "mac failing"]
    assert len(doc["runs"]) == 2
    assert doc["incidents"][0]["state"] == "detected"
    assert doc["outputs"][0]["text"].startswith("# Cron Job")
    assert doc["logs"][0]["name"] == "agent.log"
    # the aggregate view must survive alongside the rows
    assert doc["counts"]["jobs"] == 2
    assert doc["cron"]["max_age_period_ratio"] == 0.5
    # only the log BYTES are still unreachable, not the job detail
    assert len(doc["not_connected"]) == 1
    assert "log file contents" in doc["not_connected"][0]


def test_mac_schema2_still_reports_the_gap(tree):
    doc = C.collect_macstudio()
    assert doc["detail_present"] is False
    assert "no SSH key" in doc["not_connected"][0]


def test_mac_detail_fields_are_capped(tree):
    doc = _schema3_mac()
    doc["hermes_cron"]["jobs"][0]["last_error"] = "x" * 5000
    doc["hermes_cron"]["outputs"][0]["text"] = "y" * 20000
    doc["hermes_cron"]["runs"] = [{"job_id": "a", "status": "ok"}] * 900
    C.MAC_SNAPSHOT.write_text(json.dumps(doc))
    out = C.collect_macstudio()
    assert len(out["jobs"][0]["last_error"]) == 600
    assert len(out["outputs"][0]["text"]) == 4000
    assert len(out["runs"]) == 500, "a runaway producer must not balloon the payload"


def test_mac_missing_file_is_not_health(tree, monkeypatch):
    monkeypatch.setattr(C, "MAC_SNAPSHOT", Path("/nonexistent/mac.json"))
    doc = C.collect_macstudio()
    assert doc["state"] == C.MISSING
    assert doc["counts"] == {}
    assert "never wrote" in doc["not_connected"][0]



# ---------------------------------------------------------------- ollama box
SSH_FIXTURE = """
@@UNIT
active
Mon 2026-11-23 17:04:28 UTC
@@VER
ollama version is 0.40.16
@@GPU
0, GPU-aaa, NVIDIA GeForce RTX 5070 Ti, 10566, 16303, 0, 39.50, 18.89
1, GPU-bbb, NVIDIA GeForce RTX 5070 Ti, 9930, 16303, 0, 41.42, 11.16
@@APPS
GPU-aaa, 185026, /usr/local/lib/ollama/llama-server, 10556
GPU-bbb, 185026, /usr/local/lib/ollama/llama-server, 9904
@@HOST
 14:40:57 up 1 day, 21:36,  1 user,  load average: 1.68, 3.21, 3.27
32
Mem:             121          10          93           0          19         110
@@LOG
[GIN] 2026/11/25 - 14:41:37 | 200 |   1.11ms |      10.1.10.10 | GET      "/api/tags"
[GIN] 2026/11/25 - 14:41:37 | 200 |   0.02ms |       127.0.0.1 | GET      "/api/version"
[GIN] 2026/11/25 - 14:40:00 | 500 |  10.00ms |      10.1.10.34 | POST     "/api/generate"
"""


def _ollama_http_stub(ok=True):
    def fake(path, payload=None):
        if not ok:
            return {"path": path, "ok": False, "error": "URLError: refused", "latency_ms": 1.0,
                    "size": 0, "data": None}
        data = {
            "/api/version": {"version": "0.40.16"},
            "/api/tags": {"models": [{"name": "qwen3.5:27b", "size": 17_500_000_000,
                                      "modified_at": ts(-216),
                                      "details": {"family": "qwen35", "parameter_size": "26.9B",
                                                  "quantization_level": "Q4_K_M"}}]},
            "/api/ps": {"models": [{"name": "qwen3.5:27b", "model": "qwen3.5:27b",
                                    "size": 19_944_366_404, "size_vram": 19_944_366_404,
                                    "context_length": 65536, "runner": "llamacpp",
                                    "expires_at": ts(13), "details": {"parameter_size": "26.9B"}}]},
            "/api/show": {"capabilities": ["completion", "vision", "tools", "thinking"],
                          "model_info": {"qwen35.context_length": 262144}},
        }[path]
        return {"path": path, "ok": True, "error": None, "latency_ms": 1.2, "size": 100, "data": data}
    return fake


def test_ollama_document_parses_the_box(monkeypatch):
    monkeypatch.setattr(C, "_http_json", _ollama_http_stub(True))
    monkeypatch.setattr(C, "_ssh_read", lambda script: {
        "ok": True, "error": None, "latency_ms": 465.0, "text": SSH_FIXTURE})
    doc = C.collect_ollamabox()
    assert doc["state"] == "ok"
    assert doc["service"]["active"] == "active"
    assert doc["service"]["version"] == "0.40.16"
    assert doc["service"]["cli_version"] == "0.40.16"
    assert len(doc["models"]) == 1 and doc["models"][0]["quantization_level"] == "Q4_K_M"
    loaded = doc["loaded"][0]
    assert loaded["context_length"] == 65536
    assert loaded["native_context"] == 262144
    assert "vision" in loaded["capabilities"]
    assert 0 < loaded["expires_in_s"] <= 13 * 60
    assert [g["index"] for g in doc["gpus"]] == [0, 1]
    assert doc["gpus"][0]["apps"][0]["process"].endswith("llama-server")
    assert doc["gpus"][1]["apps"][0]["mem_mib"] == 9904
    assert doc["host"]["cores"] == 32
    assert doc["host"]["load"] == [1.68, 3.21, 3.27]
    assert doc["host"]["mem_total_gb"] == 121
    assert doc["counts"]["vram_used_gib"] == 20.0
    assert doc["counts"]["vram_total_gib"] == 31.8
    assert doc["counts"]["loaded_vram_gib"] == 19.9


def test_ollama_log_hides_only_this_pages_own_polls(monkeypatch):
    monkeypatch.setattr(C, "_http_json", _ollama_http_stub(True))
    monkeypatch.setattr(C, "_ssh_read", lambda script: {
        "ok": True, "error": None, "latency_ms": 400.0, "text": SSH_FIXTURE})
    doc = C.collect_ollamabox()
    assert doc["log_hidden"] == 2, "the two 200s on /api/tags and /api/version are ours"
    assert any('"/api/generate"' in ln for ln in doc["log"]), "a real 500 must survive the filter"
    assert doc["counts"]["log_errors"] == 1


def test_ollama_unreachable_is_not_health(monkeypatch):
    monkeypatch.setattr(C, "_http_json", _ollama_http_stub(False))
    monkeypatch.setattr(C, "_ssh_read", lambda script: {
        "ok": False, "error": "ssh: connect to host 10.1.10.30 port 22: Connection timed out",
        "latency_ms": 10000.0, "text": ""})
    doc = C.collect_ollamabox()
    assert doc["state"] == "unreachable"
    assert doc["gpus"] == [] and doc["loaded"] == [] and doc["models"] == []
    assert doc["counts"]["gpus"] == 0
    assert "timed out" in (doc["sources"]["ssh_gpu_host"]["error"] or "")
    assert doc["service"]["http_error"]


def test_ollama_api_only_when_ssh_fails(monkeypatch):
    monkeypatch.setattr(C, "_http_json", _ollama_http_stub(True))
    monkeypatch.setattr(C, "_ssh_read", lambda script: {
        "ok": False, "error": "Permission denied (publickey)", "latency_ms": 90.0, "text": ""})
    doc = C.collect_ollamabox()
    assert doc["state"] == "api-only", "the model API answering while ssh fails is its own state"
    assert len(doc["loaded"]) == 1
    assert doc["gpus"] == []


def test_http_200_with_an_empty_body_is_an_error(tmp_path, monkeypatch):
    """Measured on that box before: a wedged process accepts TCP and answers nothing."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Empty(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Empty)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(C, "OLLAMA_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        out = C._http_json("/api/version")
        assert out["ok"] is False
        assert "empty body" in out["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_ssh_blocks_split_on_markers():
    blocks = C._ssh_blocks(SSH_FIXTURE)
    assert set(blocks) == {"UNIT", "VER", "GPU", "APPS", "HOST", "LOG"}
    assert blocks["UNIT"][0] == "active"
    assert len([ln for ln in blocks["GPU"] if ln.strip()]) == 2

# ---------------------------------------------------------------- serving rules
def test_servable_allows_job_output_and_logs(tree):
    out = next((tree["home"] / "cron" / "output" / "aaaa11112222").glob("*.md"))
    assert C.is_servable(out)[0] is True
    assert C.is_servable(tree["home"] / "logs" / "errors.log")[0] is True


@pytest.mark.parametrize(
    "name",
    [".env", "config.yaml", "auth.json", "state.db", "cron/jobs.json", "cron/executions.db"],
)
def test_servable_refuses_config_and_secrets(tree, name):
    target = tree["home"] / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=hunter2\n")
    ok, reason = C.is_servable(target)
    assert ok is False, f"{name} must never be served"
    assert reason in ("denied filename", "extension not served", "outside the served folders")


def test_servable_refuses_traversal_and_symlink_escape(tree):
    env = tree["home"] / ".env"
    env.write_text("SECRET\n")
    out = tree["home"] / "cron" / "output" / "aaaa11112222"
    sneaky = out / "escape.md"
    sneaky.symlink_to(env)
    assert C.is_servable(sneaky)[0] is False
    assert C.is_servable(f"{out}/../../../.env")[0] is False


def test_servable_refuses_html_snapshot_under_state(tree):
    snap = tree["state"] / "hermes-snapshots" / "latest.html"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text("<script>alert(1)</script>")
    assert C.is_servable(snap)[0] is False


def test_log_tail_is_truncated_with_a_note(tree, monkeypatch):
    log = tree["home"] / "logs" / "errors.log"
    log.write_text("x" * 5000)
    monkeypatch.setattr(C, "MAX_LOG_TAIL_BYTES", 1000)
    text, reason = C.read_servable(log)
    assert reason == "ok"
    assert text.startswith("[showing the last 1000 of 5000 bytes")
    assert len(text) < 1200


# ---------------------------------------------------------------- server
@pytest.fixture()
def http(tree):
    srv = S.build("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}", port
    srv.shutdown()
    srv.server_close()


def _get(url, host=None):
    req = urllib.request.Request(url)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def test_server_serves_page_and_state(http):
    base, _ = http
    code, body = _get(base + "/")
    assert code == 200 and "Hermes jobs" in body
    code, body = _get(base + "/api/state?host=dockervm")
    assert code == 200
    assert json.loads(body)["counts"]["jobs"] == 4


def test_server_refuses_foreign_host_header(http):
    base, _ = http
    code, body = _get(base + "/", host="evil.example")
    assert code == 421
    assert "Misdirected Request" in body
    code, _ = _get(base + "/api/state", host="10.1.10.10:9130")
    assert code == 421


def test_server_answers_get_only(http):
    base, _ = http
    req = urllib.request.Request(base + "/", method="POST", data=b"x")
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            code = res.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    assert code == 405


def test_server_refuses_secret_files_over_http(http, tree):
    base, _ = http
    env = tree["home"] / ".env"
    env.write_text("SECRET=hunter2\n")
    code, body = _get(base + "/api/file?path=" + str(env))
    assert code == 403 and "refused" in body
    assert "hunter2" not in body


def test_server_serves_an_allowed_output(http, tree):
    base, _ = http
    out = next((tree["home"] / "cron" / "output" / "aaaa11112222").glob("*.md"))
    code, body = _get(base + "/api/file?path=" + str(out))
    assert code == 200 and "healthy hourly" in body


def test_server_accepts_the_configured_public_host(http):
    """The caddy route proxies with Host: hermesjobs.home — that one must pass, all else 421."""
    base, _ = http
    code, body = _get(base + "/", host="hermesjobs.home")
    assert code == 200 and "Hermes jobs" in body
    assert _get(base + "/", host="hermesjobs.home:443")[0] == 200
    assert _get(base + "/api/state", host="hermesjobs.home")[0] == 200
    for foreign in ("jobs.home", "hermes.home", "hermesjobs.home.evil.com", "evil.example"):
        assert _get(base + "/", host=foreign)[0] == 421, foreign


def test_server_rejects_unknown_host_param(http):
    base, _ = http
    code, body = _get(base + "/api/state?host=elsewhere")
    assert code == 400 and "unknown host" in body


# ---------------------------------------------------------------- snapshot
def test_snapshot_is_self_contained(tree, monkeypatch, tmp_path):
    from hermes_dash import snapshot as snap

    monkeypatch.setattr(snap, "SNAP_DIR", tmp_path / "snaps")
    monkeypatch.setattr(snap, "STATIC", Path(C.__file__).resolve().parent / "static")
    rc = snap.main(["--quiet"])
    assert rc == 0
    latest = tmp_path / "snaps" / "latest.html"
    html = latest.read_text()
    assert "window.__HERMES_STATIC__ = true" in html
    assert "window.__STATIC_DATA__" in html
    assert "<style>" in html, "CSS must be inlined or the file:// copy renders unstyled"
    assert "/static/app.js" not in html, "a snapshot may not depend on the server"
    assert "failing minutely" in html
    assert (tree["state"] / "hermes_runs.jsonl").exists()
