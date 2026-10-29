# Configuration schema

**Validation:** `trading-engine config validate`. The schema is defined in code (`core/configuration.py`): unknown
keys are errors, every value is type-, range- and choice-checked, YAML is read with `safe_load`, and there is no
`eval` or dynamic import.

## engine.yaml

| Key | Allowed | Meaning |
|---|---|---|
| `schema_version` | 1 | |
| `engine.instance_id` | `[a-z0-9-]` | |
| `broker.name` / `broker.access` | `etoro`/`live_read_only`, `simulated`/`simulated` | there is no write access for eToro |
| `broker.api_base` | `https://public-api.etoro.com` only | |
| `broker.credentials_env.{api_key,user_key}` | ENV VAR NAMES | values are never in YAML (inline values are rejected) |
| `account.ownership` | `exclusive` | all broker cash belongs to the engine; foreign positions are excluded |
| `account.equity_source` | `broker` | |
| `account.deposit_detection` | `automatic` | detection is automatic; classification needs evidence |
| `account.unknown_balance_policy` | `halt` | |
| `account.cash_tolerance_usd` | 0 to 1 | |
| `account.pre_existing_positions` | `exclude_and_report` | |
| `account.external_trade_policy` | `halt` | |
| `runtime.mode` | `simulation` \| `shadow` | **`live` raises LiveExecutionNotAvailable** |
| `runtime.initial_strategy` | strategy id | used once, at the first start of an empty database |
| `security.live_execution_enabled` | `false` only | `true` is refused, never obeyed |
| `security.approvals_required` | `true` only | |
| `reconciliation.before_* / after_*` | `true` only | |
| `reconciliation.order_poll_seconds / order_poll_attempts` | 0-30 / 1-120 | |
| `risk.max_gross_exposure`, `risk.max_instrument_weight` | 0 to 1 | |
| `risk.max_leverage` | 1 | |
| `risk.allow_short` | false | |
| `risk.daily_loss_halt_pct`, `risk.drawdown_halt_pct` | | trigger a NEW_EXPOSURE halt |
| `risk.max_single_order_usd` | 10 to 100000 | catastrophic-bug backstop |
| `risk.min_order_usd` | >= 10 | eToro minimum |
| `risk.max_quote_age_seconds` | | |
| `risk.halt_allows_reductions` | bool | |
| `market_data.provider` | `csv` \| `yahoo` | |
| `market_data.csv_dir`, `market_data.lookback_sessions`, `market_data.simulation_half_spread_bp` | | |
| `monitoring.heartbeat_stale_seconds` | | |
| `calendar.exchange` / `calendar.timezone` | `XNYS` / `America/New_York` | |
| `calendar.unscheduled_closures` | dates | add these when the exchange announces one |

Pairing rules: `simulation` requires the `simulated` broker, and `shadow` requires the read-only `etoro` broker.

## Strategy YAML (one file = one strategy)

| Key | Notes |
|---|---|
| `id`, `name`, `implementation` (registry key), `version` (semver) | |
| `fingerprint` | `sha256:` of the content without this field. A mismatch is CONFIG_TAMPERED. `trading-engine config fingerprint <file> [--write]` |
| `enabled`, `live_eligible`, `live_eligibility_note` | |
| `source.reference / sha256 / note` | the module checks the hash it requires |
| `indicators`, `settings` | module-specific; **qc60 and qc529 freeze them to the source values** |
| `universe.signal` / `universe.tradable` | signal symbols feed indicators; tradable symbols can receive weight |
| `substitutions` | final-target only (qc60 allows `{}` or `{TECS: SQQQ}`) |
| `instruments.<SYM>` | `etoro_instrument_id` (human-verified or null), `eligibility` (VERIFIED_TRADABLE \| OWNER_OBSERVED \| UNVERIFIED \| VERIFIED_UNAVAILABLE), `settlement`, `evidence` |
| `eligibility` | `required_status VERIFIED_TRADABLE`, `allowed_settlement`, `max_leverage 1`, `allow_short false` |
| `schedule` | `decision_time` (>= 16:00), `decision_deadline`, `execution: next_regular_session_open`, `execution_delay_minutes`, `execution_window_minutes`, `missed_policy: skip` |
| `allocation` | `capital_fraction` (1.0 in this release), `cash_buffer_pct` |
| `rebalancing` | `min_order_usd`, `min_order_pct_nav`, `liquidate_non_targets: true` |
| `risk` | strategy limits. The tighter of the strategy and engine limits applies |
| `execution` | `mode` (never live), `order_type: market`, `sequencing: sell_then_buy` |
| `logging` | level and alert kinds |

## Module-enforced invariants

| Module | Invariant |
|---|---|
| qc60 | indicators `{rsi 10 wilder, SMA 200/20/20}`, the eight tree thresholds, the exact nine signal symbols, tradable = tree targets after substitution, `cash_buffer_pct 0.0025`, `min_order_pct_nav 0.001` (LEAN semantics), source hash `fac0d6db...`. 50% / 75% / filtered variants cannot be expressed |
| qc529 | settings `{-0.10, -0.04, 10, 0.5, 0.25}`, the source's universe in source order, no substitutions, no buffer, no minimum percentage, source hash `40c270b8...`; logic is the vendored port `0453c1ea...` |
| goldminer687 | must be `enabled: false`, `live_eligible: false` |
