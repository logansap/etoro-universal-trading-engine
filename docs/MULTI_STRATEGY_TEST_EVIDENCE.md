# Multi-strategy, reconciliation and takeover: test evidence

All of this is **simulation or offline**. No real order, close, cancel or activation was made, and Gold Miner and the
paper databases were not touched. The real-account takeover preview read a saved file of the authorized read-only
audit.

## Suite

| Run | Result |
|---|---|
| Windows 11, CPython 3.13.14 (`python -m pytest -q`) | **161 passed**, 0 failed, 0 skipped |
| Linux, WSL Ubuntu kernel 6.18, CPython 3.14.4 (`scripts/linux_tests.sh`: a copy of the tracked tree in /tmp) | **161 passed**, 0 failed, 0 skipped |

- The suite was 129 tests at 433ed36. All 129 still pass unchanged, so single-strategy behaviour is preserved.
- 32 new tests:

| File | Tests | Covers |
|---|---|---|
| `tests/test_multi_strategy.py` | 14 | sleeves, attribution, deposits, P&L, crossing, aggregate risk, restart, lost close, unknown open, sleeve halt, planner minimums, reallocation, schema upgrade |
| `tests/test_takeover.py` | 11 | the complete takeover workflow ([TAKEOVER_STATE_MACHINE.md](TAKEOVER_STATE_MACHINE.md)) |
| `tests/test_auto_reconciliation.py` | 6 | silent routine reconciliation, one exception per issue, automatic clears, recurrence |
| `tests/test_cli_multi.py` | 1 | `portfolio status`, `exceptions`, `takeover preview/status/advance` |

## Migrations

- `test_schema_upgrade_from_version_2_keeps_data_and_audit_chain` builds a version-2 database (the audited release),
  writes an audit entry and a halt, then reopens it with this release:
  - it becomes schema 3;
  - the rows and the audit chain are intact;
  - halts gain `subject` / `sleeve`;
  - the new tables exist;
  - `sleeve_ledger` refuses DELETE.
- Fresh databases migrate 1 → 2 → 3 in every test.

## The ten required demonstrations (`python scripts/multi_strategy_demo.py` → `docs/data/multi_strategy_demo.json`)

| # | Demonstration | Result (simulated USD 2,000, 50/50) | Regression test |
|---|---|---|---|
| 1 | Two simultaneous strategies | both sleeves `DECIDED` for 2026-10-26 (two decision rows, one portfolio plan) | `test_two_sleeves_share_one_tqqq_position_with_exact_attribution` |
| 2 | Shared TQQQ attribution | **one** broker TQQQ order; broker 21.270479 units = #529 6.696454 + #60 14.574025 | same |
| 3 | Verified deposit allocated 50/50 | deposit 1,000 (broker tx `T2`) → each sleeve +500.00 cash, external flow 500, P&L unaffected | `test_verified_deposit_is_allocated_50_50_and_excluded_from_pnl`; unverified cash: `test_unverified_cash_increase_is_one_exception_never_a_deposit` |
| 4 | Independent P&L | TQQQ +5% → #529 +22.91, #60 +49.86 (by their units); the sum equals the account | `test_independent_pnl_per_sleeve` |
| 5 | Broker-level combined exposure | broker TLT 1.53, SQQQ 3.26, TQQQ 21.27, TECL 0.43 units; Σ sleeve equity 1,999.28 = account 1,999.28 | test 1 |
| 6 | Safe rebalancing | #529 forced to cash while #60 invests its deposit share. TLT, SQQQ and TECL were closed; **#529's TQQQ units were crossed internally to #60** (no TQQQ close at the broker), plus one net TQQQ open; #529 ends 1,499.68 cash; invariants clean | `test_one_sleeve_going_to_cash_never_sells_the_other_sleeves_units`, `test_opposite_trades_cross_internally_and_net_at_the_broker` |
| 7 | Restart without double orders | crash right after the broker accepted an open; after restart and three cycles: 4 submissions, all client references unique, reconciliation CLEAN, one plan | `test_restart_mid_plan_never_double_orders` |
| 8 | Unknown close outcome halts safely | lost close response, with no proceeds lookup (as on eToro): plan `HALTED_RECONCILIATION`, halts `CASH_DISCREPANCY` + `CLOSE_PROCEEDS_UNKNOWN`, 0 opens started, 0 orders in the next cycle, one exception per issue (occurrences counted) | `test_lost_close_response_without_proceeds_halts_and_needs_audited_resolution`, `test_unknown_open_is_never_retried` |
| 9 | Complete takeover preview, no mutation | GDX short x5 (exposure 3,664), owner PRE_EXISTING, proceeds 810.27 (broker P&L), financing 1.01 per day, action CLOSE; capital 2,064.56 (worst 2,055.40); 1,007.14 per sleeve; **broker state file byte-identical** | `test_preview_reads_everything_and_mutates_nothing` |
| 10 | Takeover stops at approval | state `APPROVAL_REQUIRED`; every `advance` refused ("approval DISABLE_OLD_WRITER is required"); broker unchanged | `test_takeover_stops_at_approval_without_executing` |

## Failures found during this work, and what was done

| Found | Cause | Resolution |
|---|---|---|
| Deposit test expected a sleeve P&L of exactly 0 | the first fills' spread (−0.32 USD) is correctly P&L | test expectation corrected (P&L between −2 and 0) |
| Crossing test counted 2 TQQQ intents | the filter included the previous day's plan | test filter corrected to the day-2 plan |
| Lost-close test ended `HALTED_PHASE1` | the test also disabled order-status lookup for every close (stronger than eToro, where only the lost one lacks a reference) | over-broad monkeypatch removed. Only the proceeds lookup is disabled, as on eToro |
| Planner-minimum test produced a buy | the test's target was wrong (it bought instead of selling 6.80 USD) | test setup corrected |
| **Real defect:** a pending order notified twice (the executor's plan-level UNRESOLVED_ORDER halt and the per-intent warning) | duplicate notification | fixed in `reconciliation.py`: the per-intent warning is silent while the plan-level halt is active (`test_pending_order_is_one_warning_resolved_when_final`) |

**No test is skipped, marked expected-to-fail or weakened to pass.**

## Not covered by these tests (simulated or unverified)

- Any real eToro write. The live writer has still been exercised only against the contract fake.
- Unrealized P&L, quotes and account identity read by **this engine** from the real account: there is no read-only
  key.
- Real deposit evidence: eToro has no trading-account feed.
- A real Gold Miner shutdown and the evidence of it.
- A real VPS install of the multi-strategy configuration. The container rehearsal (30/30) covered the
  single-strategy configuration at 9f126db; the multi-strategy configuration was not re-rehearsed in the container.
