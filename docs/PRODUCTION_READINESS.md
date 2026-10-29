# Production readiness: multi-strategy engine, full-account capital, takeover, Gold Miner retirement, dashboard

**Updated 2026-10-29 (Phase 4).** Changes from the first version:
- G16 and G17 re-run;
- G5, G6, G10 and G28 reworded, with **no change in status**;
- new gates G31-G36.

This supersedes the gate table of [../GO_LIVE_DECISION.md](../GO_LIVE_DECISION.md); that file's findings still apply.

## Verdicts

| | Verdict |
|---|---|
| **Simulation** | **GO.** Multi-strategy sleeves, full-account capital with automatic evidence-backed funding discovery, the takeover state machine, the Gold Miner retirement procedure and the read-only dashboard are implemented and demonstrated. 202/202 tests pass on Windows and Linux (2026-10-29 migration) |
| **Production-equivalent shadow** | **GO for the software.** The Ubuntu 24.04 + systemd container rehearsal of `config/engine.multi.yaml` passed **69/69** checks (install, reboot, retirement). **Deployment is blocked by owner actions:** a read-only eToro key and VPS shell or root access. On today's shared account the engine halts on every Gold Miner trade, by design |
| **Live** | **NO-GO.** Gates G5, G6, G8, G9, G12, G13, G14, G18, G21, G22, G27-G30 and G34 fail; G32 and G36 are unverified; see below |
| **Full-account takeover / Gold Miner retirement** | **Procedures ready; nothing executed.** The preview works on the real account (offline). The state machine, freeze, backup and restore, rollback and decommission are rehearsed in simulation or the container. Real execution is **not released** (`TAKEOVER_EXECUTION_RELEASED = False`); approvals cannot be bound to the real account until its identity is readable (G28); the Gold Miner freeze is an owner action on its host |

Nothing in this assignment placed an order, closed a position, disabled Gold Miner, revoked a credential or enabled
live trading.

## Production gates

G1-G22 are carried over from GO_LIVE_DECISION.md and re-checked; G23-G30 are new.

| # | Gate | Status | Evidence / what is missing |
|---|---|---|---|
| G1 | #60-SQQQ logic fidelity | PASS | unchanged (8/8 paper decisions, 537/537 frozen port, 48/48 native LEAN) |
| G2 | #529 (A/B) logic fidelity | PASS (simulation) | unchanged |
| G3 | #60-SQQQ instruments verified on the account | FAIL | 5 of 6 verified, but the evidence is 63 days old; BSV unverified |
| G4 | #529 instruments verified | FAIL | 11 of 19; SOXL LABU SVXY BSV SVIX UVIX unverified; BTAL TECS unavailable |
| G5 | Engine's own read-only broker access | PASS (real account, 2026-10-29) | the owner's separate READ-ONLY key: 16 scopes, all `:read`; identity, positions, balances, P&L, trade history, eToro Money transactions, eligibility and quotes read (32/32 HTTP 200). First real read found eToro's edge returning 403 (Cloudflare 1010) to Python's default User-Agent: fixed. Installed locally only; on the VPS after G18 |
| G6 | Full-equity account sizing | **FAIL (BLOCKED)** | the only real read (the Gold Miner audit) has no unrealized P&L. The engine parses per-position `unrealizedPnL`, plus a second documented source: `/api/v1/balances` `equityDetails.currentPNL`. Neither can be exercised without G5. Sizing never estimates (ACCOUNT_SIZING_BLOCKED) |
| G7 | Real-account reconciliation | PASS (audit) / UNVERIFIED (engine adapter) | reconcile-audit of 10-27, 10-28 and 10-28 22:58Z |
| G8 | Exclusive account ownership | **FAIL** | Gold Miner is live and trades the account |
| G9 | Gold Miner GDX position resolved | **FAIL** | owner decision ([ACCOUNT_TAKEOVER_PREVIEW.md](ACCOUNT_TAKEOVER_PREVIEW.md)) |
| G10 | Deposit evidence | **UNVERIFIED** | eToro has no trading-account deposit feed (official API checked 2026-10-29). Automatic classification works for **eToro Money internal transfers** (documented; contract-tested). A direct card or bank deposit stays in suspense: not spendable, one exception, no halt (G31) |
| G11 | Open-order recovery | PASS (contract) / UNVERIFIED (live) | `x-request-id` → `orders:lookup?referenceId` |
| G12 | Lost close response: economic recovery | **FAIL** | still blocker B3. Position evidence proves the close, but the proceeds need an audited TRADE_CLOSE classification. Now explicit (`CLOSE_PROCEEDS_UNKNOWN`) in every mode |
| G13 | Live writer exercised on real eToro | **FAIL** | only the contract fake; a supervised canary is required |
| G14 | Fresh-quote source | **FAIL** | live API check 2026-10-29 19:23Z, US market open: REST rates fresh (≤ 1 s) only for TQQQ, SQQQ, TECL, TLT, GDX; 11 other API-tradable instruments (SOXL, LABU, SVXY, BSV, SPXL, UVXY, …) 1.0-2.5 h old vs the 120 s limit. The engine refuses them (blocks, never misprices). Needs a streaming source ([ETORO_INSTRUMENT_ELIGIBILITY_2026-10-29.md](ETORO_INSTRUMENT_ELIGIBILITY_2026-10-29.md)) |
| G15 | Live gates closed; shadow cannot write | PASS | code constant + schema + approval chain; takeover execution not released; ENABLE_LIVE refused (tests) |
| G16 | Test suite | PASS | **212 passed on Windows (CPython 3.13) and on Linux (WSL Ubuntu)** (2026-10-29, after the read-only key work). Passing tests are not broker evidence |
| G17 | Systemd install, restart, rollback | **PASS (container, multi-strategy, 69/69)** / UNVERIFIED (real VPS) | [phase4/MULTI_STRATEGY_CONTAINER_REHEARSAL_2026-10-29.md](phase4/MULTI_STRATEGY_CONTAINER_REHEARSAL_2026-10-29.md) |
| G18 | VPS access | **FAIL** | the audit account is a forced command that ignores subcommands (re-checked 2026-10-29). The owner runs `deployment/sync_vps.sh` with their own root access (G38) |
| G19 | Alerts to a real channel | UNVERIFIED | mechanism and de-duplication tested; no webhook configured |
| G20 | Off-host backup target | UNVERIFIED | mechanism tested; no target approved |
| G21 | Paper execution-cost realism | **FAIL** | opening spreads optimistic for TECL/SOXL/SVIX (audit 2026-10-28) |
| G22 | Meaningful forward evidence | **FAIL** | 4 to 5 sessions; P529-ETORO is not variant B; 0 SQQQ substitutions |
| G23 | Multi-strategy sleeve accounting | PASS (simulation) | invariants, attribution, crossing, netting, restart, reallocation: 14 tests |
| G24 | Automatic, exception-only reconciliation | PASS (simulation) | 6 tests: silent routine, one exception per issue, automatic clears, recurrence |
| G25 | Takeover preview is read-only and complete | PASS (simulation + real account, authenticated) | 0 broker mutations; real-account preview 2026-10-29 19:21Z with fresh broker quote and broker P&L: takeover/REAL_ACCOUNT_TAKEOVER_PREVIEW_2026-10-29_LIVE.md (private report, not published) |
| G26 | Takeover state machine and approvals | PASS (simulation) | 11 tests: canceled or stale approvals, changed position, failed close, restart, mixed items, shadow refusal |
| G27 | Takeover execution against the real broker | **FAIL (not released)** | needs G5, G6, G12-G14, G28, G29 and a reviewed release |
| G28 | Account identity for binding approvals | PASS (read) | `etoro:gcid:<account-hash>` from `/api/v1/balances`, recorded in the takeover plan; the trading account id matches realCid (hashes). Binding approvals to it is exercised only in simulation |
| G29 | Old writer disabled and verified | **FAIL** | Gold Miner is ACTIVE; only the owner can disable it and record the evidence |
| G30 | Evidence that 50/50 improves risk-adjusted returns | **FAIL** | the replay shows the blend below #529-restricted alone on Sharpe and drawdown ([529_60_BLEND_COMPARISON.md](529_60_BLEND_COMPARISON.md)); the forward record is too short. An API-strict #529 paper track runs from 2026-10-29 (`X529STRICT-ETORO`). This blocks no software, but it is no reason to go live either |
| G31 | Full-account capital and automatic funding discovery | PASS (simulation) | 24 tests: takeover of a funded account, a zero-cash account, deposits (all invested, several on one day, during an open order, before their statement, delayed, months later), withdrawals, reversal, fees, restart, duplicates, price moves, divergent sleeves, reboot, sale proceeds never treated as funding. [CAPITAL_AND_FUNDING.md](CAPITAL_AND_FUNDING.md) |
| G32 | Funding evidence from the real broker | **UNVERIFIED** | eToro Money transfer evidence is contract-tested only (no key); direct deposits have no documented feed |
| G33 | Gold Miner retirement procedure | PASS (container, behavioural mock) / **not executed** | inventory, preflight, freeze (with refusals), verification, backup and restore drill (with tamper detection), two-writer-safe rollback, decommission after archival, separate purge. [GOLD_MINER_RETIREMENT_PLAN.md](GOLD_MINER_RETIREMENT_PLAN.md) |
| G34 | Gold Miner frozen and verified on the real host | **FAIL (owner)** | Gold Miner is live; the freeze is an owner action with a DISABLE_OLD_WRITER approval |
| G35 | Read-only, authenticated dashboard | PASS (simulation + container) | 10 tests, live probes, sandboxed container probe, desktop and mobile screenshots. [DASHBOARD_VALIDATION.md](DASHBOARD_VALIDATION.md) |
| G36 | Dashboard reachable securely on the VPS | **UNVERIFIED** | SSH-tunnel method documented ([DASHBOARD_SECURITY.md](DASHBOARD_SECURITY.md)); needs VPS access (G18) |
| G37 | The engine refuses a write-capable broker key | PASS (tests + real key) | `verify_read_only_key()`: any granted `…:write` scope, or no scope list, raises KeyNotReadOnly before any account read; startup records BROKER_KEY_CHECK / BROKER_KEY_REFUSED and a CRITICAL alert. Real key: `READ_ONLY` recorded 2026-10-29 |
| G38 | One-command shadow deployment next to a live Gold Miner | PASS (container rehearsal of `deployment/sync_vps.sh`) / **not run on the VPS** (G18) | GitHub-pinned deployment, [DEPLOYMENT.md](DEPLOYMENT.md); previously `deploy_shadow_vps.sh`: checksum, credential-file refusals (Gold Miner names or values, mode), Gold Miner fingerprint identical before and after, dashboard only after a password. PRODUCTION_MIGRATION_2026-10-29.md (private report, not published) |

## What is implemented and demonstrated

- **Simultaneous strategies:**
  - independent decisions, state and schedules;
  - virtual cash and units per sleeve (append-only ledger);
  - broker marks; reservations; per-sleeve P&L, TWR and drawdown;
  - per-sleeve halts and limits;
  - controlled reallocation;
  - conversion of an existing single-strategy database.
- **Shared order planning:**
  - per-sleeve wants and buying power;
  - internal crossing and broker netting;
  - eToro minimums without ever selling another sleeve's units;
  - per-sleeve phase-2 resize;
  - aggregate risk;
  - fill attribution persisted before submission;
  - restart idempotence (at most once per sleeve per session; at most one submission per intent).
- **Automatic reconciliation:**
  - routine changes are silent;
  - one durable exception per issue;
  - stale-state and mirror detection;
  - sleeve invariants;
  - an explicit "possible deposit, UNVERIFIED" exception;
  - an audited TRADE_CLOSE classification for lost close responses.
- **Takeover:**
  - read-only preview (inventory, leverage, exposure, ownership, financing, proceeds and costs, eligibility, capital,
    writers, 50/50 plan, next holdings, risks);
  - durable 10-state machine with 5 kinds of separately bound approvals, invalidation, cancel, halt and restart;
  - a real-account preview built from the read-only audit.
- **Research:** synchronized A/B/C replay, static and quarterly 50/50, all the requested metrics, and forward
  observations.

## What remains simulated or unverified

- Every write path (orders, closes, takeover closes) has run only against the simulated broker or the contract fake.
- Account identity, unrealized P&L and fresh quotes from the engine's own read-only adapter.
- Deposit evidence on eToro (no feed).
- The multi-strategy configuration on systemd or a VPS (not rehearsed in the container).
- Notification and off-host backup targets.

## Exact live broker and account blockers

1. **A read-only eToro key for this engine** (G5). It also unlocks G6 (unrealized P&L → equity), G28 (account identity)
   and fresh instrument eligibility (G3, G4).
2. **Gold Miner is live on the same account** (G8, G29). It must be disabled by the owner and verified with evidence.
3. **The GDX CFD short x5** (position <GM-POSITION-1>, <redacted> units, invested <redacted>, exposure about 3,843 USD) (G9).
   Its close needs either Gold Miner's own procedure, the eToro app, or a future released takeover execution.
4. **B3:** no reference id for close orders. A lost close response needs manual proceeds classification (G12).
5. The live writer has not been exercised on real eToro (G13), and there is no fresh-quote source (G14).
6. No VPS shell or root (G18). The paper cost model is optimistic for some instruments (G21).

## Actions that require the owner's authorization

| # | Action | Where |
|---|---|---|
| 1 | Provision a **read-only** eToro key (never Gold Miner's write key) and store it in `/etc/etoro-ute/readonly-credentials.env`, or reconnect the eToro connector | eToro settings / VPS |
| 2 | Grant a shell login and root once (or run `deployment/vps_preflight.sh` and `install.sh` yourself) | VPS |
| 3 | Decide the GDX short | owner |
| 4 | Disable Gold Miner by its own procedure; record `takeover writer-evidence --verdict DISABLED_VERIFIED` | Gold Miner VPS / engine CLI |
| 5 | Each takeover approval, with explicit bounds: `DISABLE_OLD_WRITER`, `CLOSE_POSITIONS --max-exposure`, `ACCEPT_LOSSES_AND_FEES --max-cost`, `ACTIVATE_STRATEGY` | engine CLI, each step separately |
| 6 | Confirm the 50/50 weights in spite of G30, or choose others | `config/engine.multi.yaml` before the first start, or `portfolio reallocate` |
| 7 | A separately reviewed live release (`LIVE_RELEASE_ENABLED` / `TAKEOVER_EXECUTION_RELEASED`), the owner approval file, write credentials, and the supervised canary of [LIVE_CUTOVER_RUNBOOK.md](LIVE_CUTOVER_RUNBOOK.md) §8 | future release |
| 8 | Accept or resolve B3 (a manual proceeds classification after a lost close) | owner |
| 9 | A webhook URL and an off-host backup target | owner |
| 10 | The Gold Miner retirement steps, each with its own approval file: freeze (DISABLE_OLD_WRITER), possible rollback, decommission after ≥ 30 days, separate purge; revoke the old key in eToro | Gold Miner host (`gm_retire.sh`) / eToro settings |
| 11 | The dashboard password and access method (SSH tunnel recommended) | VPS |
| 12 | Prefer funding the account through eToro Money internal transfers (automatically evidenced); classify any direct card or bank deposit once (`account resolve --classify DEPOSIT`) | owner |
