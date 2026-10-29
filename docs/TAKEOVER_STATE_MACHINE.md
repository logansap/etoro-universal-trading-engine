# Full-account takeover: state machine and approvals

> **This release can preview a takeover of the real account (read-only) and walk the full state machine in
> simulation. It cannot close a real position:** `TAKEOVER_EXECUTION_RELEASED = False`, which is a code constant, and
> shadow mode has no writer. **Nothing was closed, adopted or disabled in this assignment.**

## States

```
DISCOVER -> PREVIEW -> APPROVAL_REQUIRED -> QUIESCE_OLD_WRITER -> VERIFY_ACCOUNT -> CLOSE_EXISTING
         -> RECONCILE -> ALLOCATE -> ACTIVATE -> MONITOR
final side exits: CANCELED (operator) · INVALIDATED (account changed after approval) · HALTED (a close failed or is unknown)
```

- **Durability.** Every transition is one row in `takeover_events` plus one entry in the hash-chained audit log
  (`TAKEOVER_TRANSITION`), written **before** its effect. The run, its plan, the plan hash, the release and the
  account live in `takeover_runs`.
- **One step per command.** `trading-engine takeover advance --run <id> --operator <name>` performs at most one
  transition. **The service loop never advances a takeover.**
- **Refusals are audited** (`TAKEOVER_BLOCKED`).

| Transition | Guards (all required) | Effect |
|---|---|---|
| → DISCOVER → PREVIEW → APPROVAL_REQUIRED | the account is readable through `ReadOnlyView` | the preview is stored ([ACCOUNT_TAKEOVER_PREVIEW.md](ACCOUNT_TAKEOVER_PREVIEW.md)); **it authorizes nothing** |
| APPROVAL_REQUIRED → QUIESCE_OLD_WRITER | a VALID `DISABLE_OLD_WRITER` approval, bound to this plan and release | the approval is consumed. **The engine disables nothing**: the owner stops Gold Miner by its own procedure |
| QUIESCE_OLD_WRITER → VERIFY_ACCOUNT | `DISABLED_VERIFIED` evidence for **every** writer of the plan, recorded after quiesce started (e.g. the read-only Gold Miner audit: unit inactive and disabled, REAL kill switch engaged, no pending orders); a fresh inventory with no material change | none |
| VERIFY_ACCOUNT → CLOSE_EXISTING | a write capability exists **and** (`TAKEOVER_EXECUTION_RELEASED` or the simulated broker); VALID `CLOSE_POSITIONS` approval binding exactly the plan's positions and units; VALID `ACCEPT_LOSSES_AND_FEES` approval ≥ the estimated loss + costs; total exposure ≤ the approved maximum; no ALL halt; no unresolved intent; market open; clean reconciliation; no material change | the approvals are consumed, then the closes run through the shared executor (persist-before-submit, at most one submission per intent) |
| CLOSE_EXISTING → RECONCILE | every close FILLED | proceeds booked as `TAKEOVER_PROCEEDS` (external capital, **not** strategy P&L) into the `_UNALLOCATED` pool |
| RECONCILE → ALLOCATE | clean reconciliation; none of the approved positions remains | none |
| ALLOCATE → ACTIVATE | VALID `ACTIVATE_STRATEGY` approval; clean reconciliation; **no** foreign position, mirror or pending order left | the pool is split into the sleeves by weight; `account_takeover.exclusive_verified_at` is recorded |
| ACTIVATE → MONITOR | none | sleeves trade in the current mode (simulation or shadow). **Live stays off** |
| any → INVALIDATED | material change: a position added, removed or changed in units; pending orders, mirrors, cash or account identity changed; estimated proceeds moved by more than `price_tolerance_pct` of exposure | every VALID approval is voided; a new preview is required |
| CLOSE_EXISTING → HALTED | a close rejected (not retried) or unknown (not retried, UNKNOWN_ORDER halt) | approvals voided; the remaining account must be previewed again |
| any → CANCELED | operator command | approvals revoked; final |

## Approvals

The kinds are `DISABLE_OLD_WRITER`, `CLOSE_POSITIONS`, `ACCEPT_LOSSES_AND_FEES`, `ACTIVATE_STRATEGY` and
`ENABLE_LIVE`. Each one is separate, recorded with `takeover approve --kind …`, and **bound to:**

```json
{"account_id": "...", "release_id": "<hash of this engine's source>", "plan_hash": "...",
 "positions": [["<position id>", <units>], ...], "max_cost_usd": ..., "max_exposure_usd": ..., "expires_at": "..."}
```

**Approval rules:**
- **Expiry:** `approval_ttl_hours`, default 4. An expired approval is refused.
- **Account binding:** an approval is impossible without a verified account identity. The real-account preview today
  is UNVERIFIED, because the Gold Miner audit carries no account id. It needs the engine's own read-only key
  (`/api/v1/me`).
- **Release binding:** a different engine build cannot use the approval.
- **Single use:** each approval is CONSUMED by one transition.
- **Revocation:** an approval can be revoked at any time (`takeover revoke`).
- **ACCEPT_LOSSES_AND_FEES** needs an explicit `--max-cost` at least equal to the preview's estimated realized loss
  plus spread and slippage.
- **CLOSE_POSITIONS** needs `--max-exposure`.
- **ENABLE_LIVE** is recorded but `takeover enable-live` always raises `LiveExecutionNotAvailable`.

**Approving a preview is never authorization to execute.** No transition happens without its own command and its
own approval.

## Independence of the old writer

- The engine never stops, restarts or reconfigures Gold Miner, and it has no credential or access to do so.
- Ownership is never assumed because the new service is running. `account_takeover` is written only by the ACTIVATE
  step, after a clean, foreign-free reconciliation.
- Until then, every change Gold Miner makes halts the new engine (EXTERNAL_CHANGE).

## Restart safety

- A crash during CLOSE_EXISTING leaves the run in CLOSE_EXISTING.
- At startup, the executor resolves the in-flight intents from broker evidence.
- The next `advance` re-runs the same deterministic plan (`T<run id>`). Finished intents are skipped and nothing is
  resubmitted (`test_restart_during_takeover_resolves_without_resubmitting_and_completes`).

## Regression tests (`tests/test_takeover.py`, 11 tests)

| Scenario | Test |
|---|---|
| Preview makes no broker mutation; the read-only view refuses writes | `test_preview_reads_everything_and_mutates_nothing` |
| Stops at approval, however many advances are attempted | `test_takeover_stops_at_approval_without_executing` |
| The writer must be verified disabled first | `test_writer_must_be_verified_disabled_before_any_close` |
| Revoked approval, canceled run | `test_revoked_approval_and_canceled_run` |
| Stale (expired) approval | `test_stale_approval_is_refused` |
| Changed broker position → INVALIDATED | `test_changed_broker_position_invalidates_the_plan` |
| Failed close → HALTED, not retried | `test_failed_close_halts_the_run_and_is_not_retried` |
| Restart during takeover; completion; 50/50 allocation; ENABLE_LIVE refused | `test_restart_during_takeover_resolves_without_resubmitting_and_completes` |
| Mixed external items (mirror, pending order) | `test_mixed_external_items_need_separate_handling` |
| Shadow mode: preview and approvals work, close is refused | `test_shadow_mode_can_preview_but_never_close` |
| Explicit cost and exposure bounds | `test_approval_needs_explicit_cost_and_exposure_bounds` |

## What a future, separately authorized execution release needs

1. Every gate in [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md), above all the engine's own read-only key
   (account identity, unrealized P&L), a fresh-quote source, and the live writer exercised on the real account by a
   supervised canary.
2. A reviewed code change `TAKEOVER_EXECUTION_RELEASED = True`, shipped with `LIVE_RELEASE_ENABLED = True` and the
   owner's approval file.
3. **Blocker B3** resolved or accepted by the owner. A takeover close whose response is lost is recovered from
   position evidence, but its proceeds need an audited manual classification.
