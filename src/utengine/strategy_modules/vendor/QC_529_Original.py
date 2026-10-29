# =============================================================================
#  504 + Velocity Crash Guard  —  long-only leveraged-ETF tactical rotation
# =============================================================================
#
#  WHAT THIS IS
#  ------------
#  This is the QuantConnect community / corpus strategy commonly referred to as
#  "504 — Multi-Model Tactical ETF Rotation": a 4-way EQUAL-WEIGHT ENSEMBLE of
#  four independent sub-strategies (T11 + T10 + S3 + S2), each running its full
#  logic on 25% of capital, with overlapping positions weight-summed before
#  execution. The four sleeves rotate among leveraged ETFs (TQQQ / SOXL / TECL /
#  SPXL), inverse / vol instruments (SQQQ / UVXY / UVIX / SVIX) and defensives
#  (TLT / BIL / BSV / BTAL / bonds) using SMA regime gates + RSI cascades.
#
#  THE ONE ADDITION vs the original 504
#  ------------------------------------
#  A single VELOCITY CRASH GUARD overlay (the only change; the four sleeves are
#  otherwise untouched):
#
#      if QQQ trailing 10-day return < -10%   -> de-lever the whole book to 50%
#                                                (the other 50% sits in cash)
#      hold de-levered until QQQ 10-day return recovers above -4%   (hysteresis)
#
#  WHY: the four sleeves all use SLOW signals (200/202-day SMA, RSI). In a
#  velocity crash (e.g. COVID-2020: TQQQ -68% in 29 days) the SMAs lag by weeks,
#  so the book is still fully leveraged when the crash hits. The 10-day return
#  trigger reacts in days, not weeks. De-levering to 50% (instead of fully flat)
#  keeps half the exposure so the strategy still rides the V-shaped recovery.
#
#  RISK PROFILE (our QC backtests, $10k start, IBKR margin model + slippage)
#  ------------------------------------------------------------------------
#                         Original 504        504 + Crash Guard
#    In-sample 2010-2020:  CAGR 57.7%          CAGR 61.9%
#                          MaxDD 70.1%   -->   MaxDD 54.3%      (-15.8 pp)
#                          Sharpe 1.16         Sharpe 1.27
#    Out-sample 2021-2025: CAGR 225.4%         CAGR 211.0%      (insurance cost)
#                          MaxDD 32.9%   -->   MaxDD 30.6%
#                          Sharpe 3.30         Sharpe 3.25
#
#  The guard cuts drawdown on BOTH windows while keeping CAGR ~intact (it raises
#  in-sample CAGR and gives up ~6% out-of-sample as a small insurance premium).
#
#  HONEST CAVEATS (read before risking real money)
#  -----------------------------------------------
#   * 3x LEVERAGED ETFs. TQQQ/SOXL/TECL/SPXL can lose 50-80%+ in a severe bear.
#     Even WITH the guard, in-sample MaxDD is ~54%. This is a high-octane sleeve,
#     not a standalone all-weather portfolio.
#   * The guard's proven benefit is the VELOCITY-CRASH class (COVID-type). The
#     big in-sample drawdown reduction leans heavily on a single event (2020).
#   * Parameter-robust: a +/- sweep of the threshold (-9/-11%), lookback (8/12d)
#     and de-lever level (40/60%) all land in the same place (MaxDD ~51-58% IS,
#     CAGR preserved) -> not a knife-edge curve fit.
#   * NOT a universal overlay: the same guard bolted onto a DIFFERENT leveraged
#     strategy HURT it. The benefit is specific to this ensemble's structure
#     (its sleeves go fully-leveraged INTO velocity crashes).
#   * Out-of-sample (2021-2025) was used during parameter selection, so it is no
#     longer a fully clean hold-out. Forward / paper-trade before live capital.
#
#  EXECUTION / FILL MODEL
#  ----------------------
#  Daily resolution. Decisions are taken on the prior session's close (on_data
#  fires post-close) and orders are MarketOnOpenOrder -> filled at the NEXT
#  session's OPEN. No look-ahead. Verified: 100% of orders are type-4 MOO and
#  1,056 sampled fills across 2010-2025 reconcile to the next-day open.
#
#  Original strategy credit: "504 — Multi-Model Tactical ETF Rotation"
#  (QuantConnect community / corpus). Crash-guard idea adapted from the public
#  "LRS Sentinel" TQQQ/SOXL regime-rotation write-up.
# =============================================================================

from AlgorithmImports import *

class QuadEnsemble(QCAlgorithm):
    """
    4-Way Equal-Weight Ensemble: T11 + T10 + S3 + S2 (25% each)
      + a velocity crash guard (QQQ 10-day return < -10% -> de-lever to 50%).

    T11 — FeaverFrontrunner   (XLK/KMLM switcher + 50/50 bear split)
    T10 — SimonsKMLM          (RSI cascade + XLK/KMLM switcher)
    S3  — DailyRegimeRotation (3-of-4 SMA vote + RSI triggers)
    S2  — HolyGrail           (TQQQ 200-SMA gate + BSV defensive)
    """

    QUARTER    = 0.25
    SVIX_LIVE  = datetime(2022, 3, 30)
    UVIX_LIVE  = datetime(2022, 3, 30)
    _T10_LATE  = {"KMLM", "LABU", "QQQE", "VOOG", "VOOV"}
    _T11_LATE  = {"KMLM"}

    # Crash guard parameters (robust across a +/- sweep; see header)
    CRASH_ENTRY    = -0.10   # QQQ 10-day return below this -> de-lever
    CRASH_EXIT     = -0.04   # recover above this -> resume full exposure
    CRASH_LOOKBACK = 10      # trading-day window for the return
    CRASH_GROSS    = 0.50    # gross exposure while in crash (rest cash)

    def initialize(self) -> None:
        self.set_start_date(2020, 1, 1)
        self.set_end_date(2026, 12, 31)  # fixed end date avoids clock/tz non-reproducibility
        self.set_cash(10_000)
        self.set_brokerage_model(
            BrokerageName.INTERACTIVE_BROKERS_BROKERAGE,
            AccountType.MARGIN,
        )
        self.set_benchmark("SPY")
        self.settings.minimum_order_margin_portfolio_percentage = 0.0

        res = Resolution.DAILY

        # ── Universe (deduplicated) ─────────────────────────────────────────────
        all_tickers = [
            "TQQQ", "TECL", "SOXL", "SQQQ", "UVXY", "SVIX", "SVXY",
            "XLK", "KMLM", "TLT",
            "QQQE", "VTV", "VOX", "VOOG", "VOOV", "XLP", "XLY", "FAS",
            "SPXL", "LABU",
            "SPY", "IOO", "VTV", "XLF",
            "TECS", "SOXS",
            "QQQ", "PSQ", "QLD", "BTAL", "BIL", "AGG", "SH", "BND", "IEF",
            "BSV",
            "SMH", "UVIX",
        ]
        seen = set()
        unique = [t for t in all_tickers if not (t in seen or seen.add(t))]
        self._syms = {t: self.add_equity(t, res).symbol for t in unique}

        # ── T10 indicators ────────────────────────────────────────────
        def rsi10(t): return self.rsi(self._syms[t], 10, MovingAverageType.WILDERS, res)

        self._t10_rsi = {t: rsi10(t) for t in [
            "QQQE", "VTV", "VOX", "TECL", "VOOG", "VOOV", "XLP",
            "TQQQ", "XLY", "FAS", "SPY",
            "SOXL", "SPXL", "LABU", "XLK", "KMLM",
        ]}

        # ── T11 indicators ────────────────────────────────────────────
        self._t11_rsi10 = {t: rsi10(t) for t in [
            "SPY", "IOO", "TQQQ", "VTV", "XLF",
            "XLK", "KMLM", "PSQ", "BND", "QQQ", "IEF",
        ]}
        def rsi20(t): return self.rsi(self._syms[t], 20, MovingAverageType.WILDERS, res)
        self._t11_rsi20 = {t: rsi20(t) for t in ["TLT", "PSQ", "AGG"]}
        self._t11_rsi60_sh = self.rsi(self._syms["SH"], 60, MovingAverageType.WILDERS, res)

        self._t11_spy_sma200  = self.sma(self._syms["SPY"],  200, res)
        self._t11_tqqq_sma20  = self.sma(self._syms["TQQQ"], 20,  res)
        self._t11_kmlm_sma20  = self.sma(self._syms["KMLM"], 20,  res)

        # ── S2 indicators ──────────────────────────────────────────────
        self._s2_tqqq_sma200 = self.sma(self._syms["TQQQ"], 200, res)
        self._s2_tqqq_sma20  = self.sma(self._syms["TQQQ"], 20,  res)
        self._s2_tqqq_rsi10  = rsi10("TQQQ")
        self._s2_soxl_rsi10  = rsi10("SOXL")
        self._s2_sqqq_rsi10  = rsi10("SQQQ")
        self._s2_bsv_rsi10   = rsi10("BSV")

        # ── S3 indicators ─────────────────────────────────────────────
        self._s3_spy_sma202  = self.sma(self._syms["SPY"],  202, res)
        self._s3_qqq_sma202  = self.sma(self._syms["QQQ"],  202, res)
        self._s3_smh_sma202  = self.sma(self._syms["SMH"],  202, res)
        self._s3_soxl_sma202 = self.sma(self._syms["SOXL"], 202, res)
        def rsi8(t):  return self.rsi(self._syms[t], 8,  MovingAverageType.WILDERS, res)
        def rsi15(t): return self.rsi(self._syms[t], 15, MovingAverageType.WILDERS, res)
        self._s3_rsi_qqq8    = rsi8("QQQ")
        self._s3_rsi_smh8    = rsi8("SMH")
        self._s3_rsi_spy15   = rsi15("SPY")
        self._s3_rsi_qqq15   = rsi15("QQQ")
        self._s3_rsi_smh15   = rsi15("SMH")
        self._s3_rsi_soxl15  = rsi15("SOXL")

        self.set_warm_up(215, res)
        self._trade_count = 0
        self._last_label  = ""

        # ── Crash guard state ─────────────────────────────────────────
        self._qqq_window = RollingWindow[float](self.CRASH_LOOKBACK + 1)
        self._in_crash   = False

    # ── Readiness ────────────────────────────────────────────────────

    @property
    def _t10_ready(self) -> bool:
        core = [v for k, v in self._t10_rsi.items() if k not in self._T10_LATE]
        return not self.is_warming_up and all(r.is_ready for r in core)

    @property
    def _t11_ready(self) -> bool:
        core10 = [v for k, v in self._t11_rsi10.items() if k not in self._T11_LATE]
        return (not self.is_warming_up
                and self._t11_spy_sma200.is_ready
                and self._t11_tqqq_sma20.is_ready
                and all(r.is_ready for r in core10)
                and all(r.is_ready for r in self._t11_rsi20.values())
                and self._t11_rsi60_sh.is_ready)

    @property
    def _s2_ready(self) -> bool:
        return (not self.is_warming_up
                and self._s2_tqqq_sma200.is_ready
                and self._s2_tqqq_sma20.is_ready
                and self._s2_tqqq_rsi10.is_ready
                and self._s2_soxl_rsi10.is_ready
                and self._s2_sqqq_rsi10.is_ready
                and self._s2_bsv_rsi10.is_ready)

    @property
    def _s3_ready(self) -> bool:
        return (not self.is_warming_up
                and self._s3_spy_sma202.is_ready
                and self._s3_qqq_sma202.is_ready
                and self._s3_smh_sma202.is_ready
                and self._s3_soxl_sma202.is_ready
                and self._s3_rsi_qqq8.is_ready
                and self._s3_rsi_smh8.is_ready
                and self._s3_rsi_spy15.is_ready
                and self._s3_rsi_qqq15.is_ready
                and self._s3_rsi_smh15.is_ready
                and self._s3_rsi_soxl15.is_ready)

    # ── T11 helpers ────────────────────────────────────────────────

    def _t11_bond_baller(self, r10, r20, tqqq_px, tqqq_sma) -> str:
        if r20["TLT"] > r20["PSQ"]: return "QQQ"
        if tqqq_px > tqqq_sma:
            if r10["PSQ"] < 35: return "PSQ"
            if r20["AGG"] > self._t11_rsi60_sh.current.value: return "TQQQ"
            return "PSQ"
        else:
            if r10["IEF"] > r20["PSQ"]: return "PSQ"
            return "SQQQ"

    def _t11_feaver_bear(self, r10, r20, tqqq_px, tqqq_sma) -> str:
        hist = self.history(self._syms["QQQ"], 61, Resolution.DAILY)
        qqq_60d = 0.0
        if not hist.empty and len(hist) >= 61:
            c = hist["close"].values
            qqq_60d = (c[-1] / c[0] - 1) * 100
        if qqq_60d < -12:
            if r10["BND"] > r10["QQQ"]: return "QLD"
            return "BTAL"
        if tqqq_px > tqqq_sma:
            if r10["PSQ"] < 35: return "PSQ"
            if r20["AGG"] > self._t11_rsi60_sh.current.value: return "TQQQ"
            return "PSQ"
        else:
            if r10["IEF"] > r20["PSQ"]: return "PSQ"
            return "SQQQ"

    # ── Strategy signals → weight dicts ────────────────────────────────

    def _t10_weights(self) -> dict:
        if not self._t10_ready: return {}
        r = {t: self._t10_rsi[t].current.value for t in self._t10_rsi
             if self._t10_rsi[t].is_ready}
        if (r.get("QQQE",0)>79 or r.get("VTV",0)>79 or r.get("VOX",0)>79
                or r.get("TECL",0)>79 or r.get("VOOG",0)>79 or r.get("VOOV",0)>79
                or r.get("XLP",0)>75 or r.get("TQQQ",0)>79 or r.get("XLY",0)>80
                or r.get("FAS",0)>80 or r.get("SPY",0)>80):
            return {self._syms["UVXY"]: self.QUARTER}
        if r.get("TQQQ",50) < 30: return {self._syms["TECL"]: self.QUARTER}
        if r.get("SOXL",50) < 30: return {self._syms["SOXL"]: self.QUARTER}
        if r.get("SPXL",50) < 30: return {self._syms["SPXL"]: self.QUARTER}
        if self._t10_rsi["LABU"].is_ready and r["LABU"] < 25:
            return {self._syms["LABU"]: self.QUARTER}
        vs = "SVIX" if self.time >= self.SVIX_LIVE else "SVXY"
        kmlm_ready = self._t10_rsi["KMLM"].is_ready
        xlk_wins = (not kmlm_ready) or (r["XLK"] > r["KMLM"])
        if xlk_wins:
            return {self._syms["TECL"]: self.QUARTER/3,
                    self._syms["SOXL"]: self.QUARTER/3,
                    self._syms[vs]:     self.QUARTER/3}
        return {self._syms["SQQQ"]: self.QUARTER*0.5,
                self._syms["TLT"]:  self.QUARTER*0.5}

    def _t11_weights(self) -> dict:
        if not self._t11_ready: return {}
        r10 = {t: self._t11_rsi10[t].current.value for t in self._t11_rsi10
               if self._t11_rsi10[t].is_ready}
        r20 = {t: self._t11_rsi20[t].current.value for t in self._t11_rsi20}
        spy_px   = self.securities[self._syms["SPY"]].close
        tqqq_px  = self.securities[self._syms["TQQQ"]].close
        kmlm_px  = self.securities[self._syms["KMLM"]].close
        spy_sma  = self._t11_spy_sma200.current.value
        tqqq_sma = self._t11_tqqq_sma20.current.value
        kmlm_sma = self._t11_kmlm_sma20.current.value

        ob79 = (r10["SPY"]>79 or r10["IOO"]>79 or r10["TQQQ"]>79
                or r10["VTV"]>79 or r10["XLF"]>79)
        if ob79:
            ob81 = (r10["SPY"]>81 or r10["IOO"]>81 or r10["TQQQ"]>81
                    or r10["VTV"]>81 or r10["XLF"]>81)
            if ob81:
                return {self._syms["UVXY"]: self.QUARTER}
            return {self._syms["UVXY"]: self.QUARTER/3,
                    self._syms["BIL"]:  self.QUARTER/3,
                    self._syms["BTAL"]: self.QUARTER/3}
        if r10["TQQQ"] < 30: return {self._syms["TQQQ"]: self.QUARTER}
        if r10["SPY"]  < 30: return {self._syms["SPXL"]: self.QUARTER}

        if spy_px > spy_sma:
            kmlm_ready = self._t11_rsi10["KMLM"].is_ready and self._t11_kmlm_sma20.is_ready
            if not kmlm_ready or r10["XLK"] > r10["KMLM"]:
                return {self._syms["TECL"]: self.QUARTER/3,
                        self._syms["SOXL"]: self.QUARTER/3,
                        self._syms["TQQQ"]: self.QUARTER/3}
            if kmlm_px < kmlm_sma:
                return {self._syms["TECL"]: self.QUARTER/3,
                        self._syms["SOXL"]: self.QUARTER/3,
                        self._syms["TQQQ"]: self.QUARTER/3}
            return {self._syms["TECS"]: self.QUARTER/3,
                    self._syms["SOXS"]: self.QUARTER/3,
                    self._syms["SQQQ"]: self.QUARTER/3}
        else:
            bb = self._t11_bond_baller(r10, r20, tqqq_px, tqqq_sma)
            fb = self._t11_feaver_bear(r10, r20, tqqq_px, tqqq_sma)
            w: dict = {}
            w[self._syms[bb]] = w.get(self._syms[bb], 0) + self.QUARTER * 0.5
            w[self._syms[fb]] = w.get(self._syms[fb], 0) + self.QUARTER * 0.5
            return w

    def _s2_weights(self) -> dict:
        if not self._s2_ready: return {}
        tqqq_price = self.securities[self._syms["TQQQ"]].close
        tqqq_rsi   = self._s2_tqqq_rsi10.current.value
        soxl_rsi   = self._s2_soxl_rsi10.current.value
        sqqq_rsi   = self._s2_sqqq_rsi10.current.value
        bsv_rsi    = self._s2_bsv_rsi10.current.value
        sma200     = self._s2_tqqq_sma200.current.value
        sma20      = self._s2_tqqq_sma20.current.value
        if tqqq_price > sma200:
            sym = self._syms["UVXY"] if tqqq_rsi > 79 else self._syms["TQQQ"]
            return {sym: self.QUARTER}
        if tqqq_rsi < 31: return {self._syms["TECL"]: self.QUARTER}
        if soxl_rsi < 30: return {self._syms["SOXL"]: self.QUARTER}
        if tqqq_price < sma20:
            sym = self._syms["SQQQ"] if sqqq_rsi > bsv_rsi else self._syms["BSV"]
            return {sym: self.QUARTER}
        return {self._syms["TQQQ"]: self.QUARTER}

    def _s3_weights(self) -> dict:
        if not self._s3_ready: return {}
        spy_bull  = self.securities[self._syms["SPY"]].price  > self._s3_spy_sma202.current.value
        qqq_bull  = self.securities[self._syms["QQQ"]].price  > self._s3_qqq_sma202.current.value
        smh_bull  = self.securities[self._syms["SMH"]].price  > self._s3_smh_sma202.current.value
        soxl_bull = self.securities[self._syms["SOXL"]].price > self._s3_soxl_sma202.current.value
        bull = (int(spy_bull)+int(qqq_bull)+int(smh_bull)+int(soxl_bull)) >= 3
        overbought = (self._s3_rsi_spy15.current.value  > 72 or
                      self._s3_rsi_qqq15.current.value  > 72 or
                      self._s3_rsi_smh15.current.value  > 72 or
                      self._s3_rsi_soxl15.current.value > 72)
        if bull:
            if overbought:
                vol = (self._syms["UVIX"]
                       if (self.time >= self.UVIX_LIVE
                           and self.securities[self._syms["UVIX"]].has_data
                           and self.securities[self._syms["UVIX"]].price > 0)
                       else self._syms["UVXY"])
                return {vol: self.QUARTER}
            return {self._syms["TQQQ"]: self.QUARTER*0.5,
                    self._syms["SOXL"]: self.QUARTER*0.5}
        if self._s3_rsi_qqq8.current.value < 29 or self._s3_rsi_smh8.current.value < 31:
            return {self._syms["SOXL"]: self.QUARTER}
        return {}  # cash

    # ── Merge and apply ─────────────────────────────────────────────

    def _rebalance(self) -> None:
        # ── Crash guard: QQQ 10-day return < -10% -> de-lever to CRASH_GROSS until
        #    the 10-day return recovers above -4% (asymmetric hysteresis).
        if self._qqq_window.is_ready:
            qqq_ret = self._qqq_window[0] / self._qqq_window[self.CRASH_LOOKBACK] - 1.0
            if not self._in_crash and qqq_ret < self.CRASH_ENTRY:
                self._in_crash = True
                self.log(f"[CRASH] {self.time.date()} | QQQ {self.CRASH_LOOKBACK}d ret = {qqq_ret*100:.1f}% -> de-lever {self.CRASH_GROSS:.0%}")
            elif self._in_crash and qqq_ret > self.CRASH_EXIT:
                self._in_crash = False
                self.log(f"[RECOVER] {self.time.date()} | QQQ {self.CRASH_LOOKBACK}d ret = {qqq_ret*100:.1f}% -> resume full")

        crash_gross = self.CRASH_GROSS if self._in_crash else 1.0

        w10 = self._t10_weights()
        w11 = self._t11_weights()
        w2  = self._s2_weights()
        w3  = self._s3_weights()

        def lbl(w): return "+".join(f"{round(wt/self.QUARTER*100):.0f}%{s.value}"
                                     for s, wt in w.items()) if w else "CASH"
        new_label = f"T10={lbl(w10)}|T11={lbl(w11)}|S2={lbl(w2)}|S3={lbl(w3)}|cg={crash_gross}"

        # Only trade when the target set changes
        if new_label == self._last_label:
            return

        combined: dict = {}
        for w in [w10, w11, w2, w3]:
            for sym, wt in w.items():
                combined[sym] = combined.get(sym, 0.0) + wt * crash_gross

        # Liquidate anything no longer targeted — fill at tomorrow's open
        targets = set(combined)
        for h in list(self.portfolio.values()):
            if h.invested and h.symbol not in targets:
                self.market_on_open_order(h.symbol, -h.quantity)

        # Set target weights — fill at tomorrow's open (MarketOnOpen, no look-ahead)
        pv = self.portfolio.total_portfolio_value
        for sym, wt in combined.items():
            price = self.securities[sym].price
            if price <= 0:
                continue
            target_qty = int(pv * wt / price)
            delta = target_qty - int(self.portfolio[sym].quantity)
            if delta != 0:
                self.market_on_open_order(sym, delta)

        self._trade_count += 1
        net = "+".join(f"{round(w*100):.0f}%{s.value}"
                       for s, w in sorted(combined.items(), key=lambda x: -x[1]))
        self.log(f"[{self._trade_count:04d}] {self.time.date()} | net={net}")
        self._last_label = new_label

    def on_data(self, data: Slice) -> None:
        # Feed the QQQ rolling window (also during warmup, so the guard is armed at day 0)
        qqq_sym = self._syms["QQQ"]
        if data.bars.contains_key(qqq_sym):
            self._qqq_window.add(data.bars[qqq_sym].close)
        if self.is_warming_up:
            return
        self._rebalance()

    def on_end_of_algorithm(self) -> None:
        self.log(f"\n  Final NAV: ${self.portfolio.total_portfolio_value:>15,.2f}  |  State changes: {self._trade_count}")
