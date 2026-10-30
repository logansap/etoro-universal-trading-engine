# Supervised live pilot: operator runbook (pilot release 0.10.0a1)

**What this release adds:** runtime mode `supervised_live`.
- **Every order** waits until a human approves that exact intent.
- **Hard limits** are checked when an order would be sent.
- **Autonomous live trading stays impossible:** `runtime.mode: live` is refused, `LIVE_RELEASE_ENABLED = False`.
- The SHADOW release (`v0.9.0rc3`) is unaffected.

**The limits are operational, not loss guarantees.** Overnight gaps, leveraged-ETF moves beyond history, broker
outages and execution failures can lose more than any stop value. The stops only block new exposure, or halt; they
close nothing.

## Gates: all required, each a separate owner action

1. The release is deployed in **SHADOW**, and `verify_vps.sh` reports ALL CHECKS PASSED. `sync_vps.sh` only deploys
   SHADOW configurations.
2. The old writer is gone at the broker: its key is **revoked** in eToro, so a 401/403 is proven.
3. The forbidden-key file is installed at `/etc/etoro-ute/forbidden-user-key.sha256`, holding the hash of the old
   key.
4. A **new** eToro key is created by the owner with trade.real **write** scope. It is never Gold Miner's key and never
   the read-only key.
   - It is stored as `/etc/etoro-ute/trading-credentials.env` (root:ute 0640, LF).
   - The file defines exactly `UTE_ETORO_TRADING_API_KEY` and `UTE_ETORO_TRADING_USER_KEY`.
   - Only `etoro-ute.service` loads it.
5. The configuration is `config/engine.pilot.yaml`, installed as `/etc/etoro-ute/engine.yaml`. It sets
   `supervised_live` and the pilot limits.
6. The owner approval file `/etc/etoro-ute/pilot-approval.json`, root:ute 0640:
   ```json
   {"kind": "SUPERVISED_PILOT", "phrase": "I_APPROVE_SUPERVISED_LIVE_PILOT_WITH_PER_ORDER_APPROVAL",
    "release_sha": "<from pilot-approval-template>", "config_fingerprint": "<from pilot-approval-template>",
    "approved_by": "<owner>", "expires_at": "<ISO UTC>"}
   ```
   It is bound to this exact code (`release_sha`) and configuration (`config_fingerprint`). Any change to either
   invalidates it.
   Print the exact skeleton for the installed release and configuration with `$TE pilot-approval-template`, then
   fill in `approved_by` and `expires_at`.
7. At start the engine checks the trading key's granted scopes (`GET /api/v1/me`). Without a trade.real write scope
   it refuses to construct the writer.
8. No kill switch: `/etc/etoro-ute/KILL_SWITCH` and `/home/ute/data/KILL_SWITCH` are both absent.

Then run `systemctl restart etoro-ute.service`, followed by `verify_vps.sh --pilot --probe`, which must report ALL
CHECKS PASSED.

## Daily operation

`TE` below stands for the engine command run as the service user:
```bash
TE='runuser -u ute -- /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml --home /home/ute/data'
```
These commands open only the configuration and the database: no broker, no key.

- **See what waits:** `$TE orders pending`. It lists intent, action, symbol, USD amount, phase and approval state.
- **Approve one order:** `$TE orders approve --intent <id> --approved-by <name>`.
  - The approval expires after `approval_ttl_minutes` and is used once.
  - It is bound to the intent's exact content; a changed intent is BLOCKED.
- **Reject:** `$TE orders reject --intent <id> --operator <name> --reason <text>`. The intent becomes BLOCKED.
- **Kill switch:** `$TE kill-switch --operator <name> --reason <text>`. It writes the file and raises a halt of ALL.
  Every later order is BLOCKED, whatever its approval.
- **To resume:** remove the file, then `halt-clear --id <halt> --operator <name> --reason <text>`. Both steps are
  audited.

**Order of execution:**
- **Closes first** (sell-then-buy). No open is sent while a close of the same plan waits for approval.
- **Window:** an intent that is not approved within the plan's execution window (60 minutes after the open plus the
  delay) is **ABANDONED**. It is never sent late.

**What is checked when an order would be sent (hold = retry next cycle; block = never sent):**

| Check | Result |
|---|---|
| kill switch | block |
| no approval / approval expired | hold |
| approval used before / intent changed | block |
| `max_orders_per_session` reached | hold |
| cumulative loss ≤ −`cumulative_loss_stop_usd` | block + halt ALL |
| open above `max_order_usd` | block |
| engine exposure + order above `max_gross_usd` | block |
| daily loss ≤ −`daily_loss_stop_usd` (opens) | block |
| quote missing / older than `max_quote_age_seconds` / wider than `max_spread_bp` (opens) | hold |

Plus everything the shadow release enforces:
- x1 long, the 11 instruments only, minimum 10 USD;
- reconciliation before and after execution;
- unknown-order recovery from broker evidence, never resubmitted;
- the pending-close duplicate guard.

## Rollback

| To | How |
|---|---|
| Stop trading immediately | `kill-switch`, or `systemctl stop etoro-ute.service` |
| Back to SHADOW, positions untouched | install `config/engine.single529.yaml`, then restart |
| Back to the SHADOW release | in SHADOW, run `sync_vps.sh --ref v0.9.0rc3 --expect-sha <sha>` |
| Make the trading key unusable | revoke it in eToro |

Closing positions is always a separate decision: either orders approved in the pilot, or the owner in the eToro app.
