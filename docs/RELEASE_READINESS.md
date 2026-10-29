# Release readiness: 0.9.0rc1 (2026-10-27)

> **Superseded in part by [PHASE2_READINESS.md](../PHASE2_READINESS.md).** A live writer now exists but is
> release-gated in code. The capability table below reflects the Phase 1 baseline.

## Verdict

**Release candidate: ACCEPTED as a disabled release (simulation + shadow).**

**Live trading: NO-GO.**
- Live order submission is NOT IMPLEMENTED.
- Two critical items are open: the account is shared with the LIVE Gold Miner engine, and the eToro read adapter
  is unverified for this engine.
- Nothing in this release can submit a real order: there is no writer class, the transport is read-only, and
  `live` is refused in code.

## Evidence

| Item | Result |
|---|---|
| Test suite, Windows (CPython 3.13.14) | **96 passed, 0 failed, 0 skipped** (`python -m pytest -q tests`) |
| Test suite, Linux (WSL2 Ubuntu 26.04, CPython 3.14.4) | **96 passed, 0 failed, 0 skipped** (rehearsal of the final code commit 3208860; c43328e: 95/95) |
| skip / xfail markers | none in the suite |
| Clean install (fresh venv, non-editable, pinned requirements) | PASS on Windows and Linux; vendored sources are packaged and their hashes verify |
| Deployment rehearsal (systemd user unit, kill -9, auto-restart, backup, restore, rollback) | **PASS**, all checks, twice ([c43328e](rehearsal/WSL_REHEARSAL_2026-10-27.md), [3208860, tarball sha256 b82d69c9...](rehearsal/WSL_REHEARSAL_2026-10-27_3208860.md)) |
| Live Yahoo provider vs the recorded paper snapshot (10-26) | identical closes (0.00 bp on all 9 symbols), the same target, RSI difference 0 |
| Gold Miner / championship / X60 untouched | read-only access only; both paper tasks still Running; GM unchanged (commit 7a82e3f, production_armed) |

## Capability classification

| Capability | Class | Notes |
|---|---|---|
| Strict YAML schema, registry, fingerprints, tamper and drift detection | **PASS** | s27 |
| Live cannot be enabled by YAML; no live writer; shadow has no writer, also after restart | **PASS** | s26, s27, safety tests |
| #60 frozen tree port | **PASS** | 8/8 paper decisions, 537/537 frozen-port sessions, 48/48 native-LEAN sessions |
| TECS signal kept, SQQQ final target | **PASS** | s02 (real-data branches + synthetic) |
| #529 adapter (multi-instrument) | **PASS** (recorded sessions) | parity on the 3 recorded sessions; long-history parity is inherited from the research #504 port check |
| #687 | **NOT IMPLEMENTED** (by scope) | reference YAML; UNAVAILABLE |
| NYSE calendar, early closes, DST, missed-window policy | **PASS** | s19 (published 2024-27 holidays) |
| Broker-authoritative cash reconciliation, discrepancy halt, no balancing entries | **PASS** (simulated broker) | s07, s08, s28 |
| Deposit auto-detection and classification | **PASS** (simulated) / **UNVERIFIED** (eToro) | eToro has no verified trading-account cash-transaction feed, so a deposit halts and needs operator classification |
| Pre-existing and external positions excluded, never touched | **PASS** | s04, s23 |
| Position mismatch / vanished position halt | **PASS** | s09 |
| Order state machine, persist-before-submit, deterministic intents, never resubmit | **PASS** | s10-s17 |
| Partial fills, rejections, timeouts, network loss, crashes, restarts | **PASS** (fault injection) | s10-s16, s25 |
| Sell-then-buy, resize to available cash, minimums, caps | **PASS** | s20, units |
| Risk limits, emergency halt (no liquidation) | **PASS** | s20, s24 |
| Strategy switching (preview, hash, approval, TTL, state binding, rollback) | **PASS** (simulation) | s21, s22 |
| Durable DB, migrations, append-only hash-chained audit, backup/restore drill | **PASS** | units, rehearsal |
| Structured logs without credentials | **PASS** | s26 |
| Read-only eToro adapter against the real account | **UNVERIFIED** (critical for shadow) | no read-only key provisioned; endpoints and shapes are proven only by GM |
| eToro fields `unrealizedPnL.pnL`, `openDateTime` | **UNVERIFIED** | documented names; if absent, strategy equity is unavailable and the engine fails closed |
| Production host (VPS) install, system unit as root with `User=ute` + sandboxing, boot start | **UNVERIFIED** | rehearsed as a user unit only |
| Account exclusivity (`ownership: exclusive`) | **FAIL** (precondition) | the LIVE Gold Miner trades the same account |
| Live order submission | **NOT IMPLEMENTED** | by design |
| Adopting pre-existing positions, forced liquidation, leverage/shorts/broker stops | **NOT IMPLEMENTED** | by design |
| External alert delivery (mail/push) | **NOT IMPLEMENTED** | alerts are in the DB, `status`, the log and the health timer exit code |

## Risks and blockers, by severity

**CRITICAL**
1. **Shared account with the LIVE Gold Miner.** Its trades and cash would appear as external activity (correctly
   halting this engine). Exclusive ownership is false. Activation needs GM decommissioned, or a dedicated account
   or Agent Portfolio (owner decision).
2. **No live writer.** Live order code must be built, reviewed and canaried in a separate, separately approved
   release.
3. **Read adapter unverified for this engine.** It needs a read-only key and `broker probe`.

**HIGH**
4. **eToro offers no client idempotency key.** An ambiguous write needs a human (it halts; it is never resent).
5. **eToro REST quotes can be stale.** GM observed a 72-minute-old REST rate. The 120 s quote-age rule will then
   block legs (fail closed); a WebSocket quote source would be needed for live.
6. **#60-SQQQ diverges from the source when the tree selects BSV.** BSV has no verified instrument id, so that
   weight stays cash. TECS is replaced by SQQQ, as in the owner-approved experiment.
7. **Deposits, dividends and CFD adjustments on eToro have no verified feed.** Each one halts until classified.

**MEDIUM**
8. **Free Yahoo endpoint dependency.** A change or rate limit means NO_DECISION (fail closed), not a wrong trade.
9. **Single-host SQLite state.** Nightly local backups plus a restore drill; no off-host copy without owner approval.
10. **Partial-close trade-history schema unverified.** Close-order proceeds are used instead.

**LOW**
11. The rehearsal used systemd `--user`. The production unit's sandboxing directives are unexercised.
12. Alerts are not pushed.

## Settings that need owner approval to change

`risk.daily_loss_halt_pct` 0.20, `risk.drawdown_halt_pct` 0.50, `risk.max_single_order_usd` 25,000,
`risk.max_quote_age_seconds` 120, `account.cash_tolerance_usd` 0.05, `risk.halt_allows_reductions` false,
`allocation.capital_fraction` 1.0, and the BSV handling.

## Future activation: every step needs separate, explicit owner approval

1. **Account.** Decommission or migrate the live Gold Miner through its own procedures, or open a dedicated eToro
   account or Agent Portfolio, so that `ownership: exclusive` is true. Record the decision in
   `D:\Trading-Strategy-Research\state\HUMAN_QUEUE.md`.
2. **Read-only credential.** The owner creates a key without write scope (never GM's key) and stores it on the
   host in `/etc/etoro-ute/readonly-credentials.env` (0640 root:ute). Then `trading-engine broker probe` must
   succeed, and ETORO_CAPABILITIES.md is updated from UNVERIFIED to VERIFIED.
3. **Host.** Approve the target host. Run `sudo sh deployment/install.sh <tarball> --enable` (shadow), then verify
   the system unit, sandboxing and boot start.
4. **Shadow period.** At least 20 sessions with clean reconciliations, decisions identical to the X60 paper
   experiment, no unexplained halts, and a reviewed incident log.
5. **Live release (new code, new review).**
   - Add an eToro writer class (create-order and close-position; order and close-order lookup already exist).
   - Add a fresh-quote source (WebSocket).
   - Add independent gates, each checked in code and each separately approved:
     - (a) a release built with the writer;
     - (b) an owner-signed approval file bound to the release hash and config fingerprint;
     - (c) a separate write-scoped credential present only on the host;
     - (d) a per-day notional cap;
     - (e) a kill-switch file;
     - (f) a supervised canary (one USD 10-25 order, open and close, reconciled).
6. **Risk sign-off.** The owner approves the limits above.
7. **Go-live decision.** The owner records explicit approval; the engine is switched from shadow to the live
   release.
