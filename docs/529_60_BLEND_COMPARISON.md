# #529 eToro-restricted vs #60-SQQQ vs a 50/50 blend

**Bottom line:**
- **In this replay, the 50/50 blend does not improve risk-adjusted performance compared with #529-restricted alone.**
  #529-restricted has the highest Sharpe and Sortino and the shallowest drawdown.
- The blend improves on #60-SQQQ alone: lower volatility, a shallower drawdown and a higher Sharpe, at a lower CAGR.
- Restoring the 50/50 split every quarter beats the static split on every risk measure, at higher cost.
- The two strategies are **not** independent: daily correlation is 0.70.
- **None of this was optimized.** The 50/50 weights were fixed by the owner before any number was computed.

## Method (one synchronized replay)

- **Code:** `D:\Trading-Strategy-Research\research\blend_529_60\run_blend.py` writes `data/metrics.json` and
  `equity_daily.csv`; `forward_paper.py` writes `forward_paper.json`.
- **Identical for every portfolio:**
  - dates: 2016-01-04 to 2026-10-21, the preregistered consistency window;
  - USD 10,000 initial capital;
  - data: the pinned, adjusted bars of `research/687_fix_or_replace`, so corporate actions are treated identically;
  - fills: decide after the close of t, fill at the open of t+1;
  - eToro costs: zero commission, half the observed eToro spread plus 5 bp slippage;
  - x1 long, no margin, no financing;
  - the same eToro restriction: only the 11 API-verified instruments are tradable, and any other weight is cash.
- **#529-restricted** = the X4 #529 port under that restriction, i.e. the engine's `QC_529_ETORO_RESTRICTED` rules.
- **#60-SQQQ** = the X4 #60 port with the owner's frozen TECS→SQQQ rule; BSV is unverified, so cash.
- **Static 50/50:** two independent USD 5,000 sleeves, never rebalanced against each other.
- **Quarterly 50/50** (a separately labelled policy):
  - after the last close of each quarter, the richer sleeve transfers the excess to the other;
  - if the donor lacks cash, it sells pro rata at that close and pays the same costs;
  - 43 transfers in all.
- **Parity:** the lock-step loop reproduces the reference `run_etoro` exactly for each strategy alone (maximum
  difference USD 0.00).
- **Coverage of 2011: not available.**
  - #60 needs UVXY (first bar 2011-10-04).
  - #529 needs KMLM (2020-12), SVIX and UVIX (2022-03) and QQQE (2012-03).
  - So 2011 cannot be replayed faithfully; it is reported as n/a, not approximated.

## Results (2016-01-04 to 2026-10-21; Sharpe and Sortino with risk-free 0)

| | A: #529-restricted | B: #60-SQQQ | C: 50/50 static | C: 50/50 quarterly |
|---|---|---|---|---|
| CAGR | 107.9% | 131.3% | 122.5% | 123.5% |
| Max drawdown | **−43.2%** (2020-02-21 → 03-18) | −53.6% (2018-08-29 → 12-24) | −47.0% (2018) | −45.6% (2018) |
| Recovery from max drawdown | 85 days | 105 days | 102 days | 100 days |
| Longest time under water | 211 days | 239 days | 238 days | 221 days |
| Sharpe / Sortino | **2.01 / 3.62** | 1.57 / 2.57 | 1.68 / 2.76 | 1.85 / 3.11 |
| Annual volatility | 40.4% | 67.9% | 57.2% | 50.3% |
| Worst month | −18.8% (2018-12) | −26.6% (2019-05) | −22.7% (2018-12) | −22.1% (2018-12) |
| Worst day / 1% VaR / 1% CVaR (daily) | −15.5% / −5.9% / −7.7% | −26.9% / −10.3% / −13.0% | −24.2% / −8.6% / −11.0% | −23.2% / −7.7% / −10.0% |
| Transaction costs (% of average NAV per year) | 4.1% | 2.1% | 2.6% | 3.1% |
| Turnover (× NAV per year) | 53 | 28 | 34 | 41 |
| Cash utilization (average gross / NAV) | 67% | 96% | 87% | 82% |
| 2011 | n/a | n/a | n/a | n/a |
| 2018 (return / max drawdown) | +28.1% / −37.9% | −1.7% / −53.6% | +10.4% / −47.0% | +13.3% / −45.6% |
| 2020 | +371% / −43.2% | +1,369% / −39.1% | +978% / −35.2% | +779% / −33.7% |
| 2022 | +161% / −21.6% | +86% / −45.0% | +98% / −41.3% | +122% / −34.3% |

## Dependence (do not assume independence)

| Measure | Value |
|---|---|
| Daily return correlation (Pearson / Spearman) | **0.70 / 0.73** |
| Monthly correlation | 0.66 |
| Correlation on SPY's worst 5% of days | 0.59 (mean on those days: A −0.35%, B −2.0%) |
| Rolling 252-day correlation | 0.46 to 0.94 (median 0.74) |
| P(B down \| A down) | 78% |
| Share of days with both beyond −20% drawdown | 4.3% (A alone 5.0%, B alone 18.6%) |

**Both are leveraged-ETF momentum and rotation systems on the same NASDAQ and tech complex** (TQQQ, TECL, SQQQ). The
blend diversifies mainly because #529 holds about 33% cash and uses a crash guard. It is not diversification
between unrelated sources of return.

## How to read this

1. **The numbers are exposed, in-sample history**, not a forecast. The ports were built after the strategies were
   published, and compounding to millions of dollars from USD 10,000 has no capacity limits. Read the percentages,
   not the dollars.
2. **#529's historical advantage did not hold after publication.** In the earlier post-publication validation, #529
   returned +0.7% in 102 sessions (IBKR model), or +9.9% eToro-eligible, which is the 5th percentile of its own
   history. #60 returned +23.9% eToro-eligible in 203 sessions (21st percentile).
3. **The forward paper record cannot settle it.** The shared sessions are 2026-10-23 to 10-28, i.e. 3 daily returns.
   In that window: P529-ETORO +12.0%, X60SQQQ-ETORO +7.9%, static 50/50 of the two +10.0%.
   - P529-ETORO uses the owner-observed eligibility ELIG-2 and holds SOXL, so **it is not variant B**.
   - The replay ends before the paper start, so replay-to-forward parity cannot be measured.
4. **Costs and operations:**
   - #529 has twice #60's turnover and 4.1% annual cost drag.
   - The quarterly policy adds about 0.5 percentage points of cost per year.
   - In the engine, internal crossing removes part of that cost.
   - #529's many partial closes are the most exposed to the unresolved lost-close blocker (B3).

**Answer to "does 50/50 improve risk-adjusted performance?"**
- **Compared with #60-SQQQ alone: yes.** Sharpe 1.68 vs 1.57 static (1.85 quarterly), drawdown −47% vs −54%,
  volatility 57% vs 68%.
- **Compared with #529-restricted alone: no.** Sharpe 2.01 and drawdown −43% are better than either blend.

On this evidence, a 50/50 split trades some of #529's historical risk-adjusted quality for #60's historical return.
Whether that is worth it is the owner's choice. **The engine supports any fixed weights without code changes.**
