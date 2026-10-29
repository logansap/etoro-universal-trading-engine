# eToro capabilities: verified vs documented vs assumed

Evidence levels:

- **VERIFIED_LIVE (GM):** read on the real account by the live Gold Miner engine. The evidence is its read-only
  audit snapshot of 2026-10-27 16:52Z, which logged `DEFINITE_SUCCESS` HTTP 200 for `/trading/info/real/pnl`,
  `/trading/info/trade/history` and `/me`.
- **DOCUMENTED:** eToro's official API reference (api-portal.etoro.com), not observed by us.
- **UNAVAILABLE:** no known interface; never claimed.

**This engine itself has NOT been run against the real account.** No read-only credential was provisioned, so every
field below is UNVERIFIED *for this engine* until `trading-engine broker probe` succeeds.

| Capability | Endpoint | Level | Used by the engine |
|---|---|---|---|
| identity, scopes | GET /api/v1/me | VERIFIED_LIVE (GM) | `broker probe` |
| cash `credit`, positions, pending orders | GET /api/v1/trading/info/real/pnl | VERIFIED_LIVE (GM): GM computes available cash <redacted> from it | reconciliation |
| available cash | credit - pending opens (mirrorID 0) - pending MIT (eToro guide) | DERIVED | sizing |
| equity | available + invested + unrealized (eToro guide) | DERIVED; `unrealizedPnL.pnL` DOCUMENTED | strategy equity |
| positions: positionID, instrumentID, isBuy, units, amount, leverage, openRate, totalFees | same | VERIFIED_LIVE (GM) | ownership |
| open-order status (1 Received, 2 Placed, 3 Filled, 4 Rejected, 5 PartiallyFilled) | GET /api/v2/trading/info/orders:lookup?orderId= | VERIFIED_LIVE (GM; status 3 handling fixed in GM 7a82e3f) | polling |
| close-order status, proceeds, split position ids | GET /api/v1/trading/info/real/close-orders/{id} | VERIFIED_LIVE (GM, 2026-09-23) | polling |
| trade history | GET /api/v1/trading/info/trade/history | VERIFIED_LIVE (GM); partial-close row schema UNVERIFIED | not used for P&L in this release |
| quotes bid/ask/date | GET /api/v1/market-data/instruments/rates | VERIFIED_LIVE (GM) | risk (quote age), planner |
| eligibility (settlement, leverages, minimum exposure) | POST /api/v2/trading/info/eligibility (query) | VERIFIED_LIVE (GM) | `broker probe` (raw) |
| cash transactions | GET /api/v1/money/accounts/cash/{accountId}/transactions | DOCUMENTED, but **eToro Money cash account**, not the trading account | **UNAVAILABLE** for deposit evidence: unexplained cash goes to a halt and then the operator |
| balances | /api/v1/balances/* | DOCUMENTED; not evaluated | no |
| buying power / margin | no endpoint beyond available cash | UNAVAILABLE | x1 only |
| financing, dividends, adjustments as events | none known | UNAVAILABLE | halt path |
| client order id / idempotency key | create order: header `x-request-id` (UUID), echoed as `referenceId`; orders:lookup?referenceId= | DOCUMENTED (Phase 2) | deterministic uuid5(intent); UNKNOWN opens resolved by reference. Reuse semantics undocumented -> never resubmit |
| create order | POST /api/v2/trading/execution/orders | VERIFIED_LIVE (GM) | Phase 2: `brokers/etoro_live.py`, **release-gated, unreachable** |
| close position | POST /api/v1/trading/execution/market-close-orders/positions/{id} | VERIFIED_LIVE (GM) | Phase 2: same writer, gated. **No referenceId on closes** (blocker B3) |

## Account facts relevant to #60-SQQQ (from GM, paper ELIG-2 and owner observations)

- US ETFs on this EEA account are **CFD-only**.
- x1 long overnight fee was observed as 0 (SOXS/SQQQ/QQQ).
- Minimum position is USD 10.
- A partial close must deduct at least USD 9.90 (eToro error 776).
- Each open is its own positionId.
- A partial close splits the position.
- Instrument ids (human-verified by GM): TQQQ 4465, SQQQ 4459, UVXY 3246, TECL 10855, SPXL 3230.
- **BSV:** the owner saw a trade page, but no verified id exists, so a BSV target stays cash.
- **TECS:** absent from the account, so #60 runs with `TECS: SQQQ`.
- Observed full spreads (2026-08-26): TQQQ 1.4 bp, SQQQ 3.75, UVXY 5.3, SPXL 2.5, TECL 14.8.
- The GM REAL key carries `trade.real:write`. A separate read-only key must be provisioned for this engine.

## Read-only enforcement in this adapter

- `brokers/etoro.py` has no write method and does not subclass BrokerWriter.
- `check_read_only()` allowlists exact (method, path) patterns and runs BEFORE credentials are read or the
  transport is called.
- Tests: s26 (no POST to `/trading/execution`, refusal before the network, no writer in shadow even after a
  restart) and `test_no_live_order_code_in_package`.

## Phase 2 update (2026-10-27)

- **Real-account read evidence.** The read-only Gold Miner audit (forced command) shows the exact fields the adapter
  parses: credit/available cash, positions (`positionID` ... `totalFees`, `totalExternalFees`), pending orders and
  trade history (`positionId`, `orderId`, `netProfit`, `fees`, `investment`, ...). Unrealized P&L is not in the audit.
- **This engine's own key.** Still none. The workspace eToro MCP connector is invalidated, and the owner must
  reconnect it.
- **Order statuses.** orders:lookup status ids are 1 Received, 2 Placed, 3 Filled, 4 Rejected, 5 PartiallyFilled,
  6 PendingCancel, 7 Canceled, 8 Expired, 9 CanceledPartiallyFilled, 10 RejectedPartiallyFilled, 11 WaitingForMarket,
  12 PendingTriggeredRate. 7-10 are final and 6/11/12 are in flight; all are handled by the executor.
- **Executions** are reported in `positionExecutions[].positionId` (the adapter also accepts `positions[].positionID`).
- **Deposit sources reviewed:**
  - cash-account transactions: eToro Money only;
  - balance history: daily totals, no reasons;
  - transfers: internal, by reference;
  - deposits: partner-only.
  Result: **no trading-account deposit feed**. See docs/phase2/REAL_ACCOUNT_RECONCILIATION_2026-10-27.md.
