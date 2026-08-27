"""
Ops Brain - shared helpers (config, paths, logging).
Runtime: Python 3.10+, stdlib + yaml + docker CLI.
"""
import json
import logging
import os
import sys
from pathlib import Path

REPO = Path(os.environ.get("OPSBRAIN_REPO", "/appdata/OpsBrain")).resolve()


class Cfg:
    data = {}

    @classmethod
    def load(cls, path=None):
        import yaml
        path = Path(path or os.environ.get("OPSBRAIN_CONFIG") or REPO / "config" / "ops_brain.yaml")
        with open(path) as fh:
            cls.data = yaml.safe_load(fh)
        cls.data.setdefault("repo", str(REPO))
        return cls.data

    @classmethod
    def get(cls, dotted, default=None):
        node = cls.data
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @classmethod
    def resolve(cls, dotted, base=REPO):
        """Resolve a possibly-relative path from config against REPO."""
        v = cls.get(dotted, "")
        p = Path(v)
        return (p if p.is_absolute() else REPO / p).expanduser()


def get_logger(name="opsbrain"):
    log_dir = REPO / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(REPO / "logs" / "opsbrain.log")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2, default=str)
    return path


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")