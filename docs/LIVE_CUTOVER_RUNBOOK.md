# Live cutover runbook: Gold Miner → Universal eToro Engine (NOT EXECUTED)

> **Status: plan only.** Nothing here has been executed. Every step marked **[OWNER]** needs the owner's explicit,
> separately recorded approval: in `D:\Trading-Strategy-Research\state\HUMAN_QUEUE.md`, and for engine steps also in
> the engine's audit log through the CLI's `--approved-by` / `--operator` arguments.
> **Better paper P&L is never an authorization.**
>
> Current verdict: **NO-GO** ([../GO_LIVE_DECISION.md](../GO_LIVE_DECISION.md)).

Selected strategy for the first activation: **C, #60-SQQQ (`QC_60_SQQQ`)**. Fallback candidate: B,
`QC_529_ETORO_RESTRICTED` ([STRATEGY_529_VS_60_EXECUTABILITY.md](STRATEGY_529_VS_60_EXECUTABILITY.md)).

## 0. Preconditions: all must be PASS before step 1

1. Every gate in GO_LIVE_DECISION.md is PASS, including a live-writer release that has been built, reviewed and
   hash-recorded (`LIVE_RELEASE_ENABLED` is a code change, never configuration).
2. A read-only eToro key for this engine has been provisioned and `trading-engine broker probe` succeeds.
3. The instrument matrix has been re-verified (fewer than 7 days old) for the selected strategy's instruments.
4. The engine has run in shadow on the target host for at least 20 sessions with clean reconciliations (no
   unexplained halts) and decisions identical to the forward paper record.

## 1. Decide the existing GDX position **[OWNER]**

**Today:** Gold Miner holds a **GDX CFD short x5** (position <GM-POSITION-1>): <redacted> units, amount USD <redacted>, about
USD 3,729 short notional (2026-10-28 19:10Z).

**Options, decided by the owner, not by this engine:**
- **(a)** Gold Miner closes it through its own procedures while still the owner of that position.
- **(b)** The owner closes it manually in the eToro UI.
- **(c)** It is kept as a declared foreign position. This is **not recommended**: exclusive ownership would stay
  false, and every Gold Miner/eToro change to it halts the new engine.

**The new engine never closes or adopts it.** Adoption is not implemented.

After the close, record the realized P&L and fees from eToro's trade history for the owner's own records.

## 2. Disable Gold Miner, only after separate approval **[OWNER]**

1. Use Gold Miner's own documented procedure (D:\AI-Gold-Miner-Trading-Engine\OPERATIONS.md): engage its REAL kill
   switch, confirm no pending orders, then stop and disable `gold-miner-engine.service`.
2. The new engine's tools never do this.
3. Verify with read-only commands: the unit is inactive and disabled; the audit snapshot shows no pending orders and
   no Gold Miner position.
4. Revoke or rotate Gold Miner's write-scoped eToro key **[OWNER]**, so that no second writer exists on the account.

## 3. Reconcile broker positions and available equity

1. Run `trading-engine account reconcile`. It must print `CLEAN`.
2. Run `trading-engine report`.
   - `broker.account_equity` must be **non-null**, meaning eToro returned `unrealizedPnL` for every position.
   - If it is null, **STOP**: sizing is BLOCKED.
3. Expected state: no positions; `available_cash` = credit; no pending orders.
4. Any residual difference becomes a discrepancy. Classify it with evidence (`account resolve`) and **never** book a
   balancing entry.

## 4. Confirm exclusive account ownership **[OWNER]**

- No other engine, copy-trading mirror or manual trading on the account. The engine refuses mirrors.
- No Gold Miner key active.
- The owner confirms in writing that all cash belongs to this engine (`account.ownership: exclusive`).

## 5. Verify trading permissions and instruments

1. Run the read-only eligibility call for every instrument of the selected strategy. For C that is TQQQ, UVXY,
   TECL, SPXL, SQQQ, and BSV, which stays cash unless verified.
2. Confirm for each one: x1 long allowed, CFD settlement, `minPositionExposure`, a live bid/ask fresher than 120 s.
3. Update the YAML only through re-fingerprint plus `strategy preview-switch` / `activate`.

## 6. Position-sizing limits and risk budget **[OWNER]**

Proposed defaults, which the owner must approve:

| Setting | Proposal |
|---|---|
| Capital | the owner sets it (suggested: start at or below USD 500 of the account; the rest stays uninvested until a 20-session live review) |
| `risk.max_gross_exposure` | 1.0 |
| `risk.max_instrument_weight` | 1.0 (#60 is single-asset) |
| `risk.max_leverage` | 1 |
| `risk.allow_short` | false |
| `risk.max_single_order_usd` | about 1.1 x capital |
| `risk.daily_loss_halt_pct` | 0.20, which raises a NEW_EXPOSURE halt (no auto-liquidation) |
| `risk.drawdown_halt_pct` | 0.50, which raises a halt and needs an owner decision |
| `risk.max_quote_age_seconds` | 120, which requires a fresh-quote source |

**Expected loss tolerance:** #60's research history includes three -47% to -52% drawdowns. The owner must accept
that explicitly.

## 7. Verify live-order state and recovery mechanisms

1. Run `pytest tests/test_etoro_live_contract.py tests/test_execution.py tests/test_audit_2026_10_28.py`. All must
   pass on the release build.
2. **Blocker B3 (lost close response) must be resolved first**, with one of:
   - a verified close-order discovery path (for example, by trade history or open-order lookup, proven on the real
     account); or
   - an owner-approved operating procedure that accepts a manual reconciliation step after any lost close.
3. Confirm halts, notifications and the emergency stop. The webhook must be configured, and `notify test` must
   deliver.

## 8. Supervised small-value execution test **[OWNER]**, during market hours with the owner present

1. Install the live release. Open each gate in order: the code-level release with the writer, the owner approval
   file bound to the release SHA and config fingerprint, the write credential in `UTE_ETORO_WRITE_*`, and no kill
   switch.
2. **One** x1-long open of USD 10-25 in TQQQ. Then:
   - `orders` shows FILLED via `orders:lookup`;
   - `account reconcile` is CLEAN;
   - the eToro UI shows the same position id.
3. Close it fully. Then:
   - the close order shows proceeds;
   - reconciliation is CLEAN;
   - the realized P&L in the trade history matches.
4. Repeat once with an injected network interruption on a test host only (never on the live host). Live tests use
   real conditions only.
5. Record the evidence in RELEASE notes. Any discrepancy means **STOP**.

## 9. Activate the selected strategy **[OWNER]**

Only after steps 0-8 pass:

1. Run `trading-engine strategy preview-switch --to QC_60_SQQQ`, or start fresh with `initial_strategy`. Review the
   legs and cost.
2. Run `trading-engine strategy activate --to QC_60_SQQQ --plan-hash <hash> --approved-by <owner>`.
3. Monitor the first five sessions in person: decision at 16:50 New York, execution from 09:31.

## 10. Rollback, emergency stop and monitoring

**Emergency stop (no new exposure; holds positions):**
```bash
trading-engine halt --reason "<why>" --operator <name>
```
Stop the service with `systemctl stop etoro-ute` (positions remain at eToro). Engage the kill switch (the
`kill_switch_path` file required by `authorize_live`); the writer then refuses everything.

**Flatten:** no automatic liquidation path exists, by design. The owner closes positions in the eToro UI. The engine
then halts with POSITION_MISSING, and the operator acknowledges the closes and classifies the cash.

**Rollback the software:** `sudo sh deployment/rollback.sh`. The state is kept, and the schema version must match.

**Rollback the strategy:** run `strategy preview-switch --to <previous>` and then `activate`. The ledger continues.

**Monitoring:**
- the `etoro-ute-health.timer` (every 5 minutes, `--notify`);
- the webhook alerts;
- `trading-engine status` and `report` daily;
- the nightly backup with restore drill and off-host copy;
- `journalctl -u etoro-ute`.

**Abort criteria (go back to shadow immediately):**
- any CASH_DISCREPANCY, UNKNOWN_ORDER or POSITION_MISMATCH without a clean explanation within one session;
- a stale or suspect data halt on two consecutive sessions;
- any order rejected for eligibility.
