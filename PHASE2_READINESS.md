# Phase 2 readiness: production hardening (2026-10-27)

Baseline `ec48c9a`. Phase 2 commits: `56f321c`, `96ca250`, `a97955d`, `9606217` and the documentation commit that
adds this file. **Real-money trading stayed disabled throughout. No real order was placed or canceled, and Gold
Miner and the paper portfolios were not modified.**

## 1. eToro API capabilities verified

| Capability | Status |
|---|---|
| credit / available cash, positions + ids, pending orders, trade history (fields), `/me` scopes | **verified live**: read on the real account through the read-only Gold Miner audit (same field names the adapter parses) |
| orders:lookup by `orderId`; close-orders/{id} with `proceeds` | verified live by Gold Miner (earlier); contract-tested here |
| **orders:lookup by `referenceId` = the client `x-request-id`** | **documented** (create-order spec: "the only handle you have" when the 200 is lost); implemented (deterministic uuid5 per intent); contract-tested; never exercised live |
| order status ids 6-12 (PendingCancel ... PendingTriggeredRate) | documented; handled |
| unrealized P&L (`positions[].unrealizedPnL.pnL`), hence account equity | documented; **not verified** (absent from the audit) |
| deposits / withdrawals for the trading account | **no authorized source exists** (cash-account, balance-history and transfer APIs reviewed) |
| closes: client reference / idempotency | **none documented** (no `referenceId` on close) |
| this engine with its OWN read-only key | **not possible**: none provisioned. The eToro MCP connector is invalidated (the owner must reconnect it) |

Details: [docs/ETORO_CAPABILITIES.md](docs/ETORO_CAPABILITIES.md),
docs/phase2/REAL_ACCOUNT_RECONCILIATION_2026-10-27.md (private report, not published).

## 2. Real account reconciliation status

- Two read-only audit snapshots (16:52Z and 18:12Z) were replayed through the engine's reconciliation in a scratch
  database (`trading-engine broker reconcile-audit`). Both reconciled **CLEAN**.
- Cash is <redacted>, and **the Gold Miner GDX CFD short (x5, USD <redacted>) is PRE_EXISTING**: excluded, never touched,
  and no order references it.
- Strategy equity is <redacted>. Account equity is **unavailable** (no unrealized P&L in the audit) and is reported
  that way, never estimated.
- A synthetic test in the audit's exact shape shows that a new Gold Miner trade halts with EXTERNAL_POSITION and
  that an unexplained cash change raises CASH_DISCREPANCY.

**Not achieved:** reconciliation through the engine's own adapter, because there is no independent credential.

## 3. Deposit detection status

- **Detection:** automatic. Every unexplained cash change is found on the next reconciliation and halts new orders.
- **Automatic classification:** implemented for brokers that provide cash-transaction evidence (proven with the
  simulated broker).
- **On eToro:** **unavailable**, because no trading-account deposit feed exists. The safe manual path is kept:
  `account resolve` classifies the *observed* amount, the sign is checked, it is audited, and nothing is invented.
- **Reporting:** the account report separates initial capital, deposits, withdrawals, foreign-position cash,
  trading P&L (strategy equity minus net contributions) and the funding-adjusted TWR.

## 4. Live execution implementation and authorization boundaries

**Implemented:** `brokers/etoro_live.py`.
- It uses the documented create-order route (v2, `x-request-id` = uuid5(intent id)) and the close-by-position route.
- Its own write allowlist covers exactly those two routes.
- Error mapping: 4xx is definite; 5xx, timeouts and unreadable responses are UNKNOWN.

**Executor:** an UNKNOWN open is resolved by `referenceId` first, then by position evidence. It is never resubmitted.

**Boundaries (each independent, all must open):**
1. The code constant `LIVE_RELEASE_ENABLED = False`. Only a new, reviewed release can change it.
2. The schema refuses `runtime.mode: live` and `live_execution_enabled: true`.
3. `authorize_live()` additionally needs:
   - a live configuration;
   - an owner approval file bound to the release SHA and config fingerprint, not expired;
   - separate write-credential variables (`UTE_ETORO_WRITE_*`);
   - no kill-switch file.
4. `obtain_writer()` never constructs the live writer. The production base URL refuses construction without a
   `LiveAuthorization`, which only `authorize_live()` can create.
5. Without authorization the writer accepts only the unroutable contract-test host
   `https://etoro-contract-test.invalid`.

Tests: `tests/test_etoro_live_contract.py` covers the gates, including that opening the code gate alone is not
enough, and runs execution against a fake of the documented API.

## 5. VPS deployment results

**Not deployed.** Exact missing permission:

- `gold-miner-audit` (user `claude-audit`) is a **forced-command account**: no shell, no scp/sftp, no sudo.
- A shadow install needs:
  1. an SSH login with a shell for a deploy user;
  2. **root/sudo once** to run `deployment/install.sh`. It creates the system user `ute`, writes /opt, /etc and
     /var/lib, and installs and enables the units. The rootless alternative needs root once for
     `loginctl enable-linger ute`.
  3. outbound HTTPS to `query1.finance.yahoo.com` and `public-api.etoro.com`;
  4. the owner's read-only eToro key in `/etc/etoro-ute/readonly-credentials.env`.

**Production-equivalent rehearsal (performed):** a disposable Ubuntu 24.04 LTS container running systemd 255 as
PID 1 and Python 3.12. `install.sh` ran as root in shadow mode next to a **mock Gold Miner** (user, unit, files,
credentials). Result: **30/30 PASS** ([docs/phase2/CONTAINER_REHEARSAL_9606217.md](docs/phase2/CONTAINER_REHEARSAL_9606217.md)).

- **Startup:** systemd start as `User=ute` with full sandboxing.
- **Isolation:** the engine's sandbox cannot read the Gold Miner home or its credentials, and no GM variable is in
  its environment.
- **Fail-closed shadow:** without a key, a BROKER_UNAVAILABLE halt and no writer.
- **Live refused** by the installed code.
- **Live market-data check:** OK.
- **Logs:** JSON and journald.
- **Restart and crash:** `kill -9`, then a systemd restart, then a CRASH alert; the OnFailure notifier delivers.
- **Timers:** health and backup timers run.
- **Backups:** backup, local restore drill, and an off-host copy pulled back and verified.
- **Upgrade and rollback:** upgrade, then `rollback.sh`.
- **Shutdown:** a safe shutdown records ENGINE_STOP.
- **Boot:** the container restart started the service, with no false crash alert.
- **Gold Miner:** its files, credentials and unit are byte-identical, and the engine wrote nowhere else.

The rehearsal found and fixed **two real deployment bugs**:
1. venvs are not relocatable, so there is now a per-release venv behind a symlink;
2. helper units under `ProtectSystem=strict` had no writable /tmp, so they now set `PrivateTmp`.

The user-level WSL rehearsal (Ubuntu 26.04) passed 16/16, with 120/120 tests on Linux.

## 6. Fault-injection results

**Simulated broker (engine level), all PASS:**
- partial fills, rejections, timeout after acceptance (by reference, and by position evidence when the lookup is
  down);
- unknown outcome (halt, never resent), crash before and after submit, restart with pending orders;
- duplicate cycles, stale or suspect data, oversized orders, emergency halt, switch rollback;
- hard process kill (s25).

**Contract fake of the eToro API (live writer + read adapter + executor + reconciliation), all PASS:**
- documented request body and x-request-id;
- reject (accepted-then-rejected, and HTTP 400);
- PartiallyFilled followed by completion; CanceledPartiallyFilled as final;
- lost response and HTTP 500 after acceptance, both resolved by `referenceId` with one POST;
- dropped request (UNKNOWN + halt, no second POST across restarts);
- crash after acceptance, then restart, then FILLED;
- duplicate-plan reruns (unique, stable request ids);
- multi-instrument close-then-open transition reconciled CLEAN; partial close (split);
- lost close response, which leads to a **CASH_DISCREPANCY halt (blocker B3)**.

The Gold Miner short is untouched in every contract scenario.

**Totals:** 120 tests passed (Windows 3.13 and Linux 3.14), 0 failed, 0 skipped.

## 7. Alerts and backup verification

**Alerts:**
- Every alert carries a runbook ACTION.
- Delivery goes to the outbox file plus an optional webhook (`UTE_ALERT_WEBHOOK_URL`, kept in the environment and
  redacted), with dedupe and retry.
- Covered events: reconciliation failures and halts, missed decisions and executions, unresolved and unknown
  orders, crashes (startup detection plus a systemd OnFailure unit), stale heartbeat (`health --notify` timer),
  stale or suspect data, and backup failure.
- Tested against a local webhook, including a 500 followed by a retry.
- **No real endpoint is configured.** The owner supplies one.

**Backups:**
- Online SQLite backup, then the restore drill on the local copy, then retention.
- `--offsite` pushes the backup (copy to a mounted remote at `/mnt/etoro-ute-offsite`, or rsync) and **re-verifies
  the pulled-back copy**. Verified in tests and in the container.
- **No real off-host destination is configured.** Uploading backups needs the owner's approval of the destination.

## 8. Remaining critical blockers (live trading)

| # | Severity | Blocker |
|---|---|---|
| B1 | CRITICAL | The live Gold Miner trades the **same account**. Exclusive ownership is false, so Gold Miner's activity halts this engine (correctly). Needs GM decommissioned, or a dedicated account or Agent Portfolio |
| B2 | CRITICAL | **No independent read-only credential.** The adapter has never read the real account itself, and equity / unrealized P&L are unverified |
| B3 | CRITICAL | **A lost close response cannot be economically resolved.** There is no close `referenceId`, and the close's proceeds cannot be seen through any verified read, so the result is a CASH_DISCREPANCY halt that needs a human. Needs a verified close-order discovery or a verified trade-history cash mapping |
| B4 | CRITICAL | The **live writer has only been tested against a fake** of the documented API. It needs a supervised real canary (one USD 10-25 open and close, reconciled) after the gates are approved |
| B5 | HIGH | eToro does not document what happens to a reused `x-request-id`. A request that never reached eToro (the drop case) can only be closed by an operator |
| B6 | HIGH | No trading-account deposit feed: deposits halt until classified (safe, but manual) |
| B7 | HIGH | No fresh-quote source (REST rates can be stale, as GM observed); the 120 s quote rule blocks legs |
| B8 | MEDIUM | VPS shadow deployment not performed (permission: §5) |
| B9 | MEDIUM | Off-host backup destination and alert webhook not configured (owner decisions) |
| B10 | LOW | BSV has no verified instrument id (#60 keeps that weight in cash) |

## 9. Verdict

- **Live trading: NO-GO** (B1-B4 critical, unresolved).
- **VPS shadow deployment: NOT PERFORMED** (missing permission and read-only key). It is ready to install as rehearsed.
- **Engine release: hardened release candidate**, with simulation and shadow only and live disabled in code. Simulated
  and contract tests passing are **not** evidence of production readiness for live money.

## 10. Git commit and changed files

- **Repository:** `D:\etoro-universal-trading-engine`, branch `main`.
- **Commits:** `56f321c`, `96ca250`, `a97955d`, `9606217`, plus this documentation commit (see `git log`).
- **New:**
  - `src/utengine/brokers/etoro_live.py`
  - `src/utengine/monitoring/notify.py`
  - `src/utengine/tools/{__init__,gm_audit}.py`
  - `tests/{fake_etoro,test_etoro_live_contract,test_phase2_ops}.py`
  - `deployment/etoro-ute-notify-crash.service`
  - `deployment/container/{Dockerfile,rehearse_in_container.sh,run_rehearsal.sh}`
  - `docs/phase2/*`
  - `PHASE2_READINESS.md`
- **Changed:**
  - `src/utengine/{cli.py, core/security.py, core/execution.py, core/order_state.py, core/engine.py, core/accounting.py, core/market_data.py, core/configuration.py, brokers/etoro.py, brokers/simulated.py, persistence/db.py (migration 2), monitoring/health.py}`
  - `config/engine.yaml` (notifications and backup sections)
  - `deployment/{install.sh, rollback.sh, uninstall.sh, etoro-ute*.service}`
  - `tests/{test_config_safety,test_execution}.py`
  - docs (ETORO_CAPABILITIES, ORDER_LIFECYCLE, DEPLOYMENT_AND_ROLLBACK, OPERATIONS_RUNBOOK, TEST_MATRIX,
    RELEASE_READINESS, ACCOUNTING_AND_RECONCILIATION), README
