"""
Ops Brain - Manual Stop Protection shared helpers.

Implements the HARD INVARIANT: "Manually stopped containers must stay stopped."
A container the user manually stopped must NEVER be auto-restarted (or destroyed
via prune) by OpsBrain — regardless of autonomous remediation, confidence gating,
restart caps, anomaly detection, drift remediation, or Qwen-generated actions.

Persistence: logs/manual_stops.json, keyed by container ID (never auto-forgets).
The only automatic removal is a user re-arm: the collector observes the container
running again (or a same-name/new-ID recreate) and clears protection.

Safety IO: writes are atomic (tmp + os.replace); reads fail closed — a corrupt or
unreadable file blocks all docker_restart and logs loudly, so a parse error can
never silently drop the invariant.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Exit codes that indicate a clean/manual stop (as opposed to a crash).
#  0   -> graceful exit (docker stop, app exited cleanly)
#  143 -> default SIGTERM death (docker stop on an app that doesn't trap SIGTERM)
#  137 -> SIGKILL after grace expiry (docker stop) or `docker kill`; ambiguous,
#         but with OOM_killed false we favour protection (fail-safe).
MANUAL_STOP_EXIT_CODES = {0, 143, 137}
# Exit codes that unambiguously indicate a crash (OOM or segfault/etc).
# 139 -> SIGSEGV; 130 -> SIGINT; 132-136/139 = CPU faults; 1/2 = app error.
CRASH_EXIT_CODES = {1, 2, 130, 132, 134, 135, 136, 139}


def _norm_name(name) -> str:
    return str(name or "").lower().lstrip("/").rstrip("/")


class ManualStops:
    """Load / save / query the manual-stop registry."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict = {}           # {container_id: {name, detected_at, ...}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {"version": 1, "stops": {}}
            return
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            stops = data.get("stops", {}) if isinstance(data, dict) else {}
            self._data = {"version": 1, "stops": {k: v for k, v in stops.items()
                                                  if isinstance(v, dict)}}
        except Exception:
            # fail closed: never drop the invariant on a corrupt read.
            self._data = {"version": 1, "stops": {}, "_corrupt_read": True}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self._data, fh, indent=2, default=str)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @property
    def corrupt(self) -> bool:
        return bool(self._data.get("_corrupt_read"))

    def stops(self) -> dict:
        return self._data.get("stops", {})

    def is_protected(self, name_or_id: str) -> bool:
        n = _norm_name(name_or_id)
        for cid, rec in self.stops().items():
            if n == _norm_name(cid) or n == _norm_name(rec.get("name", "")):
                return True
        return False

    def protected_names(self) -> list:
        return sorted({_norm_name(r.get("name", k)) for k, r in self.stops().items()})

    def add(self, container_id: str, name: str, exit_code, oom_killed, finished_at,
            detected_at: str) -> None:
        if container_id in self.stops():
            return  # never overwrite / re-stamp
        self.stops()[container_id] = {
            "name": _norm_name(name),
            "detected_at": detected_at,
            "exit_code": int(exit_code) if exit_code is not None else None,
            "oom_killed": bool(oom_killed),
            "finished_at": finished_at or None,
            "reason": "manual_stop",
        }
        self.save()

    def rearm(self, container_id: str, current_name: str = "") -> None:
        """User restarted the container (running again) -> clear protection."""
        self.stops().pop(container_id, None)
        # also clear any alias entries sharing the (new) normalized name with a
        # different id, i.e. same-name/new-id recreate = re-arm.
        n = _norm_name(current_name or container_id)
        for cid in list(self.stops().keys()):
            if cid != container_id and _norm_name(self.stops()[cid].get("name", "")) == n:
                self.stops().pop(cid, None)
        self.save()


def classify_manual_stop(exit_code, oom_killed, sigkill_protect: bool = True) -> bool:
    """Decide whether a stopped container's exit signature is a manual stop.

    - OOM_killed == true            -> crash, NOT a manual stop.
    - exit_code in {0, 143, 137}    -> manual stop (137 favours protection).
    - any other nonzero exit code   -> crash, NOT a manual stop.
    """
    if oom_killed:
        return False
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        return False  # unknown exit code -> don't guess
    if code in MANUAL_STOP_EXIT_CODES:
        # 137 is ambiguous; if sigkill_protect is false it becomes a crash.
        if code == 137 and not sigkill_protect:
            return False
        return True
    return False