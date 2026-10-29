# Go-live decision: replacing Gold Miner (prepared 2026-10-28/29)

> **Update 2026-10-29:** the multi-strategy (50/50) engine and the full-account takeover workflow are now in the
> release. The current gate table and the simulation, shadow and live verdicts are in
> [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md). Live is still **NO-GO**.

## Decision

**NO-GO for live trading.**

Nothing was activated, no real order was placed, and Gold Miner, the paper services and their schedules were not
touched.

| Question | Answer |
|---|---|
| Which strategy can realistically be deployed? | **#60-SQQQ (`QC_60_SQQQ`)**: 5 of 6 targets API-verified on this account, one instrument at a time, research profile preserved by the frozen TECS→SQQQ rule. BSV (about 5% of recent sessions) stays cash |
| #529? | **Original: not deployable** (8 of 19 instruments cannot be bought). **eToro-restricted variant B (`QC_529_ETORO_RESTRICTED`, new): technically executable on 11 instruments, but a different strategy** (about 30% of its weight in cash; research CAGR 107% vs 184%), with no API-strict forward record and the heaviest close/trim load. Second candidate |
| What would be traded first? | One x1-long eToro CFD at a time among TQQQ / UVXY / TECL / SPXL / SQQQ (ids 4465 / 3246 / 10855 / 3230 / 4459), sized by the owner (see runbook §6) |
| What blocks it? | The critical gates below: shared account, no read-only key and therefore no equity, unresolved close recovery, a live writer tested only against a fake, VPS not accessible |
| Exact activation sequence | [docs/LIVE_CUTOVER_RUNBOOK.md](docs/LIVE_CUTOVER_RUNBOOK.md) (steps 0-10; every owner step marked) |

## Production gates

| # | Gate | Status | Evidence / missing |
|---|---|---|---|
| G1 | Strategy logic fidelity, #60-SQQQ | **PASS** | 8/8 paper decisions, 537/537 frozen-port sessions, 48/48 native-LEAN sessions; substitution tests |
| G2 | Strategy logic fidelity, #529 (A and B) | **PASS** (simulation) | 3/3 recorded decisions; B has identical rules (`tests/test_variant_529_etoro.py`) |
| G3 | Instruments of #60-SQQQ verified on the account | **FAIL** | 5/6 API-verified but **62 days old**; BSV UNVERIFIED |
| G4 | Instruments of #529 verified | **FAIL** | 11/19 verified (old); SOXL LABU SVXY BSV SVIX UVIX UNVERIFIED; BTAL TECS UNAVAILABLE |
| G5 | Engine's own read-only broker access | **FAIL** | no read-only key; eToro connector invalidated |
| G6 | Full-equity account sizing | **FAIL (BLOCKED)** | `unrealizedPnL` unavailable through any authorized path; the 2026-10-28 19:10Z audit shows cash 559.41 and the GDX short (45.78 units, amount <redacted>) but no P&L. Cash + amount is not equity |
| G7 | Real-account reconciliation | **PASS** (via the read-only audit) / **UNVERIFIED** (via the engine's adapter) | `broker reconcile-audit`: CLEAN on 10-27; correctly halts on Gold Miner's 10-28 trim (EXTERNAL_CHANGE + 36.90 cash) |
| G8 | Exclusive account ownership | **FAIL** | the live Gold Miner trades the account (trimmed GDX today) |
| G9 | Gold Miner GDX position resolved | **FAIL** | an owner decision is pending (runbook §1) |
| G10 | Deposit evidence | **UNVERIFIED** | no trading-account feed; manual, audited classification only |
| G11 | Open-order identification and recovery | **PASS** (contract) / **UNVERIFIED** (live) | `x-request-id` → `orders:lookup?referenceId` |
| G12 | Close-order recovery after a lost response | **FAIL** | no close reference id: a safe halt, not economic recovery (B3) |
| G13 | Live writer exercised against real eToro | **UNVERIFIED** | only a self-written fake; a supervised canary is required (runbook §8) |
| G14 | Fresh-quote source | **FAIL** | REST rates were already up to 5 minutes old in the evidence; no WebSocket source; the 120 s rule would block |
| G15 | Live authorization gates closed, shadow cannot write | **PASS** | code constant + schema + approval chain; tests |
| G16 | Engine test suite | **PASS** | commit `8f4f90c`: 129 passed / 0 failed / 0 skipped on Windows (CPython 3.13) and on Linux (WSL Ubuntu, CPython 3.14). Passing tests are not broker evidence |
| G17 | Systemd install, restart, boot, isolation | **PASS** (container rehearsal 30/30) / **UNVERIFIED** (real VPS) | |
| G18 | VPS access for a shadow install | **FAIL** | the audit account is a forced command (no shell, no sudo) |
| G19 | Alerts to a real channel | **UNVERIFIED** | mechanism tested; no webhook configured |
| G20 | Off-host backup to a real target | **UNVERIFIED** | mechanism tested; no target approved |
| G21 | Paper execution-cost realism | **FAIL** | opening costs optimistic for TECL/SOXL/SVIX (2026-10-28 audit) |
| G22 | Forward evidence that is meaningful | **FAIL** | 4-5 sessions; #529 = #504 (guard inactive); 0 SQQQ substitutions |

## Real-account status (authorized read-only data, 2026-10-28 19:10Z)

- **Cash:** 559.41 USD; no pending orders.
- **One foreign position:** Gold Miner's **GDX CFD short x5** (<GM-POSITION-1>), <redacted> units, amount <redacted>, about
  3,729 USD short notional. **It is classified PRE_EXISTING, excluded and untouched.**
- **Unrealized P&L, total equity, financing per position:** **not available**, so sizing is **BLOCKED**.
- Reproduce: `ssh gold-miner-audit > snap.txt` (read-only), then
  `trading-engine broker reconcile-audit --snapshot snap.txt --scratch-home <empty dir>`.

## October 29 forward review

- Not available yet: the 10-29 close is after this report.
- Repeatable read-only command, after 16:50 New York on 2026-10-29:
  ```bash
  python scripts/forward_review.py --session 2026-10-29
  ```
- It writes `docs/OCT29_FORWARD_TRADE_REVIEW.md`. It exits 2 and writes nothing until the snapshots exist. Validated
  on 2026-10-27 ([docs/forward_review/](docs/forward_review/)).
- It reports:
  - closing NAV (recomputed independently), day and cumulative returns, positions, fills, realized and unrealized
    P&L, costs, SPY/QQQ;
  - the #529 crash-guard state and #504 equivalence;
  - the #60-SQQQ substitution count.
- **One session cannot change this decision.**

## VPS: exact blockers and owner actions

**Run the preflight on the VPS first.** `deployment/vps_preflight.sh` is read-only, never touches Gold Miner, and
was validated in WSL and a systemd container.

| Needed | Owner action |
|---|---|
| A shell login for a deploy user (the audit account is a forced command) | create an SSH user, or run the steps yourself |
| Root once for `deployment/install.sh` (user `ute`, /opt, /etc, /var/lib, units), or root once for `loginctl enable-linger ute` (rootless variant) | run it, or grant time-limited sudo |
| `python3-venv`, outbound HTTPS to query1.finance.yahoo.com and public-api.etoro.com | the preflight reports both |
| A read-only eToro key in `/etc/etoro-ute/readonly-credentials.env` (0640 root:ute) | create it in eToro; never reuse Gold Miner's write key |
| `UTE_ALERT_WEBHOOK_URL`, an off-host backup target | choose them |

A shadow install, `sh install.sh <tarball> --enable`, would **not** interfere with Gold Miner:
- it runs as a separate user with `ProtectHome=yes`;
- it has no access to Gold Miner's files or units, proven with a mock Gold Miner in the container rehearsal;
- it never writes to eToro.

On the shared account, though, it would halt on every Gold Miner trade. This is expected, and it is why G8 blocks
live.
