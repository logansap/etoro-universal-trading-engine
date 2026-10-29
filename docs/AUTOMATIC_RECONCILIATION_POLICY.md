# Automatic reconciliation policy: silent when explained, one exception when not

**Goal:** no daily manual approval of normal balance changes, and no suppression of anything serious.

## What is reconciled automatically, and silently

Every reconciliation reads one broker snapshot (before each decision, before and after each execution phase, after
a restart, and every 15 minutes when idle).

| Item | Automatic explanation | Evidence |
|---|---|---|
| Cash and buying power | credit change = Σ evidence-backed cash flows (± `cash_tolerance_usd` 0.05) | broker `credit`, pending opens |
| Open positions | every position classified: ENGINE / PRE_EXISTING / EXTERNAL_ACK; engine units match | broker positions |
| Realized P&L (engine trades) | TRADE_OPEN −amount, TRADE_CLOSE +proceeds | the order's own fill / close status |
| Unrealized P&L | broker value per position (used for equity; never estimated) | `unrealizedPnL` |
| Executions and fees | per intent, from the broker order status | orders:lookup / close-orders |
| Funding, financing, dividends, corporate actions | DEPOSIT / WITHDRAWAL / DIVIDEND / FEE / ADJUSTMENT rows **when the broker supplies a cash-transaction record** | `cash_transactions` (simulated broker: yes; **eToro: no trading-account feed**) |
| Internal strategy cash movements | crossing and allocation are ledger entries that net to zero | sleeve ledger |
| Sleeves | Σ sleeve cash = Σ attributed flows; Σ sleeve units = broker units | sleeve invariants |

When everything is explained, the reconciliation writes its audit row and **raises no alert**. This is tested by
`test_routine_explained_changes_are_silent`: trades, a deposit, a dividend and a fee over 10 reconciliations produce
zero alerts and zero exceptions.

## What raises an exception

| Exception class | Kind(s) | Scope | Clears |
|---|---|---|---|
| Unexplained material balance discrepancy | `CASH_DISCREPANCY` for unexplained cash leaving the account; with a `capital` section, a POSITIVE unexplained credit becomes `CASH_UNCLASSIFIED` **suspense** instead ([CAPITAL_AND_FUNDING.md](CAPITAL_AND_FUNDING.md)) | negative: halt ALL. Suspense: no halt; the amount is excluded from all capital | evidence match (automatic: broker records, eToro Money internal transfers, delayed statements) or operator classification with evidence |
| Unknown order outcome | `UNKNOWN_ORDER`, `CLOSE_PROCEEDS_UNKNOWN`, `UNRESOLVED_ORDER` | halt ALL (unresolved: a warning while the broker still reports it in flight) | broker evidence (automatic) or audited operator resolution |
| Unexpected external trade | `EXTERNAL_POSITION`, `EXTERNAL_CHANGE`, `EXTERNAL_PENDING_ORDER`, `POSITION_MISMATCH`, `POSITION_MISSING` | halt ALL | `account acknowledge-external` plus classification of the cash effect |
| Missing or stale broker state | `BROKER_UNAVAILABLE`, `BROKER_STATE_STALE` (snapshot older than `max_broker_state_age_seconds`, default 300 s) | halt ALL | **automatically**, on the next good, fresh read |
| Failed deposit classification | `CASH_DISCREPANCY` (positive, no broker evidence) | halt ALL | `account resolve --classify DEPOSIT --evidence <statement>` |
| Risk or execution halt | `DAILY_LOSS_LIMIT`, `DRAWDOWN_LIMIT`, `ACCOUNT_SIZING_BLOCKED`, `SWITCH_FAILED`, `EMERGENCY_HALT`, `CONFIG_DRIFT` | NEW_EXPOSURE / ALL; account or **sleeve** | operator |
| Account-ownership conflict | `ACCOUNT_HAS_MIRRORS`, external activity from another writer on a shared account (the EXTERNAL_* kinds), sleeves not adding up (`SLEEVE_LEDGER_MISMATCH`) | halt ALL | operator; for a shared account, the takeover workflow |

**Never:**
- a balancing entry;
- a deposit inferred from a cash difference alone;
- an operator-typed amount (the classification uses the observed amount);
- a suppressed serious discrepancy.

## One durable exception per issue (no daily duplicates)

- Each issue carries a **subject** (discrepancy id, position id, order or intent id, `broker-read`).
- Halts are keyed by (kind, subject, sleeve), and every halt is mirrored by a row in the `exceptions` table.
- The operator is **notified once**, when the exception opens. Later detections only increment `occurrences` and
  `last_seen`. Example: an unexplained cash change seen by 5 reconciliations is 1 exception with
  `occurrences ≥ 5` and 1 notification (`test_unverified_cash_increase_is_one_exception_never_a_deposit`).
- A broker outage with varying error texts is 1 exception, and it resolves itself on recovery
  (`test_broker_outage_is_one_exception_and_clears_itself`).
- **De-duplication never hides a recurrence.** After resolution, the same problem re-opens the exception and notifies
  again (`test_cleared_issue_that_recurs_notifies_again`).
- `trading-engine exceptions` lists the open ones; `health` shows them under `open_exceptions`.

## Fail-closed scope

- Account-level problems stop **all** sleeves.
- A problem confined to one strategy (its YAML or module drift, its loss limits, an operator hold) is a **sleeve
  halt**: that sleeve holds, the other continues (`test_sleeve_halt_holds_that_sleeve_only`).
- When the broker cannot supply authoritative evidence, the affected execution stops and one actionable exception
  is created. Each exception carries the exact `ACTION` text (`monitoring/notify.ACTIONS`).

## Automatic funding discovery (capital section)

With `capital.funding_policy: automatic_evidence_backed`:
- every unexplained cash change is first matched against authorized broker evidence, of either sign, with the same
  amount and time, allowing subset matches for several deposits;
- a match is booked, allocated 50/50 and silent;
- a positive change without evidence waits in suspense, with one WARNING exception and no halt, and is re-checked on
  every reconciliation;
- a negative change without evidence halts.

Details, tests and the eToro limitation are in [CAPITAL_AND_FUNDING.md](CAPITAL_AND_FUNDING.md).

## Limits on this account today

- eToro exposes **no deposit or withdrawal feed** for the trading account. Every deposit therefore needs one owner
  classification: one exception per deposit, not per day. **That cannot be automated honestly.**
- Unrealized P&L is available from the engine's own read-only adapter (the pnl endpoint), but **not** from the
  Gold Miner audit snapshot, which is the only real-account read available today. Account sizing therefore stays
  BLOCKED until the engine has its own read-only key ([PRODUCTION_READINESS.md](PRODUCTION_READINESS.md), G6).
