# Dashboard architecture

**What it is.** The dashboard is a functional, responsive, **read-only** web application built into the engine
package (`src/utengine/dashboard/`). It uses only the Python standard library: `http.server`, `sqlite3`,
`hashlib.scrypt` and `secrets`. There is no web framework, no JavaScript build step, no CDN, and no commercial or
third-party dependency.

```
 browser (desktop / phone)                 SSH tunnel or VPN (never the public internet)
        |  HTTPS terminated by the tunnel; cookie HttpOnly + SameSite=Strict
        v
 dashboard/server.py   127.0.0.1:8790   ThreadingHTTPServer
   GET /login, POST /login, POST /logout        (authentication only)
   GET /, GET /api/{meta,overview,strategies,trades,operations,migration}   (read-only)
   anything else -> 404 / 405
        |
 dashboard/data.py     builds every number from the engine's DATABASE, opened mode=ro + PRAGMA query_only
   reuses the engine's own read functions: sleeves.valuation, funding.capital_report, retirement.readiness, halts
        |
 /home/ute/data/engine.sqlite  <- written ONLY by the engine service (separate process, separate unit)
```

**Why a separate process that reads the database.** The dashboard never constructs the trading engine or a broker
adapter, so it cannot reach an order path. A test scans the package source for `Executor`, `submit_*`,
`obtain_writer`, `TradingEngine` and similar names. It needs **no broker credential** at all, and it refuses to start
if a broker *write* credential is in its environment. A crash or overload of the dashboard cannot affect trading.

**Broker data provenance.** The dashboard shows the broker numbers from the engine's last recorded reconciliation
snapshot, with its age and a label:

| Label | Meaning |
|---|---|
| `SIMULATED` | simulation mode (simulated broker) |
| `SHADOW` | real broker data, read-only engine |
| `VERIFIED` | live mode (future), fresh and clean |
| `UNRECONCILED` | the latest reconciliation was not clean |
| `STALE` | older than 15 minutes, or the last read failed |
| `UNAVAILABLE` | the broker did not supply it; shown as unavailable, never estimated |

## Sections and their sources

| Section | Shows | Source |
|---|---|---|
| Overview | account equity, available cash, reserved (pending opens), invested, unrealized P&L, strategy capital, suspense, foreign positions; day/week/month/lifetime P&L excluding deposits; equity curve and drawdown; per-sleeve assigned capital vs invested; capital flows by kind | last snapshot, `funding.capital_report`, `daily_equity`, sleeve valuation, `cash_flows` |
| Strategies | per sleeve: equity, cash, TWR, max drawdown, realized/unrealized P&L (average cost), holdings and weights, latest decision with targets and full explanation, **#529 crash-guard** state and QQQ 10-day return, **#60 regime** (SPY vs SMA200) and branch path, weight held in cash because an instrument is not executable here, overlap and combined exposure | `sleeve_ledger`, `sleeve_daily`, `decisions`, strategy YAML eligibility |
| Trades | orders (intents) with status and sleeve attribution, sleeve-attributed fills and crossings, fees, financing and adjustments, rejected/canceled, pending/unresolved, partial fills | `intents`, `sleeve_ledger`, `cash_flows` |
| Operations | heartbeat, broker connectivity and label, market-data health, last reconciliation, halts (account and sleeve), **open exceptions only** (actionable), alert history, audit-chain verification, schema version, backups, last decision, next decision and execution windows per sleeve | `heartbeat`, `audit_log`, `data_health`, `exceptions`, `alerts`, scheduler |
| Migration | Gold Miner retirement checklist and the two-writer risk, foreign positions, pending orders, the latest takeover preview (positions, capital, allocation, risks), approvals and their status, required approvals and blockers, audit trail | `retirement.readiness`, `takeover_*`, `ownership`, `audit_log` |

## Front end

- **Two inline HTML documents** (`dashboard/page.py`): login and app, in vanilla JavaScript.
- **Tabs** are hash routes (`#overview`, …).
- **Refresh:** every 30 seconds while the page is visible.
- **Charts:** inline SVG.
- **Layout:** CSS grid, two columns of cards on phones, light and dark themes, no horizontal overflow at 390 px
  (measured in [DASHBOARD_VALIDATION.md](DASHBOARD_VALIDATION.md)).
- **Mode banner:** a prominent banner shows the **SIMULATION / SHADOW / LIVE** mode and that live trading is
  disabled.

## Future administrative actions

- **Separation.** Any future operator action (approve, halt-clear, reallocate) must be a **separate** service and
  unit, with its own authentication (and a second factor), its own audit, and no shared code path with this read
  server.
- **This server must stay GET-only**, apart from login and logout.
- **Live trading:** there is no live-trading toggle. Live depends on code-level release gates that no web request can
  change.
