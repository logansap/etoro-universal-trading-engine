# Phase 4 report (2026-10-29)

**Verdicts** ([PRODUCTION_READINESS.md](../PRODUCTION_READINESS.md)):
- **Simulation: GO.**
- **Production-equivalent shadow: GO for the software,** blocked by owner actions: a read-only key and VPS access.
- **Live: NO-GO.**

**What did not happen:** no real eToro order, close or takeover; no Gold Miner change; no paper reset or backfill; no
frozen-signal change; no live writer activation.

## Workstream A: production-equivalent 50/50 rehearsal

**Command:**
```bash
wsl -u root -e bash scripts/run_multi_rehearsal_wsl.sh
```

It runs `deployment/container/run_multi_rehearsal.sh` on Ubuntu 24.04 with systemd 255 and Python 3.12. The result is
**69 PASS / 0 FAIL** in all three phases: install, after reboot, and Gold Miner retirement. The report is
[MULTI_STRATEGY_CONTAINER_REHEARSAL_2026-10-29.md](MULTI_STRATEGY_CONTAINER_REHEARSAL_2026-10-29.md); the release
tarball sha256 is `fcd8463a…`.

**What it verified:**
- **Production install:** the production config `engine.multi.yaml` (shadow, 50/50) was installed by `install.sh`,
  ran under the systemd sandbox as `ute`, and failed closed without a key.
- **Simulated 50/50 instance**, run as `ute` under the same sandbox:
  - two sessions with both sleeves deciding independently;
  - a shared TQQQ position;
  - a reallocation;
  - **an internal crossing** (two CROSS entries);
  - **a crash after a broker accept and recovery**, with no duplicate submission;
  - **alert de-duplication:** one alert for four detections;
  - health check, backup and restore verification;
  - the dashboard served as `ute` under the sandbox.
- **Adversarial refusals:** weights that don't sum to 1, `mode=live`, a tampered strategy YAML, a `DELETE` on the
  sleeve ledger, a tampered audit log, and an unapproved takeover.
- **Operations:** kill −9 with a systemd restart; the health and backup units; an upgrade and a rollback that kept
  both instances' state; a clean shutdown; survival across a reboot.
- **Isolation:** the mock Gold Miner stayed byte-identical and was never restarted, and the engine wrote only inside
  its own paths.

**Real defects found by the rehearsal and fixed with regression tests:**
1. A reallocation was treated as a −40% sleeve daily loss, raising a false sleeve halt.
2. A sleeve's opens were not capped at the broker's cash while the other sleeve was negative.
3. `run --once` recorded a clean stop after an aborted cycle.

**Harness defects found and fixed:**
- **A false PASS in the earlier single-strategy rehearsal.** The image had no D-Bus, so `! systemd-run … cat` passed
  without running anything. The image now has D-Bus, and both rehearsals assert isolation positively (the probe must
  run and report HIDDEN).
- **Smaller fixes:** the weights `sed` failed on CRLF line endings; the external trade was attempted with $0.01 of
  cash; the unit checker counted foreign positions.

## Workstream B: API-strict #529 forward paper portfolio (research repo)

- **Portfolio:** `X529STRICT-ETORO` (eligibility ELIG-API-1: the 11 API-verified instruments; everything else in cash)
  runs as a separate instance: task `TSR-PaperX529Strict`, data in `D:\TSR-paper-data-x529-strict`.
- **Activation:** 2026-10-29 10:58:40 UTC (06:58:40 New York). The first genuine forward decision was the 2026-10-28
  close, recorded at 10:58:44 UTC: TRADE TECL and TQQQ, with SOXL (29%) and SVIX (8%) left in cash. Fills are at the
  10-29 open. There is no backfill.
- **Conventions:** identical to the championship. A fixture test shows its NAV and fills equal P529-ETORO's exactly
  under ELIG-1.
- **Untouched:** the championship and the #60-SQQQ experiment.
- **Comparison export:** `python -m paper_trading.compare_export` (read-only, rebased to common sessions);
  `/api/forward_comparison` is available from the next dashboard restart.
- **Commit:** TSR `c5ae7ba`, after the TSR gate passed (253 passed, 1 skipped).

## Workstream C: 29 October forward review

- **2026-10-29 not available yet** (the session closes at 16:00 New York; snapshots appear at about 16:50):
  `python scripts/forward_review.py --session 2026-10-29` exited **2 and wrote nothing**.
- **Validated on 2026-10-28.** The report is
  [FORWARD_TRADE_REVIEW_2026-10-28.md](../forward_review/FORWARD_TRADE_REVIEW_2026-10-28.md):
  - **crash guard:** inactive (QQQ 10-day −0.46%), and #529 = #504 on both tracks;
  - **NAV:** the independent recomputation differs by at most 0.0000;
  - **data integrity:** no data events, no MISSED decisions, no fallback fills;
  - **substitutions:** 0 TECS→SQQQ;
  - **strict track:** at its first decision close it holds 100% cash, with SOXL the only ELIG-2 holding it may not
    hold.
- **Command for the later run** (after 16:50 New York on 2026-10-29):
  ```bash
  python scripts/forward_review.py --session 2026-10-29
  ```

## Addition: Gold Miner retirement and migration

- **Procedure:** `deployment/gm_retirement/gm_retire.sh` (inventory, preflight, freeze, verify, backup and restore
  drill, two-writer-safe rollback, decommission after archival, separate purge), plus the engine ledger
  `core/retirement.py` (`trading-engine retirement record | status`).
- **Rehearsed** against a behavioural mock (section G); **not executed** on the real host.
- **Docs:** [GOLD_MINER_RETIREMENT_PLAN.md](../GOLD_MINER_RETIREMENT_PLAN.md) and
  [MIGRATION_AND_ROLLBACK_RUNBOOK.md](../MIGRATION_AND_ROLLBACK_RUNBOOK.md).

## Addition: Universal Engine dashboard

- **Implementation:** `src/utengine/dashboard/`, standard library only. Authenticated and read-only, with Overview,
  Strategies, Trades, Operations and Migration sections, and responsive on desktop and phone.
- **Evidence:** 10 tests, live probes, a sandboxed container probe, and 10 screenshots
  ([DASHBOARD_VALIDATION.md](../DASHBOARD_VALIDATION.md)).
- **Docs:** [DASHBOARD_ARCHITECTURE.md](../DASHBOARD_ARCHITECTURE.md), [DASHBOARD_USER_GUIDE.md](../DASHBOARD_USER_GUIDE.md)
  and [DASHBOARD_SECURITY.md](../DASHBOARD_SECURITY.md).

## Addition: full equity ownership and automatic capital discovery

- **Implementation:** `core/funding.py`, plus the `capital` schema and reconciliation integration.
- **Evidence:** 24 tests covering every listed scenario.
- **eToro investigation:** checked against the current official API. Only eToro Money internal transfers carry typed
  evidence; direct deposits have no feed, so they stay in suspense.
- **Docs:** [CAPITAL_AND_FUNDING.md](../CAPITAL_AND_FUNDING.md).

## Tests

| Run | Result |
|---|---|
| Windows, CPython 3.13.14: `python -m pytest -q` | **196 passed** |
| Linux (WSL Ubuntu, CPython 3.14.4): `wsl -e bash scripts/linux_tests.sh` | **196 passed** |
| Research repo gate (paper tests incl. 4 new strict-track tests) | 253 passed, 1 skipped; GATE PASS |

New test files: `test_capital_funding.py` (24), `test_dashboard.py` (10), plus 1 new test in `test_cli_multi.py`.

## Broker and VPS access

- **eToro connector:** still **invalidated** (re-checked 2026-10-29). This is an external blocker: no real-account
  read beyond the authorized Gold Miner audit snapshot.
- **VPS:** only the forced-command audit account exists; it was not bypassed. `deployment/vps_preflight.sh` is
  extended with the Gold Miner timer, the dashboard port, sqlite3, flock and disk checks. Validated in WSL: rc=1,
  because there is no root.

## Known limitations

- **Real-broker paths:** the live writer, funding evidence, balances, `gcid` identity and quotes are contract-tested
  only.
- **Direct card or bank deposits** cannot be classified automatically on eToro.
- **B3:** a lost close response still needs a manual proceeds classification.
- **Crossing in the rehearsal** needed one forced, clearly labelled #529 decision, because #529 does not rebalance on
  a flat synthetic session.
- **Dashboard:** no TLS or SSO deployment, Chrome only, no accessibility audit.
- **Forward evidence:** only a few sessions; the strict #529 track starts today.
