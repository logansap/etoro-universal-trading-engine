# Virtual sleeve accounting

**The broker is authoritative for the account.** The sleeve ledger only divides what the broker reports among the
sleeves. Nothing in it can create, lose or double-count money.

## The ledger

`sleeve_ledger` is **append-only**: SQL triggers abort any UPDATE or DELETE, as for the audit log. Each entry carries:
- sleeve, kind, cash, instrument and units;
- `external` (1 = capital, excluded from P&L);
- the evidence link: `flow_id` (a broker cash flow), `intent_id` or `plan_id`.

Entries are idempotent by (reference, sleeve, kind), so replaying evidence after a restart posts nothing twice.

| Entry | Source of truth | Split rule |
|---|---|---|
| `OPEN_FILL` | the broker fill (position id, amount, units) | by the USD attribution persisted on the intent **before** submission |
| `CLOSE_FILL` | close order proceeds; units from the broker position before and after | by the units attribution on the intent |
| `CLOSE_PROCEEDS` | an audited classification of an **observed** discrepancy (lost close response) | by the units the sleeves gave up |
| `CROSS_OUT` / `CROSS_IN` | internal transfer at the broker mark (no broker order) | the units and cash entries each sum to 0 |
| `INITIAL_CAPITAL`, `DEPOSIT`, `WITHDRAWAL`, `EXTERNAL_TRADE` | broker cash-flow rows (`cash_flows`) | **funding_policy proportional**: by sleeve weight |
| `DIVIDEND`, `FEE`, `ADJUSTMENT` | broker cash-flow rows | by sleeve equity (part of trading P&L) |
| `TAKEOVER_PROCEEDS` | proceeds of closing a foreign position in an approved takeover | to the `_UNALLOCATED` pool |
| `ALLOCATE_IN/OUT`, `REALLOC_IN/OUT` | an approved takeover ACTIVATE step, or an approved reallocation | weights |
| `SEED`, `SEED_UNITS` | once, when a non-empty single-strategy history is converted | holdings to the strategy that opened them; cash split to approach the weights |

## Balances, marks and equity

| Quantity | Definition |
|---|---|
| sleeve cash | Σ cash entries |
| sleeve units (per instrument) | Σ unit entries |
| mark | Σ broker value / Σ units over the engine's broker positions in that instrument |
| reserved | for OPEN intents still in flight: the sleeve's share of the order amount not yet filled (derived from durable intent rows, so it survives restarts) |
| sleeve equity | cash − reserved + Σ units × mark |

**Units are pooled.** A broker position does not belong to a sleeve; sleeves own units of an instrument. For an x1
long CFD, value = units × bid, so every unit is worth the same and pro-rata valuation is exact. Overnight fees or
financing embedded in the broker value are shared pro rata by units.

If a held instrument has no broker value (no unrealized P&L), the sleeve equity is **None** and sizing is refused
(`ACCOUNT_SIZING_BLOCKED`). It is never estimated.

## Invariants (checked after every clean account reconciliation)

1. Every broker cash flow is posted to sleeves exactly once, and Σ sleeve cash = Σ posted flows.
2. For each instrument, Σ sleeve units = units of the engine's broker positions.
3. No sleeve holds negative units.

**Consequence:** Σ sleeve equity = account strategy equity (available cash + engine positions). In the simulation
demonstration: Σ sleeves 1,999.28 = account 1,999.28.

A breach raises `SLEEVE_LEDGER_MISMATCH` and halts ALL; it is not repaired automatically. A trade flow found
without attribution is the same breach; it is never guessed.

## Performance per sleeve

`sleeve_daily` holds one row per (session, sleeve), with the same formulas as the account:
- P&L(t) = E(t) − E(t−1) − F(t);
- TWR(t) = TWR(t−1) × (E(t) − F(t)) / E(t−1);
- F = the sleeve's external entries since its previous record (deposits, withdrawals, reallocations, takeover
  allocation).

**Effects:**
- A deposit raises E and F equally: P&L and TWR are unchanged. Test: a USD 1,000 deposit gives each sleeve exactly
  +500 cash and +500 external flow; its daily P&L is only the cents of fill spread.
- Fees and dividends are P&L.
- Per-sleeve daily-loss and drawdown limits raise a **sleeve** NEW_EXPOSURE halt.

**Independent P&L** (demonstration, TQQQ +5%): #529 +22.91 USD (6.70 units), #60 +49.86 USD (14.57 units). The two add
up to the account's change.

## Deposits, withdrawals and capital

A deposit is allocated only when the broker evidences it with a transaction id (`cash_transactions`):
- reference `tx:<id>`;
- external: excluded from trading P&L;
- split 50/50;
- invested at each sleeve's **next valid rebalance**, within buying power and risk limits.

**eToro offers no trading-account deposit feed.** A cash increase without evidence is therefore a `CASH_DISCREPANCY`
exception, labelled "possible deposit, UNVERIFIED", and is never classified automatically. The owner classifies it
with `account resolve --classify DEPOSIT --evidence <statement ref>`. The amount is the observed one; no number can
be typed. It is then allocated like any deposit. See
[AUTOMATIC_RECONCILIATION_POLICY.md](AUTOMATIC_RECONCILIATION_POLICY.md).

**Withdrawals** are split by weight as well. A sleeve can go temporarily negative in cash; its own next rebalance
sells to restore it, and opens are always capped by the broker's available cash.

## Full-account capital (capital section)

- **No fixed budget.** With `capital.ownership: full_designated_account` there is no manual capital: sleeves own the
  account's verified equity, apart from suspense (unclassified credits, which nobody owns) and foreign positions
  before the takeover.
- **Proportional funding.** Deposits are split by weight; withdrawals and deposit reversals likewise.
- **Profits stay put.** A deposit never moves existing sleeve profits.
- **Realized and unrealized P&L per sleeve** use the average cost of each sleeve's own units, including crossings.
- **Reconciliation:** see [CAPITAL_AND_FUNDING.md](CAPITAL_AND_FUNDING.md) for the full check
  (Σ sleeves + suspense + foreign = account equity).

## Reallocation between sleeves

1. `portfolio reallocate-preview --weights …` computes the equity transfers for the new weights. It is valid for
   15 minutes and bound to the sleeve state.
2. `portfolio reallocate --preview-hash … --approved-by …` posts `REALLOC_OUT/IN` (cash, external) and records the
   new weights in the database.

No broker order is created by the reallocation. The sleeves' next rebalances, with crossing, adjust the holdings.
A stale or changed preview is refused (`test_controlled_reallocation_moves_equity_without_broker_orders`).

## Converting an existing single-strategy database

If `portfolio.mode: multi_strategy` is started on a database that already has cash flows, the first clean
reconciliation seeds the sleeves once (`SEED`):
- open engine holdings go to the sleeve of the strategy that opened them (otherwise split by weight);
- cash is split so that sleeve equity approaches the weights without going negative;
- all earlier flows are marked as covered by the seed.

The seed is audited (`SLEEVES_SEEDED`). **A fresh state directory is preferred**, and is what the cutover runbook
uses.
