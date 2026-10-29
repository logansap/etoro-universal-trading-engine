"""Source-faithful ports of the sanchari-lineage overlays: #529, #755, #776.

All three run the SAME four sleeves as #504 (T10 + T11 + S2 + S3, 25% each,
weight-summed), which research/qc_four_strategies already ports and parity-checks
against #504's published LEAN backtest. These classes therefore SUBCLASS that port
and add only each strategy's own overlay, with that strategy's own parameters.
Nothing is tuned here.

  #529  #504 + velocity crash guard: QQQ 10-day return < -10% halves gross,
        released above -4%. The guard state is part of the no-trade label, as in
        the source, so a guard transition does trigger a rebalance.

  #755  #504 + two overlays:
          (a) macro vol taper: SPY ATR(20, simple) above 5.5 scales every
              leveraged or volatility holding by 0.65 and parks the remainder in
              BIL. NOTE: the threshold is in DOLLARS, not a fraction of price, so
              it tightens mechanically as SPY's level rises -- reproduced as
              written, and the resulting duty cycle is reported;
          (b) portfolio circuit breaker: a 20% drawdown from the NAV high-water
              mark liquidates to BIL for 21 calendar days, then resets the
              high-water mark to the NAV at that moment.
        The source checks the breaker BEFORE its no-trade label short-circuit and
        applies the taper AFTER it, so the taper only takes effect on a session
        where a sleeve label changes. That stickiness is reproduced.

  #776  #504's sleeves + #529's crash guard, run as an IBKR CASH account:
        per-security cap 1.0, gross cap 0.98, whole shares, 5% cash reserve, no
        margin. T+1 settlement is NOT modelled; the source defers unaffordable
        buys and retries them later, so this port is favourable to #776 (its buys
        always fill at the next open). Stated as a limitation, not hidden.
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

QC4 = pathlib.Path(__file__).resolve().parents[1] / "qc_four_strategies"
if str(QC4) not in sys.path:
    sys.path.insert(0, str(QC4))

from strategies import S504  # noqa: E402

LEVERED = {"TQQQ", "TECL", "SOXL", "SQQQ", "UVXY", "SVIX", "SVXY", "SPXL", "LABU",
           "TECS", "SOXS", "UVIX"}


def _atr_simple(panel, sym, n=20):
    """LEAN AverageTrueRange(n, SIMPLE): simple mean of the true range."""
    h, lo, c = panel.h[sym], panel.l[sym], panel.c[sym]
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.nanmax(np.vstack([h - lo, np.abs(h - pc), np.abs(lo - pc)]), axis=0)
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


class CrashGuardMixin:
    CRASH_ENTRY, CRASH_EXIT, CRASH_LOOKBACK, CRASH_GROSS = -0.10, -0.04, 10, 0.50

    def _guard(self, i) -> float:
        c = self.p.c["QQQ"]
        if i - self.CRASH_LOOKBACK >= 0 and np.isfinite(c[i]) and np.isfinite(c[i - self.CRASH_LOOKBACK]):
            r = c[i] / c[i - self.CRASH_LOOKBACK] - 1.0
            if not self._in_crash and r < self.CRASH_ENTRY:
                self._in_crash = True
            elif self._in_crash and r > self.CRASH_EXIT:
                self._in_crash = False
        return self.CRASH_GROSS if self._in_crash else 1.0


class S529(S504, CrashGuardMixin):
    name = "529_504_crash_guard"

    def __init__(self, panel):
        super().__init__(panel)
        self._in_crash = False

    def decide(self, i, book, nav):
        g = self._guard(i)
        rt10, rt11, rs2, rs3 = self.ready(i)
        sl = [self.t10(i) if rt10 else {}, self.t11(i) if rt11 else {},
              self.s2(i) if rs2 else {}, self.s3(i) if rs3 else {}]
        label = (tuple(tuple((t, round(w / self.Q * 100)) for t, w in s.items()) for s in sl), g)
        if label == self.last:
            return None
        self.last = label
        comb = {}
        for s in sl:
            for t, w in s.items():
                comb[t] = comb.get(t, 0.0) + w * g
        return self.units_for(comb, nav, i, book, liquidate_others=True)


class S755(S504):
    name = "755_504_overlays"
    ATR_LEVEL, VOL_SCALAR, DD_TRIP, COOL_DAYS = 5.5, 0.65, 0.20, 21

    def __init__(self, panel):
        super().__init__(panel)
        self._atr = _atr_simple(panel, "SPY", 20)
        self._hwm, self._tripped, self._reset_on = None, False, None
        self.taper_days, self.breaker_days = 0, 0

    def decide(self, i, book, nav):
        d = self.p.dates[i]
        if not self._tripped:
            self._hwm = nav if self._hwm is None else max(self._hwm, nav)
            dd = (self._hwm - nav) / self._hwm if self._hwm else 0.0
        else:
            dd = 0.0
        if self._tripped:
            if d >= self._reset_on:
                self._tripped, self._hwm = False, nav
            else:
                self.breaker_days += 1
                self.last = ("BREAKER",)
                return self.units_for({"BIL": 1.0}, nav, i, book, liquidate_others=True)
        if dd >= self.DD_TRIP:
            self._tripped = True
            self._reset_on = d + pd.Timedelta(days=self.COOL_DAYS)
            self.breaker_days += 1
            self.last = ("BREAKER",)
            return self.units_for({"BIL": 1.0}, nav, i, book, liquidate_others=True)

        rt10, rt11, rs2, rs3 = self.ready(i)
        sl = [self.t10(i) if rt10 else {}, self.t11(i) if rt11 else {},
              self.s2(i) if rs2 else {}, self.s3(i) if rs3 else {}]
        label = tuple(tuple((t, round(w / self.Q * 100)) for t, w in s.items()) for s in sl)
        if label == self.last:
            return None
        self.last = label
        raw = {}
        for s in sl:
            for t, w in s.items():
                raw[t] = raw.get(t, 0.0) + w
        a = self._atr[i]
        k = self.VOL_SCALAR if (np.isfinite(a) and a > self.ATR_LEVEL) else 1.0
        if k < 1.0:
            self.taper_days += 1
        comb, buf = {}, 0.0
        for t, w in raw.items():
            if t in LEVERED:
                comb[t] = w * k
                buf += w - w * k
            else:
                comb[t] = comb.get(t, 0.0) + w
        if buf > 0.001:
            comb["BIL"] = comb.get("BIL", 0.0) + buf
        return self.units_for(comb, nav, i, book, liquidate_others=True)


class S776(S504, CrashGuardMixin):
    name = "776_cash_account_guard"
    SECURITY_CAP, GROSS_LIMIT, CASH_RESERVE = 1.0, 0.98, 0.05

    def __init__(self, panel):
        super().__init__(panel)
        self._in_crash = False
        self.capped_days = 0

    def decide(self, i, book, nav):
        g = self._guard(i)
        rt10, rt11, rs2, rs3 = self.ready(i)
        sl = [self.t10(i) if rt10 else {}, self.t11(i) if rt11 else {},
              self.s2(i) if rs2 else {}, self.s3(i) if rs3 else {}]
        label = (tuple(tuple((t, round(w / self.Q * 100)) for t, w in s.items()) for s in sl), g)
        if label == self.last:
            return None
        self.last = label
        raw = {}
        for s in sl:
            for t, w in s.items():
                raw[t] = raw.get(t, 0.0) + w * g
        clean = {t: w for t, w in raw.items() if w > 0}
        capped = {t: min(self.SECURITY_CAP, w) for t, w in clean.items()}
        gross = sum(capped.values())
        if gross > self.GROSS_LIMIT:
            self.capped_days += 1
        k = min(1.0, self.GROSS_LIMIT / gross) if gross > 0 else 1.0
        comb = {t: w * k for t, w in capped.items() if w * k > 1e-12}
        out = {}
        for t, w in comb.items():
            if self.p.has(t, i):
                out[t] = float(int(nav * (1 - self.CASH_RESERVE) * w / self.p.c[t][i]))
        for t, u in book.pos.items():
            if u and t not in out:
                out[t] = 0.0
        return out
