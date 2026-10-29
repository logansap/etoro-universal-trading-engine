"""NYSE (XNYS) regular-session calendar in America/New_York, computed from the exchange's published rules.

Holidays: New Year's Day, Martin Luther King Jr. Day, Washington's Birthday, Good Friday, Memorial Day,
Juneteenth (from 2022), Independence Day, Labor Day, Thanksgiving, Christmas, with NYSE weekend observance
(Saturday -> Friday, Sunday -> Monday; a Saturday New Year's Day is NOT observed on the preceding Friday).
Early closes (13:00): the day before Independence Day, the day after Thanksgiving, Christmas Eve, when those are
trading days. Unscheduled closures (e.g. 2025-01-09, national day of mourning) come from engine.yaml and must be
added by the operator when the exchange announces one -- the engine cannot infer them.
DST is handled by zoneinfo: the same 09:30/16:00 New York times map to different UTC times across the switch.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
OPEN = dt.time(9, 30)
CLOSE = dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)


def _easter(y: int) -> dt.date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return dt.date(y, month, day)


def _nth_weekday(y, month, weekday, n):
    d = dt.date(y, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(y, month, weekday):
    d = dt.date(y, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(y, 12, 31)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: dt.date) -> dt.date | None:
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def holidays(y: int) -> frozenset:
    out = set()
    ny = dt.date(y, 1, 1)
    if ny.weekday() == 6:
        out.add(ny + dt.timedelta(days=1))
    elif ny.weekday() != 5:
        out.add(ny)
    out.add(_nth_weekday(y, 1, 0, 3))                 # MLK
    out.add(_nth_weekday(y, 2, 0, 3))                 # Washington's Birthday
    out.add(_easter(y) - dt.timedelta(days=2))         # Good Friday
    out.add(_last_weekday(y, 5, 0))                    # Memorial Day
    if y >= 2022:
        out.add(_observed(dt.date(y, 6, 19)))          # Juneteenth
    out.add(_observed(dt.date(y, 7, 4)))               # Independence Day
    out.add(_nth_weekday(y, 9, 0, 1))                  # Labor Day
    out.add(_nth_weekday(y, 11, 3, 4))                 # Thanksgiving
    out.add(_observed(dt.date(y, 12, 25)))             # Christmas
    return frozenset(out)


class Calendar:
    def __init__(self, unscheduled_closures=()):
        self.closures = frozenset(dt.date.fromisoformat(str(d)) for d in unscheduled_closures)

    def is_trading_day(self, d: dt.date) -> bool:
        return d.weekday() < 5 and d not in holidays(d.year) and d not in self.closures

    def is_early_close(self, d: dt.date) -> bool:
        if not self.is_trading_day(d):
            return False
        y = d.year
        if d == dt.date(y, 7, 3) and dt.date(y, 7, 4).weekday() in (1, 2, 3, 4):
            return True
        if d == _nth_weekday(y, 11, 3, 4) + dt.timedelta(days=1):
            return True
        if d == dt.date(y, 12, 24):
            return True
        return False

    def next_session(self, d: dt.date) -> dt.date:
        d = d + dt.timedelta(days=1)
        while not self.is_trading_day(d):
            d += dt.timedelta(days=1)
        return d

    def previous_session(self, d: dt.date) -> dt.date:
        d = d - dt.timedelta(days=1)
        while not self.is_trading_day(d):
            d -= dt.timedelta(days=1)
        return d

    def sessions_between(self, a: dt.date, b: dt.date) -> list:
        out, d = [], a
        while d <= b:
            if self.is_trading_day(d):
                out.append(d)
            d += dt.timedelta(days=1)
        return out

    def open_utc(self, d: dt.date) -> dt.datetime:
        return dt.datetime.combine(d, OPEN, NY).astimezone(UTC)

    def close_utc(self, d: dt.date) -> dt.datetime:
        return dt.datetime.combine(d, EARLY_CLOSE if self.is_early_close(d) else CLOSE, NY).astimezone(UTC)

    def at_ny(self, d: dt.date, t: dt.time) -> dt.datetime:
        return dt.datetime.combine(d, t, NY).astimezone(UTC)

    def last_completed_session(self, now: dt.datetime) -> dt.date:
        """The most recent session whose close is at or before `now`."""
        d = now.astimezone(NY).date()
        if self.is_trading_day(d) and self.close_utc(d) <= now:
            return d
        return self.previous_session(d)

    def is_open(self, now: dt.datetime) -> bool:
        d = now.astimezone(NY).date()
        return self.is_trading_day(d) and self.open_utc(d) <= now < self.close_utc(d)
