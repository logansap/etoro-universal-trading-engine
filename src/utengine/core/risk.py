"""Risk engine: independent of strategies, evaluated on every plan before any intent is created.

It can only BLOCK legs or RAISE halts; it never creates orders, never liquidates and never switches strategy.
Checks (engine.yaml `risk`, the strategy YAML `risk`, broker facts):
  halts (ALL blocks everything; NEW_EXPOSURE blocks opens, and closes unless halt_allows_reductions) ·
  unresolved intents · decision staleness (the decision must be for the session just before execution) ·
  market open · gross exposure and instrument weight (min of engine and strategy limits) · x1 long only ·
  instrument eligibility · quote age and validity · single-order cap · buying power (opens <= available cash) ·
  daily loss and drawdown -> NEW_EXPOSURE halt.
GM's 15% stop-loss is deliberately NOT copied: #60 and #529 have no stop in their sources.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import halts
from .order_state import UNRESOLVED
from .portfolio import eligibility_reason


@dataclass
class RiskVerdict:
    approved: list
    blocked: list = field(default_factory=list)       # (leg, reason)
    plan_blocks: list = field(default_factory=list)   # reasons that blocked the whole plan

    @property
    def ok(self):
        return not self.plan_blocks


def check_limits(db, cfg, daily_prev, strategy_equity, flows_since, now) -> None:
    """Daily-loss and drawdown limits -> NEW_EXPOSURE halt (never a liquidation)."""
    r = cfg["risk"]
    if daily_prev and daily_prev["strategy_equity"]:
        ret = (strategy_equity - flows_since) / daily_prev["strategy_equity"] - 1.0
        if ret <= -r["daily_loss_halt_pct"]:
            halts.raise_halt(db, "DAILY_LOSS_LIMIT", "NEW_EXPOSURE",
                             f"strategy return since last close {ret:.2%} <= -{r['daily_loss_halt_pct']:.0%}", now)
        twr = daily_prev["twr_index"] * (strategy_equity - flows_since) / daily_prev["strategy_equity"]
        hwm = max(daily_prev["hwm"], twr)
        if twr / hwm - 1.0 <= -r["drawdown_halt_pct"]:
            halts.raise_halt(db, "DRAWDOWN_LIMIT", "NEW_EXPOSURE",
                             f"drawdown {twr / hwm - 1:.2%} <= -{r['drawdown_halt_pct']:.0%}", now)


def evaluate(db, cfg: dict, spec: dict, legs: list, allocation, quotes: dict, *, decision_session: str,
             execution_session: dt.date, calendar, now: dt.datetime, available_cash: float | None) -> RiskVerdict:
    r = cfg["risk"]
    v = RiskVerdict(approved=[])
    act = halts.active(db)
    if any(h["scope"] == "ALL" for h in act):
        v.plan_blocks.append("halt ALL: " + "; ".join(f"{h['kind']}" for h in act if h["scope"] == "ALL"))
    unresolved = db.q(f"SELECT intent_id FROM intents WHERE status IN ({','.join('?' * len(UNRESOLVED))})",
                      tuple(UNRESOLVED))
    if unresolved:
        v.plan_blocks.append(f"{len(unresolved)} unresolved order intent(s)")
    if calendar.previous_session(execution_session).isoformat() != decision_session:
        v.plan_blocks.append(f"stale decision {decision_session} for execution on {execution_session}")
    if not calendar.is_open(now):
        v.plan_blocks.append("market is not open")
    gross_limit = min(r["max_gross_exposure"], spec["risk"]["max_gross_exposure"])
    inst_limit = min(r["max_instrument_weight"], spec["risk"]["max_instrument_weight"])
    gross = sum(allocation.target_weights.values())
    if gross > gross_limit + 1e-9:
        v.plan_blocks.append(f"gross exposure {gross:.4f} > limit {gross_limit}")
    exposure_halt = any(h["scope"] == "NEW_EXPOSURE" for h in act)
    open_total = 0.0
    for leg in legs:
        reason = None
        if v.plan_blocks:
            reason = "plan blocked"
        elif leg.action == "OPEN":
            w = allocation.target_weights.get(leg.symbol, 0.0)
            q = quotes.get(leg.instrument_id)
            if exposure_halt:
                reason = "NEW_EXPOSURE halt active"
            elif w > inst_limit + 1e-9:
                reason = f"weight {w:.4f} > instrument limit {inst_limit}"
            elif eligibility_reason(spec, leg.symbol):
                reason = eligibility_reason(spec, leg.symbol)
            elif leg.amount > r["max_single_order_usd"]:
                reason = f"amount {leg.amount:.2f} > max_single_order_usd {r['max_single_order_usd']}"
            elif leg.amount < r["min_order_usd"]:
                reason = f"amount {leg.amount:.2f} < min_order_usd {r['min_order_usd']}"
            elif q is None or not (q.bid > 0 and q.ask >= q.bid):
                reason = "no valid quote"
            elif _age(q.quote_time, now) > r["max_quote_age_seconds"]:
                reason = f"quote {_age(q.quote_time, now):.0f}s old > {r['max_quote_age_seconds']}s"
            elif available_cash is not None and open_total + leg.amount > available_cash + 1e-6:
                reason = f"unaffordable: {open_total + leg.amount:.2f} > available {available_cash:.2f}"
        else:
            if exposure_halt and not r["halt_allows_reductions"]:
                reason = "NEW_EXPOSURE halt active (reductions not allowed)"
        if reason:
            v.blocked.append((leg, reason))
        else:
            v.approved.append(leg)
            if leg.action == "OPEN":
                open_total += leg.amount
    return v


def _age(quote_time: str, now: dt.datetime) -> float:
    try:
        t = dt.datetime.fromisoformat(str(quote_time).replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if t.tzinfo is None:
        return float("inf")
    return (now - t).total_seconds()
