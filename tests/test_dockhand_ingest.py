"""Ops Brain - tests for the Dockhand ingest module (pure functions; no live
DB, no live docker — temp SQLite files only)."""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "common"))

from common import Cfg  # noqa: E402
import collector.dockhand_ingest as D  # noqa: E402


SCHEMA = """
CREATE TABLE environments (
    id INTEGER PRIMARY KEY, name TEXT, host TEXT, port INTEGER, protocol TEXT,
    connection_type TEXT, socket_path TEXT);
CREATE TABLE stack_sources (
    id INTEGER PRIMARY KEY, stack_name TEXT, environment_id INTEGER,
    source_type TEXT, compose_path TEXT, env_path TEXT,
    git_repository_id INTEGER, git_stack_id INTEGER);
CREATE TABLE container_events (
    id INTEGER PRIMARY KEY, environment_id INTEGER, container_id TEXT,
    container_name TEXT, image TEXT, action TEXT, actor_attributes TEXT,
    timestamp TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE git_repositories (
    id INTEGER PRIMARY KEY, name TEXT, url TEXT, branch TEXT, compose_path TEXT,
    environment_id INTEGER, last_sync TEXT, last_commit TEXT, sync_status TEXT,
    sync_error TEXT, auto_update INTEGER);
CREATE TABLE git_stacks (
    id INTEGER PRIMARY KEY, stack_name TEXT, environment_id INTEGER,
    repository_id INTEGER, compose_path TEXT, last_sync TEXT, last_commit TEXT,
    sync_status TEXT, sync_error TEXT, auto_update INTEGER);
"""


def _seed_db(db_path, stack_rows=None, event_rows=None, settings_rows=None, with_git=True):
    """Create a tiny dockhand-shaped SQLite DB and return its connection-less file."""
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    con.execute(
        "INSERT INTO environments (id, name, host, connection_type, socket_path) VALUES"
        " (1, 'DockerVM', NULL, 'socket', '/var/run/docker.sock'),"
        " (3, 'Truenas-Main', '10.1.10.6', 'direct', '/var/run/docker.sock')")
    for i, s in enumerate(stack_rows or [], start=1):
        con.execute(
            "INSERT INTO stack_sources (id, stack_name, environment_id, source_type,"
            " compose_path, env_path, git_repository_id, git_stack_id) VALUES (?,?,?,?,?,?,?,?)",
            (i, s["stack_name"], s.get("environment_id", 1), s.get("source_type", "internal"),
             s["compose_path"], s.get("env_path", ""), s.get("git_repository_id"),
             s.get("git_stack_id")))
    for i, e in enumerate(event_rows or [], start=1):
        con.execute(
            "INSERT INTO container_events (id, environment_id, container_id, container_name,"
            " image, action, actor_attributes, timestamp) VALUES (?,?,?,?,?,?,?,?)",
            (i, e.get("environment_id", 1), e.get("container_id", "c"),
             e["container_name"], e.get("image", "img"), e["action"],
             e.get("actor_attributes", "{}"), e["timestamp"]))
    for k, v in (settings_rows or []) + [
            ("environment_public_ips", '{"1":"10.1.10.10","3":"10.1.10.6"}'),
            ("env_1_update_check", '{"enabled":true,"cron":"0 4 * * *","autoUpdate":true}')]:
        con.execute("INSERT INTO settings (key, value) VALUES (?,?)", (k, v))
    if with_git:
        con.execute(
            "INSERT INTO git_repositories (id, name, url, branch, environment_id,"
            " sync_status, sync_error, last_commit) VALUES"
            " (1, 'infra', 'https://example.com/infra.git', 'main', 1,"
            " 'ok', NULL, 'abc123')")
    con.commit()
    con.close()
    return db_path


def _make_stack(name, services, compose_path=None, env_path=None, environment_id=1):
    return {"stack": name, "environment_id": environment_id, "services": services,
            "compose_path": compose_path, "env_path": env_path,
            "source_type": "internal", "compose_ready": True, "compose_parse_error": None}


def _svc(name, image=None, restart=None, depends_on=None, ports=None, volumes=None,
         networks=None, env=None, labels=None, replicas=1):
    return {"service": name, "image": image, "restart": restart,
            "depends_on": depends_on or [], "ports": ports or [],
            "volumes": volumes or [], "networks": networks or [],
            "env": env or [], "labels": labels or {}, "replicas": replicas, "create": True}


def _container(name, state="running", image=None, status=None, restarting=False,
               restart_count=0, mounts=None, networks=None, labels=""):
    return {"name": name, "state": state, "image": image,
            "status": status or (f"Up {name}" if state == "running" else state),
            "restarting": restarting, "restart_count": restart_count,
            "labels": labels, "mounts": mounts, "networks": networks}


# --------------------------------------------------------------------------- pull_dockhand_state


def test_pull_ok_skeleton(tmp_path):
    db = _seed_db(tmp_path / "dockhand.db", stack_rows=[
        {"stack_name": "adminer", "compose_path": "/app/data/stacks/adminer/compose.yml",
         "env_path": "/app/data/stacks/adminer/.env"},
        {"stack_name": "plex", "compose_path": "/app/data/stacks/plex/docker-compose.yml"},
        {"stack_name": "truenas-stack", "environment_id": 3,
         "compose_path": "/custom/path/compose.yml"},
    ], event_rows=[
        {"container_name": "yamtrack", "image": "yamtrack:local", "action": "start",
         "timestamp": "2026-08-29T14:00:00Z", "id": 1},
        {"container_name": "yamtrack", "image": "yamtrack:local", "action": "die",
         "timestamp": "2026-08-29T14:05:00Z"},
        {"container_name": "yamtrack", "image": "yamtrack:local", "action": "start",
         "timestamp": "2026-08-29T14:06:00Z"},
    ])
    out = D.pull_dockhand_state(str(db), "/appdata/A--docker_stacks")
    assert out["enabled"] is True and out["up"] is True
    assert out["error"] is None
    assert out["environments"][0] == {"id": 1, "name": "DockerVM", "host": None,
                                      "connection_type": "socket", "socket_path": "/var/run/docker.sock"}
    # container prefix mapped to the host compose_root; non-prefixed path left as-is
    assert out["stack_sources"][0]["compose_path"] == "/appdata/A--docker_stacks/adminer/compose.yml"
    assert out["stack_sources"][0]["env_path"] == "/appdata/A--docker_stacks/adminer/.env"
    assert out["stack_sources"][1]["compose_path"] == "/appdata/A--docker_stacks/plex/docker-compose.yml"
    assert out["stack_sources"][2]["compose_path"] == "/custom/path/compose.yml"
    # events come back chronological (oldest first)
    ts = [e["timestamp"] for e in out["container_events_recent"]]
    assert ts == sorted(ts) and len(ts) == 3
    # settings curated subset
    assert out["settings_summary"]["environment_public_ips"]["1"] == "10.1.10.10"
    assert out["settings_summary"]["update_check"]["1"]["enabled"] is True
    assert out["settings_summary"]["update_check_count"] == 1
    # git sources surfaced
    assert out["git_sources"][0]["repo_or_stack"] == "repo"
    assert out["git_sources"][0]["name"] == "infra"
    assert out["git_sources"][0]["last_commit"] == "abc123"


def test_pull_missing_db(tmp_path):
    out = D.pull_dockhand_state(str(tmp_path / "no_such.db"), "/appdata/A--docker_stacks")
    assert out["enabled"] is True and out["up"] is False
    assert out["err"]


def test_pull_missing_table(tmp_path):
    db = tmp_path / "partial.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE environments (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    out = D.pull_dockhand_state(str(db), "/appdata/A--docker_stacks")
    assert out["up"] is False
    assert "missing tables" in out["err"]


# --------------------------------------------------------------------------- normalize


COMPOSE_YAML = """
services:
  web:
    image: nginx:1.25
    restart: unless-stopped
    depends_on:
      - db
    ports:
      - "8080:80"
    volumes:
      - ./html:/usr/share/nginx/html
      - webdata:/var/lib/data
    networks:
      - front
      - back
    environment:
      FOO: bar
    labels:
      app: demo
  db:
    image: postgres:15
    restart: no
"""
def test_normalize_groups_by_stack_and_parse_compose(tmp_path):
    stack_root = tmp_path / "stacks" / "webapp"
    stack_root.mkdir(parents=True)
    (stack_root / "compose.yml").write_text(COMPOSE_YAML)
    # a duplicate row for the same stack plus an env-3 row that must be filtered out
    docked = {"stack_sources": [
        {"stack_name": "webapp", "environment_id": 1, "source_type": "internal",
         "compose_path": str(stack_root / "compose.yml"),
         "env_path": str(stack_root / ".env"),
         "git_repository_id": None, "git_stack_id": None},
        {"stack_name": "webapp", "environment_id": 1, "source_type": "git",
         "compose_path": "", "env_path": "",
         "git_repository_id": 7, "git_stack_id": None},
        {"stack_name": "remote", "environment_id": 3, "source_type": "internal",
         "compose_path": "/app/data/stacks/remote/compose.yml",
         "env_path": "", "git_repository_id": None, "git_stack_id": None},
    ]}
    norm = D.normalize(docked)
    names = [s["stack"] for s in norm["desired_state"]]
    assert names == ["webapp"]  # env filter kept only environment 1, grouped by name
    assert list(norm["by_stack"].keys()) == ["webapp"]
    stack = norm["desired_state"][0]
    assert stack["compose_path"] == str(stack_root / "compose.yml")  # already host-mapped
    assert stack["compose_ready"] is True
    assert stack["compose_parse_error"] is None
    services = {s["service"]: s for s in stack["services"]}
    assert set(services) == {"web", "db"}
    web = services["web"]
    assert web["image"] == "nginx:1.25"
    assert web["restart"] == "unless-stopped"
    assert web["depends_on"] == ["db"]
    assert web["ports"] == ["80"]
    assert web["volumes"] == ["/usr/share/nginx/html", "/var/lib/data"]
    assert web["networks"] == ["front", "back"]
    assert web["env"] == ["FOO=bar"]
    assert web["labels"] == {"app": "demo"}
    assert services["db"]["restart"] == "no"


def test_normalize_container_path_mapped_and_unreadable(tmp_path):
    # a container-side path is mapped to the fixed host compose_root; with no
    # file there and an unreadable file we get compose_ready False, no raise
    missing = {"stack_sources": [
        {"stack_name": "ghost", "environment_id": 1, "source_type": "internal",
         "compose_path": "/app/data/stacks/ghost/compose.yml",
         "env_path": "/app/data/stacks/ghost/.env",
         "git_repository_id": None, "git_stack_id": None}]}
    norm = D.normalize(missing)
    stack = norm["desired_state"][0]
    assert stack["compose_path"] == "/appdata/A--docker_stacks/ghost/compose.yml"
    assert stack["env_path"] == "/appdata/A--docker_stacks/ghost/.env"
    assert stack["compose_ready"] is False
    assert stack["services"] == []

    bad = tmp_path / "badstack"
    bad.mkdir()
    (bad / "compose.yml").write_text("services: [unclosed")
    docked = {"stack_sources": [
        {"stack_name": "bad", "environment_id": 1, "source_type": "internal",
         "compose_path": str(bad / "compose.yml"), "env_path": "",
         "git_repository_id": None, "git_stack_id": None}]}
    stack = D.normalize(docked)["desired_state"][0]
    assert stack["compose_ready"] is False
    assert stack["compose_parse_error"]
    assert stack["services"] == []


# --------------------------------------------------------------------------- merge


def test_merge_match_by_exact_and_prefix():
    desired = [
        _make_stack("yamtrack", [_svc("yamtrack", image="yamtrack:local")]),
        _make_stack("ollama", [_svc("open-webui", image="ghcr.io/open-webui/open-webui:latest")]),
    ]
    containers = [
        _container("ollama_open-webui-1", image="ghcr.io/open-webui/open-webui:latest"),
        _container("yamtrack", image="yamtrack:local"),
    ]
    merged = D.merge_with_docker_actual(desired, containers)
    assert all(m["matched"] for m in merged)
    assert merged[0]["actual"]["name"] == "yamtrack"
    assert merged[1]["actual"]["name"] == "ollama_open-webui-1"


def test_merge_prefix_preferred_over_contains():
    desired = [_make_stack("adminer", [_svc("adminer", image="adminer:latest")])]
    containers = [
        _container("other-adminer-tool", image="x"),   # contains similarity but later
        _container("adminer_web_1", image="adminer:latest"),
    ]
    merged = D.merge_with_docker_actual(desired, containers)
    assert merged[0]["matched"] is True
    assert merged[0]["actual"]["name"] == "adminer_web_1"


def test_merge_no_match():
    desired = [_make_stack("solo", [_svc("ghost", image="ghost:3")])]
    merged = D.merge_with_docker_actual(desired, [_container("other", image="other")])
    assert merged[0]["matched"] is False
    assert merged[0]["actual"] is None
    assert merged[0]["image_mismatch"] is False
    assert merged[0]["missing_volume"] == []
    assert merged[0]["missing_network"] == []


def test_merge_health_and_resources():
    desired = [_make_stack("web", [_svc("web", image="nginx:1.25",
                                        volumes=["/data", "/missing-data"],
                                        networks=["front"])])]
    containers = [_container("web", image="nginx:1.25", status="Up 2 min (unhealthy)",
                             mounts=[{"Destination": "/data"}],
                             networks={"front": {}, "web_front2": {}})]
    m = D.merge_with_docker_actual(desired, containers)[0]
    assert m["actual"]["healthcheck"] is True
    assert m["actual"]["health_status"] == "unhealthy"
    assert m["actual"]["running"] is True
    assert m["missing_volume"] == ["/missing-data"]
    assert m["missing_network"] == []   # "front" satisfied via suffix/external name


# --------------------------------------------------------------------------- classify


def _classify(merged, containers=None):
    return D.classify_drift(merged, {"containers": containers or []}, {})


def test_classify_state_drift_missing_service():
    desired = [_make_stack("yamtrack", [_svc("yamtrack", image="yamtrack:local")])]
    merged = D.merge_with_docker_actual(desired, [])
    c = _classify(merged)
    assert c["state_drift"] is True
    assert c["state_drift_items"][0]["service"] == "yamtrack"
    assert c["state_drift_items"][0]["cause"] == "missing container"
    assert c["drift_count"] == 1


def test_classify_health_drift():
    desired = [_make_stack("web", [_svc("web", image="nginx")])]
    merged = D.merge_with_docker_actual(desired, [_container("web", status="Up 1m (unhealthy)")])
    c = _classify(merged)
    assert c["health_drift"] is True
    assert c["health_drift_items"][0]["cause"] == "unhealthy"
    assert c["state_drift"] is False


def test_classify_image_drift():
    desired = [_make_stack("web", [_svc("web", image="nginx:1.25")])]
    merged = D.merge_with_docker_actual(desired, [_container("web", image="nginx:1.26")])
    c = _classify(merged)
    assert c["image_drift"] is True
    assert c["image_drift_items"][0]["expected"] == "nginx:1.25"
    assert c["compose_drift"] is True
    assert c["state_drift"] is False


def test_classify_dependency_and_replicas():
    # api depends on "cache" which has NO container (dependency failure);
    # "scale" requests replicas=3 but only 1 container exists (replica drift);
    # "db" IS matched+running so api must not be blamed for depending on it.
    desired = [_make_stack("app", [
        _svc("api", image="api:1", depends_on=["cache"]),
        _svc("db", image="db:1"),
        _svc("scale", image="scale:1", replicas=3),
    ])]
    merged = D.merge_with_docker_actual(desired, [_container("app_db_1", image="db:1")])
    c = _classify(merged)
    # api matched nothing (correct: only app_db_1 exists, which is db) -> its dep
    # on cache fails because cache has no container in the stack
    assert any(i["cause"].startswith("depends_on cache") for i in c["dependency_drift_items"])
    # replicas 3 requested, only 1 container of image "scale:1" present
    assert c["replica_drift"] is True
    assert c["replica_drift_items"][0]["cause"].startswith("replicas 3")
    # api did NOT falsely match the db container (service-scoped matching)
    api = next(m for m in merged if m["service"] == "api")
    assert api["matched"] is False


# --------------------------------------------------------------------------- correlate


EV_BASE = "2026-08-29T"

def _ev(container, action, minute, hour=15):
    return {"container_name": container, "image": "img", "action": action,
            "timestamp": f"{EV_BASE}{hour:02d}:{minute:02d}:00Z"}


def test_correlate_restart_storm():
    netdata = {"alarms_count": 1, "alarms_active": [
        {"name": "web_restart_cpu", "status": "CRITICAL", "component": "web"}]}
    events = [_ev("web", "die", m) for m in (5, 6, 7)]
    out = D.correlate(events, netdata, {})
    assert out["recent_events_count"] == 3
    storm = out["restart_storms"][0]
    assert storm["service"] == "web"
    assert storm["count"] == 3
    assert storm["window_min"] == 30
    assert "die" in storm["sample"]
    assert out["netdata_relation"]["critical_alarm_matches_storm"] is True
    assert out["netdata_relation"]["matched_alarms"] == ["web_restart_cpu"]


def test_correlate_below_threshold_and_outside_window():
    events = [_ev("web", "die", m) for m in (5, 6)]          # only 2 in window
    events += [_ev("old", "die", 0, hour=8) for _ in range(3)]  # outside 30min window
    out = D.correlate(events, {"alarms_count": 0, "alarms_active": []}, {})
    assert out["restart_storms"] == []
    assert out["netdata_relation"]["critical_alarm_matches_storm"] is False


def test_correlate_health_flap():
    events = [
        _ev("db", "health_status: healthy", 1),
        _ev("db", "health_status: unhealthy", 2),
        _ev("db", "health_status: healthy", 3),
        _ev("db", "health_status: unhealthy", 4),
    ]
    out = D.correlate(events, {}, {})
    flap = out["health_flaps"][0]
    assert flap["service"] == "db"
    assert flap["toggle_count"] == 3
    assert flap["last_action"] == "unhealthy"
    # one toggle is below the flap threshold
    two = D.correlate(events[:2], {}, {})
    assert two["health_flaps"] == []


# --------------------------------------------------------------------------- context nodes / dashboard


def _drifty_classified():
    desired = [_make_stack("web", [
        _svc("web", image="nginx:1.25"),
        _svc("db", image="db:1", depends_on=["cache"]),
    ])]
    merged = D.merge_with_docker_actual(
        desired, [_container("web", image="nginx:1.26", status="Up 1m (unhealthy)")])
    return merged, D.classify_drift(merged, {"containers": []}, {})


def test_context_nodes_for_failing_items():
    merged, classified = _drifty_classified()
    correlated = {"restart_storms": [{"service": "web", "count": 4, "window_min": 30}],
                  "health_flaps": []}
    ctx = D.create_context_nodes(merged, classified, correlated)
    assert ctx["attention"] == "high"
    assert "drifting" in ctx["drift_summary"]
    assert "web" in ctx["drift_summary"]
    services = {n["service"] for n in ctx["nodes"]}
    assert "web" in services and "db" in services
    node = next(n for n in ctx["nodes"] if n["service"] == "web")
    assert node["evidence"] in ("not running", "unhealthy", "image mismatch")

def test_context_nodes_cap():
    merged, classified = _drifty_classified()
    # inflate state items to exceed the node cap
    for i in range(30):
        classified["state_drift_items"].append({"stack": f"s{i}", "service": f"x{i}",
                                                "cause": "missing container"})
    ctx = D.create_context_nodes(merged, classified, {}, node_cap=20)
    assert len(ctx["nodes"]) <= 20


def _drifty_classified_with_resources():
    """Like _drifty_classified but with a volume that is missing from the
    matched container, so volume_drift/missing_resources are non-empty."""
    desired = [_make_stack("web", [
        _svc("web", image="nginx:1.25", volumes=["/present", "/gone"]),
        _svc("db", image="db:1", depends_on=["cache"]),
    ])]
    merged = D.merge_with_docker_actual(
        desired, [_container("web", image="nginx:1.26", status="Up 1m (unhealthy)",
                             mounts=[{"Destination": "/present"}],
                             networks={"front": {}})])
    return merged, D.classify_drift(merged, {"containers": []}, {})


def test_update_dashboard_snapshot_lists():
    merged, classified = _drifty_classified_with_resources()
    correlated = {"restart_storms": [{"service": "web", "count": 4, "window_min": 30}]}
    docker_doc = {"containers": [_container("web"), _container("orphan_extra")]}
    dash = D.update_dashboard_snapshot(docked={"up": True}, merged=merged,
                                       classified=classified, correlated=correlated,
                                       docker_doc=docker_doc)
    assert dash["dockhand_up"] is True
    assert dash["stack_drift"] == ["web"]
    assert dash["service_drift"] == ["db", "web"]
    assert dash["dependency_failures"]
    assert dash["image_mismatch"][0]["service"] == "web"
    assert dash["health_violations"][0]["service"] == "web"
    assert dash["restart_storms"] == correlated["restart_storms"]
    orphaned = dash["orphaned_containers"]
    assert "orphan_extra" in orphaned
    assert "web" not in orphaned
    assert dash["missing_resources"]  # volume "/gone" is present as a missing volume
    assert dash["drift_count"] == classified["drift_count"]
    assert dash["generated_at"]


# --------------------------------------------------------------------------- collect


def test_collect_dockhand_disabled(monkeypatch):
    monkeypatch.setattr(Cfg, "data", {"sources": {"dockhand": {"enabled": False}}})
    assert D.collect_dockhand(Cfg) == {"enabled": False}
