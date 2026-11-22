"""Ops Brain - the ollama box as a node (`kind: ollama`).

The box at 10.1.10.30 is the fleet's only GPU host and its only LLM, and it runs no agent of
ours: nothing is installed there. So this node source is a POLLER with two read-only surfaces,
both already in use elsewhere on this host:

* `http://10.1.10.30:11434/api/{version,tags,ps}` — what the model is doing. No auth (the box
  is unauthenticated on the LAN, same trust model as the old local ollama stack).
* `ssh <alias> nvidia-smi --query-gpu=...` — what the cards are doing. The `ollama` SSH alias
  (ron@10.1.10.30, key-based) is the same one a human uses.

Why a node source instead of a producer snapshot (the Mac's pattern): the box has no share this
host can read and no agent to install, and the two questions we need answered every cycle —
"is a model resident on the GPU" and "is the ollama port answering with real bytes" — are both
HTTP-visible. Installing a producer there would add a second thing to keep alive on the only
host whose whole job is to stay up.

The failure this is built around: on 2026-11-06 this ollama (then on dockerVM, same Go server)
wedged by ACCEPTING TCP and answering zero bytes after a Go panic holding locks — `nc -z`
succeeded, `docker ps` said Up, and every request returned nothing. A poller that only checks
"can I connect" reports that box as healthy forever. Hence `_get_json` treats a 200 with an
empty body as its own explicit anomaly, and the HTTP timeout is short (5 s) so a wedged box
costs one cycle's seconds rather than hanging the federation pass.
"""
import json
import subprocess
import urllib.error
import urllib.request

DEFAULT_HTTP_TIMEOUT = 5.0
DEFAULT_SSH_TIMEOUT = 10.0
DEFAULT_TEMP_MAX_C = 80.0
DEFAULT_VRAM_MAX_PCT = 95.0

NVIDIA_QUERY = (
    "nvidia-smi --query-gpu=index,name,memory.total,memory.used,utilization.gpu,"
    "temperature.gpu,power.draw --format=csv,noheader,nounits"
)


class EmptyBody(RuntimeError):
    """HTTP 200 with nothing in it — the wedged-ollama signature."""


def _get_json(url, timeout):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw or not raw.strip():
        raise EmptyBody("HTTP 200 with an empty body")
    return json.loads(raw.decode("utf-8"))


def read_gpus(ssh_host, timeout):
    """One row per card from the box's own nvidia-smi. Raises on any failure."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % max(1, int(timeout)),
           ssh_host, NVIDIA_QUERY]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(detail[0] if detail else "ssh nvidia-smi failed rc=%d" % proc.returncode)
    gpus = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            total = float(parts[2])
            used = float(parts[3])
        except ValueError:
            continue
        gpus.append({
            "index": int(parts[0]) if parts[0].isdigit() else parts[0],
            "name": parts[1],
            "mem_total_mib": total,
            "mem_used_mib": used,
            "util_percent": _maybe_float(parts[4]),
            "temp_c": _maybe_float(parts[5]),
            "power_w": _maybe_float(parts[6]),
        })
    if not gpus:
        raise RuntimeError("nvidia-smi returned no parsable rows")
    return gpus


def _maybe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(version, tags, ps, gpus, cfg, http_error=None, ssh_error=None):
    """Pure function: the four payloads in, the node block out. No I/O, so it is testable."""
    temp_max = _maybe_float(cfg.get("gpu_temp_max_c")) or DEFAULT_TEMP_MAX_C
    vram_max = _maybe_float(cfg.get("vram_used_max_pct")) or DEFAULT_VRAM_MAX_PCT
    alerts = []

    if http_error:
        alerts.append("ollama API unreachable: %s" % http_error)
    if ssh_error:
        alerts.append("gpu query failed: %s" % ssh_error)

    ps = ps or {}
    models = [m for m in (ps.get("models") or []) if isinstance(m, dict)]
    resident = []
    for m in models:
        size = _maybe_float(m.get("size")) or 0.0
        vram = _maybe_float(m.get("size_vram")) or 0.0
        entry = {"name": m.get("name"), "size_gb": round(size / 1e9, 2),
                 "size_vram_gb": round(vram / 1e9, 2),
                 "context_length": m.get("context_length"),
                 "expires_at": m.get("expires_at")}
        resident.append(entry)
        if size and vram and vram < size * 0.995:
            alerts.append("model %s is not fully on the GPU: %.1f of %.1f GB in VRAM"
                          % (m.get("name"), vram / 1e9, size / 1e9))
        elif size and vram == 0:
            alerts.append("model %s is loaded on CPU only" % m.get("name"))

    for gpu in (gpus or []):
        total = _maybe_float(gpu.get("mem_total_mib")) or 0.0
        used = _maybe_float(gpu.get("mem_used_mib")) or 0.0
        if total and used / total * 100.0 > vram_max:
            alerts.append("gpu%s %.0f%% VRAM used (%.0f of %.0f MiB)"
                          % (gpu.get("index"), used / total * 100.0, used, total))
        temp = _maybe_float(gpu.get("temp_c"))
        if temp is not None and temp > temp_max:
            alerts.append("gpu%s %s at %.0f C" % (gpu.get("index"), gpu.get("name"), temp))

    return {
        "version": (version or {}).get("version") if isinstance(version, dict) else None,
        "tags": [t.get("name") for t in (tags or {}).get("models") or [] if isinstance(t, dict)],
        "models_resident": resident,
        "context_length": resident[0]["context_length"] if resident else None,
        "alerts": alerts,
    }


def collect(node, log=None):
    """Collect one ollama-box node block. Never raises: a dead box is a node that says so."""
    cfg = node or {}
    http_timeout = _maybe_float(cfg.get("timeout_s")) or DEFAULT_HTTP_TIMEOUT
    ssh_timeout = _maybe_float(cfg.get("ssh_timeout_s")) or DEFAULT_SSH_TIMEOUT
    base = str(cfg.get("ollama_url") or "http://10.1.10.30:11434").rstrip("/")
    ssh_host = cfg.get("ssh_host")
    expected_model = cfg.get("expect_model")

    version, tags, ps = None, None, None
    http_error = None
    try:
        # /api/version is the cheapest proof the server is answering with real bytes.
        version = _get_json(base + "/api/version", http_timeout)
        tags = _get_json(base + "/api/tags", http_timeout)
        ps = _get_json(base + "/api/ps", http_timeout)
    except EmptyBody as exc:
        http_error = "wedged: %s" % exc
    except urllib.error.HTTPError as exc:
        http_error = "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001 - any transport failure is an anomaly, not a crash
        http_error = "%s: %s" % (type(exc).__name__, exc)

    gpus, ssh_error = None, None
    if ssh_host:
        try:
            gpus = read_gpus(ssh_host, ssh_timeout)
        except Exception as exc:  # noqa: BLE001
            ssh_error = "%s: %s" % (type(exc).__name__, exc)

    block = evaluate(version, tags, ps, gpus, cfg, http_error=http_error, ssh_error=ssh_error)
    if expected_model and block["tags"] and expected_model not in block["tags"]:
        block["alerts"].append("model %s is not installed on the box (has: %s)"
                               % (expected_model, ", ".join(block["tags"])))

    online = http_error is None
    node_dict = {
        "online": online,
        "type": cfg.get("type") or "gpu",
        "confidence": None,
        "drift_events": 0,
        "anomalies": len(block["alerts"]),
        "restart_events": 0,
        "disk_used_percent": None,
        "containers": {"running": 0, "total": 0},
        "alerts_count": len(block["alerts"]),
        "ollama": block,
        "gpu": gpus or [],
    }
    if not online:
        node_dict["err"] = str(http_error)
    # federation_reasoner correlates across nodes by reading node["raw"]; keep the same compact
    # shape the polled path emits so the ollama box's alerts can correlate with dockerVM's.
    node_dict["raw"] = {"collector": {
        "netdata": {"alarms_active": [{"name": a} for a in block["alerts"]]},
        "gpu": {"drift_flags": [a for a in block["alerts"] if a.startswith("gpu")]},
    }}
    if log is not None:
        log.info("ollama node: online=%s alerts=%d gpus=%d", online, len(block["alerts"]),
                 len(gpus or []))
    return node_dict
