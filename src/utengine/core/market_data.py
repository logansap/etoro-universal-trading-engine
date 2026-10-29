"""Daily bar stores and point-in-time market snapshots.

A snapshot for decision session S contains only bars dated <= S. Required symbols must have a bar ON S and a
complete history over the indicator window; otherwise DataError (the cycle holds, nothing is guessed).
Every snapshot carries provenance: source, fetch time, the SHA-256 of the exact bars used, adjusted flag.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import pathlib
import time
import urllib.parse
import urllib.request

from .calendar import Calendar
from .canonical import canonical_json, sha256_text
from .errors import DataError
from .strategy_api import DataProvenance, MarketSnapshot

NAN = float("nan")
SUSPECT_LOG_MOVE = math.log(2.5)       # a one-day move beyond -60% / +150% is treated as bad data, not as a price


class BarStore:
    source = "abstract"
    adjusted = True

    def bars(self, symbol: str, start: dt.date, end: dt.date) -> list:
        """[(date, open, high, low, close, volume)] sorted ascending, dates within [start, end]."""
        raise NotImplementedError


class DictBarStore(BarStore):
    source = "in-memory"

    def __init__(self, data: dict, source: str = "in-memory"):
        self.data = data
        self.source = source

    def bars(self, symbol, start, end):
        return [b for b in self.data.get(symbol, []) if start <= b[0] <= end]


class CsvBarStore(BarStore):
    """One CSV per symbol: date,open,high,low,close,volume (split/dividend adjusted)."""

    def __init__(self, directory, source: str | None = None):
        self.dir = pathlib.Path(directory)
        self.source = source or f"csv:{self.dir.name}"
        self._cache = {}

    def _load(self, symbol):
        if symbol not in self._cache:
            p = self.dir / f"{symbol}.csv"
            if not p.exists() and (self.dir / f"{symbol}.csv.gz").exists():
                p = self.dir / f"{symbol}.csv.gz"
            rows = []
            if p.exists():
                import gzip
                opener = gzip.open if p.suffix == ".gz" else open
                with opener(p, "rt", newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        rows.append((dt.date.fromisoformat(r["date"]), float(r["open"]), float(r["high"]),
                                     float(r["low"]), float(r["close"]), float(r.get("volume") or 0)))
            rows.sort()
            self._cache[symbol] = rows
        return self._cache[symbol]

    def bars(self, symbol, start, end):
        return [b for b in self._load(symbol) if start <= b[0] <= end]


class YahooChartBarStore(BarStore):
    """Free Yahoo chart endpoint (no key). Used only for shadow decisions; never in tests. OHLC are adjusted with
    the adjclose/close ratio (split and dividend adjusted, as QuantConnect's default daily data)."""
    source = "yahoo-chart-v8"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?{q}"

    def __init__(self, timeout=20, pause=0.3):
        self.timeout, self.pause = timeout, pause

    def bars(self, symbol, start, end):
        p1 = int(dt.datetime.combine(start, dt.time(0), dt.timezone.utc).timestamp())
        p2 = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), dt.timezone.utc).timestamp())
        q = urllib.parse.urlencode({"period1": p1, "period2": p2, "interval": "1d", "events": "div,split"})
        req = urllib.request.Request(self.URL.format(sym=symbol, q=q), headers={"User-Agent": "utengine/0.9"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 -- any failure is a data failure
            raise DataError(f"yahoo {symbol}: {type(exc).__name__}: {exc}") from exc
        time.sleep(self.pause)
        try:
            res = payload["chart"]["result"][0]
            ts = res["timestamp"]
            qd = res["indicators"]["quote"][0]
            adj = res["indicators"]["adjclose"][0]["adjclose"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DataError(f"yahoo {symbol}: unexpected payload") from exc
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        out = []
        for k, t in enumerate(ts):
            c, a = qd["close"][k], adj[k]
            if c is None or a is None or qd["open"][k] is None:
                continue
            f = a / c
            d = dt.datetime.fromtimestamp(t, ny).date()
            if start <= d <= end:
                out.append((d, qd["open"][k] * f, qd["high"][k] * f, qd["low"][k] * f, a, float(qd["volume"][k] or 0)))
        return out


def build_snapshot(store: BarStore, calendar: Calendar, session: dt.date, required: list, optional: list = (),
                   lookback_sessions: int = 760, complete_sessions: int = 260, now: dt.datetime | None = None
                   ) -> MarketSnapshot:
    if not calendar.is_trading_day(session):
        raise DataError(f"{session} is not a trading session")
    start = session - dt.timedelta(days=int(lookback_sessions * 7 / 5) + 10)
    cal_days = [d for d in calendar.sessions_between(start, session)][-lookback_sessions:]
    idx = {d: i for i, d in enumerate(cal_days)}
    n = len(cal_days)
    arrays = {k: {} for k in ("open", "high", "low", "close")}
    used = {}
    for sym in list(required) + [s for s in optional if s not in required]:
        rows = store.bars(sym, cal_days[0], session)
        o, h, lo, c = ([NAN] * n for _ in range(4))
        for d, bo, bh, bl, bc, _v in rows:
            if d not in idx:
                raise DataError(f"{sym}: bar dated {d} is not an exchange session (calendar or data error)")
            i = idx[d]
            o[i], h[i], lo[i], c[i] = bo, bh, bl, bc
        if sym in required:
            if not rows or rows[-1][0] != session:
                last = rows[-1][0] if rows else None
                raise DataError(f"STALE_DATA: {sym} has no bar for {session} (last bar {last})")
            tail = c[-complete_sessions:]
            missing = sum(1 for x in tail if not math.isfinite(x))
            if missing:
                raise DataError(f"VENDOR_GAP: {sym} misses {missing} of the last {complete_sessions} sessions")
            for x in (o[-1], h[-1], lo[-1], c[-1]):
                if not (math.isfinite(x) and x > 0):
                    raise DataError(f"{sym}: invalid bar on {session}")
            if math.isfinite(c[-2]) and c[-2] > 0 and abs(math.log(c[-1] / c[-2])) > SUSPECT_LOG_MOVE:
                raise DataError(f"SUSPECT_PRICE: {sym} close {c[-2]:.4f} -> {c[-1]:.4f} on {session} (beyond "
                                "-60%/+150%: unadjusted split or bad print?)")
        arrays["open"][sym], arrays["high"][sym] = tuple(o), tuple(h)
        arrays["low"][sym], arrays["close"][sym] = tuple(lo), tuple(c)
        used[sym] = [(d.isoformat(), bo, bh, bl, bc) for d, bo, bh, bl, bc, _ in rows]
    digest = sha256_text(canonical_json(used))
    fetched = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).isoformat()
    prov = DataProvenance(source=store.source, fetched_at=fetched, sha256=digest, adjusted=store.adjusted,
                          first_date=cal_days[0].isoformat(), last_date=session.isoformat())
    return MarketSnapshot(session=session, dates=tuple(cal_days), open=arrays["open"], high=arrays["high"],
                          low=arrays["low"], close=arrays["close"], provenance=prov)
