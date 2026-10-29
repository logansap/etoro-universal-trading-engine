# Accounting and reconciliation

## Principle

The broker is the truth. The engine's ledger only explains changes in the broker's cash (`credit`) with evidence:

```
credit(now) - credit(previous snapshot) == sum(unreconciled cash flows)   (within account.cash_tolerance_usd)
```

| Flow | Evidence | External? |
|---|---|---|
| INITIAL_CAPITAL | available cash at bootstrap (snapshot hash) | yes |
| TRADE_OPEN (-amount) | broker order status + the position the broker shows | no |
| TRADE_CLOSE (+proceeds) | broker close-order status `proceeds` (or simulated equivalent) | no |
| DEPOSIT / WITHDRAWAL / DIVIDEND / FEE / ADJUSTMENT | broker cash-transaction record (when the adapter has the capability) or an operator classification of an **observed** discrepancy | DEPOSIT/WITHDRAWAL yes |
| EXTERNAL_TRADE (-amount) | the broker's own amount for a foreign position that appeared | yes (leaves the strategy's scope) |

**Nothing is ever booked to make the numbers balance.** An unexplained difference creates a DISCREPANCY and halts
all new orders. An operator may classify a discrepancy, but cannot type an amount: the amount is the observed one,
and its sign is checked (a DEPOSIT must be positive).

## What the engine tracks, and what it can prove

| Item | Source | Status |
|---|---|---|
| cash (`credit`) | broker | proven per snapshot (eToro: VERIFIED_LIVE by GM) |
| available / reserved cash | credit - pending opens (eToro guide) | derived, documented formula |
| equity | available + invested + unrealized (eToro guide) | derived |
| positions, ids, units, amount, open rate, leverage | broker | proven per snapshot |
| contract type (CFD/REAL) | strategy YAML evidence; eToro eligibility query | YAML evidence; the broker field is not parsed per position (UNKNOWN) |
| open orders | broker `ordersForOpen` / `orders` | proven |
| fills and realized P&L | order and close-order status | proven where the status carries the numbers |
| fees, spreads | spread implicit in fill rates; `totalFees` read per position | partially; standalone fee events are UNAVAILABLE on eToro |
| financing, dividends, adjustments | no verified eToro feed | **UNAVAILABLE**: they surface as unexplained cash, then a halt, then operator classification |
| deposits, withdrawals | simulated broker: automatic; eToro: the cash-transactions API covers the eToro Money account only | eToro: **UNVERIFIED**, so the halt path applies |
| buying power / margin | not exposed beyond available cash | UNAVAILABLE (x1 only, so available cash is the binding constraint) |
| FX | USD-only engine; eToro equity is the USD balance | non-USD balances are out of scope |

## Ownership

- **ENGINE:** opened by an engine intent. Counts toward strategy equity.
- **PRE_EXISTING:** present at bootstrap. Excluded, never closed or resized.
- **EXTERNAL_UNACK:** appeared without an intent. Triggers a halt (EXTERNAL_POSITION).
- **EXTERNAL_ACK:** acknowledged by the operator. Still excluded and never touched.

`strategy equity = broker available cash + broker value of ENGINE positions`. Owning 100% of the account
(the exclusive cash rule) is not the same as 100% exposure: the strategy's weights times `capital_fraction` decide
the exposure, and risk caps it.

Adopting pre-existing positions needs an approved transition plan. That is NOT IMPLEMENTED; the positions stay
excluded.

## Deposits

- **Detected** automatically on the next reconciliation (every cycle; idle cycles every 15 minutes), with no
  restart.
- **Classified** automatically when the broker provides a cash-transaction record (deduplicated by transaction id,
  with a one-day overlap to absorb posting lag). Without a record, the change is a discrepancy, then a halt, then
  the operator runs `account resolve --classify DEPOSIT`.
- **Available** at the next rebalance, because strategy equity is read from the broker.

## Performance

`daily_equity` is written once per session at decision time:

- `pnl = E - E_prev - F`, where F is the external flows since the previous record;
- `twr = twr_prev * (E - F) / E_prev`;
- the high-water mark and drawdown are taken on the TWR index.

Test s06 proves a USD 1,000 deposit changes neither P&L nor TWR.

## Reconciliation points

`after_restart` (startup), `before_decision`, `before_planning`, `before_execution`, `after_phase1`,
`after_execution`, `idle` (every 15 minutes outside windows), `manual` (`account reconcile`), and the switch points.
Each run stores the full snapshot (positions, pending orders, raw SHA-256) and an audit entry.

## Operator resolution procedure

1. `trading-engine status`: read `halts`, `open_discrepancies`, `last_reconciliation.issues`.
2. Check the eToro UI or statement for the exact amount and time.
3. `trading-engine account resolve --discrepancy <id> --classify DEPOSIT|WITHDRAWAL|DIVIDEND|FEE|ADJUSTMENT --evidence "<statement ref>" --operator <name>`
4. For a foreign position: `trading-engine account acknowledge-external --position-id <id> --operator <name> --note "<why>"`
5. `trading-engine halt-clear --id <halt> --operator <name> --reason "<what was verified>"`
6. `trading-engine account reconcile` must now print `CLEAN`.

Every step is in the hash-chained audit log.

## Phase 2: account vs strategy equity report (`trading-engine report`)

| Field | Meaning |
|---|---|
| broker.credit / available_cash / pending_open_amount | as reported |
| broker.account_equity | eToro formula; **None** if any position lacks broker unrealized P&L (never estimated) |
| positions[] | every broker position with its owner (ENGINE / PRE_EXISTING / EXTERNAL_*) |
| foreign_positions_amount / _value | positions that are not strategy capital |
| strategy_equity | available cash + ENGINE positions |
| funding | INITIAL_CAPITAL, DEPOSIT, WITHDRAWAL, EXTERNAL_TRADE, DIVIDEND, FEE, ADJUSTMENT; `net_contributions` = initial + deposits + withdrawals + external trades |
| trading_pnl | strategy equity - net contributions (dividends, fees and adjustments count as trading results) |
| twr_return / max_drawdown | funding-adjusted (deposits neither help nor hurt) |

`daily_equity.foreign_value` records the foreign-position value per session.

The real account (via read-only audit snapshots) reconciles CLEAN, with Gold Miner's GDX short PRE_EXISTING; see
`docs/phase2/REAL_ACCOUNT_RECONCILIATION_2026-10-27.md`.
