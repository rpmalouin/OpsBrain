"""
Ops Brain - Federation Reasoner.

Consumes logs/cluster_snapshot.json and produces a unified cluster reasoning
decision (logs/cluster_reasoner_result.json) with:

- cross-node anomaly correlation
- cross-node drift correlation
- cluster-wide confidence
- cluster stability score (0-100)
- node ranking by stability
- recommended cluster-level actions (notify-only; NO cross-node remediation)

The stability math is deterministic (no LLM required) so the score is stable and
testable. A Pro reasoning pass may enrich the summary, but all numbers here come
from the cluster snapshot.

Stability formula (cluster_stability_score, 0-100):
    score = (confidence*0.4 + drift*0.3 + anomalies*0.2 + restarts*0.1) * 100
where confidence = avg_confidence (0..1) and drift/anomalies/restarts are each
reverse-normalized to 0..1 (see reverse_normalize) before weighting.

Usage:
    python3 federation/federation_reasoner.py [--config <path>] [--out <path>]
"""
import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, REPO, get_logger, read_json  # noqa: E402

log = get_logger("federation_reasoner")

RECOMMEND_TYPES = ("notify_cluster", "escalate_cluster", "cluster_health_warning")
DEFAULT_WEIGHTS = {"confidence": 0.4, "drift": 0.3, "anomalies": 0.2, "restarts": 0.1}


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def reverse_normalize(value, max_bad):
    """Map a 0..max_bad count to a 0..1 health score (fewer bad -> closer to 1)."""
    value = float(value or 0.0)
    if max_bad <= 0:
        return 1.0 if value <= 0 else 0.0
    return _clamp(1.0 - value / float(max_bad))


def _node_health_fields(node, weights):
    """Return per-node 0..1 health components for each weight bucket.

    For an online node, confidence is used directly (a null/None confidence on an
    online node earns full credit — it is up and responsive, so assume healthy);
    drift/anomalies/restarts use reverse_normalize. For an offline node every
    bucket scores 0 so an unreachable node drags the cluster score down.
    """
    online = bool(node.get("online"))
    if not online:
        return {k: 0.0 for k in weights}
    conf = node.get("confidence")
    conf_score = _clamp(float(conf)) if conf is not None else 1.0
    return {
        "confidence": conf_score,
        "drift": reverse_normalize(node.get("drift_events", 0), 20),
        "anomalies": reverse_normalize(node.get("anomalies", 0), 20),
        "restarts": reverse_normalize(node.get("restart_events", 0), 10),
    }


def node_stability(node, weights):
    """Per-node stability 0..100 from the weighted buckets."""
    w = dict(DEFAULT_WEIGHTS)
    w.update(weights or {})
    fields = _node_health_fields(node, w)
    total = 0.0
    used_weight = 0.0
    for k, weight in w.items():
        score = max(0.0, fields.get(k, 0.0) * 100)
        total += score * float(weight)
        used_weight += float(weight)
    if used_weight <= 0:
        return 100.0 if node.get("online") else 0.0
    return round(_clamp(total / used_weight, 0, 100), 1)


def cluster_confidence(snapshot):
    """Mean confidence across online nodes (0.0 if none online)."""
    nodes = snapshot.get("nodes", {}) or {}
    confs = [float(n["confidence"]) for n in nodes.values()
             if n.get("online") and n.get("confidence") is not None]
    return round(sum(confs) / len(confs), 3) if confs else 0.0


def cluster_stability_score(weights, metrics):
    """Cluster stability 0..100 via the weighted spec formula.

    metrics: {avg_confidence (0..1), total_anomalies, drift_events, restart_events}
    """
    w = dict(DEFAULT_WEIGHTS)
    w.update(weights or {})
    avg_conf = float(metrics.get("avg_confidence", 0.0) or 0.0)
    comp = {
        "confidence": _clamp(avg_conf),
        "drift": reverse_normalize(metrics.get("drift_events", 0), 20),
        "anomalies": reverse_normalize(metrics.get("total_anomalies", 0), 20),
        "restarts": reverse_normalize(metrics.get("restart_events", 0), 10),
    }
    total = sum(comp.get(k, 0.0) * float(w[k]) for k in w if k in comp)
    used = sum(float(w[k]) for k in w if k in comp)
    if used <= 0:
        return 0.0
    # total/used is a 0..1 health value; scale to 0..100
    return round(_clamp(total / used, 0, 1) * 100, 1)


def rank_nodes(snapshot, weights):
    """Return {name: stability} and a sorted ranking (best first)."""
    nodes = snapshot.get("nodes", {}) or {}
    stab = {name: node_stability(node, weights) for name, node in nodes.items()}
    ranking = sorted(stab, key=lambda n: stab[n], reverse=True)
    return stab, ranking


def _drift_flags_from_node(node):
    """Collect a compact set of drift flags a node reports, if any are surfaced."""
    raw = node.get("raw")
    if not raw:
        return set()
    coll = raw.get("collector") or (raw.get("sources") or {}).get("collector.json") or raw
    flags = coll.get("gpu", {}).get("drift_flags") or []
    return set(str(f) for f in flags)


def detect_cross_correlations(snapshot):
    """Detect correlated signals across nodes.

    - anomaly_correlations: nodes BOTH reporting active Netdata alarms sharing the
      same alarm 'name' (best signal for a cross-node class of failure).
    - drift_correlations: nodes sharing at least one identical GPU drift flag.
    Returns {anomaly_correlations: [...], drift_correlations: [...]}.
    """
    nodes = snapshot.get("nodes", {}) or {}
    online = {n: nd for n, nd in nodes.items() if nd.get("online")}
    out = {"anomaly_correlations": [], "drift_correlations": []}
    names = list(online)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            # shared alarm names
            share_a = set(_alarm_names(online[a]))
            share_b = set(_alarm_names(online[b]))
            for alarm in sorted(share_a & share_b):
                out["anomaly_correlations"].append({
                    "nodes": [a, b], "shared_anomaly": alarm})
            # shared drift flags
            shared_drift = _drift_flags_from_node(online[a]) & _drift_flags_from_node(online[b])
            for flag in sorted(shared_drift):
                out["drift_correlations"].append({
                    "nodes": [a, b], "shared_flag": flag})
    return out


def _alarm_names(node):
    raw = node.get("raw")
    if not raw:
        return []
    coll = raw.get("collector") or (raw.get("sources") or {}).get("collector.json") or raw
    alarms = coll.get("netdata", {}).get("alarms_active") or []
    return [str(a.get("name") or "") for a in alarms if isinstance(a, dict)]


def _recommendations(snapshot, score, weights):
    """Deterministic cluster-level recommendations (notify-only)."""
    recs = []
    nodes = snapshot.get("nodes", {}) or {}
    offline = [n for n, nd in nodes.items() if not nd.get("online")]
    metrics = snapshot.get("cluster_metrics", {})

    if offline:
        recs.append({
            "type": "cluster_health_warning", "target": "cluster",
            "reason": f"{len(offline)} node(s) offline: {', '.join(offline)}",
            "severity": "critical"})
    if score is not None and score < 60:
        recs.append({
            "type": "escalate_cluster", "target": "cluster",
            "reason": f"cluster stability {score}/100 below 60",
            "severity": "warning"})
    if int(metrics.get("total_anomalies", 0)) > 0:
        recs.append({
            "type": "notify_cluster", "target": "cluster",
            "reason": f"{metrics['total_anomalies']} total anomaly(ies) across cluster",
            "severity": "info"})
    if int(metrics.get("drift_events", 0)) > 0:
        recs.append({
            "type": "notify_cluster", "target": "cluster",
            "reason": f"{metrics['drift_events']} GPU drift event(s) across cluster",
            "severity": "info"})
    return recs


def cluster_reason(snapshot, weights=None):
    """Produce the full cluster reasoning decision document."""
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(weights or {})
    metrics = snapshot.get("cluster_metrics", {}) or {}
    nodes = snapshot.get("nodes", {}) or {}
    online_any = any(nd.get("online") for nd in nodes.values())
    # With no online node, zero "bad event" counts must NOT read as healthy:
    # an unreachable cluster scores 0, not a clean 60.
    score = cluster_stability_score(weights, metrics) if online_any else 0.0
    stab, ranking = rank_nodes(snapshot, weights)
    cross = detect_cross_correlations(snapshot)
    conf = cluster_confidence(snapshot)

    offline = [n for n, nd in nodes.items() if not nd.get("online")]
    top = ranking[0] if ranking else None
    worst = ranking[-1] if ranking else None

    if not ranking:
        summary = "No node telemetry available; cluster status unknown."
    elif not online_any:
        summary = "All configured nodes are offline; cluster stability unknown (scored 0)."
    elif len(nodes) == 1:
        summary = (f"Single-node cluster ({top}): stability {stab[top]}/100, "
                   f"confidence {conf:.2f}.")
    else:
        parts = [f"Cluster stability {score}/100 (confidence {conf:.2f})."]
        parts.append(f"Best node: {top} ({stab[top]}/100); worst: {worst} ({stab[worst]}/100).")
        if cross["anomaly_correlations"]:
            parts.append(f"{len(cross['anomaly_correlations'])} cross-node anomaly correlation(s).")
        if cross["drift_correlations"]:
            parts.append(f"{len(cross['drift_correlations'])} cross-node drift correlation(s).")
        if offline:
            parts.append(f"Offline: {', '.join(offline)}.")
        summary = " ".join(parts)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cluster_stability_score": score,
        "node_stability": stab,
        "node_ranking": ranking,
        "cluster_confidence": conf,
        "cross_node": cross,
        "recommendations": _recommendations(snapshot, score, weights),
        "summary": summary,
        "weights": weights,
    }


def _atomic_write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            import json
            json.dump(doc, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main():
    ap = argparse.ArgumentParser(prog="federation_reasoner")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    Cfg.load(args.config)

    fed = Cfg.get("federation", {}) or {}
    if not fed.get("enabled", False):
        log.info("federation disabled; skipping cluster reasoning")
        return 0
    snap_path = Cfg.resolve("paths.cluster_snapshot") if Cfg.get("paths.cluster_snapshot") else REPO / "logs" / "cluster_snapshot.json"
    snapshot = read_json(snap_path, {}) or {}
    weights = fed.get("cluster_stability_weights", {}) or {}
    doc = cluster_reason(snapshot, weights)

    out = Path(args.out) if args.out else (
        Cfg.resolve("paths.cluster_reasoner") if Cfg.get("paths.cluster_reasoner") else REPO / "logs" / "cluster_reasoner_result.json")
    _atomic_write(out, doc)
    log.info("federation reasoner: stability=%s conf=%s recs=%d", 
             doc["cluster_stability_score"], doc["cluster_confidence"], len(doc["recommendations"]))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())