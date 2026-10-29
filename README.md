# eToro Universal Trading Engine (release candidate 0.9.0rc1)

This is a YAML-configured, broker-authoritative trading engine: one engine, one account ledger, one YAML per strategy,
and shared execution and risk.

- **Modes:** `simulation` (simulated broker with fault injection) and `shadow` (read-only eToro).
- **Live order submission is DISABLED.** Since Phase 2 an eToro live writer exists, but it is gated by a code
  constant, the schema and an owner-approval chain, and configuration alone cannot enable it. Live verdict: **NO-GO**
  (see [PHASE2_READINESS.md](PHASE2_READINESS.md)).

| Strategy | YAML | Status |
|---|---|---|
| QuantConnect #60, TECS -> SQQQ final target | [strategies/60.yaml](strategies/60.yaml) | implemented; parity with paper, frozen port and native LEAN |
| QuantConnect #529 (multi-instrument) | [strategies/529.yaml](strategies/529.yaml) | implemented via the hash-pinned frozen port; parity with recorded decisions; not live-eligible on eToro |
| Gold Miner #687 | [strategies/687.yaml](strategies/687.yaml) | reference only (UNAVAILABLE); the live #687 engine is never touched |

## Quick start (Windows or Linux, Python >= 3.11)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m pytest -q tests
.venv/bin/trading-engine config validate
.venv/bin/trading-engine --home var status
```

Shadow mode needs owner-provisioned **read-only** eToro credentials in the environment variables named in
`config/engine.yaml` (`UTE_ETORO_READONLY_API_KEY`, `UTE_ETORO_READONLY_USER_KEY`). Without them the engine fails
closed (BROKER_UNAVAILABLE).

## Deployment

Production runs on a VPS in **SHADOW** mode next to the live Gold Miner engine, fully isolated under `/home/ute`.
GitHub is the source of truth; the VPS deploys one pinned commit or reviewed tag at a time with
`deployment/sync_vps.sh` (backup → install → verify → automatic rollback) and checks itself with the read-only
`deployment/verify_vps.sh`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Real-account reports and evidence are kept
out of this public repository.

## Documentation

- [Existing-system inventory](docs/EXISTING_SYSTEM_INVENTORY.md)
- [Architecture](docs/ARCHITECTURE.md)
- [YAML schema](docs/STRATEGY_YAML_SCHEMA.md)
- [Accounting and reconciliation](docs/ACCOUNTING_AND_RECONCILIATION.md)
- [Order lifecycle](docs/ORDER_LIFECYCLE.md)
- [Risk engine](docs/RISK_ENGINE.md)
- [Strategy switching](docs/STRATEGY_SWITCHING.md)
- [eToro capabilities](docs/ETORO_CAPABILITIES.md)
- [Test matrix](docs/TEST_MATRIX.md)
- **[Deployment: GitHub → VPS (pinned commits, verification, rollback)](docs/DEPLOYMENT.md)**
- [Operations runbook](docs/OPERATIONS_RUNBOOK.md)
- [Release readiness](docs/RELEASE_READINESS.md) and **[Phase 2 readiness](PHASE2_READINESS.md)**
- [Learning notes](docs/LEARNING_NOTES.md)

## Layout

```
config/engine.yaml           engine configuration (shadow by default)
strategies/*.yaml            one file per strategy (fingerprinted)
src/utengine/core/           configuration, calendar, scheduler, market_data, strategy_api, portfolio, order_planner,
                             risk, halts, order_state, execution, accounting, reconciliation, strategy_manager,
                             security, engine
src/utengine/brokers/        base (reader/writer interfaces), etoro (read-only), simulated (writer, faults)
src/utengine/strategy_modules/  qc60, qc529, goldminer687 (reference), vendor/ (hash-pinned sources and ports)
src/utengine/persistence/    SQLite, migrations, hash-chained audit log
src/utengine/monitoring/     health
tests/                       28 release-blocking scenarios + unit tests; fixtures from recorded decisions
deployment/                  systemd units, install/rollback/uninstall, WSL rehearsal
scripts/make_fixtures.py     builds the parity fixtures from read-only sources
```

This repository is separate from `D:\Trading-Strategy-Research` (that repo forbids broker/order code) and from the
live Gold Miner engine. It contains no credentials. Runtime state lives in `UTE_HOME` and is git-ignored.

## Multi-strategy portfolio and account takeover (2026-10-29)

- `config/engine.multi.yaml` runs several strategies as virtual sleeves on one account. The owner's target is
  50/50 `QC_529_ETORO_RESTRICTED` + `QC_60_SQQQ`. See [docs/MULTI_STRATEGY_ARCHITECTURE.md](docs/MULTI_STRATEGY_ARCHITECTURE.md)
  and [docs/VIRTUAL_SLEEVE_ACCOUNTING.md](docs/VIRTUAL_SLEEVE_ACCOUNTING.md).
- Reconciliation is exception-only: [docs/AUTOMATIC_RECONCILIATION_POLICY.md](docs/AUTOMATIC_RECONCILIATION_POLICY.md).
- A read-only takeover preview and an approval-gated state machine: [docs/ACCOUNT_TAKEOVER_PREVIEW.md](docs/ACCOUNT_TAKEOVER_PREVIEW.md)
  and [docs/TAKEOVER_STATE_MACHINE.md](docs/TAKEOVER_STATE_MACHINE.md). Real-account execution is not released.
- Research: [docs/529_60_BLEND_COMPARISON.md](docs/529_60_BLEND_COMPARISON.md). Evidence:
  [docs/MULTI_STRATEGY_TEST_EVIDENCE.md](docs/MULTI_STRATEGY_TEST_EVIDENCE.md). Verdicts:
  [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md).
- New commands: `portfolio status|reallocate-preview|reallocate`, `exceptions`,
  `takeover preview|status|approve|revoke|cancel|writer-evidence|advance|enable-live`, and
  `account resolve --classify TRADE_CLOSE --intent <id>`.

## Phase 4 (2026-10-29): capital, Gold Miner retirement, dashboard

- Full-account capital and automatic, evidence-backed funding discovery: [docs/CAPITAL_AND_FUNDING.md](docs/CAPITAL_AND_FUNDING.md).
- The Gold Miner retirement procedure (rehearsed only): [docs/GOLD_MINER_RETIREMENT_PLAN.md](docs/GOLD_MINER_RETIREMENT_PLAN.md)
  and [docs/MIGRATION_AND_ROLLBACK_RUNBOOK.md](docs/MIGRATION_AND_ROLLBACK_RUNBOOK.md).
- Read-only, authenticated dashboard: `trading-engine dashboard set-password`, then `trading-engine dashboard serve`
  ([docs/DASHBOARD_USER_GUIDE.md](docs/DASHBOARD_USER_GUIDE.md), [docs/DASHBOARD_SECURITY.md](docs/DASHBOARD_SECURITY.md)).
- Evidence: [docs/phase4/PHASE4_REPORT.md](docs/phase4/PHASE4_REPORT.md).

