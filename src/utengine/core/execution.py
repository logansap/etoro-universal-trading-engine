"""Execution with persist-before-act, deterministic intent ids and evidence-only recovery.

Exactly-once is not achievable against a broker that offers no client order id (eToro's create-order response
is {token, orderId, referenceId}; a client-supplied idempotency key is not documented). What this module
guarantees instead:
  * at most one submission per intent: the intent row (deterministic id = hash(plan, leg)) is committed as
    SUBMITTING, with a pre-submit broker snapshot, BEFORE the broker is called; a SUBMITTING/UNKNOWN intent is never
    submitted again -- not after a timeout, not after a crash, not after a restart;
  * an ambiguous outcome is resolved only from broker evidence (order lookup by id, or a new position / pending
    order / changed position that matches the intent and did not exist in the pre-submit snapshot); if the evidence
    is missing or ambiguous the intent stays UNKNOWN and the engine halts (all new orders) for an operator;
  * opens (phase 2) start only after every close (phase 1) is final AND a reconciliation is clean, and are re-sized
    to the cash the broker then reports available.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from . import halts
from .accounting import record_flow
from .canonical import canonical_json, short_id
from .errors import BrokerAmbiguousError, BrokerError, BrokerUnavailable, EngineError, ShadowWriteViolation
from .order_planner import resize_opens
from .order_state import DONE, UNRESOLVED, transition
from ..brokers.base import STATUS_FILLED, STATUS_PARTIAL, STATUS_PLACED, STATUS_RECEIVED, STATUS_REJECTED

# eToro orders:lookup status ids beyond 1-5 (api-portal, 2026-10-27): 6 PendingCancel, 7 Canceled, 8 Expired,
# 9 CanceledPartiallyFilled, 10 RejectedPartiallyFilled, 11 WaitingForMarket, 12 PendingTriggeredRate
IN_FLIGHT = (STATUS_RECEIVED, STATUS_PLACED, 6, 11, 12)
FINAL_CANCELED = (7, 8, 9, 10)
FINAL_PARTIAL = (9, 10)


def intent_id(plan_id: str, leg) -> str:
    return "I" + short_id(plan_id, leg.leg_index, leg.action, leg.symbol, leg.position_id, n=23)


class Executor:
    def __init__(self, db, reader, writer, cfg, mode: str, clock, sleep=time.sleep):
        self.db, self.reader, self.writer, self.cfg, self.mode = db, reader, writer, cfg, mode
        self.clock, self.sleep = clock, sleep
        self.fill_hook = None             # multi-strategy: sleeves.on_fill(db, row, info, now)
        rc = cfg["reconciliation"]
        self.poll_s, self.poll_n = rc["order_poll_seconds"], rc["order_poll_attempts"]

    def now(self) -> str:
        return self.clock().astimezone(dt.timezone.utc).isoformat()

    # ------------------------------------------------------------------ intents
    def ensure_intent(self, plan_id: str, leg, strategy_id: str) -> dict:
        iid = intent_id(plan_id, leg)
        row = self.db.one("SELECT * FROM intents WHERE intent_id=?", (iid,))
        if row:
            return dict(row)
        now = self.now()
        with self.db.tx():
            self.db.con.execute(
                "INSERT INTO intents(intent_id,plan_id,leg_index,phase,action,symbol,instrument_id,amount,units,"
                "position_id,status,broker_order_id,mode,created_at,updated_at,pre_submit,result) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, plan_id, leg.leg_index, leg.phase, leg.action, leg.symbol, leg.instrument_id, leg.amount,
                 leg.units, leg.position_id, "INTENT_PERSISTED", None, self.mode, now, now, None,
                 canonical_json({"strategy_id": strategy_id, "reason": leg.reason,
                                 **({"sleeves": [list(x) for x in leg.sleeves]} if leg.sleeves else {})})))
            self.db.con.execute("INSERT INTO order_events(intent_id,ts,from_status,to_status,detail) VALUES(?,?,?,?,?)",
                                (iid, now, None, "INTENT_PERSISTED", canonical_json(leg.to_dict())))
            self.db.audit("INTENT_PERSISTED", {"intent_id": iid, "leg": leg.to_dict(), "mode": self.mode}, ts=now)
        return dict(self.db.one("SELECT * FROM intents WHERE intent_id=?", (iid,)))

    # ------------------------------------------------------------------ plan execution
    def execute(self, plan_id: str, legs: list, strategy_id: str, recon_fn, resize_fn=None) -> dict:
        """recon_fn(point) -> ReconResult. resize_fn(pending_opens, available_cash) -> (legs, notes) replaces the
        single-strategy resize (multi-strategy: per-sleeve cash caps). Returns a summary dict."""
        if self.mode == "shadow":
            for leg in legs:
                row = self.ensure_intent(plan_id, leg, strategy_id)
                if row["status"] == "INTENT_PERSISTED":
                    transition(self.db, row["intent_id"], "SHADOW_RECORDED", self.now(),
                               {"note": "shadow mode: recorded, never sent (no write capability)"})
            return {"status": "SHADOW", "legs": len(legs)}
        if self.writer is None:
            raise ShadowWriteViolation("no writer available")
        out = {"status": "DONE", "phase1": [], "phase2": [], "notes": []}
        p1 = [l for l in legs if l.phase == 1]
        for leg in p1:
            out["phase1"].append(self.run_leg(plan_id, leg, strategy_id))
        if any(s in UNRESOLVED for s in out["phase1"]):
            halts.raise_halt(self.db, "UNRESOLVED_ORDER", "ALL", f"plan {plan_id}: a close is unresolved; opens not "
                             "started", self.now())
            out["status"] = "HALTED_PHASE1"
            return out
        p2 = [l for l in legs if l.phase == 2]
        if p2 and "REJECTED" in out["phase1"]:
            # sell-then-buy: the buys were sized on cash the rejected close should have released
            out["status"] = "PHASE1_REJECTED"
            out["notes"].append("a close was rejected; opens not started")
            self.db.alert("WARNING", "ORDER_REJECTED", f"plan {plan_id}: close rejected; opens skipped", ts=self.now())
            return out
        if p2:
            r = recon_fn("after_phase1")
            if not r.clean:
                out["status"] = "HALTED_RECONCILIATION"
                out["notes"].append([i["kind"] for i in r.issues])
                return out
            done2 = {row["leg_index"] for row in self.db.q("SELECT leg_index FROM intents WHERE plan_id=? AND phase=2",
                                                            (plan_id,))}
            pending = [l for l in p2 if l.leg_index not in done2]
            resized, notes = (resize_fn or resize_opens)(pending, r.snapshot.available_cash)
            out["notes"] += notes
            keep = {l.leg_index for l in resized}
            for l in pending:
                if l.leg_index not in keep:
                    row = self.ensure_intent(plan_id, l, strategy_id)
                    transition(self.db, row["intent_id"], "BLOCKED", self.now(), {"reason": "unaffordable after resize"})
            for leg in sorted([l for l in p2 if l.leg_index in done2] + resized, key=lambda l: l.leg_index):
                out["phase2"].append(self.run_leg(plan_id, leg, strategy_id))
            if any(s in UNRESOLVED for s in out["phase2"]):
                halts.raise_halt(self.db, "UNRESOLVED_ORDER", "ALL", f"plan {plan_id}: an open is unresolved",
                                 self.now())
                out["status"] = "HALTED_PHASE2"
        if out["status"] == "DONE" and "REJECTED" in out["phase1"] + out["phase2"]:
            out["status"] = "DONE_WITH_REJECTIONS"
            self.db.alert("WARNING", "ORDER_REJECTED", f"plan {plan_id}: broker rejected a leg (not retried)",
                          ts=self.now())
        return out

    def run_leg(self, plan_id: str, leg, strategy_id: str) -> str:
        row = self.ensure_intent(plan_id, leg, strategy_id)
        st = row["status"]
        if st in DONE:
            return st                                     # idempotent: never redo a finished intent
        if st in UNRESOLVED:
            return self.resolve(row)                      # never resubmit: query only
        return self.submit(row)

    # ------------------------------------------------------------------ submission
    def submit(self, row: dict) -> str:
        if self.writer is None:
            raise ShadowWriteViolation("submit() called without write capability")
        iid = row["intent_id"]
        try:
            snap = self.reader.account_snapshot()
        except (BrokerUnavailable, BrokerError) as exc:
            transition(self.db, iid, "ABANDONED", self.now(), {"reason": f"pre-submit read failed: {exc}"})
            return "ABANDONED"
        pre = {"positions": {p.position_id: p.units for p in snap.positions},
               "pending": [o.order_id for o in snap.pending_orders], "credit": snap.credit, "at": snap.as_of}
        transition(self.db, iid, "SUBMITTING", self.now(), {"pre_submit_snapshot": snap.raw_sha256[:16]},
                   pre_submit=pre)
        try:
            if row["action"] == "OPEN":
                res = self.writer.submit_open(row["instrument_id"], row["amount"], is_buy=True, leverage=1,
                                              client_ref=iid)
            else:
                units = row["units"] if row["action"] == "PARTIAL_CLOSE" else None
                res = self.writer.submit_close(row["position_id"], row["instrument_id"], units, client_ref=iid)
        except BrokerAmbiguousError as exc:
            transition(self.db, iid, "UNKNOWN", self.now(), {"reason": f"ambiguous write: {exc}"})
            return self.resolve(dict(self.db.one("SELECT * FROM intents WHERE intent_id=?", (iid,))))
        except BrokerError as exc:
            transition(self.db, iid, "REJECTED", self.now(), {"reason": f"definite broker error: {exc}"})
            return "REJECTED"
        transition(self.db, iid, "ACKNOWLEDGED", self.now(), {"order_id": res.order_id}, broker_order_id=res.order_id)
        return self.poll(dict(self.db.one("SELECT * FROM intents WHERE intent_id=?", (iid,))))

    def poll(self, row: dict) -> str:
        iid, oid = row["intent_id"], row["broker_order_id"]
        kind = "open" if row["action"] == "OPEN" else "close"
        status = row["status"]
        for k in range(self.poll_n):
            try:
                st = self.reader.order_status(oid, kind)
            except (BrokerUnavailable, BrokerError):
                st = None
            if st is not None:
                if st.status_id == STATUS_FILLED:
                    self.apply_fill(row, st)
                    transition(self.db, iid, "FILLED", self.now(), {"order_status": st.raw})
                    return "FILLED"
                if st.status_id == STATUS_REJECTED:
                    transition(self.db, iid, "REJECTED", self.now(),
                               {"error_code": st.error_code, "error": st.error_message})
                    return "REJECTED"
                if st.status_id in FINAL_CANCELED:
                    if st.status_id in FINAL_PARTIAL:          # final, with some units executed
                        self.apply_fill(row, st)
                    transition(self.db, iid, "CANCELED", self.now(), {"broker_status": st.status_id,
                                                                      "error_code": st.error_code})
                    return "CANCELED"
                if st.status_id == STATUS_PARTIAL:
                    self.apply_fill(row, st)
                    transition(self.db, iid, "PARTIALLY_FILLED", self.now(), {"filled_amount": st.filled_amount})
                    status = "PARTIALLY_FILLED"
                elif st.status_id in IN_FLIGHT and status in ("ACKNOWLEDGED", "UNKNOWN"):
                    transition(self.db, iid, "PENDING", self.now(), {"broker_status": st.status_id})
                    status = "PENDING"
            if k + 1 < self.poll_n and self.poll_s:
                self.sleep(self.poll_s)
        return status                                     # still unresolved: caller halts; resolve() later

    # ------------------------------------------------------------------ effects of fills
    def apply_fill(self, row: dict, st) -> None:
        now = self.now()
        snap = self.reader.account_snapshot()
        live = {p.position_id: p for p in snap.positions}
        strategy_id = json.loads(row["result"] or "{}").get("strategy_id") if row["result"] else None
        if row["action"] == "OPEN":
            for pid in st.position_ids:
                if self.db.one("SELECT 1 FROM ownership WHERE position_id=?", (pid,)):
                    continue
                p = live.get(pid)
                if p is None:
                    continue                               # not visible yet: reconciliation will flag it
                with self.db.tx():
                    self.db.con.execute("INSERT INTO ownership VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                        (pid, p.instrument_id, row["symbol"], "ENGINE", strategy_id, row["intent_id"],
                                         p.units, p.amount, now, None, None, f"opened by order {row['broker_order_id']}"))
                    fid = record_flow(self.db, "TRADE_OPEN", -p.amount, source="broker-order",
                                      evidence=f"order {row['broker_order_id']} position {pid}", ref=f"open:{pid}",
                                      now=now)
                    if self.fill_hook:
                        self.fill_hook(self.db, row, {"opened": [(pid, p.units, p.amount, fid)]}, now)
            return
        pid = row["position_id"]
        own = self.db.one("SELECT * FROM ownership WHERE position_id=?", (pid,))
        foreign = own is not None and own["owner"] != "ENGINE"
        fid = None
        if st.proceeds is not None:
            # closing a FOREIGN position (takeover only) brings capital into the engine's scope: it is a capital
            # contribution (TAKEOVER_PROCEEDS, external), not strategy P&L
            fid = record_flow(self.db, "TAKEOVER_PROCEEDS" if foreign else "TRADE_CLOSE", st.proceeds,
                              source="broker-order", evidence=f"close order {row['broker_order_id']} proceeds",
                              ref=f"close:{row['broker_order_id'] or row['intent_id']}", now=now)
        before = own["units"] if own and own["closed_at"] is None else None
        after = live[pid].units if pid in live else 0.0
        with self.db.tx():
            if pid in live:
                self.db.con.execute("UPDATE ownership SET units=?, amount=? WHERE position_id=?",
                                    (live[pid].units, live[pid].amount, pid))
            elif own and own["closed_at"] is None:
                self.db.con.execute("UPDATE ownership SET closed_at=?, close_intent_id=? WHERE position_id=?",
                                    (now, row["intent_id"], pid))
            if self.fill_hook and before is not None and not foreign:
                self.fill_hook(self.db, row, {"closed_units": round(before - after, 9), "proceeds": st.proceeds,
                                              "flow_id": fid}, now)

    # ------------------------------------------------------------------ recovery
    def resolve(self, row: dict) -> str:
        """Resolve an unresolved intent from broker evidence only. Never submits."""
        iid = row["intent_id"]
        if row["broker_order_id"]:
            return self.poll(row)
        if row["status"] == "SUBMITTING":
            transition(self.db, iid, "UNKNOWN", self.now(), {"reason": "found SUBMITTING without an order id "
                                                                       "(crash or lost response)"})
            row = dict(self.db.one("SELECT * FROM intents WHERE intent_id=?", (iid,)))
        if row["action"] == "OPEN" and hasattr(self.reader, "order_status_by_client_ref"):
            # eToro: orders:lookup?referenceId=<the x-request-id this intent was sent with> -- the documented
            # handle for a lost create-order response. Found -> follow that order by its id.
            try:
                st = self.reader.order_status_by_client_ref(iid, "open")
            except (BrokerUnavailable, BrokerError):
                st = None
            if st is not None and st.order_id:
                with self.db.tx():
                    self.db.con.execute("UPDATE intents SET broker_order_id=? WHERE intent_id=?", (st.order_id, iid))
                self.db.audit("ORDER_FOUND_BY_REFERENCE", {"intent_id": iid, "order_id": st.order_id}, ts=self.now())
                return self.poll(dict(self.db.one("SELECT * FROM intents WHERE intent_id=?", (iid,))))
        pre = json.loads(row["pre_submit"] or "{}")
        try:
            snap = self.reader.account_snapshot()
        except (BrokerUnavailable, BrokerError):
            return "UNKNOWN"
        owned = {r["position_id"] for r in self.db.q("SELECT position_id FROM ownership")}
        engine_orders = {r["broker_order_id"] for r in self.db.q(
            "SELECT broker_order_id FROM intents WHERE broker_order_id IS NOT NULL")}
        tol = max(0.02, 0.01 * (row["amount"] or 0))
        if row["action"] == "OPEN":
            new_pos = [p for p in snap.positions if p.position_id not in pre.get("positions", {})
                       and p.position_id not in owned and p.instrument_id == row["instrument_id"]
                       and abs(p.amount - row["amount"]) <= tol]
            new_pend = [o for o in snap.pending_orders if o.order_id not in pre.get("pending", [])
                        and o.order_id not in engine_orders and o.instrument_id == row["instrument_id"]]
            if len(new_pos) == 1 and not new_pend:
                p = new_pos[0]
                from ..brokers.base import OrderStatus
                st = OrderStatus(order_id="", status_id=STATUS_FILLED, error_code=0, error_message="",
                                 position_ids=(p.position_id,), filled_amount=p.amount, filled_units=p.units,
                                 proceeds=None, rate=p.open_rate)
                self.apply_fill(row, st)
                transition(self.db, iid, "FILLED", self.now(), {"evidence": f"new position {p.position_id} matches "
                                                                            "the intent and was absent pre-submit"})
                return "FILLED"
            if len(new_pend) == 1 and not new_pos:
                o = new_pend[0]
                with self.db.tx():
                    self.db.con.execute("UPDATE intents SET broker_order_id=? WHERE intent_id=?", (o.order_id, iid))
                transition(self.db, iid, "PENDING", self.now(), {"evidence": f"new pending order {o.order_id}"})
                return self.poll(dict(self.db.one("SELECT * FROM intents WHERE intent_id=?", (iid,))))
            evidence = f"{len(new_pos)} matching new position(s), {len(new_pend)} new pending order(s)"
        else:
            pid = row["position_id"]
            before = pre.get("positions", {}).get(pid)
            now_units = {p.position_id: p.units for p in snap.positions}.get(pid)
            if before is not None and (now_units is None or now_units < before - 1e-9):
                info = self.reader.closed_position_proceeds(pid) if hasattr(self.reader, "closed_position_proceeds") else None
                from ..brokers.base import OrderStatus
                st = OrderStatus(order_id="", status_id=STATUS_FILLED, error_code=0, error_message="",
                                 position_ids=(), filled_amount=None, filled_units=None,
                                 proceeds=info.get("proceeds") if info else None, rate=None)
                self.apply_fill(row, st)
                transition(self.db, iid, "FILLED", self.now(), {"evidence": f"position {pid} units {before} -> "
                                                                            f"{now_units}", "proceeds_evidence": info})
                return "FILLED"
            evidence = f"position {pid} unchanged ({now_units} units)"
        transition(self.db, iid, "UNKNOWN", self.now(), {"evidence": evidence})
        # No evidence either way. The intent is never resubmitted; all new orders stop until the broker shows the
        # outcome (re-checked every cycle) or an operator records it after checking the broker by hand.
        halts.raise_halt(self.db, "UNKNOWN_ORDER", "ALL", f"intent {iid} outcome unknown: {evidence}; verify at "
                         "the broker, then `trading-engine orders resolve`", self.now())
        return "UNKNOWN"

    def resolve_all(self) -> dict:
        out = {}
        for r in self.db.q(f"SELECT * FROM intents WHERE status IN ({','.join('?' * len(UNRESOLVED))})",
                           tuple(UNRESOLVED)):
            out[r["intent_id"]] = self.resolve(dict(r))
        return out

    def abandon_stale(self, live_plan_ids: set) -> int:
        """Intents persisted but never sent, whose plan's window has passed: ABANDONED (never sent later)."""
        n = 0
        for r in self.db.q("SELECT intent_id, plan_id FROM intents WHERE status='INTENT_PERSISTED'"):
            if r["plan_id"] not in live_plan_ids:
                transition(self.db, r["intent_id"], "ABANDONED", self.now(), {"reason": "execution window passed"})
                n += 1
        return n


def operator_resolve(db, reader, intent_id: str, as_status: str, operator: str, evidence: str, now: str,
                     position_id: str | None = None) -> None:
    """Audited operator resolution of an UNKNOWN intent after checking the broker by hand."""
    row = db.one("SELECT * FROM intents WHERE intent_id=?", (intent_id,))
    if not row or row["status"] != "UNKNOWN":
        raise EngineError(f"{intent_id} is not UNKNOWN")
    if not operator.strip() or not evidence.strip():
        raise EngineError("--operator and --evidence are required")
    if as_status not in ("NOT_SUBMITTED", "REJECTED"):
        raise EngineError("an operator may only resolve an unknown intent as NOT_SUBMITTED or REJECTED; a fill "
                          "must be proven by broker evidence (the engine attributes it on the next cycle)")
    snap = reader.account_snapshot()
    pre = json.loads(row["pre_submit"] or "{}")
    if row["action"] == "OPEN":
        owned = {r["position_id"] for r in db.q("SELECT position_id FROM ownership")}
        cand = [p for p in snap.positions if p.position_id not in pre.get("positions", {}) and
                p.position_id not in owned and p.instrument_id == row["instrument_id"]]
        if cand:
            raise EngineError(f"refused: candidate position(s) {[p.position_id for p in cand]} exist at the broker")
    transition(db, intent_id, as_status, now, {"operator": operator, "evidence": evidence})
    db.audit("OPERATOR_RESOLVED_INTENT", {"intent_id": intent_id, "as": as_status, "operator": operator,
                                          "evidence": evidence}, ts=now)
