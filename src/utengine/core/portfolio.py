"""Portfolio manager: turns a strategy's target weights into broker-executable USD targets.

strategy target   = StrategyDecision.target_weights (e.g. #60: {SQQQ: 1.0} when the tree selects TECS)
executable target = strategy_equity x capital_fraction x weight x (1 - cash_buffer_pct)
                    for instruments that pass the eligibility requirements of the strategy YAML; weight of an
                    ineligible instrument stays CASH and is reported (never redistributed to other symbols).
strategy_equity   = broker available cash + broker value of ENGINE-owned positions. Pre-existing and external
                    positions are excluded: 100% account ownership does not mean 100% exposure.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Allocation:
    strategy_equity: float
    targets_usd: dict                    # symbol -> USD target
    target_weights: dict                 # symbol -> executable weight of strategy equity
    instrument_ids: dict                 # symbol -> instrument id
    blocked: dict = field(default_factory=dict)   # symbol -> reason (weight left in cash)
    cash_usd: float = 0.0

    def to_dict(self):
        return self.__dict__.copy()


def eligibility_reason(spec: dict, symbol: str) -> str | None:
    inst = spec["instruments"].get(symbol)
    req = spec["eligibility"]
    if inst is None:
        return "no instruments entry"
    if inst["etoro_instrument_id"] is None:
        return "eToro instrument id not human-verified"
    if inst["eligibility"] != req["required_status"]:
        return f"eligibility {inst['eligibility']} (required {req['required_status']})"
    if inst["settlement"] not in req["allowed_settlement"]:
        return f"settlement {inst['settlement']} not allowed"
    return None


def allocate(decision, spec: dict, strategy_equity: float) -> Allocation:
    frac = spec["allocation"]["capital_fraction"]
    buf = spec["allocation"]["cash_buffer_pct"]
    targets, weights, ids, blocked = {}, {}, {}, {}
    for sym, w in sorted(decision.target_weights.items()):
        why = eligibility_reason(spec, sym)
        if why:
            blocked[sym] = why
            continue
        ew = w * frac * (1.0 - buf)
        weights[sym] = ew
        targets[sym] = round(strategy_equity * ew, 2)
        ids[sym] = spec["instruments"][sym]["etoro_instrument_id"]
    cash = round(strategy_equity - sum(targets.values()), 2)
    return Allocation(strategy_equity, targets, weights, ids, blocked, cash)
