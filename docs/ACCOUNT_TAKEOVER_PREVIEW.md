# Account takeover preview (read-only)

**The preview reads the whole broker account and says what a takeover would do.** It makes no broker call that
writes, closes, cancels or adopts anything:
- the broker handle is `takeover.ReadOnlyView`, and any write method raises before I/O;
- the test asserts the simulated broker's state file is byte-identical before and after.

Commands:
```bash
trading-engine --config config/engine.multi.yaml takeover preview --report takeover_preview.md
```
```bash
python scripts/takeover_preview_real.py --snapshot snap.txt --out docs/takeover/REAL_ACCOUNT_TAKEOVER_PREVIEW_<date>.md
```

The second command works offline from a saved, read-only `ssh gold-miner-audit` snapshot.

## What it contains

| # | Item | How |
|---|---|---|
| 1 | Full account read | `inventory()`: credit, available cash, pending opens, positions, **copy-trading mirrors** (the normal snapshot refuses an account with mirrors). eToro sub-accounts are not exposed by the trading API beyond the real account; demo is never read |
| 2 | Direct and mirrored positions | every position row; mirrors listed separately |
| 3 | Open orders, leverage, margin, exposure | pending orders; per position direction, leverage, invested amount (margin) and exposure (units × price) |
| 4 | Ownership and financing | owner class from the engine database (ENGINE / PRE_EXISTING / EXTERNAL_* / UNCLASSIFIED); financing: x1 long has 0 published; a leveraged or short CFD uses the observed annual rate if configured (GDX 10.05%), otherwise "applies, rate unknown" |
| 5 | Close proceeds and costs | basis in order of preference: **broker unrealized P&L** → **broker quote** (bid for longs, ask for shorts) → a market-context price (labelled, never broker evidence) → UNAVAILABLE. Spread (half) plus a slippage allowance (`close_slippage_bp`, default 25 bp) give a worst case |
| 6 | Not eligible for automatic closure | mirrors (stop copying in the app), pending orders (no cancel capability), no broker value, non-broker estimate, stale quote → `SEPARATE_HANDLING` |
| 7 | Capital after settlement | available cash + estimated proceeds of the CLOSE rows, a worst case, and an investable amount after a 2% reserve. An indicative figure that includes separately handled items is shown but never used for approval |
| 8 | Per-position plan | `CLOSE`, `SEPARATE_HANDLING` or `KEEP_ENGINE_SLEEVE` |
| 9 | Conflicting writers | `takeover.known_writers` (Gold Miner), shown as ACTIVE until disable evidence is recorded, plus foreign-activity events in the last 7 days |
| 10 | Proposed 50/50 allocation and next holdings | investable × weights; each sleeve's latest decision applied to its proposed capital, and the aggregate broker targets |

It also lists the material risks, and binds the plan hash to the account, the release and the state hash.

## Real account, 2026-10-28 22:58 UTC (read-only Gold Miner audit, offline)

Full report: takeover/REAL_ACCOUNT_TAKEOVER_PREVIEW_2026-10-28.md (private report, not published)
(machine-readable `.json` beside it). The snapshot's SHA-256 is `3bf49992…bfd8e`, and it is kept in
`D:\TSR-evidence\takeover_preview_2026-10-28\`.

| | |
|---|---|
| Cash | **559.41 USD** available; no pending orders; no mirrors |
| Position | **GDX CFD short x5**, position <GM-POSITION-1>: <redacted> units, invested **<redacted>**, open rate 81.44, exposure about **3,843 USD**. Owner: Gold Miner (PRE_EXISTING for this engine) |
| Estimated close | about **630.80 USD** proceeds and about **−114.91** realized P&L, using the 2026-10-28 public close of 83.95 (**MARKET_CONTEXT_NOT_BROKER**). Spread + slippage about 9.61; financing about 1.06 USD per day at the observed 10.05% |
| Plan for GDX | **SEPARATE_HANDLING**: neither broker P&L nor a broker quote is available, so it cannot be approved for automatic closure from this evidence |
| Capital | investable now **548.22 USD** (cash only) → **274.11 per sleeve**. Indicatively, about 1,190 USD if GDX were closed at the estimate |
| Proposed holdings (10-28 decisions) | #529-restricted: TECL 45.69, TQQQ 125.63, and its SOXL / SVIX weight in cash; #60-SQQQ: TQQQ 273.42. **Aggregate: TQQQ 399.05, TECL 45.69** |
| Account identity | **UNVERIFIED**: the audit carries no account id, so **no approval can be recorded** for the real account today. With the engine's own read-only key, identity comes from `/api/v1/me` or, documented, the `gcid` of `/api/v1/balances` (stored only as a hash) |
| Writer | **Gold Miner ACTIVE**: it traded GDXD/GDX this week and trimmed this position on 10-28 |
| Context (Gold Miner's own ledger, not engine evidence) | since 2026-08-27: realized −<redacted>, fees 62.34, financing 38.35; Gold Miner reports an unexplained residual of 35.32 USD |

**What the owner would have to do before this account can be taken over:**
1. Decide the GDX short: let Gold Miner close it, close it in the eToro app, or ask a future execution release to
   close it.
2. Disable Gold Miner by its own procedure and record the evidence.
3. Provision the engine's read-only key. This gives account identity, unrealized P&L and quotes, so GDX becomes
   eligible for an approved close.
4. Re-preview and approve each step.

## Simulated preview (same code path)

[takeover/SIMULATED_TAKEOVER_PREVIEW.md](takeover/SIMULATED_TAKEOVER_PREVIEW.md) shows a simulated account with a GDX
short x5 and broker P&L. There:
- GDX is a `CLOSE` with BROKER_UNREALIZED_PNL proceeds of 810.27;
- capital after closes is 2,064.56 (worst case 2,055.40);
- investable is 2,014.29, i.e. 1,007.14 per sleeve;
- the proposed aggregate targets are TQQQ 1,466, TLT 126, SQQQ 126, TECL 84;
- broker mutations: 0.
