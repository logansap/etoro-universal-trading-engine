# Architecture

```
config/engine.yaml        strategies/{60,529,687}.yaml      (strict schema, fingerprints, explicit registry)
        |                          |
        v                          v
  core/configuration.py ---> strategy_modules/__init__.py REGISTRY {qc60, qc529, goldminer687(UNAVAILABLE)}
        |
        v
  core/engine.py  (one cycle every runtime.cycle_interval_seconds)
     |-- core/scheduler.py + core/calendar.py      which window is open (America/New_York, NYSE rules, DST)
     |-- core/reconciliation.py                    what the broker says happened (before/after everything)
     |-- core/market_data.py                       point-in-time daily snapshot + provenance hash
     |-- strategy module .decide()                 TARGETS ONLY -> StrategyDecision
     |-- core/portfolio.py                         capital + exposure -> executable USD targets
     |-- core/order_planner.py                     changes: phase 1 closes, phase 2 opens
     |-- core/risk.py + core/halts.py              permissibility; can only block or halt
     |-- core/execution.py + core/order_state.py   intents, state machine, evidence-only recovery
     |-- core/accounting.py                        cash-flow ledger, deposits, TWR, P&L
     |-- persistence/db.py                         SQLite WAL, migrations, hash-chained audit log
     |-- monitoring/health.py                      status / health / heartbeat
  brokers/base.py        BrokerReader (read)  |  BrokerWriter (write) -- separate capabilities
  brokers/etoro.py       READ-ONLY eToro adapter (allowlisted GETs + the eligibility query; no write method)
  brokers/simulated.py   the only BrokerWriter; eToro semantics + fault injection; persistent JSON state
  core/security.py       obtain_writer(): simulated broker in simulation, None in shadow, error otherwise
  cli.py                 trading-engine (see OPERATIONS_RUNBOOK.md)
```

## Separation of concerns

| Concern | Component | It may | It may NOT |
|---|---|---|---|
| Targets | strategy module | read a MarketSnapshot and its own state; return weights + cash | see the broker, cash, orders, positions |
| Capital and exposure | portfolio | turn weights into USD from **broker** equity; drop ineligible instruments to cash | invent capital or redistribute weight |
| Permissibility | risk | block legs or plans, raise halts | create orders, liquidate, switch strategy |
| Changes | planner | compute closes (phase 1) and opens (phase 2) for ENGINE-owned positions | touch pre-existing or external positions |
| Submit and read | broker adapter | call the broker | decide anything |
| What happened | reconciliation | classify positions, explain cash, halt | book balancing entries |

## The `decide` interface (STRATEGY_INTERFACE_VERSION 1.0)

`decide(market_snapshot, strategy_state, now) -> StrategyDecision` with:

- `signal_target` (before substitutions) and `target_weights` (final);
- `cash_weight` (explicit; the weights plus cash sum to 1);
- `instrument_ids`, `session`, `decided_at` (UTC), `strategy_version`, `module_version`, `config_fingerprint`;
- `rebalance` (#529 trades only when its label changes);
- `explanation` (branch path, indicator values), `data_provenance` (source, fetch time, SHA-256 of the bars,
  module and source hashes), `strategy_state`.

Multi-position decisions are first-class: #529 returns up to 19 weights.

## Time

- All stored timestamps are UTC ISO-8601.
- Exchange time is derived with `zoneinfo` (America/New_York).
- Broker timestamps are stored as the broker sends them, and quote age is checked against them.
- `--now` gives a fixed starting clock for rehearsals and tests only.

## Why a separate repository

`D:\Trading-Strategy-Research\CLAUDE.md`: *"This repo contains no broker/order code; do not add any."* The engine
therefore lives in `D:\etoro-universal-trading-engine` (its own git repository, own state directory, own service
identity).
