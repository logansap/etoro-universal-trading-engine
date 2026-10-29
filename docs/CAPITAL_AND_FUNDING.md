# Capital ownership and automatic funding discovery

**Status:** implemented and tested in simulation (`tests/test_capital_funding.py`, 24 tests). The eToro read paths
are contract-tested against the documented response shapes only: this engine has no read-only key, so it has never
observed them.

## Target design (configured in `config/engine.multi.yaml`)

```yaml
capital:
  ownership: full_designated_account      # after the approved takeover, 100% of THIS trading account
  sizing_source: verified_broker_equity   # broker cash + broker-valued positions; never a typed budget
  manual_capital_amount: null             # any number is refused by the schema
  funding_policy: automatic_evidence_backed
  external_flow_allocation: proportional  # verified NET NEW capital split by the sleeve weights
  preserve_strategy_cash: true            # a deposit never redistributes existing sleeve profits or cash
portfolio:
  mode: multi_strategy
  sleeves: [{strategy: QC_529_ETORO_RESTRICTED, weight: 0.50}, {strategy: QC_60_SQQQ, weight: 0.50}]
```

**What the weights mean.** The weights are *capital ownership* weights, applied to each new external flow.
- They are not a requirement to stay invested: a sleeve's unused or ineligible weight stays cash.
- They do not trigger rebalancing between sleeves after market moves. Each sleeve keeps its own profits, losses,
  cash, units and cost basis (average cost per sleeve and instrument).
- Rebalancing between sleeves happens only through the audited `portfolio reallocate`.

**Scope.** Other eToro accounts, sub-accounts, the eToro Money cash account and crypto are **outside** the designated
account:
- the trading snapshot (`/api/v1/trading/info/real/pnl`) covers the main real trading account only;
- `account_balances()` requests `includeSubAccounts=false`;
- the eToro Money cash account is read only as *evidence* of internal transfers, never as capital.

## What the engine discovers and verifies at takeover

| Quantity | Source | Notes |
|---|---|---|
| Available cash | pnl endpoint `credit` minus pending opens (eToro "Calculate Available Cash") | |
| Open positions, invested amount, units, leverage | pnl endpoint `positions[]` | |
| Unrealized P&L | per position `unrealizedPnL.pnL` | cross-check: `/api/v1/balances` `equityDetails.currentPNL` (account level) |
| Reserved funds / margin | pending opens; `equityDetails.totalUsedMargin` | x1 only: used margin = invested amount |
| Fees and financing liabilities | position `totalFees`; financing appears as broker cash changes | a leveraged/short CFD's financing is listed in the takeover preview |
| Total equity | available + invested + unrealized (eToro "Calculate Equity") | **None** if any position lacks broker P&L; never estimated |
| Pending funding movements | open suspense items (below) | |

`funding.capital_report()` and the dashboard Overview show this table. The check is:

**Σ sleeve equity + suspense + foreign positions = broker account equity** (within the 0.05 USD reconciliation
tolerance)

The result is `VERIFIED`, `MISMATCH` or `INCOMPLETE` (with the reason). In the dashboard demo it is VERIFIED:
- account equity 2,793.88;
- sleeves 2,342.91 + suspense 125.00 + foreign 325.97;
- unattributed 0.00.

## Automatic funding discovery

Every reconciliation explains the change in broker cash with evidence-backed flows: engine fills, closes, broker cash
records. **A change that remains unexplained is investigated in this order:**

1. **Match to authorized evidence**, of either sign, with the same amount (±0.01) and a time window of −7 days to +5
   days:
   - a broker cash-transaction record (`cash_transactions`);
   - funding evidence (`funding_evidence`). On eToro these are the settled `internalTransfer` rows of the eToro Money
     cash account with an internal counterparty. A cash-account debit is a candidate deposit into trading; a credit
     is a candidate withdrawal.
   - A combination of up to three records can explain one change (several deposits between two reconciliations).
   - **Only records in the trading account's currency (USD) can match.** eToro Money rows are in the cash account's
     currency (EUR on this account) and the API gives no conversion rate for them, so they never match by amount,
     even when the numbers coincide. They are cited on the suspense item as `cross_currency_evidence`, together with
     the rate they would imply. See "Cross-currency funding" below.
   - The match books a `DEPOSIT` or `WITHDRAWAL` flow, referenced and idempotent (`fund:<source>:<id>` or
     `tx:<id>`), split 50/50, excluded from P&L, and available at each sleeve's next rebalance.
2. **Positive and no evidence → suspense** (`CASH_UNCLASSIFIED`). The amount:
   - is kept out of every sleeve and out of strategy equity, so it can't be spent;
   - raises **one** WARNING exception;
   - does **not** halt trading on the explained capital;
   - is **re-investigated on every reconciliation**. A delayed statement row or transfer record classifies it
     automatically when it appears, booked against the snapshot that first saw the cash, so it is never counted
     twice.
3. **Negative and no evidence → `CASH_DISCREPANCY` halt (ALL).** Money that the sleeves believe they own has left the
   account, so nothing may be sized until it is explained.

**Never:**
- **Sale proceeds as funding.** Proceeds come only from close orders (`TRADE_CLOSE`). Automatic classification is
  suspended while any close has unknown proceeds (blocker B3), even if a coincidental transfer of the same amount
  exists. This is tested.
- **A deposit as income.** Deposits are external flows: daily P&L = Δequity − external flows; TWR is unaffected.
- **Retroactive changes.** A deposit classified two days later changes nothing already recorded. This is tested.
- **A capital reset at restart or upgrade.** One `INITIAL_CAPITAL` per database, ever. This is tested.

**Corrections.** A deposit reversal (`DEPOSIT_REVERSAL`) is external and split by weight. Duplicate broker records
(the same transaction id) are booked once. Financing and fee deductions with evidence are P&L, split by sleeve equity.

## eToro limitation, investigated 2026-10-29 against the official API portal

| Capability | Endpoint (documented) | What it gives | Enough to classify? |
|---|---|---|---|
| Trading account snapshot | `GET /api/v1/trading/info/real/pnl` | credit, positions, pending orders | no reasons |
| Aggregated balances | `GET /api/v1/balances?expand=equityDetails` | per account `available`, `currentPNL`, `totalUsedMargin`, account ids, `gcid` | equity cross-check and account identity; no reasons |
| Daily balance history | `GET /api/v1/balances/trading/{id}/history` | end-of-day cash, invested, pnl, total | corroborates the **day and amount**; the schema has no transaction type or reason field |
| eToro Money cash-account transactions | `GET /api/v1/money/accounts/cash/{uuid}/transactions` | typed rows (internalTransfer, bankTransfer, card…) with ids | **yes, for internal transfers** between eToro Money and trading |
| Internal transfers | `/transfers…` (write and lookup) | transfer records | lookup is evidence; the engine never transfers |
| Direct card/bank deposits into the trading account | **none documented** | none | **no** |

So a deposit funded **through eToro Money (internal transfer)** can be classified automatically once a read-only key
with `etoro-public:money.cash-transactions:read` exists. A **direct card or bank deposit** into the trading account
cannot be distinguished by any documented API, so it stays in suspense, with one exception, until either:
- the owner classifies it with evidence (`account resolve --classify DEPOSIT --evidence <statement ref>`, using the
  observed amount only); or
- eToro exposes a trading-account transaction feed with typed deposit and withdrawal rows and reference ids.

**That feed is the specific broker capability needed to remove this last manual step.**

Recommended practice for the owner: fund the account through eToro Money internal transfers. They are evidenced
automatically.

## Capital safety guarantees, and where each is enforced

| Guarantee | Enforcement |
|---|---|
| Σ sleeve equity reconciles to verified account equity | `sleeves.check_invariants` (ledger vs flows, units vs broker), `funding.capital_report` |
| No unaccounted cash silently allocated | suspense is excluded from sleeves and from strategy equity; only evidence-matched flows are allocated |
| No sleeve spends another's funds | planner buying power per sleeve; per-sleeve phase-2 resize; opens capped by broker cash (see the regression below) |
| No double counting of margin or unrealized P&L | value = amount + unrealized per broker position; marks pooled per instrument; units invariant |
| No trading against unsettled or unavailable buying power | opens ≤ available cash (credit − pending) − suspense |
| No extra CFD leverage | schema: `max_leverage: 1`, `allow_short: false`; eligibility requires x1 |
| No retroactive alteration of returns | external flows enter the period in which they are classified; history is append-only |
| No capital reset after restart or upgrade | single bootstrap; sleeves seeded once; DB migrations keep history (test `test_capital_survives_reboot_without_reset`) |

**Two defects fixed during this work** (found by the container rehearsal, now regression-tested):
- A reallocation was mis-read as a −40% sleeve "daily loss".
- A sleeve's buys could exceed the broker's cash when the other sleeve was negative.

## Cross-currency funding (investigated 2026-10-29 with the engine's read-only key)

The owner funds the account in EUR: bank → eToro Money (EUR) → trading account (USD). The eToro Money rows are typed
evidence (`bankTransfer` credit, then `internalTransfer` debit to the trading account, minutes apart), but they are in
EUR and the API reports **no conversion rate and no conversion fee**. The trading account's daily balance history
shows the matching USD credit the same day.

- **Defect found and fixed:** the matcher compared EUR evidence with USD credits as if both were USD. A real deposit
  could never match, and a EUR row whose number happened to equal an unrelated USD credit would have been matched.
  Now only USD evidence can classify a credit; a cross-currency row is cited on the suspense item
  (`cross_currency_evidence`, with the rate it would imply) and nothing is booked
  (`test_cross_currency_funding_evidence_is_cited_never_matched`).
- **Until a policy is chosen**, an owner deposit made through eToro Money in EUR stays in suspense: visible on the
  dashboard, not spendable, and it does not halt trading on the rest.

**Options (each an owner policy decision; none is implemented):**
1. **A manual, audited classification command** for a suspense item, citing the cross-currency record. It does not
   exist yet; `account resolve` handles `CASH_UNEXPLAINED` only.
2. **An FX-tolerance rule.** Match a EUR row when the implied rate falls within a band around an independent reference
   rate for that day (for example the ECB reference rate) and the posting times are minutes apart. This needs an
   approved FX source and band; eToro's own conversion fee would sit inside that band, unmeasured.
3. **Fund in USD.** A USD internal transfer matches exactly, with no new rule.
