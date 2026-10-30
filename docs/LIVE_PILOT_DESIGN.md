# Supervised live pilot: design (NOT implemented, NOT enabled)

**Scope:** the first live use of the engine.
- **Strategy:** one strategy, `QC_529_ETORO_RESTRICTED` (owner decision 2026-10-30).
- **Account:** one eToro account the engine owns exclusively.
- **Approval:** every order needs explicit human approval.

**Status:** this is a design only.
- This release has no live writer path: `LIVE_RELEASE_ENABLED=False` is a code constant, `runtime.mode: live` is
  refused, and the eToro writer is never constructed.
- Building the pilot is a **separate, reviewed release**, with its own rehearsal and the owner's written approval.
- Nothing here may be enabled by configuration alone.

## Principles

1. **One writer, proven at the broker.** The old writer's key is revoked in eToro (owner action), so "frozen"
   becomes "cannot trade". The engine's trading key is a **new** key, never Gold Miner's. Its scopes are recorded from
   `GET /api/v1/me` before the first order.
2. **Human approval per order.** The engine plans as it does today. Each intent then waits in `AWAITING_APPROVAL`
   with:
   - instrument, side, USD amount;
   - quote, quote age, spread;
   - the resulting exposure.

   The operator approves one intent id within a short TTL. An expired or changed intent is never sent.
3. **Hard caps, enforced in code**, all of which must pass. The pilot values are in the private plan.
   - `pilot.max_order_usd`: per order;
   - `pilot.max_gross_usd`: total engine exposure after the order;
   - `pilot.max_orders_per_session`;
   - `pilot.daily_loss_stop_usd`: realized plus unrealized since the session open; at the stop, no new exposure;
   - `pilot.cumulative_loss_stop_usd`: since the pilot started; at the stop, a halt of ALL. Further action is human.
   - The existing gates still apply: x1 long only; the 11 instruments; quote age ≤ 120 s; spread ≤ `max_spread_bp`;
     minimum order 10 USD; available cash.
   - **These caps and stops are operational limits, not loss guarantees.** The exposure caps limit what may be
     opened, not what can be lost. The stops are evaluated at the next engine cycle, block new exposure or halt, and
     close nothing. Overnight gaps, leveraged-ETF moves beyond their history, broker outages and execution failures
     (a lost response, a partial fill) can each produce losses larger than any stop value.
4. **No automatic liquidation.** Stops block new exposure; reductions are proposed and need approval. A kill switch
   blocks every write, whatever any approval says.
5. **Every write is recoverable from broker evidence:**
   - an open is found by its `x-request-id` reference;
   - a queued close by the pending close order that names the position;
   - a filled close by the position evidence; its proceeds come from trade history, or from an operator
     classification of the observed amount.
   - Nothing is ever resubmitted while an outcome is unknown.
6. **A short window.** Approvals are accepted from the open plus the execution delay, up to the plan's window
   (60 minutes). After that the plan is abandoned, never sent late.

## Controls to verify before the pilot (checklist owner: operator)

| Control | Evidence required |
|---|---|
| Fresh prices | WebSocket snapshot age ≤ 120 s for all 11 instruments at the open, on at least 3 sessions (`ws_quote_probe`) |
| Trading permissions | the new key's `/me` scopes include `trade.real:write`; eligibility API: allowOpen, allowClose and x1 long CFD for all 11 |
| Order sizing | the shadow plans of the last 10 sessions are reproduced by hand, within 1 USD |
| Spread limit | the open spreads of the 11 instruments observed over 3 sessions; `max_spread_bp` set above their 95th percentile |
| Partial fills | simulated (`test_s10`). The first real partial fill is reconciled by hand |
| Unknown-order recovery | simulated (`test_s12`, `test_s13`, `test_pending_close`). The first real order is looked up by its reference id before the next order |
| Financing | x1 long is 0 by eToro's rule. Confirmed on the first overnight hold by reading `totalExternalFees` |
| Kill switches | `trading-engine halt` blocks the next approved order (rehearsed). `systemctl stop etoro-ute.service` stops everything |
| Rollback | revoke the trading key, or stop the service, then return to SHADOW. Positions stay as they are; closing them is a separate, approved action |
