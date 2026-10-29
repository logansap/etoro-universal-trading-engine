# Release-blocking test matrix

Run with `python -m pytest -q tests`. Each test's docstring states its invariant. The order-changing tests use the
simulated broker (with fault injection); shadow tests use a recording fake eToro transport. No real order is
placed by any test.

| # | Scenario | Test(s) | Invariant (short) |
|---|---|---|---|
| 1 | #60 parity vs recorded decisions | test_qc60_parity: `test_s01_parity_with_recorded_paper_decisions`, `..._frozen_research_port`, `..._native_lean` | 8/8 paper decisions (RSI < 0.005, SMA exact); 537/537 sessions identical to the frozen S60; 48/48 native-LEAN buy sessions agree |
| 2 | TECS signal preserved, SQQQ target | `test_s02_regression_branches_real_data` (9 real branches), `test_s02_tecs_rsi_still_feeds_argmax`, `test_s02_synthetic_high_volatility_branches` (5), `test_strategy_target_vs_executable_allocation` | the substitution is final-target only; TECS RSI drives argmax; the buffer and BSV->cash are executable-allocation effects |
| 3 | #529 multi-instrument | test_qc529 (3 tests) | weights = recorded units x close / NAV for 3 sessions; no rebalance on an unchanged label; engine opens every eligible leg |
| 4 | existing positions at startup | `test_s04_existing_positions_at_startup_are_excluded` | PRE_EXISTING, excluded, never touched |
| 5 | deposit detected without restart | `test_s05_deposit_detected_and_invested_without_restart` | DEPOSIT from broker evidence, invested at the next rebalance |
| 6 | deposit excluded from P&L | `test_s06_deposit_excluded_from_pnl_and_twr` | P&L and TWR identical with and without the deposit |
| 7 | unknown cash discrepancy blocks orders | `test_s07_unknown_cash_discrepancy_blocks_orders` | halt; no intents; operator classification of the observed amount only |
| 8 | broker cash != internal cash | `test_s08_broker_cash_differs_from_internal_after_fill` | the exact difference is reported; DONE_UNRECONCILED + halt |
| 9 | position mismatch | `test_s09_position_mismatch_halts`, `test_s09_engine_position_vanishing_halts` | halt ALL |
| 10 | partial fills | `test_s10_partial_fill` | one submission; both parts attributed; cash reconciles |
| 11 | rejections | `test_s11_rejection_not_retried` | final, no cash moved, never retried |
| 12 | timeout after acceptance | `test_s12_timeout_after_acceptance` | evidence resolves to FILLED; one submission |
| 13 | network interruption | `test_s13_network_interruption_halts_never_resubmits` | UNKNOWN + halt; never resent |
| 14 | crash before submit | `test_s14_crash_before_submit` | submitted exactly once after restart |
| 15 | crash after submit before ack | `test_s15_crash_after_submit_before_ack` | resolved at restart from evidence |
| 16 | restart with unresolved orders | `test_s16_restart_with_unresolved_orders` | re-polled by order id; never resubmitted |
| 17 | duplicate cycles | `test_s17_duplicate_cycles` | 1 decision, 1 plan, 1 submission |
| 18 | missing or stale data | `test_s18_missing_or_stale_data_holds` (2), `test_s18_stale_decision_blocked_by_risk` | NO_DECISION; nothing guessed; stale decisions blocked |
| 19 | holiday and DST | test_calendar (5 tests) | published NYSE holidays 2024-27, early closes, DST UTC shift, missed windows not caught up |
| 20 | oversized or unaffordable order | `test_s20_oversized_order_blocked`, `test_s20_unaffordable_*` (2) | blocked or scaled; never submitted beyond cash |
| 21 | switch #60 -> #529 | `test_s21_switch_60_to_529`, `test_s21_activation_refused_when_state_changed_or_expired` | hash + approver + fresh state; COMPLETED; history kept |
| 22 | failed switch rollback | `test_s22_failed_switch_rolls_back`, `test_s22_partially_executed_switch_halts` | ROLLED_BACK leaves everything unchanged; FAILED_PARTIAL halts |
| 23 | manual or external trade | `test_s23_external_trade_detected_and_never_touched` | halt; cash explained by the broker position amount; never closed |
| 24 | emergency halt | `test_s24_emergency_halt_blocks_new_exposure_without_liquidating` | no new exposure; no liquidation; audited clear |
| 25 | reboot recovery | `test_s25_hard_kill_and_restart` (real process, hard kill x2) + WSL systemd rehearsal | exactly-once effects after kills |
| 26 | shadow cannot execute live | test_shadow (7 tests), `test_live_writer_cannot_be_obtained`, `test_no_live_order_code_in_package` | only allowlisted GETs; refusal before the network; no writer after restart; no credential in logs |
| 27 | config tampering, unsupported modules | test_config_safety (parametrised: 9 engine-yaml + 8 strategy cases + drift + initial_strategy + 687) | live, leverage, shorts, secrets, variants and unknown modules are all refused |
| 28 | ledger consistency across restarts | `test_s28_ledger_consistency_across_restarts` | audit chain; INITIAL_CAPITAL + flows = broker cash; unique flows; ownership = broker |

Supporting unit tests (test_units.py) cover: illegal transitions, an append-only and tamper-evident audit log,
planner sequencing and partial closes, backup and restore verification, and UTC timestamps.

**Skips:** none. The suite has no `skip` or `xfail` markers. The results of the last run are in
RELEASE_READINESS.md.

## Phase 2 additions

| Area | Test(s) | Invariant |
|---|---|---|
| live authorization boundary | test_etoro_live_contract: `test_live_writer_cannot_target_production_without_authorization`, `test_opening_the_code_gate_alone_is_not_enough`, `test_writer_allowlist_and_engine_never_gets_it`; test_config_safety: `test_live_order_code_is_isolated_and_never_constructed` | no LiveAuthorization can be issued; each gate is independent; only two POST routes; the engine never constructs the writer |
| submission success / documented request | `test_c01_success_documented_request_and_reconciliation` | exact body; x-request-id = uuid5(intent); CLEAN; GM short untouched |
| rejection | `test_c02_rejection` (2) | final, no cash, no retry |
| partial fills | `test_c03_partial_fill_then_complete`, `test_c03b_canceled_partially_filled_is_final` | one POST; the executed part is booked |
| timeout after acceptance | `test_c04_timeout_after_acceptance_resolved_by_reference` (2), `test_s12_timeout_after_acceptance_without_reference_lookup` | resolved by referenceId (or position evidence); one POST |
| unknown outcome | `test_c05_unknown_outcome_halts_and_is_never_resubmitted` | halt; never a second POST across restarts |
| restart during execution | `test_c06_restart_during_execution` | SUBMITTING, then FILLED via reference after restart |
| duplicate prevention | `test_c07_duplicate_submission_prevention` | stable, unique request ids; no re-POST |
| multi-instrument transition + reconciliation | `test_c08_multi_instrument_transition_and_reconciliation`, `test_c09_partial_close` | close before opens; CLEAN; ownership = broker |
| live blocker B3 | `test_c10_lost_close_response_is_a_live_blocker` | lost close leads to a CASH_DISCREPANCY halt, never re-sent |
| notifications | test_phase2_ops: `test_notifications_are_actionable_deduplicated_and_retried`, `test_missed_decision_and_unresolved_order_notify`, `test_crash_is_detected_at_next_start` | actionable, deduplicated, retried, crash detected |
| market data | `test_market_data_health_and_suspect_price_fail_closed` | SUSPECT_PRICE leads to NO_DECISION and alert, unhealthy |
| equity / funding report | `test_account_report_separates_funding_from_trading_pnl` | deposit = funding; trading P&L = equity - contributions |
| off-host backup | `test_offsite_backup_pushes_and_verifies_pulled_copy` | pushed, pulled back, verified; retention |
| real-account importer | `test_gm_audit_importer_classifies_real_account_shapes` | GM short PRE_EXISTING; new GM trade halts; unexplained cash is a discrepancy |

**Totals (Phase 2):** 120 passed, 0 failed, 0 skipped (Windows CPython 3.13; Linux CPython 3.14).
