"""Order planner: the changes needed to move ENGINE-owned positions to the executable targets.

Phase 1 (risk-reducing, first): CLOSE every position of a symbol that is no longer targeted; reduce over-weight
symbols with full closes (largest position first) and, where needed, one PARTIAL_CLOSE by units.
Phase 2 (after phase 1 is filled AND reconciled): OPEN by USD amount, re-sized to the cash the broker then
reports available (resize_opens). eToro constraints: minimum position USD 10; a partial close must deduct at least
USD 9.90 and leave at least USD 10; amounts in cents, units to 6 decimals; no share counts are assumed.
An adjustment smaller than max(min_order_usd, min_order_pct_nav x equity) is not traded (LEAN minimum order).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

MIN_POSITION_USD = 10.0
MIN_PARTIAL_USD = 9.9


@dataclass(frozen=True)
class Leg:
    leg_index: int
    phase: int                  # 1 = closes, 2 = opens
    action: str                 # OPEN | CLOSE | PARTIAL_CLOSE
    symbol: str
    instrument_id: int
    amount: float | None        # USD for OPEN; estimated value for closes
    units: float | None         # PARTIAL_CLOSE units; None = all units
    position_id: str | None
    reason: str
    # multi-strategy attribution, persisted with the intent BEFORE submission: OPEN -> ((sleeve, usd), ...),
    # CLOSE / PARTIAL_CLOSE -> ((sleeve, units), ...). None = single-strategy leg.
    sleeves: tuple | None = None

    def to_dict(self):
        d = asdict(self)
        if d["sleeves"] is not None:
            d["sleeves"] = [list(x) for x in d["sleeves"]]
        return d


def _floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100.0


def plan(allocation, positions: list, quotes: dict, rebal: dict, risk_min_order: float) -> tuple:
    """positions: engine-owned Position objects. quotes: instrument_id -> Quote. Returns (legs, notes)."""
    eq = allocation.strategy_equity
    threshold = max(rebal["min_order_usd"], risk_min_order, rebal["min_order_pct_nav"] * eq)
    by_sym = {}
    for p in positions:
        by_sym.setdefault(p.symbol, []).append(p)
    legs, notes, k = [], [], 0
    symbols = sorted(set(by_sym) | set(allocation.targets_usd))
    for sym in symbols:
        held = sorted(by_sym.get(sym, []), key=lambda p: (-p.value, p.position_id))
        cur = sum(p.value for p in held)
        tgt = allocation.targets_usd.get(sym, 0.0)
        if sym not in allocation.targets_usd:
            for p in held:
                legs.append(Leg(k, 1, "CLOSE", sym, p.instrument_id, round(p.value, 2), None, p.position_id,
                                "not targeted: liquidate"))
                k += 1
            continue
        delta = tgt - cur
        if delta <= -threshold:
            need = -delta
            for p in held:
                if need <= 0:
                    break
                if p.value <= need + MIN_POSITION_USD:
                    legs.append(Leg(k, 1, "CLOSE", sym, p.instrument_id, round(p.value, 2), None, p.position_id,
                                    f"reduce {sym} by {need:.2f}"))
                    k += 1
                    need -= p.value
                else:
                    q = quotes.get(p.instrument_id)
                    if q is None or q.bid <= 0:
                        notes.append(f"{sym}: no quote for a partial close; reduction skipped")
                        break
                    if need < MIN_PARTIAL_USD:
                        notes.append(f"{sym}: residual reduction {need:.2f} below USD {MIN_PARTIAL_USD}; skipped")
                        break
                    units = math.floor(need / q.bid * 1e6) / 1e6
                    legs.append(Leg(k, 1, "PARTIAL_CLOSE", sym, p.instrument_id, round(units * q.bid, 2), units,
                                    p.position_id, f"reduce {sym} by {need:.2f}"))
                    k += 1
                    need = 0
        elif delta >= threshold:
            amt = _floor_cents(delta)
            if amt >= MIN_POSITION_USD:
                legs.append(Leg(k, 2, "OPEN", sym, allocation.instrument_ids[sym], amt, None, None,
                                f"raise {sym} from {cur:.2f} to {tgt:.2f}"))
                k += 1
            else:
                notes.append(f"{sym}: open {amt:.2f} below eToro minimum USD {MIN_POSITION_USD}")
        elif abs(delta) > 0.005:
            notes.append(f"{sym}: |{delta:.2f}| below minimum order {threshold:.2f}; not traded")
    ordered = sorted(legs, key=lambda l: (l.phase, l.leg_index))
    return [Leg(i, l.phase, l.action, l.symbol, l.instrument_id, l.amount, l.units, l.position_id, l.reason)
            for i, l in enumerate(ordered)], notes


def resize_opens(legs: list, available_cash: float) -> tuple:
    """Scale phase-2 opens down to the cash the broker reports available after phase 1. Never scales up."""
    opens = [l for l in legs if l.action == "OPEN"]
    total = sum(l.amount for l in opens)
    cap = _floor_cents(max(available_cash - 0.01, 0.0))
    if total <= cap or total == 0:
        return opens, []
    f = cap / total
    out, notes = [], [f"opens scaled by {f:.6f}: planned {total:.2f}, available {cap:.2f}"]
    for l in opens:
        amt = _floor_cents(l.amount * f)
        if amt < MIN_POSITION_USD:
            notes.append(f"{l.symbol}: scaled open {amt:.2f} below USD {MIN_POSITION_USD}; skipped")
            continue
        out.append(Leg(l.leg_index, l.phase, l.action, l.symbol, l.instrument_id, amt, None, None,
                       l.reason + f" (scaled to available cash)"))
    return out, notes
