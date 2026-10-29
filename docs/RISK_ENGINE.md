# Risk engine

The risk engine is independent of strategies. It runs on every plan before any intent exists. It can only
**block** or **halt**. It never creates an order, never liquidates and never switches strategy.

## Checks

| Check | Default | Effect |
|---|---|---|
| active halt, scope ALL | | the whole plan is blocked |
| active halt, scope NEW_EXPOSURE | | opens blocked; closes too unless `halt_allows_reductions` |
| unresolved intents | | the plan is blocked |
| stale decision (not the session before execution) | | the plan is blocked |
| market closed | | the plan is blocked |
| gross exposure | min(engine 1.0, strategy) | the plan is blocked |
| instrument weight | min(engine 1.0, strategy) | the leg is blocked |
| instrument eligibility (verified id, status, settlement) | VERIFIED_TRADABLE | the leg is blocked (the portfolio already keeps its weight in cash) |
| leverage / shorts | x1 long only (schema-enforced) | impossible to configure otherwise |
| single order cap | USD 25,000 | the leg is blocked |
| minimum order | USD 10 | the leg is blocked |
| quote missing, invalid or older than 120 s | | the leg is blocked |
| buying power (opens <= available cash) | | the leg is blocked |
| daily loss since the last close | -20% | NEW_EXPOSURE halt |
| drawdown of the TWR index | -50% | NEW_EXPOSURE halt |
| position mismatch, external activity, unexplained cash, unknown order, config drift, failed switch, broker unreadable | | halt ALL (reconciliation, execution or manager) |
| emergency halt (`trading-engine halt`) | | NEW_EXPOSURE halt |

The engine never exceeds broker capability. Opens are x1 long CFDs on instruments with human-verified ids. A
leveraged or short CFD (which eToro requires to carry a stop) cannot be configured.

**Gold Miner's 15% stop is not copied.** #60 and #529 have no stop in their sources, and adding one would be a
new strategy.

## Defaults that need owner approval to change

`daily_loss_halt_pct` 0.20, `drawdown_halt_pct` 0.50, `max_single_order_usd` 25,000, `max_quote_age_seconds`
120, `cash_tolerance_usd` 0.05, `halt_allows_reductions` false, `capital_fraction` 1.0.

These were chosen conservatively for a 3x-ETF strategy: daily moves of 10% or more happen, and a halt is not a
liquidation. Changing one is an engine.yaml edit (fingerprinted in the startup audit), and the owner's approval
should be recorded in the operations log.

## Halts

- A halt stops NEW orders only.
- Forced liquidation is a separate path that does NOT exist in this release.
- Only BROKER_UNAVAILABLE clears itself. Every other halt is cleared with
  `trading-engine halt-clear --id ... --operator ... --reason ...` (audited).
- There is no automatic strategy switching, ever.
