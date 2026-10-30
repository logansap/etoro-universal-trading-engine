"""The multi-strategy cycle (portfolio.mode multi_strategy). Same safety path as the single-strategy cycle:

  reconcile (account, then sleeves) -> per-sleeve decisions -> ONE portfolio plan per session for the sleeves whose
  execution window is open -> aggregate risk -> internal crossings -> executor (sell-then-buy, per-sleeve resize)
  -> reconcile -> record (account + sleeves).

Strategy independence: each sleeve has its own schedule, state, decision and rebalance flag. A sleeve that is
halted (sleeve halt), drifted, undecided or not rebalancing simply holds; the others continue. A sleeve is planned
at most once per session (engine_state sleeve_planned:<session>:<sleeve>), so a restart, a duplicate cycle or a
late decision of another sleeve never executes it twice.
"""
from __future__ import annotations

import datetime as dt
import json

from . import accounting, halts, sleeves
from .calendar import NY
from .canonical import short_id
from .multi_planner import plan_portfolio, resize_sleeve_opens
from .order_planner import Leg
from .order_state import transition
from .portfolio import Allocation, allocate
from .reconciliation import engine_position_ids
from .risk import check_limits
from .scheduler import Scheduler
from .strategy_api import StrategyDecision
from ..strategy_modules import REGISTRY

RESUMABLE = ("PLANNED", "HALTED_PHASE1", "HALTED_PHASE2", "HALTED_RECONCILIATION")


def _sleeve_blocked(e, sid: str, drift: dict, for_execution: bool) -> str | None:
    if sid in drift:
        return "config drift: " + "; ".join(drift[sid])
    for h in halts.active_sleeve(e.db, sid):
        if h["scope"] == "ALL":
            return f"sleeve halt {h['kind']}"
        if for_execution and not e.cfg["risk"]["halt_allows_reductions"]:
            return f"sleeve halt {h['kind']} (NEW_EXPOSURE, reductions not allowed)"
    return None


def cycle_portfolio(e, now: dt.datetime) -> dict:
    nows = e.nows(now)
    out = {"at": now.isoformat(), "mode": e.mode, "portfolio": True}
    drift = sleeves.drift(e.db, e.specs, REGISTRY)
    for sid, why in drift.items():
        halts.raise_halt(e.db, "CONFIG_DRIFT", "ALL", "; ".join(why), nows, subject=sid, sleeve=sid)
    if drift:
        out["drift"] = drift
    if e.writer is not None:
        out["resolved"] = e.executor.resolve_all()
    ids = e.sleeve_ids()
    out["missed"] = [m for sid in ids for m in e.record_missed(e.specs[sid], now)]
    dec, exe = {}, {}
    for sid in ids:
        sched = Scheduler(e.cal, e.specs[sid]["schedule"])
        dw = sched.current_decision_window(now)
        if dw and not _sleeve_blocked(e, sid, drift, False):
            dec.setdefault(dw.session, []).append((sid, dw.is_late(now)))
        ew = sched.current_execution_window(now)
        if ew:
            exe.setdefault(ew.session, []).append(sid)
    if dec:
        out["decisions"] = {s.isoformat(): decide_portfolio(e, s, v, now) for s, v in sorted(dec.items())}
    if exe:
        out["execution"] = {s.isoformat(): execute_portfolio(e, s, v, now, drift) for s, v in sorted(exe.items())}
    last_idle = e.db.get("last_idle_reconcile")
    if not dec and not exe and (not last_idle or (now - dt.datetime.fromisoformat(last_idle)).total_seconds() >= 900):
        r = e.reconcile("idle", now)
        e.db.put("last_idle_reconcile", now.isoformat())
        out["reconciliation"] = r.status
    if e.writer is not None and not exe:
        e.executor.abandon_stale(set())
    e.db.put("last_cycle_at", now.isoformat())
    out["notifications"] = e.flush_notifications(now)
    e.db.heartbeat("engine", {"phase": "cycle", "mode": e.mode, "strategy": "PORTFOLIO", "sleeves": ids}, ts=nows)
    e.log("cycle", **{k: v for k, v in out.items() if k != "at"})
    return out


def decide_portfolio(e, session: dt.date, sids: list, now: dt.datetime) -> dict:
    r = e.reconcile("before_decision", now)
    res = {sid: e.decide(e.specs[sid], session, now, late, record=False, recon=r) for sid, late in sids}
    record_portfolio_daily(e, r, session.isoformat(), now)
    return res


def record_portfolio_daily(e, r, session: str, now: dt.datetime) -> dict | None:
    if r.snapshot is None or r.strategy_equity is None or not r.clean:
        return None
    eng = [p for p in r.snapshot.positions if p.position_id in engine_position_ids(e.db)]
    exposure = {e.id_to_symbol.get(p.instrument_id, str(p.instrument_id)): round(p.value, 2) for p in eng}
    rep = accounting.account_report(e.db, r)
    acc = accounting.record_daily(e.db, session, account_equity=r.account_equity, strategy_equity=r.strategy_equity,
                                  credit=r.snapshot.credit, exposure=exposure, strategy_id="PORTFOLIO",
                                  now=e.nows(now), foreign_value=rep["foreign_positions_value"])
    val = e.sleeve_valuation(r)
    return {"account": acc, "sleeves": sleeves.record_daily(e.db, session, val, e.nows(now)) if val["complete"] else
            "incomplete valuation (no broker value for a held instrument): not recorded"}


def _merged_spec(e, specs: list) -> dict:
    inst = {}
    for sp in specs:
        for sym, x in sp["instruments"].items():
            if sym not in inst or x["eligibility"] == sp["eligibility"]["required_status"]:
                inst[sym] = x
    return {"id": "PORTFOLIO", "instruments": inst, "eligibility": specs[0]["eligibility"],
            "risk": {"max_gross_exposure": e.cfg["risk"]["max_gross_exposure"],
                     "max_instrument_weight": e.cfg["risk"]["max_instrument_weight"]}}


def execute_portfolio(e, session: dt.date, sids: list, now: dt.datetime, drift: dict) -> dict:
    db, nows, ss = e.db, e.nows(now), session.isoformat()
    notes, group = [], []
    for sid in sorted(sids):
        drow = db.one("SELECT * FROM decisions WHERE strategy_id=? AND session=?", (sid, ss))
        if drow is None or drow["status"] != "DECIDED":
            notes.append(f"{sid}: no decision for {ss}; holds")
            continue
        planned = db.get(f"sleeve_planned:{ss}:{sid}")
        why = _sleeve_blocked(e, sid, drift, True)
        if why and not planned:
            notes.append(f"{sid}: {why}; holds")
            continue
        d = json.loads(drow["decision"])
        if not d["rebalance"] and not planned:
            cu = sleeves.catch_up_pending(db, sid)
            if cu is None:
                notes.append(f"{sid}: decision does not rebalance; holds")
                continue
            d = {**d, "rebalance": True}
            notes.append(f"{sid}: CAPITAL_CATCH_UP ({cu['reason']}, {cu['ref']}): re-targets the decision's standing "
                         "weights on the sleeve's equity (no new signal)")
        group.append((sid, drow["decision_id"], d, planned))
    if not group:
        return {"status": "NO_REBALANCE", "notes": notes}
    planned_ids = {g[3] for g in group if g[3]}
    new = [g for g in group if not g[3]]
    out = {"notes": notes}
    for pid in sorted(planned_ids):                     # resume a plan started earlier (restart, halted phase)
        out[pid] = _resume(e, pid, now)
    if not new:
        done = all(out[p]["status"] == "ALREADY_EXECUTED" for p in planned_ids)
        return {**out, "status": "ALREADY_EXECUTED" if done else "RESUMED"}
    key = "+".join(g[0] for g in new)
    plan_id = "P" + short_id("PORTFOLIO", ss, key, e.mode, n=23)
    if db.one("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)):
        return {**out, plan_id: _resume(e, plan_id, now)}
    r = e.reconcile("before_planning", now)
    if not r.clean:
        return {**out, "status": "BLOCKED_RECONCILIATION", "issues": [i["kind"] for i in r.issues]}
    val = e.sleeve_valuation(r)
    if r.strategy_equity is None or not val["complete"]:
        halts.raise_halt(db, "ACCOUNT_SIZING_BLOCKED", "ALL", "account or sleeve equity unavailable: the broker gave no "
                         "unrealized P&L / value for an engine position; no order can be sized", nows,
                         subject="equity")
        return {**out, "status": "BLOCKED_EQUITY_UNAVAILABLE"}
    flows = db.one("SELECT COALESCE(SUM(amount),0) s FROM cash_flows WHERE external=1 AND performance_ts IS NULL")["s"]
    prev = db.one("SELECT * FROM daily_equity ORDER BY session DESC LIMIT 1")
    check_limits(db, e.cfg, dict(prev) if prev else None, r.strategy_equity, flows, nows)
    sleeves.limit_checks(db, e.cfg, ss, val, nows)
    inputs, decision_ids = [], {}
    for sid, did, d, _p in new:
        why = _sleeve_blocked(e, sid, drift, True)
        if why:
            notes.append(f"{sid}: {why}; holds")
            continue
        spec = e.specs[sid]
        dec = StrategyDecision(**{k: v for k, v in d.items() if k != "decision_id"})
        alloc = allocate(dec, spec, val["sleeves"][sid]["equity"])
        gross = sum(alloc.target_weights.values())
        if gross > spec["risk"]["max_gross_exposure"] + 1e-9:
            notes.append(f"{sid}: gross {gross:.4f} > its limit {spec['risk']['max_gross_exposure']}; holds")
            continue
        inputs.append({"sleeve": sid, "spec": spec, "alloc": alloc, "rebalance": True})
        decision_ids[sid] = did
    if not inputs:
        return {**out, "status": "NO_REBALANCE", "notes": notes}
    positions = e.engine_positions(r)
    tgt, exact, iids = {}, {}, {}
    for inp in inputs:
        a = inp["alloc"]
        for sym, usd in a.targets_usd.items():
            tgt[sym] = round(tgt.get(sym, 0.0) + usd, 2)
            exact[sym] = exact.get(sym, 0.0) + a.target_weights[sym] * a.strategy_equity
            iids[sym] = a.instrument_ids[sym]
    eq = r.strategy_equity
    # weights from the UNROUNDED sleeve targets: a fully invested sleeve (gross exactly 1.0) must not read as
    # 1.00000x because each USD target was rounded up to the cent (that blocked the whole plan at the gross limit)
    agg = Allocation(eq, tgt, {s: (v / eq if eq > 0 else 0.0) for s, v in exact.items()}, iids, {},
                     round(eq - sum(tgt.values()), 2))
    quotes = e.quotes_for(positions, agg)
    pf = sleeves.portfolio(db)
    from . import funding
    pp = plan_portfolio(inputs, val, positions, quotes, e.id_to_symbol, e.cfg["risk"]["min_order_usd"],
                        crossing=pf.get("internal_crossing", True),
                        broker_available=r.snapshot.available_cash - funding.suspense_total(db))
    body = {"decision": {"session": ss, "decision_id": "PORTFOLIO:" + key}, "allocation": agg.to_dict(),
            "notes": notes + pp["notes"], "crossings": pp["crossings"], "sleeves": [i["sleeve"] for i in inputs],
            "sleeve_decisions": decision_ids, "summary": pp["summary"]}

    def cross():
        if any(h["scope"] == "NEW_EXPOSURE" for h in halts.active(db)):
            return {"skipped": "NEW_EXPOSURE halt active (a crossing is new exposure for the buying sleeve)"}
        return {"posted": sleeves.apply_crossings(db, plan_id, pp["crossings"], e.nows()),
                "crossings": len(pp["crossings"])}

    res = e.execute_legs(plan_id, pp["legs"], _merged_spec(e, [i["spec"] for i in inputs]), body, now,
                         plan_kind="portfolio", resize_fn=_resizer(e), before_submit=cross)
    if db.one("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)):
        for inp in inputs:                           # each sleeve is planned at most once per session
            db.put(f"sleeve_planned:{ss}:{inp['sleeve']}", plan_id)
            sleeves.consume_catch_up(db, inp["sleeve"], plan_id, nows)
    after = e.reconcile("after_portfolio", e.now())
    return {**out, plan_id: {**res, "after": after.status, "legs": len(pp["legs"]),
                             "crossings": len(pp["crossings"]), "notes": body["notes"]}}


def _resizer(e):
    def fn(pending, available_cash):
        bal, res = sleeves.balances(e.db), sleeves.reserved(e.db)
        cash = {s: round(b["cash"] - res.get(s, 0.0), 6) for s, b in bal.items()}
        return resize_sleeve_opens(pending, available_cash, cash)
    return fn


def _resume(e, plan_id: str, now: dt.datetime) -> dict:
    row = e.db.one("SELECT status, plan FROM plans WHERE plan_id=?", (plan_id,))
    if row is None:
        return {"status": "NOT_FOUND"}
    if row["status"] not in RESUMABLE:
        return {"status": "ALREADY_EXECUTED", "plan_status": row["status"]}
    if any(h["scope"] == "ALL" for h in halts.active(e.db)):
        return {"status": "HALTED_RESOLVE_ONLY", "resolved": e.executor.resolve_all()}
    plan = json.loads(row["plan"])
    if plan.get("crossings") and not any(h["scope"] == "NEW_EXPOSURE" for h in halts.active(e.db)):
        sleeves.apply_crossings(e.db, plan_id, plan["crossings"], e.nows())         # idempotent
    legs = [Leg(**l) for l in plan["legs"]]
    approved = [l for l in legs if l.leg_index not in {b[0]["leg_index"] for b in plan["blocked"]}]
    res = e.executor.execute(plan_id, approved, "PORTFOLIO", lambda p: e.reconcile(p, e.now()), resize_fn=_resizer(e))
    after = e.reconcile("after_execution", e.now())
    if after.clean and res["status"].startswith("DONE"):
        for x in e.db.q("SELECT intent_id, status FROM intents WHERE plan_id=?", (plan_id,)):
            if x["status"] in ("FILLED", "REJECTED", "CANCELED", "NOT_SUBMITTED"):
                transition(e.db, x["intent_id"], "RECONCILED", e.nows(), {"snapshot": after.snapshot_id})
    e.db.x("UPDATE plans SET status=? WHERE plan_id=?", (res["status"], plan_id))
    return {**res, "resumed": True}


def portfolio_report(e, r) -> dict:
    val = e.sleeve_valuation(r) if r.snapshot is not None else {"sleeves": {}, "complete": False}
    w = sleeves.weights(e.db)
    eq = r.strategy_equity
    return {"weights": w, "valuation": val, "performance": sleeves.performance(e.db),
            "equity_share": {s: (v["equity"] / eq if eq and v["equity"] is not None else None)
                             for s, v in val["sleeves"].items()},
            "sleeve_halts": halts.active_sleeve(e.db), "drift": sleeves.drift(e.db, e.specs, REGISTRY),
            "yaml_weights_note": _yaml_note(e, w),
            "execution_session_ny": e.now().astimezone(NY).date().isoformat()}


def _yaml_note(e, w: dict):
    y = {s["strategy"]: s["weight"] for s in e.cfg["portfolio"]["sleeves"]}
    return None if y == w else (f"engine.yaml portfolio weights {y} differ from the active weights {w}; YAML is used "
                                "only at the first start. Use `portfolio reallocate-preview` / `reallocate`.")
