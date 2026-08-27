"""
Ops Brain - scheduler (cron-like controller loop).

Runs the full pipeline
    collector -> reasoner (Qwen3 14B) -> hermes_actions
every `interval_seconds` (default 120), runs the federation collector+reasoner
every `poll_interval_cycles` (default 2 cycles = 4 min), and generates the daily
ops report at the configured report time (default 23:55).

Usage:
    python3 scheduler/scheduler.py --daemon        # run forever (e.g. under systemd)
    python3 scheduler/scheduler.py --once          # one full cycle then exit
    python3 scheduler/scheduler.py --report        # generate daily report only
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import Cfg, REPO, get_logger  # noqa: E402

log = get_logger("scheduler")

PY = sys.executable or "python3"

SCRIPTS = {
    "collector": "collector/collector.py",
    "reasoner": "reasoner/reasoner.py",
    "actions": "hermes_actions/actions.py",
    "report": "scheduler/report.py",
    "feed_collector": "federation/federation_collector.py",
    "feed_reasoner": "federation/federation_reasoner.py",
}


def run_script(name, args=()):
    script = REPO / SCRIPTS[name]
    cmd = [PY, str(script), *(str(a) for a in args)]
    log.info("  exec: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log.error("stage %s failed rc=%s: %s", name, p.returncode, p.stderr[-900:])
    return p.returncode


def run_cycle(dry_run=True):
    t0 = time.time()
    run_script("collector")
    run_script("reasoner")
    aargs = ["--dry-run"] if (dry_run or Cfg.get("actions.dry_run", True)) else []
    run_script("actions", aargs)
    log.info("cycle complete in %.1fs", time.time() - t0)
    return 0


def run_federation(cycle_no=0):
    """Run federation collector + reasoner if enabled and on the poll interval."""
    if not Cfg.get("federation.enabled", False):
        return
    every = int(Cfg.get("federation.poll_interval_cycles", 2) or 2)
    if every < 1:
        every = 1
    if cycle_no % every != 0:
        return
    run_script("feed_collector")
    run_script("feed_reasoner")


def should_report(now):
    """Return the report date string when today's report_time has just been reached, else None."""
    hhmm = now.strftime("%H:%M")
    if hhmm == str(Cfg.get("report.time", "23:55")):
        return now.strftime("%Y-%m-%d")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--daemon", action="store_true", help="run the supervisor loop forever")
    ap.add_argument("--once", action="store_true", help="run one full cycle then exit")
    ap.add_argument("--report", action="store_true", help="generate the daily report only")
    args = ap.parse_args()
    Cfg.load(args.config)

    if args.report:
        return run_script("report")

    if args.once:
        run_cycle(dry_run=bool(Cfg.get("actions.dry_run", True)))
        run_federation(0)
        return 0

    # daemon (default)
    interval = int(Cfg.get("interval_seconds", 120))
    dry_run = bool(Cfg.get("actions.dry_run", True))
    log.info("daemon started: cadence=%ss report_at=%s dry_run=%s",
             interval, Cfg.get("report.time", "23:55"), dry_run)
    reported = None
    cycle_no = 0
    while True:
        cycle_start = time.time()
        run_cycle(dry_run=dry_run)
        run_federation(cycle_no)
        cycle_no += 1
        day = should_report(datetime.now())
        if day and day != reported:
            run_script("report")
            reported = day
        gap = interval - (time.time() - cycle_start)
        if gap > 0:
            time.sleep(gap)


if __name__ == "__main__":
    sys.exit(main())