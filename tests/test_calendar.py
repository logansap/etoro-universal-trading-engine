"""Scenario 19: holidays, early closes, DST and missed windows (no catch-up)."""
import datetime as dt

from conftest import DECIDE_AT, Env
from utengine.core.calendar import Calendar, holidays
from utengine.core.scheduler import Scheduler

UTC = dt.timezone.utc
CAL = Calendar(["2025-01-09"])
SCHED = {"decision_time": "16:50", "decision_deadline": "09:15", "execution_delay_minutes": 1,
         "execution_window_minutes": 60}
D = dt.date

NYSE_PUBLISHED = {  # nyse.com holiday schedule
    2024: {D(2024, 1, 1), D(2024, 1, 15), D(2024, 2, 19), D(2024, 3, 29), D(2024, 5, 27), D(2024, 6, 19),
           D(2024, 7, 4), D(2024, 9, 2), D(2024, 11, 28), D(2024, 12, 25)},
    2025: {D(2025, 1, 1), D(2025, 1, 20), D(2025, 2, 17), D(2025, 4, 18), D(2025, 5, 26), D(2025, 6, 19),
           D(2025, 7, 4), D(2025, 9, 1), D(2025, 11, 27), D(2025, 12, 25)},
    2026: {D(2026, 1, 1), D(2026, 1, 19), D(2026, 2, 16), D(2026, 4, 3), D(2026, 5, 25), D(2026, 6, 19),
           D(2026, 7, 3), D(2026, 9, 7), D(2026, 11, 26), D(2026, 12, 25)},
    2027: {D(2027, 1, 1), D(2027, 1, 18), D(2027, 2, 15), D(2027, 3, 26), D(2027, 5, 31), D(2027, 6, 18),
           D(2027, 7, 5), D(2027, 9, 6), D(2027, 11, 25), D(2027, 12, 24)},
}


def test_s19_holidays_match_published_schedule():
    """INVARIANT: computed NYSE holidays equal the published schedule 2024-2027; the 2025-01-09 unscheduled
    closure comes from configuration."""
    for y, exp in NYSE_PUBLISHED.items():
        assert set(holidays(y)) == exp, y
    assert not CAL.is_trading_day(D(2025, 1, 9)) and Calendar().is_trading_day(D(2025, 1, 9))


def test_s19_early_closes():
    """INVARIANT: 13:00 closes on Jul 3 (when Jul 4 is Tue-Fri), the day after Thanksgiving and Christmas Eve."""
    early = [d for d in CAL.sessions_between(D(2024, 1, 1), D(2027, 12, 31)) if CAL.is_early_close(d)]
    assert early == [D(2024, 7, 3), D(2024, 11, 29), D(2024, 12, 24), D(2025, 7, 3), D(2025, 11, 28),
                     D(2025, 12, 24), D(2026, 11, 27), D(2026, 12, 24), D(2027, 11, 26)]
    assert CAL.close_utc(D(2026, 11, 27)) == dt.datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_s19_dst_switch_moves_utc_times():
    """INVARIANT: 09:30 New York is 13:30Z before the 2026-11-01 DST end and 14:30Z after; windows follow."""
    s = Scheduler(CAL, SCHED)
    assert CAL.open_utc(D(2026, 10, 30)) == dt.datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert CAL.open_utc(D(2026, 11, 2)) == dt.datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    w = s.decision_window(D(2026, 10, 30))
    assert w.start == dt.datetime(2026, 10, 30, 20, 50, tzinfo=UTC)       # 16:50 EDT
    assert w.end == dt.datetime(2026, 11, 2, 14, 15, tzinfo=UTC)          # 09:15 EST on Monday
    x = s.execution_window(D(2026, 10, 30))
    assert (x.start, x.end) == (dt.datetime(2026, 11, 2, 14, 31, tzinfo=UTC), dt.datetime(2026, 11, 2, 15, 31, tzinfo=UTC))
    assert s.decision_window(D(2026, 11, 27)).start == dt.datetime(2026, 11, 27, 18, 50, tzinfo=UTC)  # early close


def test_s19_no_decision_on_holiday_and_window_spans_it():
    """INVARIANT: Thanksgiving has no session; the 11-25 decision stays open until 09:15 on 11-27 and its
    execution is on 11-27 (the next session)."""
    s = Scheduler(CAL, SCHED)
    now = dt.datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
    w = s.current_decision_window(now)
    assert w.session == D(2026, 11, 25)
    assert s.current_execution_window(now) is None
    assert s.execution_window(D(2026, 11, 25)).start.date() == D(2026, 11, 27)


def test_s19_missed_windows_recorded_and_not_caught_up(tmp_path):
    """INVARIANT: if the engine is down for a whole decision window, a MISSED_DECISION is recorded and no decision
    is made late (missed_policy skip); the following session's window is processed normally."""
    env = Env(tmp_path, clock_at=dt.datetime(2026, 10, 26, 15, 0, tzinfo=UTC))
    e = env.engine()
    e.cycle()                                                     # 11:00 New York, idle
    env.clock.set(dt.datetime(2026, 10, 27, 14, 0, tzinfo=UTC))   # back after the 10-26 decision window closed
    out = e.cycle()
    assert ("MISSED_DECISION", "2026-10-26") in out["missed"]
    assert e.db.one("SELECT COUNT(*) c FROM decisions")["c"] == 0
    assert "execution" in out and out["execution"]["status"] == "NO_DECISION"
    assert e.db.one("SELECT COUNT(*) c FROM intents")["c"] == 0
    env.clock.set(dt.datetime(2026, 10, 27, 21, 0, tzinfo=UTC))
    assert DECIDE_AT < env.clock()
    e.cycle()                                                     # 10-27 window: decided (data permitting)
    assert e.db.one("SELECT COUNT(*) c FROM schedule_events WHERE kind='MISSED_DECISION'")["c"] == 1
