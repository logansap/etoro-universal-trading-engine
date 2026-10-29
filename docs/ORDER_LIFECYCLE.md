# Order lifecycle

## From target to reconciliation

1. **target:** StrategyDecision (persisted, unique per strategy and session).
2. **current:** ENGINE-owned positions from a clean reconciliation.
3. **plan:** Legs from the planner, plus the risk verdict. Persisted in `plans`, with a deterministic `plan_id`
   (`hash(decision_id, mode)`).
4. **intent:** one row per leg. `intent_id = "I" + hash(plan_id, leg_index, action, symbol, position_id)`.
   Committed as INTENT_PERSISTED.
5. **submission:** the row moves to SUBMITTING together with a pre-submit broker snapshot (position ids and
   units, pending order ids, credit). Only then is the broker called.
6. **ack:** ACKNOWLEDGED with the broker order id.
7. **pending / partial:** PENDING, PARTIALLY_FILLED (each partial position is attributed as it appears).
8. **final:** FILLED or REJECTED (or CANCELED).
9. **unknown:** UNKNOWN on a timeout, connection loss or crash. Resolved only from evidence.
10. **final reconciliation:** RECONCILED after a clean post-execution reconciliation.

Allowed transitions: `core/order_state.py:TRANSITIONS`. Anything else raises IllegalTransition. **UNKNOWN can never
go back to SUBMITTING.**

## Sequencing

- Phase 1 (closes and partial closes) runs first.
- If a close is REJECTED, the opens are skipped (`PHASE1_REJECTED`).
- If a close is unresolved, the engine halts (UNRESOLVED_ORDER).
- Otherwise a reconciliation must be CLEAN, and then the opens are re-sized to the broker's available cash
  (scaled down, never up). An open below USD 10 is dropped.

## Failure handling

| Situation | What happens | Test |
|---|---|---|
| definite broker error (HTTP 4xx) | REJECTED, nothing retried | s11, s22 |
| accepted, then rejected by the broker | REJECTED via status lookup | s11 |
| accepted, then timeout | UNKNOWN, then a new matching position absent pre-submit, then FILLED (one submission) | s12 |
| connection lost before the broker | UNKNOWN, no evidence, then halt UNKNOWN_ORDER. Re-queried each cycle, never resent. The operator may record NOT_SUBMITTED only if the broker shows no candidate | s13 |
| crash before the broker call | INTENT_PERSISTED. A restart inside the window submits once; outside it, ABANDONED | s14 |
| crash after the broker accepted | SUBMITTING. At restart: resolve, then UNKNOWN, then evidence, then FILLED | s15 |
| pending at restart | re-polled by order id until final | s16 |
| partial fill | PARTIALLY_FILLED and a halt; the rest is attributed when filled | s10 |
| duplicate cycles or restarts | same decision, plan and intent ids, so no duplicate | s17 |

## Limits of exactly-once

eToro's create-order call returns `{token, orderId, referenceId}`, and no client idempotency key is documented. A
request lost in flight can therefore not be de-duplicated by the broker. The engine guarantees **at most one
submission per intent** and **evidence-only resolution**. If the evidence is ambiguous (two candidate positions, or
none), it stops and asks a human. It never guesses, and it never resubmits.

Matching rule for UNKNOWN opens: same instrument, amount within max(USD 0.02, 1%), a position id absent from the
pre-submit snapshot and not owned by anyone.

## CFDs and units

- Opens are USD amounts.
- Closes are per position: all units, or units for a partial close. A partial close on eToro splits the position;
  the parent keeps the remaining units.
- Fractional units are stored to 6 decimals.
- No share-count rounding is assumed. This avoids the LEAN whole-share artefacts found in the #60 research.

## Phase 2: eToro recovery handles

- **Opens.** The live writer sends `x-request-id = uuid5(intent_id)`. eToro echoes it as `referenceId` and
  documents `orders:lookup?referenceId=` as the recovery path for a lost create-order response. An UNKNOWN open is
  resolved by that lookup first, then by position evidence. eToro does not document the effect of a *reused*
  `x-request-id`, so the engine still never resubmits. A request that never reached eToro (lookup 404, no position)
  stays UNKNOWN and halts.
- **Closes.** No `referenceId` exists. A lost close response is proven from the position's disappearance or reduced
  units, but its proceeds are visible only through `close-orders/{orderId}`, and that order id was in the lost
  response. The cash change therefore stays unexplained and the engine halts (CASH_DISCREPANCY). **This is live
  blocker B3** in PHASE2_READINESS.md; it is not weakened into a guess.
- **Statuses 6-12.** 7 and 8 (Canceled, Expired) end CANCELED. 9 and 10 (Canceled/RejectedPartiallyFilled) book
  the executed part and then end CANCELED. 6, 11 and 12 count as in flight.
- **Contract tests.** `tests/test_etoro_live_contract.py` runs these paths against `tests/fake_etoro.py`, a fake of
  the documented API.
