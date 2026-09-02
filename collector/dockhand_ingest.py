"""
Ops Brain - Dockhand ingest module.

Reads Dockhand's Docker desired-state registry (SQLite at
/appdata/dockhand/sqlite/db/dockhand.db, direct read-only access — the HTTP API
is session/auth-gated and unusable), merges it against the docker container
state the collector already gathered, and produces drift classification,
event correlation, reasoner context nodes, and a dashboard snapshot.

The module is self-contained: every component function is PURE (data in, data
out) with explicit parameters, so they are unit-testable like
evaluate_gpu_drift. Only the orchestrator collect_dockhand(cfg) reads config /
the prior collector.json.

Degrades gracefully: a missing/unreadable Dockhand DB yields a
{"enabled":true,"up":false,"err":...} skeleton, never a raised exception.

Usage (standalone, same import pattern as collector.py):
    sys.path.insert(0, <parent>)
    from common import Cfg, read_json, write_json, now_iso
    from collector.dockhand_ingest import collect_dockhand
"""
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, read_json, write_json, now_iso  # noqa: E402

import yaml  # noqa: E402

# Dockhand's container-side stack prefix; on the host it maps to compose_root.
CONTAINER_STACK_PREFIX = "/app/data/stacks/"

# Actions that indicate a container was (re)started/destroyed, for storm counting.
STORM_ACTIONS = {"die", "destroy", "restart", "recreate"}


# --------------------------------------------------------------------------- path mapping


def host_map_path(path, compose_root):
    """Map a Dockhand container-side path to the host path.

    Replaces the leading ``/app/data/stacks/`` prefix (Dockhand's in-container
    mount point) with ``compose_root`` (the host's stack directory). Paths that
    do not start with the container prefix are returned unchanged, so the
    mapping is idempotent on already-mapped paths.
    """
    p = str(path or "")
    if p.startswith(CONTAINER_STACK_PREFIX):
        return str(Path(str(compose_root)) / p[len(CONTAINER_STACK_PREFIX):])
    return p


def _close(con):
    try:
        con.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- 1) DB pull


def pull_dockhand_state(db_path, compose_root="/appdata/A--docker_stacks", event_limit=400):
    """Read Dockhand's SQLite desired-state registry read-only.

    Returns the full docked document: environments, host-mapped stack_sources,
    the most recent container_events (chronological, last ``event_limit`` by
    id), a curated settings_summary, and git_sources.

    On open failure or a missing table this returns
    ``{"enabled": true, "up": false, "err": "..."}`` — never raises. If the DB
    opens but an individual section fails to map, that section is replaced with
    an empty value, ``up`` stays true, and ``error`` carries the message.
    """
    out = {"enabled": True, "up": False, "err": None}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception as e:
        out["err"] = f"open failed: {e}"
        return out

    _required = {"environments", "stack_sources", "container_events", "settings"}
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception as e:
        out["err"] = f"query failed: {e}"
        _close(con)
        return out
    missing = _required - tables
    if missing:
        out["err"] = "missing tables: " + ", ".join(sorted(missing))
        _close(con)
        return out

    out["up"] = True
    out["error"] = None
    errors = []
    con.row_factory = sqlite3.Row

    # environments
    try:
        rows = con.execute(
            "SELECT id, name, host, connection_type, socket_path FROM environments ORDER BY id"
        ).fetchall()
        out["environments"] = [dict(r) for r in rows]
    except Exception as e:
        errors.append(f"environments: {e}")
        out["environments"] = []

    # stack_sources (desired-state registry) with host-mapped compose/env paths
    try:
        rows = con.execute(
            "SELECT stack_name, environment_id, source_type, compose_path, env_path,"
            " git_repository_id, git_stack_id FROM stack_sources ORDER BY id"
        ).fetchall()
        out["stack_sources"] = [{
            "stack_name": r["stack_name"],
            "environment_id": r["environment_id"],
            "source_type": r["source_type"],
            "compose_path": host_map_path(r["compose_path"], compose_root),
            "env_path": host_map_path(r["env_path"], compose_root),
            "git_repository_id": r["git_repository_id"],
            "git_stack_id": r["git_stack_id"],
        } for r in rows]
    except Exception as e:
        errors.append(f"stack_sources: {e}")
        out["stack_sources"] = []

    # container_events: last N by id, returned in chronological order
    try:
        rows = con.execute(
            "SELECT container_name, image, action, timestamp FROM container_events"
            " ORDER BY id DESC LIMIT ?", (event_limit,)).fetchall()
        out["container_events_recent"] = [dict(r) for r in reversed(rows)]
    except Exception as e:
        errors.append(f"container_events: {e}")
        out["container_events_recent"] = []

    # settings: curated subset (environment_public_ips + per-env update_check)
    try:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
        out["settings_summary"] = _settings_summary(rows)
    except Exception as e:
        errors.append(f"settings: {e}")
        out["settings_summary"] = {}

    # git_sources: repositories + git stacks
    try:
        repos = con.execute(
            "SELECT name, environment_id, sync_status, sync_error, last_commit"
            " FROM git_repositories ORDER BY id").fetchall()
        stacks = con.execute(
            "SELECT stack_name, environment_id, sync_status, sync_error, last_commit"
            " FROM git_stacks ORDER BY id").fetchall()
        git = [{"repo_or_stack": "repo", "name": r["name"], "env": r["environment_id"],
                "sync_status": r["sync_status"], "sync_error": r["sync_error"],
                "last_commit": r["last_commit"]} for r in repos]
        git += [{"repo_or_stack": "stack", "name": s["stack_name"], "env": s["environment_id"],
                 "sync_status": s["sync_status"], "sync_error": s["sync_error"],
                 "last_commit": s["last_commit"]} for s in stacks]
        out["git_sources"] = git
    except Exception as e:
        errors.append(f"git_sources: {e}")
        out["git_sources"] = []

    _close(con)
    if errors:
        out["error"] = "; ".join(errors)
    return out


def _parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def _settings_summary(rows):
    """Curate settings rows -> {environment_public_ips, update_check(per env), count}."""
    ips = {}
    update_check = {}
    for r in rows:
        key = str(r["key"] or "")
        raw = r["value"]
        if key == "environment_public_ips":
            v = _parse_json(raw)
            if isinstance(v, dict):
                ips = {str(k): str(val) for k, val in v.items()}
        elif key.startswith("env_") and key.endswith("_update_check"):
            env_id = key[len("env_"):-len("_update_check")]
            v = _parse_json(raw)
            if isinstance(v, dict):
                update_check[env_id] = v
    return {"environment_public_ips": ips, "update_check": update_check,
            "update_check_count": len(update_check)}


# --------------------------------------------------------------------------- 2) normalize


def normalize(docked, environment_id=1, compose_root="/appdata/A--docker_stacks"):
    """Filter stack_sources into the desired-state model, grouped by stack_name.

    Returns ``{"desired_state": [...], "by_stack": {name: stack_doc}}``. Each
    stack doc carries host-mapped compose_path/env_path and best-effort parsed
    services from the compose file (compose_ready False + compose_parse_error
    when the file is unreadable/unparseable — never raises).
    """
    wanted = []
    for s in docked.get("stack_sources") or []:
        try:
            env = int(s.get("environment_id"))
        except Exception:
            env = None
        if env == int(environment_id):
            wanted.append(s)

    # group by stack_name, merging duplicate rows (first non-empty wins per field)
    by_stack = {}
    for s in wanted:
        name = s.get("stack_name")
        if not name:
            continue
        if name not in by_stack:
            by_stack[name] = dict(s)
        else:
            cur = by_stack[name]
            for k in ("compose_path", "env_path", "source_type",
                      "git_repository_id", "git_stack_id"):
                if cur.get(k) in (None, "") and s.get(k) not in (None, ""):
                    cur[k] = s[k]

    desired_state = []
    for name, s in by_stack.items():
        compose_path = host_map_path(s.get("compose_path"), compose_root)
        env_path = host_map_path(s.get("env_path"), compose_root)
        services, ready, perr = _parse_stack_compose(compose_path, env_path)
        desired_state.append({
            "stack": name,
            "environment_id": int(environment_id),
            "services": services,
            "compose_path": compose_path,
            "env_path": env_path,
            "source_type": s.get("source_type"),
            "compose_ready": ready,
            "compose_parse_error": perr,
        })
    return {"desired_state": desired_state, "by_stack": by_stack}


def _find_compose_file(compose_path):
    """Locate the host compose file: the path itself, then compose.yml/.yaml next to it."""
    p = Path(str(compose_path or ""))
    if p.is_file():
        return p
    for cand in (Path(p) / "compose.yml", Path(p) / "compose.yaml",
                 p.parent / "compose.yml", p.parent / "compose.yaml"):
        if cand.is_file():
            return cand
    return None


def _load_env_file(env_path):
    """Best-effort KEY=VALUE parse of a compose .env file (no shell expansion)."""
    env = {}
    if not env_path:
        return env
    try:
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    except Exception:
        pass
    return env


def _resolve_compose_env(text, env):
    """Resolve ${VAR} and ${VAR:-default} against a .env dict.

    Matches compose interpolation semantics for the common cases: an explicit
    .env value wins; ``:-default`` falls back when the key is absent. A bare
    ${VAR} with no .env value resolves to '' (as compose does). Leaves anything
    else untouched so unresolvable expressions surface visibly.
    """
    if not isinstance(text, str) or "{" not in text:
        return text
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

    def _sub(m):
        key, default = m.group(1), m.group(2)
        if key in env:
            return str(env[key])
        return default if default is not None else ""

    return pattern.sub(_sub, text)


def _parse_stack_compose(compose_path, env_path=None):
    """Best-effort compose parse -> (services, ready, parse_error). Never raises."""
    cf = _find_compose_file(compose_path)
    if cf is None:
        return [], False, f"compose file not found under {compose_path}"
    try:
        with open(cf) as fh:
            doc = yaml.safe_load(fh)
        if not isinstance(doc, dict):
            return [], False, "compose root is not a mapping"
        env = _load_env_file(env_path)
        return _compose_services(doc, env), True, None
    except Exception as e:
        return [], False, f"compose parse failed: {e}"


def _compose_services(doc, env=None):
    """Derive the service list from a parsed compose document."""
    env = env or {}
    serv = doc.get("services") or {}
    out = []
    if isinstance(serv, dict):
        for name, cfg in serv.items():
            if isinstance(cfg, dict):
                out.append(_service_doc(name, cfg, env))
    elif isinstance(serv, list):
        for i, item in enumerate(serv):
            if isinstance(item, dict):
                name = item.get("name") or str(i)
                out.append(_service_doc(name, item, env))
    return out


def _service_doc(name, cfg, env=None):
    """Derive one desired-service record from a compose service config."""
    # yaml.safe_load parses `restart: no` as the boolean False; normalize to "no".
    restart = cfg.get("restart")
    if restart is False:
        restart = "no"
    elif isinstance(restart, str) and restart.strip().lower() == "no":
        restart = "no"
    return {
        "service": name,
        "image": _resolve_compose_env(cfg.get("image"), env or {}),
        "restart": restart,
        "depends_on": _depends_on(cfg),
        "ports": _ports(cfg),
        "volumes": _volumes(cfg),
        "networks": _networks(cfg),
        "env": _env(cfg),
        "labels": _labels(cfg),
        "replicas": _replicas(cfg),
        "create": True,
    }


def _depends_on(cfg):
    d = cfg.get("depends_on")
    if isinstance(d, dict):
        return list(d.keys())
    if isinstance(d, list):
        out = []
        for x in d:
            if isinstance(x, dict):
                n = x.get("service")
                if n:
                    out.append(str(n))
            else:
                out.append(str(x))
        return out
    return []


def _ports(cfg):
    out = []
    for p in cfg.get("ports") or []:
        if isinstance(p, dict):
            t = p.get("target")
            if t is not None:
                out.append(str(t).split("/")[0])
        else:
            s = str(p).split("/")[0]
            if ":" in s:
                s = s.rsplit(":", 1)[-1]
            out.append(s)
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


def _volumes(cfg):
    out = []
    for v in cfg.get("volumes") or []:
        if isinstance(v, dict):
            t = v.get("target")
            if t is not None:
                out.append(str(t))
        else:
            s = str(v)
            parts = s.split(":")
            out.append(parts[1] if len(parts) >= 2 else s)
    return out


def _networks(cfg):
    n = cfg.get("networks")
    if isinstance(n, dict):
        return list(n.keys())
    if isinstance(n, list):
        return [str(x) for x in n]
    return []


def _env(cfg):
    e = cfg.get("environment")
    if isinstance(e, dict):
        return [f"{k}={v}" for k, v in e.items()]
    if isinstance(e, list):
        return [str(x) for x in e]
    return []


def _labels(cfg):
    l = cfg.get("labels")
    if isinstance(l, dict):
        return {str(k): str(v) for k, v in l.items()}
    if isinstance(l, list):
        out = {}
        for item in l:
            s = str(item)
            if "=" in s:
                k, _, v = s.partition("=")
                out[k] = v
            else:
                out[s] = ""
        return out
    return {}


def _replicas(cfg):
    try:
        return int((cfg.get("deploy") or {}).get("replicas") or 1)
    except Exception:
        return 1


# --------------------------------------------------------------------------- 3) merge


def _norm_image(image):
    """Normalize an image reference for comparison.

    Strips the registry prefix and defaults a missing tag to :latest, so
    "nginx" and "docker.io/nginx:latest" compare equal. Digests are kept
    verbatim."""
    s = str(image or "").strip()
    for pfx in ("https://", "http://", "docker.io/", "index.docker.io/"):
        if s.startswith(pfx):
            s = s[len(pfx):]
    if "@" in s:
        return s
    if ":" not in s.rsplit("/", 1)[-1]:
        s = f"{s}:latest"
    return s


def _compose_labels(container):
    """Parse the collector's flat 'k=v,k=v' label string into a dict.

    Docker inspect label output arrives as a comma-joined string; some test /
    future record shapes may carry a dict already. Returns {} on garbage.
    """
    raw = (container or {}).get("labels") or ""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out = {}
    for part in str(raw).split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v
    return out


def _norm_sep(s):
    """Lowercase and collapse '-'/'_' so immich_server == service immich-server."""
    return str(s or "").lower().replace("-", "_")


def _find_match(service_name, stack_name, containers):
    """Deterministic container match, service-scoped to avoid collisions.

    Match order (first hit wins):
      0. Compose label identity: container carries
         ``com.docker.compose.project == stack_name`` AND
         ``com.docker.compose.service == service_name``. Authoritative — immune
         to ``container_name`` overrides and separator differences (e.g.
         container ``immich_server`` for compose service ``immich-server``).
      1. Exact name == service_name / == ``<stack>_<service>``
      2. Separator-normalized exact + compose-default naming
         (``<stack>_<service>[_<n>]``, ``<service>[_|-]<n>``)
      3. Substring similarity (service in name, or name in service)
    Rules 1-3 are project-scoped: a compose-labeled container belonging to a
    DIFFERENT project can never satisfy them (a standalone project's ``redis``
    container must not match this stack's ``redis`` service). Unlabeled
    containers (manual ``docker run``) fall back to name heuristics as before.
    Returns the first container record that satisfies the rule, or None.
    """
    service_name = str(service_name or "").lower()
    stack_name = str(stack_name or "").lower()
    svc_norm = _norm_sep(service_name)
    stack_svc_norm = f"{_norm_sep(stack_name)}_{svc_norm}"

    def _name(c):
        return str((c or {}).get("name") or "").lower()

    def _same_project(c):
        """False when the container is compose-labeled for another project."""
        labels = _compose_labels(c)
        proj = labels.get("com.docker.compose.project")
        if proj is None or not str(proj).strip():
            return True
        return str(proj).lower() == stack_name

    # 0) label identity (project + service)
    for c in containers:
        labels = _compose_labels(c)
        if (labels.get("com.docker.compose.service") or "").lower() == service_name and \
                (labels.get("com.docker.compose.project") or "").lower() == stack_name:
            return c
    # 1) exact names
    for c in containers:
        if not _same_project(c):
            continue
        if _name(c) in (service_name, f"{stack_name}_{service_name}"):
            return c
    # 2) separator-normalized exact + compose-default prefixes
    for c in containers:
        if not _same_project(c):
            continue
        n = _name(c)
        if _norm_sep(n) in (svc_norm, stack_svc_norm):
            return c
        if n.startswith(f"{stack_name}_{service_name}") or \
                n.startswith(service_name + "_") or n.startswith(service_name + "-"):
            return c
    # 3) substring similarity (only within the project scope)
    for c in containers:
        if not _same_project(c):
            continue
        n = _name(c)
        if service_name and n and (service_name in n or n in service_name):
            return c
    return None


def merge_with_docker_actual(desired, docker_containers, cap=120):
    """Merge desired services with docker container state -> per-service records.

    Each record carries expected (compose-derived), actual (matched container,
    or None when unmatched), matched/image_mismatch flags, and best-effort
    missing_volume / missing_network detection (only when the container record
    exposes the data). Output is capped at ``cap`` items.
    """
    merged = []
    for stack in desired or []:
        for svc in stack.get("services") or []:
            if len(merged) >= cap:
                return merged
            cont = _find_match(svc.get("service"), stack.get("stack"), docker_containers or [])
            exp_img = svc.get("image")
            act_img = cont.get("image") if cont else None
            mismatch = bool(exp_img and act_img and _norm_image(exp_img) != _norm_image(act_img))
            missing_vol = _missing_volumes(svc, cont)
            missing_net = _missing_networks(svc, cont)
            if cont is None:
                actual = None
                matched = False
            else:
                matched = True
                running = cont.get("state") == "running"
                status = str(cont.get("status") or "").lower()
                healthcheck = bool(running and "unhealthy" in status)
                if healthcheck:
                    health_status = "unhealthy"
                elif running and "healthy" in status:
                    health_status = "healthy"
                else:
                    health_status = None
                actual = {
                    "name": cont.get("name"),
                    "state": cont.get("state"),
                    "image": cont.get("image"),
                    "restart_count": int(cont.get("restart_count") or 0),
                    "healthcheck": healthcheck,
                    "health_status": health_status,
                    "restarting": bool(cont.get("restarting")),
                    "running": running,
                    "status": cont.get("status"),
                    # labels (raw docker compose label string) so restart-policy
                    # inference can see com.docker.compose.* markers
                    "labels": cont.get("labels") or "",
                    "mounts": cont.get("mounts") or None,
                    "networks": cont.get("networks") or None,
                }
            merged.append({
                "stack": stack.get("stack"),
                "service": svc.get("service"),
                "expected": {
                    "image": exp_img,
                    "restart": svc.get("restart"),
                    "depends_on": svc.get("depends_on") or [],
                    "ports": svc.get("ports") or [],
                    "volumes": svc.get("volumes") or [],
                    "networks": svc.get("networks") or [],
                    "running_requested": True,
                    "replicas": int(svc.get("replicas") or 1),
                },
                "actual": actual,
                "matched": matched,
                "image_mismatch": mismatch,
                "missing_volume": missing_vol,
                "missing_network": missing_net,
                # Desired services are by definition never orphaned; orphaned
                # containers are computed at the dashboard level.
                "orphaned": False,
            })
    return merged


def _missing_volumes(svc, cont):
    """Compose volume targets absent from the container's mounts.

    The collector record only has mounts when docker inspect exposes them; with
    no mounts data we cannot detect anything and return []."""
    mounts = cont.get("mounts") if cont else None
    if not isinstance(mounts, list):
        return []
    targets = set()
    for m in mounts:
        if isinstance(m, dict):
            t = m.get("Destination") or m.get("Target")
            if t:
                targets.add(str(t).rstrip("/"))
    missing = []
    for v in svc.get("volumes") or []:
        t = str(v).rstrip("/")
        if t and t not in targets:
            missing.append(t)
    return missing


def _missing_networks(svc, cont):
    """Compose networks absent from the container's networks.

    Detection only runs when the record exposes networks. A compose network is
    satisfied when an actual network equals it or carries it as a
    project-suffixed/external-suffixed name."""
    nets = cont.get("networks") if cont else None
    if nets is None:
        return []
    actual = set()
    if isinstance(nets, dict):
        actual = {str(k) for k in nets}
    elif isinstance(nets, list):
        actual = {str(x) for x in nets}
    missing = []
    for n in svc.get("networks") or []:
        n = str(n)
        if n and not any(n == an or an.endswith("_" + n) or n in an for an in actual):
            missing.append(n)
    return missing


# --------------------------------------------------------------------------- 4) classify


def classify_drift(merged, docker_doc, cfg_thresholds=None):
    """Classify drift from the merged records -> booleans + capped item lists.

    docker_doc is the collector's docker section (dict with ``containers``) or
    a plain container list; it is used only for replica counting. cfg_thresholds
    currently supports ``item_cap`` (default 10) per item list.
    """
    cfg = cfg_thresholds or {}
    cap = int(cfg.get("item_cap", 10))

    state, health, replica = [], [], []
    image, volume, network = [], [], []
    dependency, policy = [], []

    def _add(items, item):
        if len(items) < cap:
            items.append(item)

    containers = []
    if isinstance(docker_doc, dict):
        containers = docker_doc.get("containers") or []
    elif isinstance(docker_doc, list):
        containers = docker_doc
    per_image = {}
    for c in containers:
        if isinstance(c, dict):
            img = _norm_image(c.get("image"))
            if img:
                per_image[img] = per_image.get(img, 0) + 1

    by_stack = {}
    for m in merged:
        by_stack.setdefault(m.get("stack"), {})[m.get("service")] = m

    for m in merged:
        expected = m.get("expected") or {}
        if expected.get("running_requested", True) and (
                not m.get("matched") or not (m.get("actual") or {}).get("running")):
            if not m.get("matched"):
                cause = "missing container"
            else:
                cause = f"not running ({m.get('actual', {}).get('state')})"
            _add(state, {"stack": m.get("stack"), "service": m.get("service"), "cause": cause})

        if m.get("matched") and (m.get("actual") or {}).get("health_status") == "unhealthy":
            _add(health, {"stack": m.get("stack"), "service": m.get("service"), "cause": "unhealthy"})

        if m.get("image_mismatch"):
            _add(image, {"stack": m.get("stack"), "service": m.get("service"),
                         "expected": expected.get("image"),
                         "actual": (m.get("actual") or {}).get("image"),
                         "cause": f"image mismatch {expected.get('image')} vs {(m.get('actual') or {}).get('image')}"})

        if m.get("missing_volume"):
            _add(volume, {"stack": m.get("stack"), "service": m.get("service"),
                          "missing": m["missing_volume"][:cap],
                          "cause": "missing volumes: " + ", ".join(m["missing_volume"][:cap])})

        if m.get("missing_network"):
            _add(network, {"stack": m.get("stack"), "service": m.get("service"),
                           "missing": m["missing_network"][:cap],
                           "cause": "missing networks: " + ", ".join(m["missing_network"][:cap])})

        for dep in expected.get("depends_on") or []:
            dep_m = by_stack.get(m.get("stack"), {}).get(dep)
            if dep_m is None:
                _add(dependency, {"stack": m.get("stack"), "service": m.get("service"),
                                  "cause": f"depends_on {dep} not present in stack"})
            elif not dep_m.get("matched") or not (dep_m.get("actual") or {}).get("running"):
                _add(dependency, {"stack": m.get("stack"), "service": m.get("service"),
                                  "cause": f"depends_on {dep} not running"})

        if m.get("matched") and m.get("actual"):
            actual_policy = _actual_restart_policy(m["actual"])
            if actual_policy is not None and _policy_family(expected.get("restart")) != _policy_family(actual_policy):
                _add(policy, {"stack": m.get("stack"), "service": m.get("service"),
                              "cause": f"restart policy {expected.get('restart') or 'no'} vs {actual_policy}"})

        replicas = int(expected.get("replicas") or 1)
        exp_img = expected.get("image")
        if replicas > 1 and exp_img:
            count = per_image.get(_norm_image(exp_img), 0)
            if count < replicas:
                _add(replica, {"stack": m.get("stack"), "service": m.get("service"),
                               "cause": f"replicas {replicas}, only {count} container(s) found"})

    state_drift = bool(state)
    health_drift = bool(health)
    replica_drift = bool(replica)
    image_drift = bool(image)
    volume_drift = bool(volume)
    network_drift = bool(network)
    dependency_drift = bool(dependency)
    policy_drift = bool(policy)
    return {
        "state_drift": state_drift, "state_drift_items": state,
        "health_drift": health_drift, "health_drift_items": health,
        "replica_drift": replica_drift, "replica_drift_items": replica,
        "image_drift": image_drift, "image_drift_items": image,
        "volume_drift": volume_drift, "volume_drift_items": volume,
        "network_drift": network_drift, "network_drift_items": network,
        "dependency_drift": dependency_drift, "dependency_drift_items": dependency,
        "policy_drift": policy_drift, "policy_drift_items": policy,
        "compose_drift": image_drift or policy_drift or volume_drift or network_drift or replica_drift,
        "drift_count": (len(state) + len(health) + len(replica) + len(image)
                        + len(volume) + len(network) + len(dependency) + len(policy)),
    }


def _actual_restart_policy(cont):
    """Best-effort actual restart policy from a docker.containers record.

    The collector record does not carry HostConfig.RestartPolicy, so this
    infers from observable evidence only: a 'restarting' state proves an active
    policy; a nonzero restart_count proves restarts have been attempted.
    Compose-managed containers without such evidence return None (unknown, so
    no policy claim is made)."""
    if cont.get("state") == "restarting":
        return "always"
    rc = int(cont.get("restart_count") or 0)
    labels = str(cont.get("labels") or "")
    if rc > 0:
        if "com.docker.compose." in labels:
            return "unless-stopped"
        return "on-failure"
    if "com.docker.compose." in labels:
        return None
    return "no"


def _policy_family(policy):
    """Group restart policies for comparison: no/keep/on-failure."""
    p = str(policy or "").strip().lower()
    if p in ("", "no", "none"):
        return "no"
    if p in ("always", "unless-stopped"):
        return "keep"
    if p.startswith("on-failure"):
        return "on-failure"
    return p


# --------------------------------------------------------------------------- 5) correlate


def _parse_ts(ts):
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except Exception:
        return None


def correlate(docked_events, netdata, dozzle, window_s=1800, storm_min=3, flap_min=2, storm_cap=20):
    """Correlate recent container events into restart storms and health flaps.

    Pure: the newest event timestamp is the window reference, so the result
    depends only on the input (timestamps as ISO strings). Counts
    die/destroy/restart/recreate per container in the last ``window_s`` seconds
    (storm at >= ``storm_min``) and healthy<->unhealthy transitions (flap at
    >= ``flap_min`` toggles). netdata_relation flags CRITICAL alarms that
    mention a stormed container. dozzle is accepted for the contract; the
    relation is netdata-only.
    """
    netdata = netdata or {}
    events = [e for e in (docked_events or []) if isinstance(e, dict)]
    parsed = []
    for e in events:
        ts = _parse_ts(e.get("timestamp"))
        if ts is not None:
            parsed.append((ts, e))
    ref = max((ts for ts, _ in parsed), default=None)
    cutoff = ref - timedelta(seconds=window_s) if ref else None

    storms = []
    if cutoff is not None:
        by_cont = {}
        for ts, e in parsed:
            if ts < cutoff:
                continue
            cont = str(e.get("container_name") or "")
            if cont:
                by_cont.setdefault(cont, []).append(e)
        for cont, ev_list in by_cont.items():
            storm_events = [e for e in ev_list if e.get("action") in STORM_ACTIONS]
            if len(storm_events) >= storm_min:
                storms.append({
                    "service": cont,
                    "count": len(storm_events),
                    "window_min": int(window_s // 60),
                    "sample": [str(e.get("action")) for e in storm_events[-5:]],
                })
        storms = storms[:storm_cap]

    flaps = []
    if cutoff is not None:
        health_by = {}
        for ts, e in parsed:
            if ts < cutoff:
                continue
            action = str(e.get("action") or "")
            if action.startswith("health_status: "):
                cont = str(e.get("container_name") or "")
                if cont:
                    health_by.setdefault(cont, []).append((ts, action))
        for cont, hlist in health_by.items():
            hlist.sort(key=lambda t: t[0])
            toggles = sum(1 for i in range(1, len(hlist)) if hlist[i][1] != hlist[i - 1][1])
            if toggles >= flap_min:
                last = hlist[-1][1]
                flaps.append({
                    "service": cont,
                    "toggle_count": toggles,
                    "window_min": int(window_s // 60),
                    "last_action": last.split(": ", 1)[1] if ": " in last else last,
                })

    stormed = {s["service"] for s in storms}
    matched_alarms = []
    for a in netdata.get("alarms_active") or []:
        if isinstance(a, dict) and a.get("status") == "CRITICAL":
            hay = " ".join(str(a.get(k) or "") for k in ("name", "component", "info", "class")).lower()
            if any(sc.lower() and sc.lower() in hay for sc in stormed):
                matched_alarms.append(a.get("name") or (a.get("info") or "")[:80] or "alarm")

    return {
        "restart_storms": storms,
        "health_flaps": flaps,
        "recent_events_count": len(events),
        "recent_events": events[-20:],
        "netdata_relation": {
            "alarms_count": int(netdata.get("alarms_count") or 0),
            "critical_alarm_matches_storm": bool(matched_alarms),
            "matched_alarms": matched_alarms[:10],
        },
    }


# --------------------------------------------------------------------------- 6) context nodes


def create_context_nodes(merged, classified, correlated, node_cap=20):
    """Collapse drift into compact reasoner-facing context nodes.

    Union of the failing classified items plus restart storms / health flaps,
    each with a short evidence string. Capped at ~``node_cap`` nodes; attention
    is high whenever anything is drifting or storming.
    """
    classified = classified or {}
    correlated = correlated or {}
    nodes = []
    seen = set()

    def _add_node(stack, service, cause, evidence, expected=None, actual=None):
        key = (stack, service)
        if len(nodes) >= node_cap or key in seen:
            return
        seen.add(key)
        nodes.append({"stack": stack, "service": service, "expected": expected,
                      "actual": actual, "cause": cause, "evidence": evidence})

    by_actual_name = {}
    for m in merged:
        a = m.get("actual")
        if a and a.get("name"):
            by_actual_name.setdefault(str(a["name"]), m)

    kind_evidence = {
        "state": "not running",
        "health": "unhealthy",
        "image": "image mismatch",
        "volume": "missing volumes",
        "network": "missing networks",
        "dependency": "dependency failure",
        "policy": "restart policy mismatch",
        "replica": "replica mismatch",
    }
    for kind in ("state", "health", "image", "volume", "network", "dependency", "policy", "replica"):
        for it in classified.get(f"{kind}_drift_items") or []:
            m = next((x for x in merged if x.get("stack") == it.get("stack")
                      and x.get("service") == it.get("service")), None)
            _add_node(it.get("stack"), it.get("service"), it.get("cause"),
                      kind_evidence[kind],
                      expected=(m or {}).get("expected"),
                      actual=(m or {}).get("actual"))

    for s in correlated.get("restart_storms") or []:
        service = s.get("service")
        m = by_actual_name.get(str(service))
        stack = (m or {}).get("stack") or "unknown"
        _add_node(stack, service, f"{s.get('count')} restarts in {s.get('window_min')}min",
                  f"{s.get('count')} restarts/{s.get('window_min')}min",
                  expected=(m or {}).get("expected"), actual=(m or {}).get("actual"))

    for f in correlated.get("health_flaps") or []:
        service = f.get("service")
        m = by_actual_name.get(str(service))
        stack = (m or {}).get("stack") or "unknown"
        _add_node(stack, service, f"{f.get('toggle_count')} health toggles in {f.get('window_min')}min",
                  f"{f.get('toggle_count')} health toggles/{f.get('window_min')}min",
                  expected=(m or {}).get("expected"), actual=(m or {}).get("actual"))

    stack_kinds = {}
    for kind in ("state", "health", "image", "volume", "network", "dependency", "policy", "replica"):
        for it in classified.get(f"{kind}_drift_items") or []:
            stack_kinds.setdefault(it.get("stack"), set()).add(kind)
    if stack_kinds:
        parts = ", ".join(f"{s} ({', '.join(sorted(ks))})" for s, ks in sorted(stack_kinds.items()))
        summary = f"{len(stack_kinds)} stack{'s' if len(stack_kinds) != 1 else ''} drifting: {parts}"
    else:
        summary = "no drift"

    attention = ("high" if (classified.get("drift_count") or 0) > 0
                 or correlated.get("restart_storms") or correlated.get("health_flaps")
                 else "normal")
    return {"drift_summary": summary, "nodes": nodes, "attention": attention}


# --------------------------------------------------------------------------- 7) dashboard


def update_dashboard_snapshot(docked, merged, classified, correlated, docker_doc=None):
    """Reconcile the drift classification into the dashboard snapshot shape.

    Orphaned containers (docker containers not matching any desired service)
    are computed here from ``docker_doc``'s container list. generated_at uses
    now_iso(); everything else is derived purely from the inputs.
    """
    classified = classified or {}
    correlated = correlated or {}
    drift_items = []
    for kind in ("state", "health", "image", "volume", "network", "dependency", "policy", "replica"):
        drift_items += classified.get(f"{kind}_drift_items") or []
    stack_drift = sorted({it.get("stack") for it in drift_items if it.get("stack")})
    service_drift = sorted({it.get("service") for it in drift_items if it.get("service")})

    missing_resources = []
    for it in (classified.get("volume_drift_items") or []) + (classified.get("network_drift_items") or []):
        for name in it.get("missing") or []:
            missing_resources.append({"stack": it.get("stack"), "service": it.get("service"),
                                      "kind": "volume" if name in (it.get("missing") or []) else "network",
                                      "name": name})

    def _matches_desired(cname):
        cn = str(cname or "").lower()
        if not cn:
            return False
        for m in merged:
            svc = str(m.get("service") or "").lower()
            stack = str(m.get("stack") or "").lower()
            if svc == cn or (stack and cn.startswith(stack + "_")) or (svc and (svc in cn or cn in svc)):
                return True
        return False

    orphaned = []
    containers = []
    if isinstance(docker_doc, dict):
        containers = docker_doc.get("containers") or []
    elif isinstance(docker_doc, list):
        containers = docker_doc
    for c in containers:
        if isinstance(c, dict) and not _matches_desired(c.get("name")):
            orphaned.append(c.get("name"))
    orphaned = [n for n in orphaned if n]

    dockhand_up = bool((docked or {}).get("up")) if isinstance(docked, dict) else False
    return {
        "stack_drift": stack_drift,
        "service_drift": service_drift,
        "dependency_failures": classified.get("dependency_drift_items") or [],
        "image_mismatch": classified.get("image_drift_items") or [],
        "health_violations": classified.get("health_drift_items") or [],
        "restart_storms": correlated.get("restart_storms") or [],
        "orphaned_containers": orphaned,
        "missing_resources": missing_resources,
        "dockhand_up": dockhand_up,
        "generated_at": now_iso(),
        "drift_count": int(classified.get("drift_count") or 0),
    }


# --------------------------------------------------------------------------- orchestration


def collect_dockhand(cfg):
    """Orchestrate the Dockhand ingest for one collector cycle.

    Reads ``sources.dockhand`` config (db_path, compose_root, environment_id,
    enabled, storm_min_events, storm_window_s, flap_min_transitions), pulls the
    Dockhand DB, merges against the docker containers from the PRIOR
    collector.json (empty when absent), classifies/correlates, and returns the
    full multi-level doc (every intermediate stage is kept for the
    dashboard/reasoner). Disabled -> ``{"enabled": false}``; DB down ->
    the pull skeleton.
    """
    sd = ((cfg.get("sources.dockhand", {}) or {}) if cfg is not None else {})
    if not sd.get("enabled", True):
        return {"enabled": False}
    db_path = sd.get("db_path", "/appdata/dockhand/sqlite/db/dockhand.db")
    compose_root = sd.get("compose_root", "/appdata/A--docker_stacks")
    environment_id = int(sd.get("environment_id", 1))
    storm_min = int(sd.get("storm_min_events", 3))
    storm_window = int(sd.get("storm_window_s", 1800))
    flap_min = int(sd.get("flap_min_transitions", 2))

    docked = pull_dockhand_state(db_path, compose_root)
    if not docked.get("up"):
        return docked

    desired = normalize(docked, environment_id=environment_id, compose_root=compose_root)
    prev = read_json(Cfg.resolve("paths.collector_json"), {}) or {}
    prev_docker = prev.get("docker") or {}
    docker_containers = prev_docker.get("containers") or []
    netdata = prev.get("netdata") or {}
    dozzle = prev.get("dozzle") or {}

    merged = merge_with_docker_actual(desired["desired_state"], docker_containers)
    classified = classify_drift(merged, prev_docker, {})
    correlated = correlate(docked.get("container_events_recent") or [], netdata, dozzle,
                           window_s=storm_window, storm_min=storm_min, flap_min=flap_min)
    ctx_nodes = create_context_nodes(merged, classified, correlated)
    dash = update_dashboard_snapshot(docked, merged, classified, correlated,
                                     docker_doc=prev_docker)

    desired_doc = desired["desired_state"]
    return {
        "enabled": True,
        "up": True,
        "normalize": {
            "stacks": len(desired_doc),
            "services": sum(len(s.get("services") or []) for s in desired_doc),
            "compose_ready": sum(1 for s in desired_doc if s.get("compose_ready")),
        },
        "merged": merged,
        "classify": classified,
        "correlate": correlated,
        "context_nodes": ctx_nodes,
        "dashboard": dash,
        "error": docked.get("error"),
    }