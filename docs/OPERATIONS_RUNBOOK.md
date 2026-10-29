# Operations runbook

Environment: `UTE_CONFIG` (engine.yaml) and `UTE_HOME` (state directory), or `--config` / `--home`.

## Daily

```bash
trading-engine status            # mode, active strategy, halts, discrepancies, unresolved orders, last decision, performance
trading-engine health            # exit 0 healthy, 1 unhealthy
trading-engine orders            # open intents (add --all for history)
trading-engine account status    # ownership, cash flows, discrepancies, performance
```

Expected rhythm (New York time):
- decision at 16:50 (13:50 on early closes), allowed until 09:15;
- execution from 09:31 for 60 minutes;
- idle reconciliation every 15 minutes;
- missed windows appear in `status.schedule_events` and are never caught up.

## Commands (all implemented and tested)

| Command | Purpose |
|---|---|
| `config validate` | schema, fingerprints, registry, module invariants |
| `config fingerprint <yaml> [--write]` | compute or write a strategy fingerprint (the active strategy then shows CONFIG_DRIFT until re-activated) |
| `account reconcile` | read the broker and reconcile now (exit 1 if not clean) |
| `account resolve --discrepancy ID --classify KIND --evidence TEXT --operator NAME` | classify an observed cash change |
| `account acknowledge-external --position-id ID --operator NAME --note TEXT` | accept a foreign position as excluded, or accept a foreign position's changed units as the new baseline (then classify its cash with `account resolve --classify EXTERNAL_TRADE`) |
| `strategy list` | strategies, enabled state, module status, active flag |
| `strategy preview-switch --to ID` | transition plan + hash |
| `strategy activate --to ID --plan-hash H --approved-by NAME` | execute the approved switch |
| `orders resolve --intent ID --as NOT_SUBMITTED\|REJECTED --operator NAME --evidence TEXT` | close an UNKNOWN intent after checking the broker by hand |
| `halt --reason TEXT --operator NAME` | emergency halt (no new exposure; nothing is liquidated) |
| `halt-clear --id ID --operator NAME --reason TEXT` | clear a halt |
| `run --once` / `run --service` | one cycle / the service loop (SIGTERM-safe) |
| `broker probe` | read-only credential and capability check |
| `verify` | audit chain, SQLite integrity, vendored hashes, drift |
| `backup --to DIR` / `restore-verify --backup FILE` | backup and restore drill |
| `sim deposit --amount X`, `sim external-open ...` | simulation mode only (rehearsals) |
| `report` | broker cash and account equity vs strategy equity, foreign positions, contributions, trading P&L, TWR |
| `data check` | market-data freshness, gaps and suspect prices for the active strategy (exit 1 if not OK) |
| `health --notify` | as `health`, plus a HEARTBEAT_STALE alert and notification flush (used by the timer) |
| `notify flush` / `notify test` / `notify crash` | deliver pending alerts / send a test / OnFailure hook |
| `backup --to DIR [--offsite]` | backup + restore drill (+ push off-host, pull back, verify); retention |
| `broker reconcile-audit --snapshot F ... --scratch-home DIR` | offline reconciliation of the real account from saved read-only Gold Miner audit snapshots |

## Incidents

| Symptom | Action |
|---|---|
| halt CASH_DISCREPANCY | check the eToro statement, then `account resolve`, then `halt-clear`, then `account reconcile` |
| halt EXTERNAL_POSITION | find out who traded. If it is legitimate: `account acknowledge-external` + `halt-clear`. Never close it from here |
| halt UNKNOWN_ORDER | check the eToro UI for the intent's instrument and amount. If a position exists, wait: the engine attributes it on the next cycle. If nothing exists after the window: `orders resolve --as NOT_SUBMITTED` |
| halt UNRESOLVED_ORDER | the order is pending or partial at the broker. The engine keeps polling it. Clear the halt after it is final |
| halt POSITION_MISMATCH / POSITION_MISSING | something changed an engine position outside the engine. Investigate before clearing |
| halt CONFIG_DRIFT | someone changed a strategy YAML or module. Revert it, or re-activate through preview and activate |
| halt BROKER_UNAVAILABLE | clears itself when reads work again; check the credentials or network |
| halt SWITCH_FAILED | review the switch plan and intents (`orders --all`), then preview again (forward or back) |
| unhealthy heartbeat | `systemctl status etoro-ute`; `journalctl -u etoro-ute -n 200` |

## Never

- Never put credentials in YAML, Git or the command line.
- Never point this engine at Gold Miner's write-scoped key.
- Never edit `engine.sqlite` by hand (the audit chain would show it).
- Never delete `/home/ute/data` (the engine state) or `/home/ute/backups`.

## Notifications (Phase 2)

- Every alert carries the ACTION to take (see `monitoring/notify.py`).
- Messages always go to `$UTE_HOME/alerts.jsonl` (and to journald through the service).
- When `UTE_ALERT_WEBHOOK_URL` is set in `/etc/etoro-ute/ops.env`, messages are also POSTed as JSON with a
  Slack/Teams/ntfy-compatible `text`. The URL is never logged.
- Duplicates within `notifications.dedupe_minutes` are not re-sent, and failed deliveries are retried.
