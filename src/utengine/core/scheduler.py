"""Decision and execution windows for a strategy schedule (exchange time, America/New_York).

Decision for session S: from close(S) + (decision_time - 16:00) until decision_deadline on the next session
(e.g. 16:50 .. 09:15 the next morning; on a 13:00 early close the decision opens at 13:50).
Execution for decision S: the next regular session N, from open(N) + execution_delay_minutes for
execution_window_minutes. Nothing is ever executed outside that window.

MISSED POLICY = skip (the only supported policy): a decision or execution window that passes without the work
being done is recorded as MISSED and is never caught up later. A late start INSIDE a window is allowed and is
flagged `late` when it begins more than LATE_AFTER after the window opened.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .calendar import NY, Calendar

LATE_AFTER = dt.timedelta(minutes=30)


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


@dataclass(frozen=True)
class Window:
    kind: str                # decision | execution
    session: dt.date         # decision session S (for both kinds)
    start: dt.datetime
    end: dt.datetime

    def contains(self, now: dt.datetime) -> bool:
        return self.start <= now < self.end

    def is_late(self, now: dt.datetime) -> bool:
        return now - self.start > LATE_AFTER


class Scheduler:
    def __init__(self, calendar: Calendar, schedule: dict):
        self.cal = calendar
        self.s = schedule
        dtime = _hm(schedule["decision_time"])
        self.offset = dt.timedelta(hours=dtime.hour - 16, minutes=dtime.minute)
        if self.offset < dt.timedelta(0):
            raise ValueError("decision_time must be at or after the 16:00 close")
        self.deadline = _hm(schedule["decision_deadline"])

    def decision_window(self, s: dt.date) -> Window:
        start = self.cal.close_utc(s) + self.offset
        end = self.cal.at_ny(self.cal.next_session(s), self.deadline)
        return Window("decision", s, start, end)

    def execution_window(self, s: dt.date) -> Window:
        n = self.cal.next_session(s)
        start = self.cal.open_utc(n) + dt.timedelta(minutes=self.s["execution_delay_minutes"])
        return Window("execution", s, start, start + dt.timedelta(minutes=self.s["execution_window_minutes"]))

    def current_decision_window(self, now: dt.datetime) -> Window | None:
        s = self.cal.last_completed_session(now)
        w = self.decision_window(s)
        return w if w.contains(now) else None

    def current_execution_window(self, now: dt.datetime) -> Window | None:
        today = now.astimezone(NY).date()
        if not self.cal.is_trading_day(today):
            return None
        w = self.execution_window(self.cal.previous_session(today))
        return w if w.contains(now) else None

    def windows_ended_since(self, since: dt.datetime, now: dt.datetime, kind: str) -> list:
        """Windows of `kind` that ended in (since, now] -- candidates for MISSED records."""
        out = []
        d = (since - dt.timedelta(days=5)).date()
        while d <= now.date():
            if self.cal.is_trading_day(d):
                w = self.decision_window(d) if kind == "decision" else self.execution_window(d)
                if since < w.end <= now:
                    out.append(w)
            d += dt.timedelta(days=1)
        return out
