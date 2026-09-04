"""
Ops Brain - scheduler cadence tests.

Covers the report-due window semantics that make the daily report cadence-
independent: a report for date D is due from D's report_time until the same
time on D+1, so coarse poll cadences (e.g. hourly, whose phase never lands
inside the 23:55 minute) still generate it daily — on the first cycle at/after
the report moment, or the first post-midnight cycle if the poll straddles it.

Run:  python3 -m pytest -q tests/test_scheduler_cadence.py
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from common import Cfg  # noqa: E402
from scheduler import scheduler as S  # noqa: E402


@pytest.fixture
def cfg(monkeypatch):
    """Minimal config: default 23:55 report time (override per test)."""
    data = {"report": {"time": "23:55", "dir": "reports"}}
    monkeypatch.setattr(Cfg, "data", data)
    return data


def _now(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss)


def test_exact_report_minute(cfg):
    assert S.should_report(_now(2026, 9, 4, 23, 55)) == "2026-09-04"


def test_first_cycle_after_report_time(cfg):
    # Old exact-match logic fired only inside the :55 minute; any cycle after
    # 23:55 must fire too.
    assert S.should_report(_now(2026, 9, 4, 23, 56)) == "2026-09-04"
    assert S.should_report(_now(2026, 9, 4, 23, 59)) == "2026-09-04"


def test_midnight_straddle_returns_previous_day(cfg):
    # Hourly poll at :30 lands at 00:30 on D+1 — yesterday's report is still due.
    assert S.should_report(_now(2026, 9, 5, 0, 30)) == "2026-09-04"


def test_due_window_covers_full_next_day(cfg):
    # Before today's report time the previous day's window is still open; the
    # daemon's `reported` latch + on-disk guard dedupe, so this is harmless.
    assert S.should_report(_now(2026, 9, 5, 12, 0)) == "2026-09-04"
    assert S.should_report(_now(2026, 9, 5, 23, 54)) == "2026-09-04"


def test_next_days_report_fires_after_window(cfg):
    # First cycle at/after 23:55 on D+1 returns D+1 (latch then flips).
    assert S.should_report(_now(2026, 9, 5, 23, 56)) == "2026-09-05"


def test_custom_report_time(cfg):
    cfg["report"]["time"] = "09:00"
    assert S.should_report(_now(2026, 9, 4, 9, 5)) == "2026-09-04"
    # 08:59 is still inside the previous day's due window.
    assert S.should_report(_now(2026, 9, 4, 8, 59)) == "2026-09-03"


def test_report_outpath(cfg, tmp_path):
    cfg["report"]["dir"] = str(tmp_path)
    assert S._report_outpath("2026-09-04") == tmp_path / "2026-09-04.md"
