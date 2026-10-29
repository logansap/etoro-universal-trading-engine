# Multi-strategy architecture: several strategies on one eToro account

> **Status:** implemented and demonstrated in **simulation**; it runs in **shadow** with no write capability. Live
> trading stays disabled (see [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)). Single-strategy mode is unchanged
> and remains the default (`config/engine.yaml`).

## Configuration

`config/engine.multi.yaml` holds the owner's target:

```yaml
portfolio:
  mode: multi_strategy
  sleeves:
    - strategy: QC_529_ETORO_RESTRICTED
      weight: 0.50
    - strategy: QC_60_SQQQ
      weight: 0.50
  funding_policy: proportional
  rebalance_policy: strategy_independent
  aggregate_risk: enforced
  internal_crossing: true
```

**Schema checks:**
- Weights must be greater than 0 and sum to exactly 1.
- Strategies must be distinct and must each have an enabled, fingerprinted YAML.
- `live` remains refused.

**Weights are fixed a priori and are never optimized.** A sleeve's 50% is capital, not exposure. Weight that the
strategy leaves in cash, or that falls on an ineligible instrument (for #529: SOXL, LABU, SVXY, BSV, SVIX, UVIX,
BTAL, TECS), stays in **that sleeve's cash**.

**The database is the authority.**
- At the first start, the sleeves, weights, YAML fingerprints and module hashes are recorded in the database (`engine_state.portfolio`), as the single-strategy engine already does for its active strategy.
- Editing the YAML afterwards changes nothing. `portfolio status` reports the difference.
- Weights change only through the audited `portfolio reallocate-preview` / `reallocate` commands.

**Adding or removing a sleeve** means a new portfolio activation, i.e. a new state directory or a future migration
command. **The execution and broker layers do not change:** they see only legs with sleeve attribution.

## Components

| Concern | Module | Notes |
|---|---|---|
| Strategy logic | `strategy_modules/*` (unchanged, hash-pinned) | Each sleeve's module decides from its own state; it never sees the broker or the other sleeve |
| Scheduling | `core/portfolio_engine.cycle_portfolio` | Each sleeve uses its own schedule. Sleeves whose execution window is open form one plan per session |
| Virtual books | `core/sleeves.py` | Append-only ledger of cash and units per sleeve, broker marks, reservations, daily P&L/TWR, per-sleeve limits |
| Shared planner | `core/multi_planner.py` | Per-sleeve wants → buying power → internal crossing → broker netting → legs with attribution |
| Risk | `core/risk.evaluate` on the aggregate | Account gross and instrument limits on the **combined** target; per-sleeve gross from the strategy YAML |
| Execution | `core/execution.Executor` (shared) | Persist-before-submit, sell-then-buy, never resubmit. New hooks: per-sleeve phase-2 resize, and a fill hook that posts fills to sleeves |
| Reconciliation | `core/reconciliation` + `TradingEngine._reconcile_sleeves` | First the account against the broker, then the sleeves against the account |
| Takeover | `core/takeover.py` | Separate, approval-gated ([TAKEOVER_STATE_MACHINE.md](TAKEOVER_STATE_MACHINE.md)) |

## One session, end to end

1. **Reconcile the account** (broker authoritative). Then attribute new cash flows to sleeves and check the sleeve
   invariants. A breach is `SLEEVE_LEDGER_MISMATCH`, which halts everything.
2. **Decide (16:50 New York).** Each eligible sleeve decides for the completed session. A decision is persisted per
   sleeve (`decisions(strategy_id, session)`), and so is each sleeve's state. After the decisions, account and
   sleeve daily records are written.
3. **Execute (from 09:31).** For the sleeves whose window is open and whose decision is `DECIDED` and rebalancing:
   1. A sleeve is **excluded** (it holds) if it has a sleeve halt, configuration drift, no decision, or its strategy
      did not ask to rebalance.
   2. Each remaining sleeve's targets are `allocate(decision, spec, sleeve equity)`, with eligibility, cash buffer
      and the strategy's own minimum-order rule.
   3. **Planner.** A sleeve can only sell **its own units** and spend **its own cash** plus its own sale proceeds.
      Opposite trades in the same instrument are **crossed** at the broker mark. The residual is netted into at most
      one direction per instrument at the broker.
   4. **Aggregate risk** runs on the combined broker targets: halts, staleness, market open, gross, per-instrument
      weight, x1 long, eligibility, quotes, single-order cap, buying power.
   5. **Crossings are posted** only if the plan passed risk and no NEW_EXPOSURE halt is active. Then the broker legs
      run: closes, a clean reconciliation, opens re-sized to each sleeve's cash and the broker's available cash.
   6. Reconcile; mark the intents reconciled; record `sleeve_planned:<session>:<sleeve>`, so a sleeve is never
      planned twice in a session.

## Shared instruments (the TQQQ case)

On 2026-10-27 in simulation:
- #529-restricted targets TQQQ about 46%, TECL, SQQQ and TLT;
- #60-SQQQ targets TQQQ 99.75%.

**Result:** one broker OPEN for TQQQ of USD 1,455 with attribution ((#529, 458.15), (#60, 997.10)). Sleeves own units
(6.70 and 14.57); the broker holds 21.27 units. Each sleeve's value is its units × the broker's mark. Both
invariants hold: the sums equal the broker's numbers.

## Netting and crossing

When #529 sells TQQQ while #60 buys TQQQ in the same plan:
1. The overlap moves between the sleeves at the broker mark, as `CROSS_OUT` / `CROSS_IN` ledger entries. There is no
   broker order and no spread.
2. Only the residual reaches the broker.
3. Crossing is equity-neutral: the units and cash entries each sum to zero (test
   `test_opposite_trades_cross_internally_and_net_at_the_broker`).
4. Under a NEW_EXPOSURE halt, crossings are skipped, because a crossing is new exposure for the buying sleeve.

## Order constraints

| Constraint | Handling |
|---|---|
| eToro minimum USD 10 per open | A net open below 10 is not traded (note) |
| Partial close ≥ USD 9.90 and leaving ≥ USD 10 | If impossible on any position, the residual sale stays with the selling sleeve as drift. **It is never rounded up into a full close that would sell another sleeve's units** (`test_partial_close_minimums_never_spill_into_another_sleeve`) |
| Fractional units | Units to 6 decimals at the broker, 9 in the ledger; cash in cents for orders |
| Per-sleeve minimum order | `max(min_order_usd, risk.min_order_usd, min_order_pct_nav × sleeve equity)`, the strategy's own LEAN rule |
| Rounding | `sleeves.split` assigns the remainder to the last sleeve, so every split sums exactly |

## Failure handling

| Event | Behaviour |
|---|---|
| Partial fill | Each fill posts its new positions to sleeves, idempotent by position id |
| Broker rejection | Not retried. Sell-then-buy: a rejected close stops the opens |
| Lost acknowledgement (open) | Recovered with `orders:lookup?referenceId` (eToro's documented handle). Never resubmitted |
| Lost close response | The units leave the sleeves from position evidence, but the proceeds are unknown. This raises `CLOSE_PROCEEDS_UNKNOWN` + `CASH_DISCREPANCY` (halt ALL). An audited `account resolve --classify TRADE_CLOSE --intent` books the **observed** amount. This is still live blocker B3 |
| Crash / restart | Intents are resolved from broker evidence before reconciliation. Plan ids are deterministic. Each sleeve is planned at most once per session |
| External interference | Account-level halts (EXTERNAL_POSITION / EXTERNAL_CHANGE / POSITION_MISMATCH) stop every sleeve |
| Sleeve-specific problem | A sleeve halt (`halts.sleeve`) stops only that sleeve: configuration drift, per-sleeve daily loss or drawdown, or an operator hold |

**Exactly-once is not claimed.** The guarantee is at most one submission per intent, with unknown outcomes halting,
as in [ORDER_LIFECYCLE.md](ORDER_LIFECYCLE.md).

## Defects found by the 50/50 container rehearsal (fixed, regression-tested)

1. **Per-sleeve daily-loss check.** It compared against a daily record older than the latest one, so a 50/50 → 30/70
   reallocation looked like a −40% "loss" and halted the donor sleeve. It now uses the sleeve's latest record and the
   flows posted after it (`test_reallocation_is_not_a_daily_loss`).
2. **Broker cash cap for opens.** When one sleeve's cash is negative, the other sleeve's buys could exceed the broker's
   available cash, and risk blocked them outright. The planner now caps the net opens at the broker's available cash
   (minus suspense) plus the plan's own sale proceeds (`test_opens_are_capped_by_broker_cash_when_a_sleeve_is_negative`).
3. **Audit record of an aborted run.** `run --once` recorded a clean stop even when its cycle died. It now records
   `aborted: <error>` (`test_aborted_run_once_is_audited_as_aborted`).

## Operator commands

```bash
trading-engine --config config/engine.multi.yaml portfolio status
```
```bash
trading-engine --config config/engine.multi.yaml portfolio reallocate-preview --weights QC_529_ETORO_RESTRICTED=0.6,QC_60_SQQQ=0.4
```
```bash
trading-engine --config config/engine.multi.yaml portfolio reallocate --preview-hash <hash> --approved-by <owner>
```
```bash
trading-engine --config config/engine.multi.yaml exceptions
```

`report` includes a `portfolio` section in multi-strategy mode. `strategy preview-switch` is refused in this mode.
