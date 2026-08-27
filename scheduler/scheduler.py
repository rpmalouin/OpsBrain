"""
Ops Brain - scheduler (cron-like controller loop).

Runs the full pipeline
    collector -> reasoner (Qwen3 14B) -> hermes_actions
every `interval_seconds` (default 120), and generates the daily ops report at
the configured report time (default 23:55).

Usage:
    python3 scheduler/scheduler.py --daemon        # run forever (e.g. under systemd)
    python3 scheduler/scheduler.py --once          # one full cycle then exit
    python3 scheduler/scheduler.py --report        # generate daily report only
    python3 scheduler/scheduler.py --once --no-dry-run   # collect+reason+REAL actions
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
    ap.add_argument("--no-dry-run", action="store_true",
                    help="allow live remediation (default honours config actions.dry_run)")
    args = ap.parse_args()
    Cfg.load(args.config)

    dry = not args.no_dry_run

    if args.report:
        return run_script("report")

    if args.once:
        return run_cycle(dry_run=dry)

    # daemon (default)
    interval = int(Cfg.get("interval_seconds", 120))
    log.info("daemon started: cadence=%ss report_at=%s dry_run=%s",
             interval, Cfg.get("report.time", "23:55"), dry)
    reported = None
    while True:
        cycle_start = time.time()
        run_cycle(dry_run=dry)
        day = should_report(datetime.now())
        if day and day != reported:
            run_script("report")
            reported = day
        gap = interval - (time.time() - cycle_start)
        if gap > 0:
            time.sleep(gap)


if __name__ == "__main__":
    sys.exit(main())