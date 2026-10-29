"""Source-faithful ports of #788, #60, #504, #785, the frozen composite E (with ablations) and benchmarks.

Each port reproduces the source's DECISION logic line by line (comments cite the source). Execution follows the
engine's convention (decide after close t, fill at open t+1); deviations from the sources are listed in AUDIT.md
and PREREGISTRATION.yaml. Nothing here was tuned: every number is the source's own, or frozen in the
pre-registration before any replay result was computed.
"""
from __future__ import annotations

import math
import statistics

import numpy as np
import pandas as pd

from engine import Base, set_holdings_orders, roc

SVIX_LIVE = pd.Timestamp("2022-03-30")


# ============================================================================ A: #788
class S788(Base):
    """Weekly (first session of the week, 30 min after the open in the source) rank QQQ/XLE/GLD by ROC(60).
    Bull (SPY > SMA200): 40/35/20 by rank. Bear: GLD/XLE lead 40/35 by ROC, QQQ 20. SetHoldings with
    FreePortfolioValuePercentage 0.05. Here: decided after the last close of the week, filled at the next open."""
    name = "A_788"
    U = ("QQQ", "XLE", "GLD")

    def __init__(self, panel):
        super().__init__(panel)
        self.roc = {t: roc(panel.c[t], 60) for t in self.U}

    def decide(self, i, book, nav):
        d = self.p.dates
        if d[i + 1].isocalendar()[1] == d[i].isocalendar()[1]:      # next session is not the week's first
            return None
        if not all(self.ok(x) for x in [self.sma("SPY", 200, i)] + [self.roc[t][i] for t in self.U]):
            return None
        r = {t: self.roc[t][i] for t in self.U}
        if self.px("SPY", i) > self.sma("SPY", 200, i):
            ranked = sorted(self.U, key=lambda t: r[t], reverse=True)    # Python sort is stable, like the source
            w = dict(zip(ranked, (0.40, 0.35, 0.20)))
        else:
            lead, second = ("GLD", "XLE") if r["GLD"] >= r["XLE"] else ("XLE", "GLD")
            w = {lead: 0.40, second: 0.35, "QQQ": 0.20}
        return set_holdings_orders(self, w, nav, i, book, free_pct=0.05)


# ============================================================================ B: #60
class S60(Base):
    """Daily single-asset tree, 100% SetHoldings(liquidateExistingHoldings=True), default free buffer 0.25%."""
    name = "B_60"

    def decide(self, i, book, nav):
        r = lambda t: self.rsi(t, 10, i)  # noqa: E731
        need = [r(t) for t in ("SPY", "QQQ", "TQQQ", "SQQQ", "UVXY", "TECS", "BSV")] + [
            self.sma("SPY", 200, i), self.sma("QQQ", 20, i), self.sma("TQQQ", 20, i)]
        if not all(self.ok(x) for x in need):
            return None
        if self.px("SPY", i) > self.sma("SPY", 200, i):
            tgt = "UVXY" if r("QQQ") > 81 else ("UVXY" if r("SPY") > 80 else "TQQQ")
        elif r("TQQQ") < 30:
            tgt = "TECL"
        elif r("SPY") < 30:
            tgt = "SPXL"
        elif r("UVXY") > 74:
            if r("UVXY") > 84:
                if self.px("QQQ", i) > self.sma("QQQ", 20, i):
                    tgt = "TECS" if r("SQQQ") < 31 else "TECL"
                else:
                    tgt = max(("TECS", "BSV"), key=r)
            else:
                tgt = "UVXY"
        elif self.px("TQQQ", i) > self.sma("TQQQ", 20, i):
            tgt = "TECS" if r("SQQQ") < 34 else "TECL"
        else:
            tgt = max(("TECS", "BSV"), key=r)
        return set_holdings_orders(self, {tgt: 1.0}, nav, i, book, free_pct=0.0025, liquidate_others=True)


# ============================================================================ shared #504 / #785 sleeves
class QuadSleeves(Base):
    """The four sleeves T10, T11, S2, S3 (identical in #504 and #785; #785 adds the OFF-regime dip veto)."""
    Q = 0.25
    T10_LATE = {"KMLM", "LABU", "QQQE", "VOOG", "VOOV"}
    T11_LATE = {"KMLM"}
    T10 = ("QQQE", "VTV", "VOX", "TECL", "VOOG", "VOOV", "XLP", "TQQQ", "XLY", "FAS", "SPY", "SOXL", "SPXL",
           "LABU", "XLK", "KMLM")
    T11_10 = ("SPY", "IOO", "TQQQ", "VTV", "XLF", "XLK", "KMLM", "PSQ", "BND", "QQQ", "IEF")
    T11_20 = ("TLT", "PSQ", "AGG")

    def regime_allows_dips(self):
        return True

    def _r10(self, tickers, i):
        return {t: self.rsi(t, 10, i) for t in tickers if self.ok(self.rsi(t, 10, i))}

    def ready(self, i):
        t10 = all(self.ok(self.rsi(t, 10, i)) for t in self.T10 if t not in self.T10_LATE)
        t11 = (all(self.ok(self.rsi(t, 10, i)) for t in self.T11_10 if t not in self.T11_LATE) and
               all(self.ok(self.rsi(t, 20, i)) for t in self.T11_20) and self.ok(self.rsi("SH", 60, i)) and
               self.ok(self.sma("SPY", 200, i)) and self.ok(self.sma("TQQQ", 20, i)))
        s2 = all(self.ok(x) for x in (self.sma("TQQQ", 200, i), self.sma("TQQQ", 20, i), self.rsi("TQQQ", 10, i),
                                      self.rsi("SOXL", 10, i), self.rsi("SQQQ", 10, i), self.rsi("BSV", 10, i)))
        s3 = all(self.ok(x) for x in [self.sma(t, 202, i) for t in ("SPY", "QQQ", "SMH", "SOXL")] +
                 [self.rsi("QQQ", 8, i), self.rsi("SMH", 8, i)] +
                 [self.rsi(t, 15, i) for t in ("SPY", "QQQ", "SMH", "SOXL")])
        return t10, t11, s2, s3

    def t10(self, i):
        r = self._r10(self.T10, i)
        g = lambda t, d: r.get(t, d)  # noqa: E731
        if (g("QQQE", 0) > 79 or g("VTV", 0) > 79 or g("VOX", 0) > 79 or g("TECL", 0) > 79 or g("VOOG", 0) > 79
                or g("VOOV", 0) > 79 or g("XLP", 0) > 75 or g("TQQQ", 0) > 79 or g("XLY", 0) > 80
                or g("FAS", 0) > 80 or g("SPY", 0) > 80):
            return {"UVXY": self.Q}
        if self.regime_allows_dips():
            if g("TQQQ", 50) < 30:
                return {"TECL": self.Q}
            if g("SOXL", 50) < 30:
                return {"SOXL": self.Q}
            if g("SPXL", 50) < 30:
                return {"SPXL": self.Q}
            if "LABU" in r and r["LABU"] < 25:
                return {"LABU": self.Q}
        vs = "SVIX" if self.p.dates[i] >= SVIX_LIVE else "SVXY"
        if "KMLM" not in r or r["XLK"] > r["KMLM"]:
            p = self.Q / 3
            return {"TECL": p, "SOXL": p, vs: p}
        return {"SQQQ": self.Q / 2, "TLT": self.Q / 2}

    def _qqq_ret60(self, i):
        c = self.p.c["QQQ"][i - 60:i + 1]
        return (c[-1] / c[0] - 1) * 100 if len(c) == 61 else 0.0

    def t11(self, i):
        r10 = self._r10(self.T11_10, i)
        r20 = {t: self.rsi(t, 20, i) for t in self.T11_20}
        sh60 = self.rsi("SH", 60, i)
        tq, tq_sma = self.px("TQQQ", i), self.sma("TQQQ", 20, i)
        ob = ("SPY", "IOO", "TQQQ", "VTV", "XLF")
        if any(r10[t] > 79 for t in ob):
            if any(r10[t] > 81 for t in ob):
                return {"UVXY": self.Q}
            p = self.Q / 3
            return {"UVXY": p, "BIL": p, "BTAL": p}
        if self.regime_allows_dips():
            if r10["TQQQ"] < 30:
                return {"TQQQ": self.Q}
            if r10["SPY"] < 30:
                return {"SPXL": self.Q}
        if self.px("SPY", i) > self.sma("SPY", 200, i):
            p = self.Q / 3
            kmlm_ready = "KMLM" in r10 and self.ok(self.sma("KMLM", 20, i))
            if not kmlm_ready or r10["XLK"] > r10["KMLM"] or self.px("KMLM", i) < self.sma("KMLM", 20, i):
                return {"TECL": p, "SOXL": p, "TQQQ": p}
            return {"TECS": p, "SOXS": p, "SQQQ": p}

        def bond_baller():
            if r20["TLT"] > r20["PSQ"]:
                return "QQQ"
            if tq > tq_sma:
                if r10["PSQ"] < 35:
                    return "PSQ"
                return "TQQQ" if r20["AGG"] > sh60 else "PSQ"
            return "PSQ" if r10["IEF"] > r20["PSQ"] else "SQQQ"

        def feaver_bear():
            if self._qqq_ret60(i) < -12:
                return "QLD" if r10["BND"] > r10["QQQ"] else "BTAL"
            if tq > tq_sma:
                if r10["PSQ"] < 35:
                    return "PSQ"
                return "TQQQ" if r20["AGG"] > sh60 else "PSQ"
            return "PSQ" if r10["IEF"] > r20["PSQ"] else "SQQQ"

        out = {}
        for t in (bond_baller(), feaver_bear()):
            out[t] = out.get(t, 0.0) + self.Q / 2
        return out

    def s2(self, i):
        px, rsi_t, rsi_s = self.px("TQQQ", i), self.rsi("TQQQ", 10, i), self.rsi("SOXL", 10, i)
        if px > self.sma("TQQQ", 200, i):
            return {("UVXY" if rsi_t > 79 else "TQQQ"): self.Q}
        if self.regime_allows_dips():
            if rsi_t < 31:
                return {"TECL": self.Q}
            if rsi_s < 30:
                return {"SOXL": self.Q}
        if px < self.sma("TQQQ", 20, i):
            return {("SQQQ" if self.rsi("SQQQ", 10, i) > self.rsi("BSV", 10, i) else "BSV"): self.Q}
        return {"TQQQ": self.Q}

    def s3(self, i):
        votes = sum(int(self.px(t, i) > self.sma(t, 202, i)) for t in ("SPY", "QQQ", "SMH", "SOXL"))
        ob = any(self.rsi(t, 15, i) > 72 for t in ("SPY", "QQQ", "SMH", "SOXL"))
        if votes >= 3:
            if ob:
                uvix = self.p.dates[i] >= SVIX_LIVE and self.p.has("UVIX", i)
                return {("UVIX" if uvix else "UVXY"): self.Q}
            return {"TQQQ": self.Q / 2, "SOXL": self.Q / 2}
        if self.regime_allows_dips() and (self.rsi("QQQ", 8, i) < 29 or self.rsi("SMH", 8, i) < 31):
            return {"SOXL": self.Q}
        return {}


# ============================================================================ C: #504
class S504(QuadSleeves):
    """Equal 25% sleeves, weight-summed; trades ONLY when the sleeve label changes; then liquidates every
    non-target and sets each target to weight x NAV / close; market-on-open."""
    name = "C_504"

    def __init__(self, panel):
        super().__init__(panel)
        self.last = None

    def decide(self, i, book, nav):
        rt10, rt11, rs2, rs3 = self.ready(i)
        sl = [self.t10(i) if rt10 else {}, self.t11(i) if rt11 else {}, self.s2(i) if rs2 else {},
              self.s3(i) if rs3 else {}]
        label = tuple(tuple((t, round(w / self.Q * 100)) for t, w in s.items()) for s in sl)
        if label == self.last:
            return None
        self.last = label
        comb = {}
        for s in sl:
            for t, w in s.items():
                comb[t] = comb.get(t, 0.0) + w
        return self.units_for(comb, nav, i, book, liquidate_others=True)


# ============================================================================ D: #785
class S785(QuadSleeves):
    """#504's sleeves + regime model, long/short-vol gates, caps, same-index netting, beta budgets, QQQ-vol
    proxy target, 5% cash buffer, no-trade bands, minimum ticket, market-on-open. Optional guard OFF by default
    (USE_PORTFOLIO_GUARD = False in the published source)."""
    name = "D_785"
    EQUITY_BETA = {"TQQQ": 3.0, "SQQQ": -3.0, "QLD": 2.0, "PSQ": -1.0, "QQQ": 1.0, "TECL": 3.0, "TECS": -3.0,
                   "SOXL": 3.0, "SOXS": -3.0, "SPXL": 3.0, "SH": -1.0, "SPY": 1.0, "LABU": 3.0, "FAS": 3.0}
    FAMILY = {"NASDAQ100": ("TQQQ", "SQQQ", "QLD", "PSQ", "QQQ"), "TECH": ("TECL", "TECS"),
              "SEMIS": ("SOXL", "SOXS"), "SP500": ("SPXL", "SH", "SPY")}
    LONG_VOL, SHORT_VOL = {"UVXY", "UVIX"}, {"SVIX", "SVXY"}
    TRIPLE = {"TQQQ", "SQQQ", "TECL", "TECS", "SOXL", "SOXS", "SPXL", "LABU", "FAS"}
    SYMS = ("TQQQ", "TECL", "SOXL", "SQQQ", "UVXY", "SVIX", "SVXY", "XLK", "KMLM", "TLT", "QQQE", "VTV", "VOX",
            "VOOG", "VOOV", "XLP", "XLY", "FAS", "SPXL", "LABU", "SPY", "IOO", "XLF", "TECS", "SOXS", "QQQ", "PSQ",
            "QLD", "BTAL", "BIL", "AGG", "SH", "BND", "IEF", "BSV", "SMH", "UVIX")
    CASH_BUFFER, REBALANCE_DRIFT, SIGNAL_CHANGE_DRIFT, MIN_TRADE_USD = 0.05, 0.03, 0.025, 100.0
    MAX_3X, MAX_LONG_VOL, MAX_SHORT_VOL, TARGET_PROXY_VOL = 0.35, 0.06, 0.05, 0.25
    LONG_BUDGET = {"ON": 1.50, "CAUTION": 1.00, "OFF": 0.50}
    SHORT_BUDGET = {"ON": 0.25, "CAUTION": 0.50, "OFF": 0.75}

    def __init__(self, panel, guard=False, name=None):
        super().__init__(panel)
        self.guard_on = guard
        self.name = name or ("D_785_guard" if guard else "D_785")
        self.regime, self.up_cand, self.up_n = "CAUTION", None, 0
        self.dd60 = self.rv20 = self.ret60 = 0.0
        self.peak, self.pdd, self.g_active, self.g_factor, self.g_traded = None, 0.0, False, 1.0, 1.0
        self.last_label = ""

    def regime_allows_dips(self):
        return self.regime != "OFF"

    def qqq_risk(self, i):
        c = self.p.c["QQQ"][max(0, i - 60):i + 1]
        if len(c) < 61 or not np.all(np.isfinite(c)):
            return False
        self.dd60 = c[-1] / c.max() - 1
        self.ret60 = 100 * (c[-1] / c[0] - 1)
        rec = c[-21:]
        self.rv20 = statistics.pstdev([math.log(rec[k] / rec[k - 1]) for k in range(1, 21)]) * math.sqrt(252)
        return True

    def update_regime(self, i):
        q, s = self.px("QQQ", i), self.sma("QQQ", 200, i)
        if self.dd60 <= -0.15 or self.rv20 >= 0.45 or (q < 0.98 * s and (self.dd60 < -0.08 or self.rv20 > 0.30)):
            cand = "OFF"
        elif q > 1.01 * s and self.dd60 > -0.08 and self.rv20 < 0.30:
            cand = "ON"
        else:
            cand = "CAUTION"
        rank = {"OFF": 0, "CAUTION": 1, "ON": 2}
        if rank[cand] <= rank[self.regime]:
            self.regime, self.up_cand, self.up_n = cand, None, 0
            return
        if self.up_cand != cand:
            self.up_cand, self.up_n = cand, 1
        else:
            self.up_n += 1
        if self.up_n >= 3:
            self.regime, self.up_cand, self.up_n = cand, None, 0

    def update_guard(self, nav):
        self.peak = nav if self.peak is None else max(self.peak, nav)
        self.pdd = nav / self.peak - 1
        if not self.guard_on:
            self.g_active, self.g_factor = False, 1.0
            return
        if not self.g_active and self.pdd <= -0.10:
            self.g_active = True
        elif self.g_active and self.pdd >= -0.06:
            self.g_active = False
        self.g_factor = (0.90 if (self.regime == "ON" and self.rv20 < 0.25) else 0.75) if self.g_active else 1.0

    def can_trade(self, t, i):
        return self.p.has(t, i) and self.p.c[t][i] > 0

    def overlay(self, raw, i):
        w = {t: float(x) for t, x in raw.items() if x > 0 and self.can_trade(t, i)}
        lv = self.rv20 > 0.24 and (self.dd60 < -0.06 or self.px("QQQ", i) < 0.995 * self.sma("QQQ", 200, i))
        if not lv:
            for t in self.LONG_VOL:
                w.pop(t, None)
        self._cap_group(w, self.LONG_VOL, self.MAX_LONG_VOL)
        if "UVIX" in w:
            w["UVIX"] = min(w["UVIX"], 0.025)
        if "UVXY" in w:
            w["UVXY"] = min(w["UVXY"], 0.05)
        if not (self.regime == "ON" and self.rv20 <= 0.25 and self.dd60 > -0.05):
            for t in self.SHORT_VOL:
                w.pop(t, None)
        self._cap_group(w, self.SHORT_VOL, self.MAX_SHORT_VOL)
        w = self._net(w)
        for t in self.TRIPLE:
            if t in w:
                w[t] = min(w[t], self.MAX_3X)
        cl, cs = self.LONG_BUDGET[self.regime], self.SHORT_BUDGET[self.regime]
        proxy = self.TARGET_PROXY_VOL / max(self.rv20, 0.10)
        self._cap_beta(w, 1.0, min(cl, proxy))
        self._cap_beta(w, -1.0, min(cs, proxy))
        if self.guard_on and self.g_active:
            w = {t: x * (self.g_factor if (self.EQUITY_BETA.get(t, 0) > 0 or t in self.SHORT_VOL) else 1.0)
                 for t, x in w.items()}
        tot = sum(w.values())
        if tot > 1 - self.CASH_BUFFER:
            k = (1 - self.CASH_BUFFER) / tot
            w = {t: x * k for t, x in w.items()}
        return {t: x for t, x in w.items() if x > 1e-6}

    def _net(self, w):
        w = dict(w)
        for fam in self.FAMILY.values():
            lo = [t for t in fam if t in w and self.EQUITY_BETA[t] > 0]
            sh = [t for t in fam if t in w and self.EQUITY_BETA[t] < 0]
            gl = sum(w[t] * self.EQUITY_BETA[t] for t in lo)
            gs = sum(w[t] * abs(self.EQUITY_BETA[t]) for t in sh)
            m = min(gl, gs)
            if m <= 0:
                continue
            for t in lo:
                w[t] *= 1 - m / gl
            for t in sh:
                w[t] *= 1 - m / gs
        return w

    @staticmethod
    def _cap_group(w, tickers, mx):
        tot = sum(w.get(t, 0.0) for t in tickers)
        if tot > mx and tot > 0:
            for t in tickers:
                if t in w:
                    w[t] *= mx / tot

    def _cap_beta(self, w, direction, budget):
        sel = [t for t in w if self.EQUITY_BETA.get(t, 0.0) * direction > 0]
        g = sum(w[t] * abs(self.EQUITY_BETA[t]) for t in sel)
        if g > budget and g > 0:
            for t in sel:
                w[t] *= budget / g

    def plan(self, targets, label, i, book, nav, universe):
        band = self.SIGNAL_CHANGE_DRIFT if label != self.last_label else self.REBALANCE_DRIFT
        guard_changed = abs(self.g_factor - self.g_traded) > 1e-8
        out = {}
        for t in universe:
            now = book.pos.get(t, 0.0)
            tw = targets.get(t, 0.0)
            if not now and tw <= 0:
                continue
            if not self.can_trade(t, i):
                continue
            px = self.px(t, i)
            tgt = nav * tw / px
            delta = tgt - now
            if abs(delta) * px < 1e-9:
                continue
            close_all = now != 0 and tw <= 0
            cw = now * px / nav
            de_risk = self.regime == "OFF" and delta < 0 and now > 0
            guarded = self.EQUITY_BETA.get(t, 0.0) > 0 or t in self.SHORT_VOL
            gt = guard_changed and guarded and ((self.g_factor < self.g_traded and delta < 0) or
                                                (self.g_factor > self.g_traded and delta > 0))
            if not close_all and not de_risk and not gt and abs(tw - cw) < band:
                continue
            if not close_all and abs(delta) * px < self.MIN_TRADE_USD:
                continue
            out[t] = 0.0 if close_all else tgt
        self.g_traded = self.g_factor
        self.last_label = label
        return out

    def sleeves(self, i):
        rt10, rt11, rs2, rs3 = self.ready(i)
        return {"T10": self.t10(i) if rt10 else {}, "T11": self.t11(i) if rt11 else {},
                "S2": self.s2(i) if rs2 else {}, "S3": self.s3(i) if rs3 else {}}

    def decide(self, i, book, nav):
        has = self.qqq_risk(i)
        if not (has and self.ok(self.sma("QQQ", 200, i)) and all(self.ready(i))):
            return None
        self.update_regime(i)
        self.update_guard(nav)
        sl = self.sleeves(i)
        raw = {}
        for s in sl.values():
            for t, w in s.items():
                raw[t] = raw.get(t, 0.0) + w
        tgt = self.overlay(raw, i)
        label = self.regime + "|" + "|".join(k + ":" + ",".join("%s=%.5f" % (t, w) for t, w in sorted(s.items()))
                                             for k, s in sl.items())
        return self.plan(tgt, label, i, book, nav, self.SYMS)


# ============================================================================ E: frozen composite + ablations
class Composite(S785):
    """E (PREREGISTRATION.yaml): #785 regime detector; #788 momentum ranking (75% sleeve); one conditional
    pullback (25% sleeve: TQQQ when RSI10(TQQQ) < 30 and regime != OFF); #785-style aggregation, caps, bands;
    cash alternative (BIL for negative-momentum assets and the idle pullback sleeve)."""
    EQUITY_BETA = {"TQQQ": 3.0, "QQQ": 1.0, "XLE": 1.0, "GLD": 0.0, "BIL": 0.0}
    TRIPLE = {"TQQQ"}
    LONG_VOL, SHORT_VOL = set(), set()
    U = ("QQQ", "XLE", "GLD")
    MOM_BUDGET, PB_BUDGET = 0.75, 0.25

    def __init__(self, panel, regime=True, pullback=True, cash_alt=True, overlay=True, name="E0_composite"):
        super().__init__(panel, guard=False, name=name)
        self.use_regime, self.use_pb, self.use_cash, self.use_overlay = regime, pullback, cash_alt, overlay
        self.roc = {t: roc(panel.c[t], 60) for t in self.U}
        self.mom_w, self.mom_regime = None, None

    def regime_allows_dips(self):
        return (self.regime != "OFF") if self.use_regime else True

    def _momentum(self, i, reg):
        r = {t: self.roc[t][i] for t in self.U}
        if reg != "OFF":
            ranked = sorted(self.U, key=lambda t: r[t], reverse=True)
            w = dict(zip(ranked, (0.40, 0.35, 0.20)))
        else:
            lead, second = ("GLD", "XLE") if r["GLD"] >= r["XLE"] else ("XLE", "GLD")
            w = {lead: 0.40, second: 0.35, "QQQ": 0.20}
        w = {t: x * self.MOM_BUDGET / 0.95 for t, x in w.items()}
        if self.use_cash:
            for t in list(w):
                if r[t] <= 0:
                    w["BIL"] = w.get("BIL", 0.0) + w.pop(t)
        return w

    def decide(self, i, book, nav):
        has = self.qqq_risk(i)
        if not (has and self.ok(self.sma("QQQ", 200, i)) and self.ok(self.rsi("TQQQ", 10, i)) and
                all(self.ok(self.roc[t][i]) for t in self.U)):
            return None
        if self.use_regime:
            self.update_regime(i)
        else:
            self.regime = "ON"
        d = self.p.dates
        week_roll = d[i + 1].isocalendar()[1] != d[i].isocalendar()[1]
        if self.mom_w is None or week_roll or self.regime != self.mom_regime:
            self.mom_w, self.mom_regime = self._momentum(i, self.regime), self.regime
        raw = dict(self.mom_w)
        if self.use_pb:
            if self.regime_allows_dips() and self.rsi("TQQQ", 10, i) < 30:
                raw["TQQQ"] = raw.get("TQQQ", 0.0) + self.PB_BUDGET
            elif self.use_cash:
                raw["BIL"] = raw.get("BIL", 0.0) + self.PB_BUDGET
        universe = ("QQQ", "XLE", "GLD", "TQQQ", "BIL")
        if self.use_overlay:
            tgt = self.overlay(raw, i)
            label = self.regime + "|" + ",".join("%s=%.4f" % kv for kv in sorted(raw.items()))
            return self.plan(tgt, label, i, book, nav, universe)
        # E4: no overlay -- plain aggregation, LEAN-like 0.1% minimum order, no buffer, bands or caps
        out = {}
        for t in universe:
            tw = raw.get(t, 0.0)
            now = book.pos.get(t, 0.0)
            if not self.can_trade(t, i) or (not now and tw <= 0):
                continue
            tgt = nav * tw / self.px(t, i)
            if abs(tgt - now) * self.px(t, i) >= 0.001 * nav:
                out[t] = tgt
        return out


# ============================================================================ benchmarks
class BuyHold(Base):
    def __init__(self, panel, weights, name, monthly=True):
        super().__init__(panel)
        self.w, self.name, self.monthly, self.started = weights, name, monthly, False

    def decide(self, i, book, nav):
        d = self.p.dates
        if not self.started or (self.monthly and d[i + 1].month != d[i].month):
            self.started = True
            return set_holdings_orders(self, self.w, nav, i, book, free_pct=0.0, min_order_pct=0.001)
        return None


def all_strategies(panel):
    return [
        S788(panel), S60(panel), S504(panel), S785(panel), S785(panel, guard=True),
        Composite(panel), Composite(panel, regime=False, name="E1_no_regime"),
        Composite(panel, pullback=False, name="E2_no_pullback"),
        Composite(panel, cash_alt=False, name="E3_no_cash_alt"),
        Composite(panel, overlay=False, name="E4_no_overlay"),
    ]


def benchmarks(panel):
    return [
        BuyHold(panel, {"BIL": 1.0}, "BM_cash_BIL", monthly=False),
        BuyHold(panel, {"SPY": 1.0}, "BM_SPY", monthly=False),
        BuyHold(panel, {"SPY": 0.6, "IEF": 0.4}, "BM_60_40"),
        BuyHold(panel, {"QQQ": 1 / 3, "XLE": 1 / 3, "GLD": 1 / 3}, "BM_EW_QQQ_XLE_GLD"),
    ]
