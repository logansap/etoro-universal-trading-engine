"""Shared order planner for several sleeves on one broker account (docs/MULTI_STRATEGY_ARCHITECTURE.md).

1. Each REBALANCING sleeve states what it wants from its OWN holdings and cash only:
     sell units   instruments it no longer targets (all of ITS units), or over-weight by >= its threshold
     buy USD      under-weight by >= its threshold (threshold = max(min_order_usd, risk.min_order_usd,
                  min_order_pct_nav x sleeve equity), the strategy's own LEAN rule)
   A sleeve that is not rebalancing, halted or undecided contributes nothing: one sleeve's rebalance can never sell
   another sleeve's units (it only ever sells units it holds itself).
2. Buying power per sleeve: its available virtual cash + the estimated proceeds of its own sales. Buys above that are
   scaled down; one sleeve never spends another sleeve's cash.
3. Internal crossing (portfolio.internal_crossing, default on): when sleeve X sells and sleeve Y buys the SAME
   instrument in the same plan, the overlap is transferred between the sleeves at the broker mark, with no broker
   order and no spread cost. Sum of sleeve equity is unchanged by a crossing.
4. Netting: the residual per instrument becomes AT MOST one direction at the broker:
     net sell -> CLOSE / PARTIAL_CLOSE legs on the engine's positions of that instrument (largest first) for exactly
                 the sellers' residual units; eToro minimums (partial >= USD 9.90 and leaving >= USD 10) can leave a
                 residual unsold (reported as drift, never forced by selling other units);
     net buy  -> one OPEN by USD amount; below the USD 10 eToro minimum it is not traded (reported).
   Every leg carries its sleeve attribution, persisted with the intent before submission.
"""
from __future__ import annotations

import math

from .order_planner import MIN_PARTIAL_USD, MIN_POSITION_USD, Leg, _floor_cents
from .sleeves import split

EPS = 1e-9


def plan_portfolio(inputs: list, val: dict, positions: list, quotes: dict, id_to_symbol: dict,
                   risk_min_order: float, crossing: bool = True, broker_available: float | None = None) -> dict:
    """inputs: [{sleeve, spec, alloc, rebalance}] (alloc = portfolio.allocate on the SLEEVE's equity).
    val: sleeves.valuation(). positions: the engine's broker positions. quotes: instrument id -> Quote."""
    mk = val["marks"]
    sells, buys, notes, syms, summary = {}, {}, [], dict(id_to_symbol), {}

    def bid(iid):
        q = quotes.get(iid)
        return q.bid if q is not None and q.bid > 0 else mk.get(iid)

    for inp in inputs:
        s, alloc, spec = inp["sleeve"], inp["alloc"], inp["spec"]
        v = val["sleeves"][s]
        summary[s] = {"equity": v["equity"], "targets_usd": dict(alloc.targets_usd), "blocked": dict(alloc.blocked),
                      "rebalance": inp["rebalance"], "sells_units": {}, "buys_usd": {}}
        if not inp["rebalance"]:
            continue
        rb = spec["rebalancing"]
        thr = max(rb["min_order_usd"], risk_min_order, rb["min_order_pct_nav"] * v["equity"])
        held = {iid: h["units"] for iid, h in v["holdings"].items() if h["units"] > EPS}
        tgt = {}
        for sym, usd in alloc.targets_usd.items():
            iid = alloc.instrument_ids[sym]
            tgt[iid] = usd
            syms[iid] = sym
        for iid in sorted(set(held) | set(tgt)):
            u = held.get(iid, 0.0)
            px = mk.get(iid) or bid(iid)
            if iid not in tgt:
                sells[(s, iid)] = u                       # not targeted: all of this sleeve's units
                continue
            cur = u * px if u and px else 0.0
            delta = tgt[iid] - cur
            if delta <= -thr and u > 0 and px:
                sells[(s, iid)] = min(u, -delta / px)
            elif delta >= thr:
                buys[(s, iid)] = delta
            elif abs(delta) > 0.005:
                notes.append(f"{s} {syms.get(iid)}: |{delta:.2f}| below its minimum order {thr:.2f}; not traded")
    # 2. buying power per sleeve
    for s in summary:
        want = sum(x for (t, _i), x in buys.items() if t == s)
        if want <= 0:
            continue
        budget = val["sleeves"][s]["available_cash"] + sum(u * (bid(i) or 0.0) for (t, i), u in sells.items() if t == s)
        if want > budget + 0.005:
            f = max(budget, 0.0) / want
            notes.append(f"{s}: buys {want:.2f} exceed its own cash + sales {budget:.2f}; scaled by {f:.6f}")
            for k in [k for k in buys if k[0] == s]:
                buys[k] *= f
    # 3./4. per instrument: cross, then net
    crossings, closes, opens = [], {}, {}
    for iid in sorted({i for _s, i in sells} | {i for _s, i in buys}):
        S = {s: u for (s, i), u in sells.items() if i == iid and u > EPS}
        B = {s: x for (s, i), x in buys.items() if i == iid and x > 0.005}
        m = mk.get(iid)
        if crossing and S and B and m:
            cross = min(sum(S.values()) * m, sum(B.values()))
            cu = cross / m
            fr = {s: min(S[s], x) for s, x in split(cu, S, 9).items()}
            cu = sum(fr.values())
            to = split(cu, B, 9)
            crossings.append({"instrument_id": iid, "symbol": syms.get(iid), "mark": m,
                              "from": [[s, u] for s, u in sorted(fr.items()) if u > 0],
                              "to": [[s, u] for s, u in sorted(to.items()) if u > 0], "usd": round(cu * m, 6)})
            S = {s: S[s] - fr.get(s, 0.0) for s in S}
            B = {s: B[s] - to.get(s, 0.0) * m for s in B}
        S = {s: u for s, u in S.items() if u > EPS}
        B = {s: x for s, x in B.items() if x > 0.005}
        if S:
            closes[iid] = S
        if B:
            opens[iid] = B
        for s, u in S.items():
            summary.setdefault(s, {}).setdefault("sells_units", {})[syms.get(iid)] = u
        for s, x in B.items():
            summary.setdefault(s, {}).setdefault("buys_usd", {})[syms.get(iid)] = round(x, 2)
    if broker_available is not None and opens:
        # the broker can fund at most its available cash + the proceeds of this plan's own sales: a sleeve's virtual
        # cash can exceed that when another sleeve is temporarily negative (after a reallocation or withdrawal).
        # Scale the opens down here instead of letting risk block them outright (rehearsal finding 2026-10-29).
        proceeds = sum(sum(S.values()) * (bid(i) or 0.0) for i, S in closes.items())
        cap = max(broker_available + proceeds, 0.0)
        tot = sum(sum(B.values()) for B in opens.values())
        if tot > cap + 0.005:
            f = cap / tot
            notes.append(f"net opens {tot:.2f} exceed the broker's available cash + sale proceeds {cap:.2f}; "
                         f"scaled by {f:.6f}")
            opens = {i: {s: x * f for s, x in B.items()} for i, B in opens.items()}
    legs, k = [], 0
    for iid, S in sorted(closes.items()):
        need = sum(S.values())
        b = bid(iid)
        held = sorted([p for p in positions if p.instrument_id == iid], key=lambda p: (-p.units, p.position_id))
        for p in held:
            if need <= EPS:
                break
            if p.units <= need + 1e-6:
                legs.append(Leg(k, 1, "CLOSE", syms.get(iid) or str(iid), iid, round(p.value, 2), None, p.position_id,
                                f"sleeve sales {sorted(S)}", tuple(sorted(split(p.units, S, 9).items()))))
                need -= p.units
                k += 1
                continue
            if not b:
                notes.append(f"{syms.get(iid)}: no quote for a partial close")
                break
            units = math.floor(need * 1e6) / 1e6
            if units * b >= MIN_PARTIAL_USD and (p.units - units) * b >= MIN_POSITION_USD:
                legs.append(Leg(k, 1, "PARTIAL_CLOSE", syms.get(iid) or str(iid), iid, round(units * b, 2), units,
                                p.position_id, f"sleeve sales {sorted(S)}", tuple(sorted(split(units, S, 9).items()))))
                need -= units
                k += 1
        if need > 1e-6:
            notes.append(f"{syms.get(iid)}: {need:.6f} units ({need * (b or 0):.2f} USD) of sleeve sales not executable "
                         "within eToro minimums; they stay with the selling sleeve(s) (drift)")
    for iid, B in sorted(opens.items()):
        amt = _floor_cents(sum(B.values()))
        if amt < MIN_POSITION_USD:
            notes.append(f"{syms.get(iid)}: net open {amt:.2f} below eToro minimum USD {MIN_POSITION_USD}; not traded")
            continue
        legs.append(Leg(k, 2, "OPEN", syms.get(iid) or str(iid), iid, amt, None, None, f"sleeve buys {sorted(B)}",
                        tuple(sorted((s, round(x, 6)) for s, x in B.items()))))
        k += 1
    ordered = sorted(legs, key=lambda l: (l.phase, l.leg_index))
    legs = [Leg(i, l.phase, l.action, l.symbol, l.instrument_id, l.amount, l.units, l.position_id, l.reason,
                l.sleeves) for i, l in enumerate(ordered)]
    return {"legs": legs, "crossings": crossings, "notes": notes, "summary": summary}


def resize_sleeve_opens(legs: list, available_cash: float, sleeve_cash: dict) -> tuple:
    """Phase 2: cap every sleeve's share of the opens by ITS available virtual cash (after the phase-1 fills were
    posted), then the total by the broker's available cash. Never scales up."""
    left = dict(sleeve_cash)
    out, notes = [], []
    for l in sorted([l for l in legs if l.action == "OPEN"], key=lambda l: l.leg_index):
        new = {}
        for s, x in l.sleeves:
            a = max(0.0, min(float(x), left.get(s, 0.0)))
            if a < float(x) - 0.005:
                notes.append(f"{l.symbol}: sleeve {s} open {float(x):.2f} capped to its cash {a:.2f}")
            left[s] = left.get(s, 0.0) - a
            if a > 0:
                new[s] = round(a, 6)
        out.append((l, new))
    total = sum(sum(n.values()) for _l, n in out)
    cap = _floor_cents(max(available_cash - 0.01, 0.0))
    f = cap / total if total > cap and total > 0 else 1.0
    if f < 1.0:
        notes.append(f"opens scaled by {f:.6f} to the broker's available cash {cap:.2f}")
    res = []
    for l, new in out:
        sh = {s: x * f for s, x in new.items()}
        amt = _floor_cents(sum(sh.values()))
        if amt < MIN_POSITION_USD:
            notes.append(f"{l.symbol}: resized open {amt:.2f} below USD {MIN_POSITION_USD}; skipped")
            continue
        res.append(Leg(l.leg_index, l.phase, l.action, l.symbol, l.instrument_id, amt, None, None,
                       l.reason + (" (resized to sleeve cash)" if amt < l.amount else ""),
                       tuple(sorted((s, round(x, 6)) for s, x in sh.items()))))
    return res, notes
