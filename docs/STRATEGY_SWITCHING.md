# Strategy switching

The active strategy is stored in the database (`engine_state.active_strategy`, with the YAML fingerprint and
module hash).

**Editing YAML never switches.**
- `runtime.initial_strategy` is used once, at the first start.
- Changing the active strategy's YAML causes CONFIG_DRIFT: a halt, and no decisions are made.

## The workflow (implemented: `core/strategy_manager.py`, tests s21/s22)

| # | Step | How |
|---|---|---|
| 1 | validate target YAML | `trading-engine config validate` (schema, fingerprint, registry, module invariants) |
| 2 | readiness | the target is enabled, its module is AVAILABLE, and a decision for the last completed session is computable (data) |
| 3 | freeze | refused while any intent is unresolved or any halt is active |
| 4 | inspect | `trading-engine status`, `account status` |
| 5 | reconcile | the preview runs a reconciliation that must be CLEAN |
| 6 | preview | `trading-engine strategy preview-switch --to QC_529` prints the transition legs and stores the plan under its hash |
| 7 | cost and risk | the preview shows the estimated spread cost, blocked instruments and risk-relevant legs |
| 8 | explicit approval | `trading-engine strategy activate --to QC_529 --plan-hash <hash> --approved-by <name>` |
| 9 | persist | APPROVED is written to the audit log before any order |
| 10 | execute with recovery | transition legs go through the normal risk, executor and state-machine path |
| 11 | reconcile | post-execution reconciliation |
| 12 | audit record | COMPLETED, then STRATEGY_ACTIVATED (with the previous record); or ROLLED_BACK; or FAILED_PARTIAL plus a SWITCH_FAILED halt |

Activation is refused when:
- the hash is wrong;
- the approver is empty;
- the preview is older than 15 minutes;
- the target YAML changed;
- the reconciliation is not clean;
- the broker state (credit and position units) differs from the preview.

**Capital and history are never reset.** Engine positions that overlap (e.g. TQQQ in #60 and #529) are kept and
re-assigned. Cash flows, daily equity and the audit chain continue.

## Rollback semantics

- **ROLLED_BACK:** no transition order executed. The old strategy stays active. No halt.
- **FAILED_PARTIAL:** some orders executed. The old strategy stays recorded as active, a SWITCH_FAILED halt stops
  everything, and an operator reviews.
  - Trades cannot be "undone" without new trades.
  - Recovery is a fresh preview: either back to the old strategy or forward to the new one.

## Demo (simulation, from tests/test_switching.py)

#60-SQQQ held TQQQ USD 1,995. A preview to #529 produced:
- `PARTIAL_CLOSE TQQQ`, then `OPEN SQQQ`, `OPEN TLT`, `OPEN TECL`;
- SOXL was blocked: no verified eToro id, so its weight stayed in cash.

Activation completed, the active strategy became QC_529, positions were TQQQ/SQQQ/TLT/TECL, and a reconciliation
was CLEAN.

## Adopting positions / migrating #687

- **Adopting pre-existing positions** (e.g. the live #687 GDX CFD) would need an approved transition plan. That is
  NOT IMPLEMENTED: pre-existing positions stay excluded.
- **A #687 migration requires:**
  1. a goldminer687 module with recorded-decision parity;
  2. leveraged/short execution as an adapter capability, with broker stops and new risk rules;
  3. GM decommissioned first, so the account becomes exclusive;
  4. an adoption plan for GM's open positions.
