# Autonomous live trading (release 0.11.x, branch `release/autonomous-live`)

**Purpose:** replace Gold Miner's autonomous trading on the owner's eToro account with the Universal Engine running
`QC_529_ETORO_RESTRICTED` at 100%:
- the 11 instruments UVXY, TECL, SPXL, SQQQ, TLT, BIL, TQQQ, SOXS, QQQ, PSQ, QLD;
- every other target weight in cash;
- frozen rules;
- decision after the close (16:50 ET), execution at the next regular session open.

**Routine orders need no approval.** A human is needed only for:
- the one-time activation;
- exceptions that fail closed;
- capital or strategy changes;
- emergencies.

**The SHADOW release `v0.9.0rc3` stays the rollback point.**

## One-time activation: every gate is required

**Installing any config template by hand** (live, single529, pilot): set the absolute strategies path afterwards,
`sed -i "s#^  strategies_dir: .*#  strategies_dir: /home/ute/current/strategies#" /etc/etoro-ute/engine.yaml`.
`install.sh` does this on every deployment, but a hand-installed template keeps the relative path and the engine
refuses to start.

1. **The code gate.** `LIVE_RELEASE_ENABLED = True` is set only in this release. The SHADOW and pilot releases
   contain `False`.
2. **Two configuration switches,** both required, each refused alone:
   - `runtime.mode: live`;
   - `security.live_execution_enabled: true`.

   A `live_controls` section is also required. Template: `config/engine.live.yaml`. A strategy YAML can never
   request live execution.
3. **The owner activation file** `/etc/etoro-ute/live-activation.json` (root:ute 0640). Print its exact skeleton with
   `trading-engine live-activation-template`:
   ```json
   {"kind": "AUTONOMOUS_LIVE", "phrase": "I_ACTIVATE_AUTONOMOUS_LIVE_TRADING_OF_THIS_ACCOUNT_BY_THE_UNIVERSAL_ENGINE",
    "release_sha": "<this code>", "config_fingerprint": "<this config>", "approved_by": "<owner>",
    "expires_at": null}
   ```
   - It is bound to the exact code and configuration: any change requires a new activation.
   - `expires_at: null` means until revoked; an ISO date may be set instead.
4. **A new trading key,** `/etc/etoro-ute/trading-credentials.env` (root:ute 0640, LF):
   - it defines exactly `UTE_ETORO_TRADING_API_KEY` and `UTE_ETORO_TRADING_USER_KEY`;
   - only `etoro-ute.service` loads it;
   - it must have a trade.real **write** scope, proven at start with `GET /api/v1/me`;
   - it must not be the read-only key.
5. **The forbidden-key file** `/etc/etoro-ute/forbidden-user-key.sha256`:
   - it holds the old Gold Miner user-key hash, produced by `gm_archive_vps.sh`;
   - it must exist;
   - a trading key with a hash in this file is refused.
6. **No kill switch.**

Reads always use the read-only key. The trading key is used only to send orders and for the one start-time scope
check.

## What happens automatically

| Situation | Behaviour |
|---|---|
| Normal session | decision at 16:50 ET, then sell-then-buy orders at the next open plus the delay, each one checked when sent: kill switch, `max_order_usd`, `max_gross_usd`, `max_orders_per_session`, daily and cumulative loss stops, broker quote valid, ≤ 120 s old, spread ≤ `max_spread_bp` |
| Friday decision | executed at Monday's open (the next regular session); nothing over the weekend |
| Quote stale or spread too wide at the open | the order is **HELD** and retried every cycle **inside the window**; sent once when fresh |
| Window ends with orders unsent (engine down, held all window) | **ABANDONED**; the old plan is **never** sent late. The **standing target** is carried forward to the **next valid window** with fresh quotes, sizes and every check (`UNEXECUTED_TARGET_CARRIED_FORWARD`) |
| HOLD decision with the target already executed | nothing is traded (strategy semantics) |
| New capital through an audited transfer (restructure, reallocation, takeover pool, classified foreign-position proceeds) | invested once at the standing target at the next execution (`CAPITAL_CATCH_UP`) |
| Broker unreadable | nothing is sent; a `BROKER_UNAVAILABLE` halt **clears itself** on the next good read, and the plan resumes inside its window |
| Partial fill | tracked to completion from broker status; never re-sent |
| Lost response after the broker accepted | open: reference lookup or position evidence. Close: the pending close order that names the position, or position evidence. Never resubmitted |
| **Outcome cannot be proven** | **UNKNOWN: halt ALL plus a CRITICAL alert, nothing resubmitted.** The engine waits for broker evidence (checked every cycle) or an audited operator resolution |
| Crash or restart | durable state; finished intents are never redone, in-flight ones are only queried; no duplicate opens or closes (restart tests at every stage) |
| External change on the account, unexplained cash leaving | halt ALL plus an alert (fail closed) |

**The limits are operational, not loss guarantees.** Overnight gaps, leveraged-ETF moves beyond history, broker
outages and execution failures can produce larger losses. The stops block new exposure, or halt; they close nothing
by themselves.

## Alerts, reporting and the dashboard

**Alerts:**
- every halt (unknown order, kill switch, loss stops, external change, ledger mismatch) raises one de-duplicated
  CRITICAL alert;
- alerts go to `notifications.webhook_url_env` (when set) and to the outbox;
- `trading-engine exceptions` lists the open ones.

**Reporting:** `portfolio status`, `report` and `health` show equity, positions, P&L, drawdown, orders and halts. The
authenticated read-only dashboard (`127.0.0.1:8790`, through an SSH tunnel) shows the same.

## Emergency shutdown (any one is enough)

| Action | Effect |
|---|---|
| `trading-engine kill-switch --operator <name> --reason <text>` | writes `KILL_SWITCH` and halts ALL; every later order is blocked. Resume: remove the file, then `halt-clear` (both audited) |
| `touch /etc/etoro-ute/KILL_SWITCH` (root) | the same gate, without the CLI |
| `systemctl stop etoro-ute.service` | no cycle runs |
| Revoke the trading key in eToro | the engine can no longer send anything (proven at the broker) |

**Positions stay as they are.** Closing them is a separate decision, made by the owner in the eToro app or through a
normal strategy rebalance.

## Rollback

1. Stop: `systemctl stop etoro-ute.service`.
2. Install `config/engine.single529.yaml` (SHADOW), with the absolute path: `sed -i "s#^  strategies_dir: .*#  strategies_dir: /home/ute/current/strategies#" /etc/etoro-ute/engine.yaml`. The activation stays inert, because it is bound to the live
   configuration.
3. In SHADOW, if wanted, return to the SHADOW release: `sync_vps.sh --ref v0.9.0rc3 --expect-sha 7f6109d…`.

`sync_vps.sh` only ever deploys a SHADOW configuration, so a deployment can never happen while live.

## Broker capabilities: demonstrated vs untested on this account

| Capability | Status |
|---|---|
| `POST /api/v2/trading/execution/orders`, x1 long CFD open (body without `leverage`) | **demonstrated by Gold Miner** (SQQQ, SOXS, QQQ, since 2026-08-27) |
| Same open for TQQQ, TECL, SPXL, UVXY, TLT, BIL, PSQ, QLD | same endpoint, body and settlement. **The instrument itself is not yet traded on this account** (eligibility API: allowOpen, x1 long CFD) |
| `GET orders:lookup?orderId=` (open status) | **demonstrated by Gold Miner** |
| `POST market-close-orders/positions/{id}` with `{InstrumentId, UnitsToDeduct}` (full and partial close) | **demonstrated by Gold Miner** (including a rejected partial close, errorCode 776) |
| `GET close-orders/{orderId}` (close status, proceeds) | **demonstrated by Gold Miner** |
| `ordersForClose` in the portfolio (pending close) | documented; parsed; **not yet observed on this account** |
| `orders:lookup?referenceId=<x-request-id>` (lost-open recovery) | documented by eToro; **never exercised by Gold Miner**. Position evidence is the fallback |
| WebSocket quote snapshots (`instrument:<id>`) | **demonstrated** with the read-only key (2026-10-30: 11/11 fresh at the open) |
| Real partial fills of x1 market orders | **never observed on this account** (handled from status; simulated and fake-HTTP tests) |
| Overnight fee on x1 long CFD | 0 by eToro's rule; to be confirmed on the first night |
