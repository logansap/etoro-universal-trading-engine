"""Strict YAML configuration: engine.yaml and one YAML per strategy.

Rules enforced here, in code (a YAML edit can never weaken them):
  * unknown keys are rejected everywhere; types, ranges and choices are checked;
  * YAML is parsed with yaml.safe_load (no tags, no object construction, no eval);
  * `implementation` must be a key of the explicit strategy registry -- nothing is imported by name from YAML;
  * runtime.mode is `simulation` or `shadow`; `live` is refused with LiveExecutionNotAvailable;
  * security.live_execution_enabled must be false; setting it true is refused, never obeyed;
  * no credential values may appear in YAML -- only the NAMES of environment variables;
  * every strategy YAML carries a `fingerprint` that must equal the hash of its own content, so an edit that is
    not re-fingerprinted is detected (CONFIG_TAMPERED), and the engine refuses to run a strategy whose
    fingerprint differs from the one the operator activated (CONFIG_DRIFT, see strategy_manager).
"""
from __future__ import annotations

import copy
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Callable

import yaml

from .canonical import fingerprint
from .errors import ConfigError, LiveExecutionNotAvailable

SCHEMA_VERSION = 1
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
STRATEGY_ID = re.compile(r"^[A-Z0-9][A-Z0-9_]{1,40}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SECRET_KEY_HINT = re.compile(r"(api[_-]?key|user[_-]?key|secret|password|token|bearer)", re.I)


# ----------------------------------------------------------------------------- tiny strict schema language
@dataclass(frozen=True)
class F:
    kind: str                       # str|int|float|bool|list|dict|map|any
    required: bool = True
    choices: tuple | None = None
    schema: dict | None = None      # dict: field name -> F
    item: "F | None" = None         # list item / map value spec
    lo: float | None = None
    hi: float | None = None
    pattern: Any = None
    nullable: bool = False
    check: Callable[[Any], str | None] | None = None


def _fail(path: str, msg: str):
    raise ConfigError(f"{path or '<root>'}: {msg}")


def validate(value: Any, spec: F, path: str = "") -> Any:
    if value is None:
        if spec.nullable:
            return None
        _fail(path, "is required (null given)")
    k = spec.kind
    if k == "str":
        if not isinstance(value, str):
            _fail(path, f"must be a string, got {type(value).__name__}")
        if spec.pattern is not None and not spec.pattern.match(value):
            _fail(path, f"{value!r} does not match {spec.pattern.pattern}")
    elif k == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            _fail(path, f"must be an integer, got {value!r}")
    elif k == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail(path, f"must be a number, got {value!r}")
        value = float(value)
    elif k == "bool":
        if not isinstance(value, bool):
            _fail(path, f"must be true/false, got {value!r}")
    elif k == "list":
        if not isinstance(value, list):
            _fail(path, "must be a list")
        value = [validate(v, spec.item, f"{path}[{i}]") for i, v in enumerate(value)] if spec.item else value
    elif k == "dict":
        if not isinstance(value, dict):
            _fail(path, "must be a mapping")
        unknown = sorted(set(map(str, value)) - set(spec.schema))
        if unknown:
            _fail(path, f"unknown key(s) {unknown}; allowed: {sorted(spec.schema)}")
        out = {}
        for name, sub in spec.schema.items():
            p = f"{path}.{name}" if path else name
            if name not in value:
                if sub.required:
                    _fail(p, "is required")
                continue
            out[name] = validate(value[name], sub, p)
        value = out
    elif k == "map":
        if not isinstance(value, dict):
            _fail(path, "must be a mapping")
        out = {}
        for key, v in value.items():
            if not isinstance(key, str) or (spec.pattern is not None and not spec.pattern.match(key)):
                _fail(path, f"invalid key {key!r}")
            out[key] = validate(v, spec.item, f"{path}.{key}") if spec.item else v
        value = out
    elif k != "any":
        raise AssertionError(k)
    if spec.choices is not None and value not in spec.choices:
        _fail(path, f"{value!r} not allowed; choices: {list(spec.choices)}")
    if spec.lo is not None and value < spec.lo:
        _fail(path, f"{value!r} below minimum {spec.lo}")
    if spec.hi is not None and value > spec.hi:
        _fail(path, f"{value!r} above maximum {spec.hi}")
    if spec.check is not None:
        msg = spec.check(value)
        if msg:
            _fail(path, msg)
    return value


def D(required=True, **schema) -> F:
    return F("dict", required=required, schema=schema)


# ----------------------------------------------------------------------------- engine.yaml schema
def _mode_check(v):
    if v == "live":
        raise LiveExecutionNotAvailable(
            "runtime.mode=live is refused: live order submission is NOT IMPLEMENTED in this release and can never "
            "be enabled by a configuration file (docs/RELEASE_READINESS.md, 'Future activation').")
    return None if v in ("simulation", "shadow") else f"{v!r} not allowed; choices: ['simulation', 'shadow']"


def _live_flag_check(v):
    if v is not False:
        raise LiveExecutionNotAvailable(
            "security.live_execution_enabled must be false. Setting it true does not enable anything; it is "
            "refused so that a tampered file cannot be mistaken for an approval.")
    return None


ENGINE_SCHEMA = D(
    schema_version=F("int", choices=(SCHEMA_VERSION,)),
    engine=D(instance_id=F("str", pattern=re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")),
             description=F("str", required=False)),
    broker=D(
        name=F("str", choices=("etoro", "simulated")),
        access=F("str", choices=("live_read_only", "simulated")),
        api_base=F("str", choices=("https://public-api.etoro.com",)),
        credentials_env=D(api_key=F("str", pattern=ENV_NAME), user_key=F("str", pattern=ENV_NAME)),
        request_timeout_seconds=F("float", lo=1, hi=60),
    ),
    account=D(
        ownership=F("str", choices=("exclusive",)),
        equity_source=F("str", choices=("broker",)),
        base_currency=F("str", choices=("USD",)),
        deposit_detection=F("str", choices=("automatic",)),
        unknown_balance_policy=F("str", choices=("halt",)),
        cash_tolerance_usd=F("float", lo=0.0, hi=1.0),
        pre_existing_positions=F("str", choices=("exclude_and_report",)),
        external_trade_policy=F("str", choices=("halt",)),
        max_broker_state_age_seconds=F("int", lo=5, hi=3600, required=False),
    ),
    runtime=D(
        mode=F("str", check=_mode_check),
        initial_strategy=F("str", pattern=STRATEGY_ID),
        strategies_dir=F("str"),
        cycle_interval_seconds=F("int", lo=10, hi=3600),
    ),
    security=D(
        live_execution_enabled=F("bool", check=_live_flag_check),
        approvals_required=F("bool", choices=(True,)),
    ),
    reconciliation=D(
        before_decision=F("bool", choices=(True,)),
        before_execution=F("bool", choices=(True,)),
        after_execution=F("bool", choices=(True,)),
        after_restart=F("bool", choices=(True,)),
        order_poll_seconds=F("float", lo=0.0, hi=30.0),
        order_poll_attempts=F("int", lo=1, hi=120),
    ),
    risk=D(
        max_gross_exposure=F("float", lo=0.0, hi=1.0),
        max_instrument_weight=F("float", lo=0.0, hi=1.0),
        max_leverage=F("int", choices=(1,)),
        allow_short=F("bool", choices=(False,)),
        daily_loss_halt_pct=F("float", lo=0.01, hi=0.5),
        drawdown_halt_pct=F("float", lo=0.05, hi=0.9),
        max_single_order_usd=F("float", lo=10.0, hi=100000.0),
        min_order_usd=F("float", lo=10.0, hi=1000.0),
        max_quote_age_seconds=F("int", lo=5, hi=3600),
        max_spread_bp=F("float", lo=1.0, hi=500.0, required=False),
        halt_allows_reductions=F("bool"),
    ),
    market_data=D(provider=F("str", choices=("csv", "yahoo")), csv_dir=F("str", required=False),
                  lookback_sessions=F("int", lo=260, hi=5000),
                  simulation_half_spread_bp=F("float", lo=0.0, hi=50.0)),
    monitoring=D(heartbeat_stale_seconds=F("int", lo=30, hi=86400)),
    notifications=D(required=False, webhook_url_env=F("str", pattern=ENV_NAME, nullable=True),
                    min_severity=F("str", choices=("INFO", "WARNING", "CRITICAL")),
                    dedupe_minutes=F("int", lo=1, hi=1440), timeout_seconds=F("float", lo=1, hi=60),
                    max_attempts=F("int", lo=1, hi=100)),
    backup=D(required=False, local_dir=F("str"), retain=F("int", lo=1, hi=365),
             offsite_method=F("str", choices=("none", "copy", "rsync")),
             offsite_target_env=F("str", pattern=ENV_NAME, nullable=True)),
    calendar=D(exchange=F("str", choices=("XNYS",)), timezone=F("str", choices=("America/New_York",)),
               unscheduled_closures=F("list", item=F("str", pattern=re.compile(r"^\d{4}-\d{2}-\d{2}$")))),
    # multi-strategy virtual sleeves (docs/MULTI_STRATEGY_ARCHITECTURE.md); absent = single_strategy
    portfolio=D(required=False,
                mode=F("str", choices=("single_strategy", "multi_strategy")),
                sleeves=F("list", item=D(strategy=F("str", pattern=STRATEGY_ID), weight=F("float", lo=0.0, hi=1.0))),
                funding_policy=F("str", choices=("proportional",)),
                rebalance_policy=F("str", choices=("strategy_independent",)),
                aggregate_risk=F("str", choices=("enforced",)),
                internal_crossing=F("bool", required=False)),
    # full-account capital ownership + automatic funding discovery (docs/CAPITAL_AND_FUNDING.md); absent = previous
    # behaviour (every unexplained cash change halts)
    capital=D(required=False,
              ownership=F("str", choices=("full_designated_account",)),
              sizing_source=F("str", choices=("verified_broker_equity",)),
              manual_capital_amount=F("any", nullable=True, check=lambda v: None if v is None else
                                      "must be null: capital is discovered from the broker, never entered by hand"),
              funding_policy=F("str", choices=("automatic_evidence_backed",)),
              external_flow_allocation=F("str", choices=("proportional",)),
              preserve_strategy_cash=F("bool", choices=(True,))),
    # full-account takeover preview / state machine (docs/ACCOUNT_TAKEOVER_PREVIEW.md); absent = defaults
    takeover=D(required=False,
               known_writers=F("list", item=D(name=F("str"), description=F("str"))),
               approval_ttl_hours=F("float", lo=0.25, hi=72.0),
               price_tolerance_pct=F("float", lo=0.001, hi=0.2),
               close_slippage_bp=F("float", lo=0.0, hi=500.0),
               investable_reserve_pct=F("float", lo=0.0, hi=0.5),
               financing_annual_rate_observed=F("map", pattern=SYMBOL, item=F("float", lo=0.0, hi=1.0),
                                                required=False)),
)

TAKEOVER_DEFAULTS = {"known_writers": [], "approval_ttl_hours": 4.0, "price_tolerance_pct": 0.02,
                     "close_slippage_bp": 25.0, "investable_reserve_pct": 0.02, "financing_annual_rate_observed": {}}


# ----------------------------------------------------------------------------- strategy YAML schema
INSTRUMENT = D(
    etoro_instrument_id=F("int", nullable=True, lo=1),
    eligibility=F("str", choices=("VERIFIED_TRADABLE", "OWNER_OBSERVED", "UNVERIFIED", "VERIFIED_UNAVAILABLE")),
    settlement=F("str", choices=("CFD", "REAL", "UNKNOWN")),
    evidence=F("str"),
)

STRATEGY_SCHEMA = D(
    schema_version=F("int", choices=(SCHEMA_VERSION,)),
    id=F("str", pattern=STRATEGY_ID),
    name=F("str"),
    implementation=F("str"),
    version=F("str", pattern=SEMVER),
    fingerprint=F("str", pattern=re.compile(r"^sha256:[0-9a-f]{64}$")),
    enabled=F("bool"),
    live_eligible=F("bool"),
    live_eligibility_note=F("str"),
    source=D(reference=F("str"), sha256=F("str", pattern=re.compile(r"^[0-9a-f]{64}$"), nullable=True),
             note=F("str", required=False)),
    indicators=F("map", item=F("any"), required=False),
    universe=D(signal=F("list", item=F("str", pattern=SYMBOL)), tradable=F("list", item=F("str", pattern=SYMBOL))),
    substitutions=F("map", pattern=SYMBOL, item=F("str", pattern=SYMBOL)),
    instruments=F("map", pattern=SYMBOL, item=INSTRUMENT),
    eligibility=D(required_status=F("str", choices=("VERIFIED_TRADABLE",)),
                  allowed_settlement=F("list", item=F("str", choices=("CFD", "REAL"))),
                  max_leverage=F("int", choices=(1,)), allow_short=F("bool", choices=(False,))),
    schedule=D(calendar=F("str", choices=("XNYS",)), timezone=F("str", choices=("America/New_York",)),
               decision_time=F("str", pattern=HHMM), decision_deadline=F("str", pattern=HHMM),
               execution=F("str", choices=("next_regular_session_open",)),
               execution_delay_minutes=F("int", lo=0, hi=120), execution_window_minutes=F("int", lo=5, hi=240),
               missed_policy=F("str", choices=("skip",))),
    allocation=D(capital_fraction=F("float", choices=(1.0,)), cash_buffer_pct=F("float", lo=0.0, hi=0.05)),
    rebalancing=D(min_order_usd=F("float", lo=10.0, hi=1000.0), min_order_pct_nav=F("float", lo=0.0, hi=0.05),
                  liquidate_non_targets=F("bool", choices=(True,))),
    risk=D(max_gross_exposure=F("float", lo=0.0, hi=1.0), max_instrument_weight=F("float", lo=0.0, hi=1.0)),
    execution=D(mode=F("str", check=_mode_check), order_type=F("str", choices=("market",)),
                sequencing=F("str", choices=("sell_then_buy",))),
    logging=D(level=F("str", choices=("DEBUG", "INFO", "WARNING")),
              alerts=F("list", item=F("str", choices=("discrepancy", "halt", "missed_cycle", "unknown_order",
                                                      "external_activity", "decision")))),
    settings=F("map", item=F("any"), required=False),
)


# ----------------------------------------------------------------------------- loading
def _load_yaml(path: pathlib.Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read ({exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _reject_inline_secrets(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if SECRET_KEY_HINT.search(str(k)) and isinstance(v, str) and not ENV_NAME.match(v):
                raise ConfigError(f"{p}: looks like an inline credential. Only environment-variable NAMES are "
                                  "allowed in configuration; secrets are never stored in YAML or Git.")
            _reject_inline_secrets(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_inline_secrets(v, f"{path}[{i}]")


def load_engine_config(path: str | pathlib.Path) -> dict:
    path = pathlib.Path(path)
    raw = _load_yaml(path)
    _reject_inline_secrets(raw)
    cfg = validate(raw, ENGINE_SCHEMA)
    b, mode = cfg["broker"], cfg["runtime"]["mode"]
    if b["name"] == "etoro" and b["access"] != "live_read_only":
        raise ConfigError("broker.access: the eToro adapter in this release is read-only (live_read_only)")
    if b["name"] == "simulated" and b["access"] != "simulated":
        raise ConfigError("broker.access must be 'simulated' for the simulated broker")
    if mode == "simulation" and b["name"] != "simulated":
        raise ConfigError("runtime.mode=simulation requires broker.name=simulated")
    if mode == "shadow" and b["name"] != "etoro":
        raise ConfigError("runtime.mode=shadow requires broker.name=etoro (read-only)")
    pf = cfg.get("portfolio")
    if pf and pf["mode"] == "multi_strategy":
        ids = [s["strategy"] for s in pf["sleeves"]]
        if not ids:
            raise ConfigError("portfolio.sleeves: multi_strategy needs at least one sleeve")
        if len(set(ids)) != len(ids):
            raise ConfigError(f"portfolio.sleeves: duplicate strategy in {ids}")
        if any(s["weight"] <= 0 for s in pf["sleeves"]):
            raise ConfigError("portfolio.sleeves: every weight must be > 0 (remove a sleeve instead)")
        if abs(sum(s["weight"] for s in pf["sleeves"]) - 1.0) > 1e-9:
            raise ConfigError("portfolio.sleeves: weights must sum to 1.0 (a sleeve's own cash stays cash; "
                              "unallocated capital is not a sleeve)")
    cfg["_path"] = str(path.resolve())
    cfg["_fingerprint"] = fingerprint({k: v for k, v in cfg.items() if not k.startswith("_")})
    return cfg


def takeover_settings(cfg: dict) -> dict:
    return {**TAKEOVER_DEFAULTS, **(cfg.get("takeover") or {})}


def is_multi(cfg: dict) -> bool:
    return bool(cfg.get("portfolio") and cfg["portfolio"]["mode"] == "multi_strategy")


def strategy_content_fingerprint(raw: dict) -> str:
    """Fingerprint of a strategy YAML's content, excluding the `fingerprint` field itself."""
    body = {k: v for k, v in raw.items() if k != "fingerprint"}
    return fingerprint(body)


def load_strategy_spec(path: str | pathlib.Path, registry: dict) -> dict:
    path = pathlib.Path(path)
    raw = _load_yaml(path)
    _reject_inline_secrets(raw)
    spec = validate(copy.deepcopy(raw), STRATEGY_SCHEMA)
    actual = strategy_content_fingerprint(raw)
    if spec["fingerprint"] != actual:
        raise ConfigError(f"{path.name}: CONFIG_TAMPERED -- declared fingerprint {spec['fingerprint']} != content "
                          f"{actual}. A changed strategy file must be re-fingerprinted deliberately "
                          "(`trading-engine config fingerprint`) and re-activated through the approval workflow.")
    impl = spec["implementation"]
    if impl not in registry:
        raise ConfigError(f"{path.name}: implementation {impl!r} is not in the approved strategy registry "
                          f"{sorted(registry)}; modules are never imported by name from configuration")
    entry = registry[impl]
    entry.validate_spec(spec)          # module-specific parameter schema (frozen values, universes ...)
    for sym in spec["universe"]["tradable"]:
        if sym not in spec["instruments"]:
            raise ConfigError(f"{path.name}: tradable {sym} has no instruments entry")
    for src, dst in spec["substitutions"].items():
        if dst not in spec["universe"]["tradable"]:
            raise ConfigError(f"{path.name}: substitution target {dst} is not tradable")
        if src in spec["universe"]["tradable"]:
            raise ConfigError(f"{path.name}: substituted symbol {src} must not also be tradable")
    if spec["risk"]["max_instrument_weight"] > spec["risk"]["max_gross_exposure"] + 1e-12:
        raise ConfigError(f"{path.name}: risk.max_instrument_weight exceeds risk.max_gross_exposure")
    spec["_path"] = str(path.resolve())
    spec["_file_name"] = path.name
    return spec


def load_all_strategies(directory: str | pathlib.Path, registry: dict) -> dict:
    directory = pathlib.Path(directory)
    out = {}
    for p in sorted(directory.glob("*.yaml")):
        s = load_strategy_spec(p, registry)
        if s["id"] in out:
            raise ConfigError(f"duplicate strategy id {s['id']} in {p.name}")
        out[s["id"]] = s
    return out
