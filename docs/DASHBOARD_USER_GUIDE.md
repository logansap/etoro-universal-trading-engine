# Dashboard user guide

## Start it (one command)

**Locally** (state directory `var/`, the default):
```bash
trading-engine --config config/engine.multi.yaml dashboard set-password
```
```bash
trading-engine --config config/engine.multi.yaml dashboard serve
```
Open http://127.0.0.1:8790/ and sign in.

**For a quick look without typing a password** (for example on the same machine), print a single-use link valid for 2
minutes:
```bash
trading-engine dashboard login-link
```

**Demo on simulated data** (no broker): build a state, set a password, serve it with a fixed clock:
```bash
python scripts/dashboard_demo_state.py /tmp/dash
```
```bash
trading-engine --config /tmp/dash/config/engine.yaml --home /tmp/dash/home dashboard set-password
```
```bash
trading-engine --config /tmp/dash/config/engine.yaml --home /tmp/dash/home --now 2026-10-28T13:45:00+00:00 dashboard serve
```

**On the VPS:** keep it on 127.0.0.1, and from your computer run:
```bash
ssh -N -L 8790:127.0.0.1:8790 <user>@<vps>
```
Then open http://127.0.0.1:8790/. See [DASHBOARD_SECURITY.md](DASHBOARD_SECURITY.md).

The dashboard **cannot trade, approve, halt or change anything**. Every operator action stays a CLI command with
`--operator` / `--approved-by`, recorded in the audit log.

## Reading it

**Top banner:** SIMULATION (blue), SHADOW (amber) or LIVE (red), and "live trading DISABLED" while the release gates
are closed. Next to the snapshot time, the **data label** tells you how far to trust the numbers:

| Label | Meaning |
|---|---|
| SIMULATED | a simulated broker (test data) |
| SHADOW | real broker data, nothing traded |
| VERIFIED | live, fresh and reconciled |
| STALE | older than 15 minutes, or the broker could not be read |
| UNRECONCILED | the latest reconciliation found a problem |
| unavailable (red) | the broker did not provide the value; nothing is guessed |

### Overview

| Card | What it means |
|---|---|
| Account equity | broker cash + invested + unrealized P&L, for the designated account. "capital reconciliation VERIFIED · unattributed $0.00" means the engine can account for every dollar |
| Available cash | cash not invested and not reserved; "reserved by pending opens" is cash tied to orders in flight |
| Invested (margin) | the amount in open positions (x1, so margin = invested) |
| Strategy capital | the equity the strategies own (both sleeves) |
| Unclassified (suspense) | money that arrived without evidence of what it is; **not allocated and not spendable** until evidence arrives |
| Foreign positions | positions the engine does not own (for example Gold Miner's GDX short before the takeover) |

- **P&L** day, week, month and lifetime exclude deposits and withdrawals. A deposit never shows up as profit.
- **Allocation table:** *equity* is the capital a sleeve owns; *invested* is the part currently in positions. A low
  invested share means the sleeve is holding cash (#529 often is). A share above 1 means negative sleeve cash, which
  the sleeve's own next rebalance restores.

### Strategies

- **Return and P&L:** each sleeve's TWR (time-weighted return, unaffected by deposits), drawdown, and realized and
  unrealized P&L (average cost).
- **Holdings and decision:** current holdings with weights, the latest decision's targets and its full explanation.
- **#529 crash guard:** ACTIVE when QQQ's 10-day return is ≤ −10%, released at ≥ −4%.
- **#60 regime:** BULL or BEAR, from SPY against its 200-day average, plus the decision path.
- **"Held in cash because the instrument is not executable on this account":** weight that would have gone to SOXL,
  SVIX and similar is kept in cash, never substituted.
- **Overlap:** instruments both sleeves hold; the broker sees one combined position.

### Trades

- **Orders:** every order intent with its status and which sleeve(s) it belongs to.
- **Executions:** fills and internal crossings as booked to each sleeve.
- **Fees and financing.**
- **Problem lists:** rejected, pending/unresolved and partially filled orders.

### Operations

- **Status cards:** engine heartbeat, broker connectivity, market-data freshness, the last reconciliation, audit-chain
  verification and backups.
- **Actionable exceptions:** only open problems that need you, each with the exact action to take. Routine,
  fully explained reconciliation is silent.
- **Halts, schedule and alerts:** halts (account or sleeve), the next decision and execution windows per sleeve, and
  the alert history.

### Migration

- **Gold Miner retirement checklist,** and whether two writers could currently exist.
- **Foreign positions** and pending orders.
- **The latest takeover preview,** with approvals and their status, the required approvals and blockers, and the audit
  trail.

## Finance concepts used here

- **Equity vs cash vs invested.** Equity is everything the account would be worth if closed at current prices.
  Invested is what you paid in (margin). Unrealized P&L is the difference.
- **External flows vs P&L.** Deposits and withdrawals change equity but are not performance. P&L = Δequity − external
  flows. TWR chains daily (equity − flows) / previous equity, so a deposit neither helps nor hurts it.
- **Sleeves.** Each strategy owns a share of the account: its cash and units of each instrument. When both want TQQQ,
  eToro holds one position and the engine records how many units each sleeve owns.
- **Suspense.** Money the engine cannot yet explain is parked, not guessed. It is classified automatically when
  evidence arrives.
