"""Fill attribution: which broker positions this engine opened, proven by broker ORDER evidence.

First live session (2026-11-02): eToro's order lookup reported FILLED, with the new position ids in
positionExecutions[], up to more than 2 s before the portfolio endpoint listed those positions. The executor used to
record ownership only for positions the portfolio already listed and still marked the order FILLED, so the next
reconciliation classified two of the engine's own fills as foreign (EXTERNAL_POSITION), removed their cost from
strategy equity (a phantom -376.88 that tripped the daily loss stop) and halted ALL.

Rules (production lessons from Gold Miner, which never treated a submit acknowledgement as a fill and resolved every
open from the broker's own order status or position evidence on later cycles):
  * the order lookup is authoritative for WHICH positions an order created; the portfolio is authoritative for their
    current units and value. Ownership is recorded from the order as soon as the order says FILLED;
  * a position that is recorded but not yet listed is AWAITING_VISIBILITY: it counts at cost in strategy equity and in
    gross exposure (never a phantom loss, never an exposure under-count), its sleeve booking waits until the broker
    lists its units, and only after VISIBILITY_GRACE_S does its absence become POSITION_MISSING;
  * a filled full close whose position the portfolio still lists is AWAITING_ABSENCE for the same grace period;
  * a position that appears without an engine ownership row is attributed only if an engine order's lookup lists it
    (late attribution); otherwise it stays foreign, as before. Nothing is ever attributed by amount alone here.
"""
from __future__ import annotations

import datetime as dt
import json

from .accounting import record_flow
from .errors import BrokerError, BrokerUnavailable, EngineError

VISIBILITY_GRACE_S = 600
AWAIT_VISIBLE, AWAIT_ABSENT = "await_visible:", "await_absent:"
# intents whose order may have created positions that are not yet attributed
ATTRIBUTABLE = ("ACKNOWLEDGED", "PENDING", "PARTIALLY_FILLED", "UNKNOWN", "FILLED", "CANCELED", "RECONCILED")


def _strategy_of(row) -> str | None:
    return json.loads(row["result"] or "{}").get("strategy_id") if row["result"] else None


def markers(db, prefix: str) -> dict:
    out = {}
    for r in db.q("SELECT key, value FROM engine_state WHERE key LIKE ?", (prefix + "%",)):
        v = json.loads(r["value"])
        if v:
            out[r["key"][len(prefix):]] = v
    return out


def _age_s(since: str, now: str) -> float:
    return (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(since)).total_seconds()


def awaiting_cost(db, snap=None) -> float:
    """Cost of engine positions recorded from order evidence that the portfolio does not list yet (in `snap`, when
    given: a listed one is valued from the portfolio instead, never counted twice)."""
    listed = {p.position_id for p in snap.positions} if snap is not None else set()
    return round(sum(float(v["amount"]) for pid, v in markers(db, AWAIT_VISIBLE).items() if pid not in listed), 6)


def unlisted(db, snap) -> list:
    """Engine positions recorded from their order that this snapshot does not list: the snapshot may predate the
    fill (eToro's portfolio lagged as a whole on 2026-11-02 -- cash and positions together), so neither its cash nor
    an equity figure derived from it can be compared with the books yet."""
    listed = {p.position_id for p in snap.positions}
    return sorted(pid for pid in markers(db, AWAIT_VISIBLE) if pid not in listed)


def awaiting_flow_ids(db) -> set:
    ids = set()
    for v in markers(db, AWAIT_VISIBLE).values():
        ids.update(x for x in (v.get("flow_id"), v.get("fee_flow_id")) if x)
    return ids


def record_open(db, row: dict, pid: str, *, instrument_id: int, amount: float, units: float | None,
                fee: float | None, visible: bool, now: str, fill_hook=None, evidence: str) -> bool:
    """Record ONE engine-opened position (idempotent by position id). Returns False if it was already recorded."""
    if db.one("SELECT 1 FROM ownership WHERE position_id=?", (pid,)):
        return False
    oid = row["broker_order_id"]
    with db.tx():
        db.con.execute("INSERT INTO ownership VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, instrument_id, row["symbol"], "ENGINE", _strategy_of(row), row["intent_id"],
                        float(units or 0.0), float(amount), now, None, None,
                        f"opened by order {oid}" + ("" if visible else "; recorded from the order lookup before the "
                                                                       "portfolio listed it")))
        fid = record_flow(db, "TRADE_OPEN", -amount, source="broker-order", evidence=f"order {oid} position {pid}",
                          ref=f"open:{pid}", now=now)
        ffid = None
        if fee:
            ffid = record_flow(db, "FEE", -fee, source="broker-order",
                               evidence=f"order {oid} opening fee: frozenAmount - margin", ref=f"fee:open:{pid}",
                               now=now)
        db.audit("FILL_ATTRIBUTED", {"intent_id": row["intent_id"], "order_id": oid, "position_id": pid,
                                     "amount": amount, "fee": fee, "visible": visible, "evidence": evidence}, ts=now)
        if visible:
            if fill_hook:
                fill_hook(db, row, {"opened": [(pid, float(units or 0.0), float(amount), fid)],
                                    "fee": (fee, ffid) if ffid else None}, now)
        else:
            db.put(AWAIT_VISIBLE + pid, {"since": now, "intent_id": row["intent_id"], "amount": float(amount),
                                         "flow_id": fid, "fee": fee, "fee_flow_id": ffid})
    return True


def complete_visibility(db, snap, now: str, fill_hook=None) -> list:
    """Positions recorded from order evidence that the broker now lists: take the broker's units and amount, run
    the deferred sleeve booking. Returns the completed position ids."""
    live = {p.position_id: p for p in snap.positions}
    done = []
    for pid, m in markers(db, AWAIT_VISIBLE).items():
        p = live.get(pid)
        if p is None:
            continue
        row = db.one("SELECT * FROM intents WHERE intent_id=?", (m["intent_id"],))
        with db.tx():
            db.con.execute("UPDATE ownership SET units=?, amount=? WHERE position_id=?", (p.units, p.amount, pid))
            if fill_hook and row is not None:
                fill_hook(db, dict(row), {"opened": [(pid, p.units, float(m["amount"]), m["flow_id"])],
                                          "fee": (m["fee"], m["fee_flow_id"]) if m.get("fee_flow_id") else None}, now)
            db.put(AWAIT_VISIBLE + pid, None)
            db.audit("POSITION_VISIBLE", {"position_id": pid, "units": p.units, "amount": p.amount,
                                          "after_s": round(_age_s(m["since"], now), 3)}, ts=now)
        done.append(pid)
    for pid, m in markers(db, AWAIT_ABSENT).items():
        if pid not in live:
            db.put(AWAIT_ABSENT + pid, None)
            db.audit("POSITION_ABSENT", {"position_id": pid, "after_s": round(_age_s(m["since"], now), 3)}, ts=now)
    return done


def within_grace(db, prefix: str, pid: str, now: str) -> bool:
    m = db.get(prefix + pid)
    return bool(m) and _age_s(m["since"], now) < VISIBILITY_GRACE_S


def _candidates(db, instrument_id: int) -> list:
    return [dict(r) for r in db.q(
        f"SELECT * FROM intents WHERE action='OPEN' AND broker_order_id IS NOT NULL AND instrument_id=? AND mode<>"
        f"'shadow' AND status IN ({','.join('?' * len(ATTRIBUTABLE))}) ORDER BY created_at DESC",
        (instrument_id, *ATTRIBUTABLE))]


def late_attribute(db, reader, snap, now: str, fill_hook=None) -> list:
    """A listed position with no ownership row, created by an engine order (its lookup lists the position): record
    it as the engine's. Read-only broker calls only. Returns the attributed position ids."""
    owned = {r["position_id"] for r in db.q("SELECT position_id FROM ownership")}
    out = []
    for p in snap.positions:
        if p.position_id in owned:
            continue
        for row in _candidates(db, p.instrument_id):
            try:
                st = reader.order_status(row["broker_order_id"], "open")
            except (BrokerUnavailable, BrokerError):
                continue
            if st is None or p.position_id not in st.position_ids:
                continue
            if record_open(db, row, p.position_id, instrument_id=p.instrument_id, amount=p.amount, units=p.units,
                           fee=st.fee, visible=True, now=now, fill_hook=fill_hook,
                           evidence=f"late: order {row['broker_order_id']} lookup lists position {p.position_id}"):
                out.append(p.position_id)
            break
    return out


# ---------------------------------------------------------------------------------------------------- repair
def repair_plan(db, reader, position_id: str, intent_id: str) -> dict:
    """Read-only checks for re-attributing an EXTERNAL_UNACK position that an engine order created (the 2026-11-02
    case). Raises EngineError naming the first failed check; returns the plan otherwise."""
    own = db.one("SELECT * FROM ownership WHERE position_id=?", (position_id,))
    if own is None:
        raise EngineError(f"refused: position {position_id} has no ownership record")
    if own["owner"] != "EXTERNAL_UNACK":
        raise EngineError(f"refused: position {position_id} is {own['owner']}; only an unacknowledged position can "
                          "be re-attributed (an acknowledged or pre-existing position is never taken over this way)")
    row = db.one("SELECT * FROM intents WHERE intent_id=?", (intent_id,))
    if row is None or row["action"] != "OPEN" or not row["broker_order_id"]:
        raise EngineError(f"refused: {intent_id} is not a submitted OPEN intent with a broker order id")
    if row["mode"] == "shadow" or row["status"] not in ATTRIBUTABLE:
        raise EngineError(f"refused: intent {intent_id} is {row['mode']}/{row['status']}")
    if int(row["instrument_id"]) != int(own["instrument_id"]):
        raise EngineError(f"refused: intent instrument {row['instrument_id']} != position instrument "
                          f"{own['instrument_id']}")
    st = reader.order_status(row["broker_order_id"], "open")
    if st is None or position_id not in st.position_ids:
        raise EngineError(f"refused: the broker's lookup of order {row['broker_order_id']} does not list position "
                          f"{position_id}")
    if st.status_id not in (3, 5, 9, 10):
        raise EngineError(f"refused: order {row['broker_order_id']} status {st.status_id} is not a fill")
    snap = reader.account_snapshot()
    p = next((x for x in snap.positions if x.position_id == position_id), None)
    if p is None:
        raise EngineError(f"refused: position {position_id} is not listed by the broker now")
    orig = db.one("SELECT * FROM cash_flows WHERE ref=? AND kind='EXTERNAL_TRADE'", (f"pos:{position_id}",))
    if orig is None:
        raise EngineError(f"refused: no EXTERNAL_TRADE flow was booked for position {position_id}")
    if abs(float(orig["amount"]) + p.amount) > 0.01:
        raise EngineError(f"refused: booked external amount {orig['amount']} != broker position amount {p.amount}")
    return {"position_id": position_id, "intent_id": intent_id, "order_id": row["broker_order_id"],
            "symbol": row["symbol"], "instrument_id": p.instrument_id, "units": p.units, "amount": p.amount,
            "order_fee": st.fee, "reverses_flow": orig["flow_id"], "recon_snapshot_id": orig["recon_snapshot_id"],
            "order_status": st.status_id}


def repair_apply(db, plan: dict, operator: str, note: str, now: str, fill_hook=None) -> dict:
    """Re-attribute per a plan from repair_plan(): ownership -> ENGINE; the EXTERNAL_TRADE booking is reversed and the
    TRADE_OPEN booked, both against the snapshot the original was reconciled in (the cash reconciliation is not
    affected twice); the sleeves receive the reversal and the open fill (no catch-up trade is requested). Fees are
    not booked here: the observed cash discrepancy is classified with `account resolve --classify FEE`."""
    if not operator.strip() or not note.strip():
        raise EngineError("--operator and --note are required")
    pid = plan["position_id"]
    row = dict(db.one("SELECT * FROM intents WHERE intent_id=?", (plan["intent_id"],)))
    from . import halts, sleeves
    with db.tx():
        own = db.one("SELECT owner FROM ownership WHERE position_id=?", (pid,))
        if own is None or own["owner"] != "EXTERNAL_UNACK":
            raise EngineError(f"refused: position {pid} changed since the plan was made")
        db.con.execute("UPDATE ownership SET owner='ENGINE', strategy_id=?, intent_id=?, units=?, amount=?, note=? "
                       "WHERE position_id=?",
                       (_strategy_of(row), row["intent_id"], plan["units"], plan["amount"],
                        f"opened by order {plan['order_id']}; re-attributed by {operator}: {note}", pid))
        rev = record_flow(db, "EXTERNAL_TRADE", plan["amount"], source="attribution-repair",
                          evidence=f"reverses {plan['reverses_flow']}: position {pid} was opened by engine order "
                                   f"{plan['order_id']}", ref=f"reverse:pos:{pid}", now=now,
                          recon_snapshot_id=plan["recon_snapshot_id"])
        fid = record_flow(db, "TRADE_OPEN", -plan["amount"], source="broker-order",
                          evidence=f"order {plan['order_id']} position {pid} (re-attributed)", ref=f"open:{pid}",
                          now=now, recon_snapshot_id=plan["recon_snapshot_id"])
        posted = db.q("SELECT sleeve, cash FROM sleeve_ledger WHERE flow_id=?", (plan["reverses_flow"],))
        for r in posted:
            sleeves.post(db, r["sleeve"], "EXTERNAL_TRADE", now, ref=f"flow:{rev}", cash=-float(r["cash"]),
                         flow_id=rev, detail={"policy": "REVERSAL", "reverses": plan["reverses_flow"]})
        sleeves.mark_posted(db, rev, "REVERSAL", now)
        if fill_hook:
            fill_hook(db, row, {"opened": [(pid, plan["units"], plan["amount"], fid)], "fee": None}, now)
        for x in db.q("SELECT exception_id FROM exceptions WHERE kind='EXTERNAL_POSITION' AND subject=? AND "
                      "status='OPEN'", (pid,)):
            halts.resolve_exception(db, x["exception_id"], operator, now)
        db.audit("ATTRIBUTION_REPAIRED", {**plan, "operator": operator, "note": note, "reversal_flow": rev,
                                          "open_flow": fid}, ts=now)
    return {"repaired": pid, "reversal_flow": rev, "open_flow": fid}
