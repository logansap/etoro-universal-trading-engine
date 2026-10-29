"""Point-in-time daily replay engine for the four QuantConnect strategies and the frozen composite (2026-10-21).

VIRTUAL PORTFOLIOS, not LEAN orders. Every strategy decides after the close of session t using only closes <= t
and trades at the OPEN of session t+1 (the sources' own convention for #504/#785 market-on-open orders; for #60
LEAN's after-close market orders also fill at the next open; for #788 the source's Monday-10:00 market order on
daily data fills at the stale Friday close, which this engine replaces with the Monday open -- see AUDIT.md).

Data: Yahoo daily OHLC, split/dividend adjusted (tsr.data.fetch_yahoo, content-addressed snapshots pinned in
results/data_pins.json). Share quantities are fractional notional (adjusted prices are not traded prices).

Costs (identical for every strategy and benchmark):
  commission  IBKR Fixed-style: max(USD 1.00, 1 bp of notional) per order, capped at 1% of notional
              (USD 0.005/share is about 1 bp on a USD 50 ETF);
  spread      half-spread by instrument class (bp of notional): broad ETFs 1, leveraged ETFs 2,
              volatility ETPs and thin ETFs 5;
  financing   negative cash (margin) charged 5%/yr; idle positive cash earns 0 (as in LEAN).
  stress      costs x2 (reported separately).
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.dont_write_bytecode = True

HALF_SPREAD_BP = {
    **{t: 1.0 for t in ("SPY", "QQQ", "XLE", "GLD", "XLK", "XLF", "XLP", "XLY", "TLT", "IEF", "AGG", "BND", "BSV",
                        "BIL", "SH", "PSQ", "IOO", "VTV", "VOX", "SMH")},
    **{t: 2.0 for t in ("TQQQ", "SQQQ", "SOXL", "SOXS", "TECL", "TECS", "SPXL", "FAS", "LABU", "QLD")},
    **{t: 5.0 for t in ("UVXY", "SVXY", "SVIX", "UVIX", "QQQE", "VOOG", "VOOV", "KMLM", "BTAL")},
}
MARGIN_RATE = 0.05


# ----------------------------------------------------------------------------- data
class Panel:
    def __init__(self, frames: dict, end: str):
        cal = frames["SPY"].index
        cal = cal[cal <= pd.Timestamp(end)]
        self.dates = cal
        self.o, self.c, self.h, self.l = {}, {}, {}, {}
        for t, df in frames.items():
            d = df.reindex(cal)
            self.o[t], self.c[t] = d.open.to_numpy(float), d.close.to_numpy(float)
            self.h[t], self.l[t] = d.high.to_numpy(float), d.low.to_numpy(float)
        self.idx = {d: i for i, d in enumerate(cal)}
        self.n = len(cal)

    def has(self, t, i):
        return t in self.c and i >= 0 and np.isfinite(self.c[t][i])


# ----------------------------------------------------------------------------- indicators (causal, LEAN-style)
def sma(x, n):
    out = np.full(len(x), np.nan)
    s = pd.Series(x)
    v = s.rolling(n, min_periods=n).mean().to_numpy()
    out[:] = v
    return out


def rsi_wilder(x, n):
    """LEAN RelativeStrengthIndex(n, Wilders): Wilder averages seeded with the simple mean of the first n
    changes; ready after n+1 prices. NaN-aware: starts at the first finite price."""
    out = np.full(len(x), np.nan)
    f = np.where(np.isfinite(x))[0]
    if len(f) < n + 1:
        return out
    s = f[0]
    y = x[s:]
    d = np.diff(y)
    g, l_ = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    ag, al = g[:n].mean(), l_[:n].mean()
    for k in range(n, len(d) + 1):
        if k > n:
            ag = (ag * (n - 1) + g[k - 1]) / n
            al = (al * (n - 1) + l_[k - 1]) / n
        rsi = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
        out[s + k] = rsi
    return out


def roc(x, n):
    out = np.full(len(x), np.nan)
    out[n:] = (x[n:] / x[:-n] - 1.0) * 100.0
    return out


# ----------------------------------------------------------------------------- portfolio simulator
@dataclass
class Book:
    cash: float
    pos: dict = field(default_factory=dict)          # ticker -> units (fractional)
    cost_mult: float = 1.0
    fees: float = 0.0
    spread: float = 0.0
    financing: float = 0.0
    traded: float = 0.0
    fills: int = 0
    lots: dict = field(default_factory=dict)         # ticker -> [units, cost basis] for round-trip stats
    trades: list = field(default_factory=list)       # closed round trips: (ticker, pnl, entry_date, exit_date)

    def value(self, prices: dict) -> float:
        return self.cash + sum(u * prices[t] for t, u in self.pos.items() if u)

    def fill(self, t, units, px, date):
        if units == 0 or not np.isfinite(px) or px <= 0:
            return
        notional = abs(units) * px
        comm = min(max(1.0, 1e-4 * notional), 0.01 * notional) * self.cost_mult
        spr = notional * HALF_SPREAD_BP.get(t, 2.0) / 1e4 * self.cost_mult
        self.cash -= units * px + comm + spr
        self.fees += comm
        self.spread += spr
        self.traded += notional
        self.fills += 1
        u0 = self.pos.get(t, 0.0)
        u1 = u0 + units
        lot = self.lots.get(t, [0.0, 0.0, date])
        if units > 0:
            lot = [lot[0] + units, lot[1] + units * px + comm + spr, lot[2] if lot[0] > 0 else date]
        else:
            frac = min(1.0, -units / lot[0]) if lot[0] > 0 else 1.0
            basis = lot[1] * frac
            self.trades.append((t, -units * px - comm - spr - basis, lot[2], date))
            lot = [lot[0] * (1 - frac), lot[1] * (1 - frac), lot[2]]
        self.lots[t] = lot
        self.pos[t] = u1 if abs(u1) > 1e-12 else 0.0


def run(panel: Panel, strategy, start: str, cash: float, cost_mult=1.0, fill_at="open", delay=1):
    """Daily loop. strategy.decide(i, book, nav) after the close of day i returns {ticker: target units} for the
    tickers it wants to change (or None). Orders fill at the open of day i+1 (fill_at='close' fills at the same
    close: the impossible stale-price convention, used only to quantify #788's source behaviour). delay=2 fills
    them at the open of day i+2 instead (one extra session of execution lag; a robustness diagnostic)."""
    i0 = panel.idx[panel.dates[panel.dates >= pd.Timestamp(start)][0]]
    book = Book(cash=cash, cost_mult=cost_mult)
    navs, dates, weights = [], [], []
    queue = {}                                   # fill day index -> orders
    for i in range(i0, panel.n):
        pending = queue.pop(i, None)
        if pending:
            for t, units in sorted(pending.items(), key=lambda kv: kv[1] - book.pos.get(kv[0], 0.0)):
                d = units - book.pos.get(t, 0.0)
                book.fill(t, d, panel.o[t][i], panel.dates[i])
        px = {t: panel.c[t][i] for t in book.pos if book.pos[t]}
        for t, p in px.items():
            if not np.isfinite(p):          # no bar today: carry last finite close
                j = i
                while j > 0 and not np.isfinite(panel.c[t][j]):
                    j -= 1
                px[t] = panel.c[t][j]
        if book.cash < 0:
            fin = -book.cash * MARGIN_RATE / 252
            book.cash -= fin
            book.financing += fin
        nav = book.value(px)
        navs.append(nav)
        dates.append(panel.dates[i])
        weights.append({t: book.pos[t] * px[t] / nav for t in px} if nav > 0 else {})
        if i + 1 < panel.n:
            orders = strategy.decide(i, book, nav)
            if orders:
                if fill_at == "close":
                    for t, units in sorted(orders.items(), key=lambda kv: kv[1] - book.pos.get(kv[0], 0.0)):
                        book.fill(t, units - book.pos.get(t, 0.0), panel.c[t][i], panel.dates[i])
                elif i + delay < panel.n:
                    queue[i + delay] = orders
    return pd.Series(navs, index=pd.DatetimeIndex(dates)), book, weights


# ----------------------------------------------------------------------------- helpers shared by strategies
class Base:
    name = "base"

    def __init__(self, panel: Panel):
        self.p = panel
        self._rsi, self._sma = {}, {}

    def rsi(self, t, n, i):
        k = (t, n)
        if k not in self._rsi:
            self._rsi[k] = rsi_wilder(self.p.c[t], n)
        return self._rsi[k][i]

    def sma(self, t, n, i):
        k = (t, n)
        if k not in self._sma:
            self._sma[k] = sma(self.p.c[t], n)
        return self._sma[k][i]

    def px(self, t, i):
        return self.p.c[t][i]

    @staticmethod
    def ok(v):
        return v is not None and np.isfinite(v)

    def units_for(self, weights: dict, nav: float, i: int, book: Book, liquidate_others=True):
        """Target units from target weights at today's close (as the sources size their orders)."""
        out = {}
        for t, w in weights.items():
            if self.p.has(t, i):
                out[t] = nav * w / self.p.c[t][i]
        if liquidate_others:
            for t, u in book.pos.items():
                if u and t not in out:
                    out[t] = 0.0
        return out


def set_holdings_orders(strat: Base, weights: dict, nav: float, i: int, book: Book, free_pct: float,
                        min_order_pct=0.001, liquidate_others=False):
    """LEAN SetHoldings semantics: target value = weight x NAV x (1 - FreePortfolioValuePercentage); an order is
    skipped when its value is below MinimumOrderMarginPortfolioPercentage (0.1%) of NAV."""
    out = {}
    for t, w in weights.items():
        tgt = nav * (1 - free_pct) * w / strat.p.c[t][i]
        if abs(tgt - book.pos.get(t, 0.0)) * strat.p.c[t][i] >= min_order_pct * nav:
            out[t] = tgt
    if liquidate_others:
        for t, u in book.pos.items():
            if u and t not in weights:
                out[t] = 0.0
    return out


# ----------------------------------------------------------------------------- metrics
def metrics(nav: pd.Series, book: Book, weights: list, bil: pd.Series, beta: dict, start_cash: float) -> dict:
    r = nav.pct_change().dropna()
    yrs = len(r) / 252
    rb = bil.reindex(nav.index).pct_change().reindex(r.index).fillna(0.0)
    ex = r - rb
    eq = nav / nav.iloc[0]
    dd = eq / eq.cummax() - 1
    mo = (1 + r).resample("ME").prod() - 1
    lr = np.log1p(r)
    top10 = lr.nlargest(10).sum()
    gross = book.fees + book.spread + book.financing
    w_df = pd.DataFrame(weights, index=nav.index).fillna(0.0)
    lev = (w_df.abs() * pd.Series({t: abs(beta.get(t, 1.0)) for t in w_df.columns})).sum(axis=1)
    maxw = w_df.abs().max(axis=1)
    tr = book.trades
    wins = [p for _, p, *_ in tr if p > 0]
    loss = [p for _, p, *_ in tr if p <= 0]
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if eq.iloc[-1] > 0 else -1.0
    return dict(
        start=str(nav.index[0].date()), end=str(nav.index[-1].date()), start_cash=start_cash,
        end_value=float(nav.iloc[-1]), net_total=float(eq.iloc[-1] - 1), cagr=float(cagr),
        gross_total=float((nav.iloc[-1] + gross) / nav.iloc[0] - 1),
        vol=float(r.std() * math.sqrt(252)),
        sharpe=float(ex.mean() / ex.std() * math.sqrt(252)) if ex.std() > 0 else float("nan"),
        maxdd=float(dd.min()), calmar=float(cagr / abs(dd.min())) if dd.min() < 0 else float("nan"),
        worst_day=float(r.min()), worst_month=float(mo.min()), cvar5_day=float(r[r <= r.quantile(0.05)].mean()),
        fees=book.fees, spread=book.spread, financing=book.financing,
        cost_pct_of_start=float(gross / start_cash),
        turnover_ann=float(book.traded / nav.mean() / yrs), fills=book.fills, round_trips=len(tr),
        win_rate=float(len(wins) / len(tr)) if tr else float("nan"),
        avg_win=float(np.mean(wins)) if wins else float("nan"), avg_loss=float(np.mean(loss)) if loss else float("nan"),
        expectancy_usd=float(np.mean([p for _, p, *_ in tr])) if tr else float("nan"),
        avg_beta_exposure=float(lev.mean()), max_beta_exposure=float(lev.max()),
        avg_max_weight=float(maxw.mean()), time_in_market=float((w_df.abs().sum(axis=1) > 0.01).mean()),
        cagr_ex_top10_days=float(np.exp(lr.sum() - top10) ** (1 / yrs) - 1),
        top10_days_share_of_log_growth=float(top10 / lr.sum()) if lr.sum() > 0 else float("nan"),
        yearly={str(k.year): float(v) for k, v in ((1 + r).resample("YE").prod() - 1).items()},
    )
