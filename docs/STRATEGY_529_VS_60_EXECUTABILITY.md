# Strategy executability on this eToro account: #529 vs #60-SQQQ (2026-10-29)

**Ground rules:**
- No strategy was optimized and no instrument was invented.
- No ETF or CFD was silently substituted. The only substitution anywhere is #60's owner-approved, frozen
  TECS→SQQQ rule, which is declared in its YAML.
- Instrument status comes from [ETORO_INSTRUMENT_ELIGIBILITY_2026-10-29.md](ETORO_INSTRUMENT_ELIGIBILITY_2026-10-29.md).
- Historical numbers are **research replays** (virtual portfolios, not LEAN, not live). Sources:
  `D:\Trading-Strategy-Research\research\consistency_championship` (2016-01-04 to 2026-10-21; the eToro version
  keeps the API-verified set ELIG-1 and holds the rest in cash) and `research\strategy_60_final\TECS_REPLACEMENT_STUDY.md`.

## The three candidates

| | A. #529 original | B. #529 eToro-restricted | C. #60-SQQQ |
|---|---|---|---|
| Engine strategy | `QC_529` ([strategies/529.yaml](../strategies/529.yaml)) | **`QC_529_ETORO_RESTRICTED` v1.0.0** ([strategies/529_etoro_restricted.yaml](../strategies/529_etoro_restricted.yaml), new; identical frozen rules, own id; test `tests/test_variant_529_etoro.py`) | `QC_60_SQQQ` ([strategies/60.yaml](../strategies/60.yaml)) |
| Rules | frozen #529 (hash-pinned port) | the same; unavailable weight held in **cash** | frozen #60 tree; final target TECS → SQQQ at the same dollar weight |
| Target instruments | 19 | 19 (11 executable) | 6 after substitution (5 executable) |
| Not executable on this account | **8**: SOXL LABU SVXY BSV (UNVERIFIED) and SVIX UVIX (UNVERIFIED, owner sees unavailable); BTAL TECS (UNAVAILABLE) | same 8 → cash | **1**: BSV (UNVERIFIED) → cash. TECS is never traded (SQQQ instead) |
| Share of historical weight executable | **69.8%** (2016-26) | 69.8% | about **91% before** substitution; TECS weight (166 sessions in the study window) becomes SQQQ, BSV stays cash (25 of 537 sessions in 2024-26 selected BSV) |
| Research CAGR / max drawdown | 183.9% / -54.5% (all instruments) | **107.5% / -43.2%** (eToro-restricted) | **117.7% / -53.8%** (eToro base costs, TECS→SQQQ; original 117.6% / -53.7%; TECS→cash 104.5%) |
| Activity | 5,639 fills in 10 years (about 2.2 per session), up to about 10 instruments at once; turnover about 76x a year | about the same order flow on 11 instruments | **852 fills** in the study window, one instrument at a time; turnover about 31x a year |
| Order types needed | opens, full closes, **frequent partial reductions** | same | mostly a full close followed by one open per switch, plus small top-ups |
| Exposure to blocker B3 (lost close response) | high (many closes and trims) | high | lower, but still one close per switch |
| Post-publication (unexposed) | +0.7% in 102 sessions (IBKR model), the 5th percentile of its history; eToro-eligible +9.9% | — | +44.0% (IBKR) / +23.9% (eToro-eligible) in 203 sessions; the 21st percentile |
| Forward paper (2026-10-22 to 10-27, 4 sessions) | P529-ORIG +10.28% (**identical to P504**: the crash guard was never active) | P529-ETORO +9.32%. **Not variant B**: the paper eToro track uses owner-observed eligibility and held SOXL at 22% | X60SQQQ-ETORO +5.92% (0 substitutions; entered one session later) |

## Can it actually execute (once the production gates are resolved)?

**A. #529 original: NO.**
- 8 of 19 targets cannot be bought on this account.
- Running A here would silently become B, because the engine keeps blocked weight in cash. A is therefore never a
  live candidate on eToro (`live_eligible: false`).

**B. #529 eToro-restricted: technically YES, but it is a different strategy.**
- About 30% of the original's weight sits in cash.
- The historical return roughly halves (184% → 107% CAGR in the replay). The drawdown is shallower mainly because
  less capital is invested.
- It needs multi-leg rebalances with many small partial closes. At USD 2,000 a 1/12-sleeve leg is about USD 167, so
  many adjustments fall below the USD 10 minimum and are skipped (drift).
- It is the most exposed to the unresolved close-recovery blocker (B3).
- Its forward record is **not** being measured: P529-ETORO is not API-strict (it holds SOXL).

**C. #60-SQQQ: YES, the most realistic first deployment.**
- 5 of its 6 targets are API-verified.
- The substitution preserves the historical profile (117.7% vs 117.6% CAGR).
- It holds one instrument at a time, has the fewest orders, and has the simplest reconciliation.
- Its residual divergence is BSV going to cash in the bear-branch argmax, about 5% of recent sessions.
- The TECS→SQQQ rule has **no forward evidence yet** (0 substitutions).

**Recommendation for the first owner-approved activation: C (#60-SQQQ)**, with B as the next candidate.

B becomes stronger only if:
1. SOXL (the largest blocked weight) and the other UNVERIFIED instruments are API-verified;
2. B gets its own API-strict forward paper record;
3. blocker B3 is resolved.

A is not deployable on this account.

None of this is authorization to go live: [../GO_LIVE_DECISION.md](../GO_LIVE_DECISION.md) is **NO-GO**.

## What would be traded (C, today's decision as an example)

- **The 2026-10-27 decision for #60:** TQQQ 100% (bull regime, no overbought). The paper books hold TQQQ.
- **On eToro (after the gates):** one x1-long CFD on TQQQ (id 4465) of 99.75% of strategy equity (LEAN's 0.25%
  buffer), with no stop. The engine's risk caps are max instrument weight 1.0, x1 only, and a USD 25,000 order cap.
