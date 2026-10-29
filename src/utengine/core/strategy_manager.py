"""Active strategy, configuration drift and operator-approved strategy switching.

The active strategy lives in the DATABASE (engine_state.active_strategy), never in YAML. engine.yaml's
runtime.initial_strategy is used exactly once, at the first start of an empty database. Editing it afterwards
changes nothing (status reports the difference).

Activation pins the strategy YAML fingerprint and the module source hash. If either changes while the strategy
is active, the engine raises CONFIG_DRIFT (halt ALL) and computes no further decisions until an operator
re-activates through the switch workflow (which records the new fingerprints with an approval).

Switch workflow (docs/STRATEGY_SWITCHING.md): preview-switch builds a transition plan from a CLEAN reconciliation
and stores it under its hash; activate requires that exact hash, an approver name, a preview younger than
PREVIEW_TTL, and an unchanged broker state; then it persists APPROVED, executes the transition through the normal
risk/execution path, reconciles, and records COMPLETED, ROLLED_BACK (nothing executed) or FAILED_PARTIAL (halt).
"""
from __future__ import annotations

import datetime as dt
import json

from . import halts
from .canonical import canonical_json, fingerprint
from .errors import SwitchError
from .order_planner import plan as make_legs
from .order_state import UNRESOLVED
from .portfolio import allocate
from ..strategy_modules import REGISTRY

PREVIEW_TTL = dt.timedelta(minutes=15)


def active(db) -> dict | None:
    return db.get("active_strategy")


def _record(spec: dict) -> dict:
    return {"strategy_id": spec["id"], "fingerprint": spec["fingerprint"],
            "module_sha256": REGISTRY[spec["implementation"]].source_sha256, "version": spec["version"]}


def bootstrap(db, cfg: dict, specs: dict, now: str) -> dict:
    cur = active(db)
    if cur:
        return cur
    sid = cfg["runtime"]["initial_strategy"]
    spec = specs.get(sid)
    if spec is None or not spec["enabled"]:
        raise SwitchError(f"initial_strategy {sid} is missing or disabled")
    if REGISTRY[spec["implementation"]].status != "AVAILABLE":
        raise SwitchError(f"initial_strategy {sid}: implementation unavailable")
    rec = {**_record(spec), "activated_at": now, "approved_by": "engine.yaml runtime.initial_strategy (first start)",
           "via": "bootstrap"}
    db.put("active_strategy", rec)
    db.audit("STRATEGY_ACTIVATED", rec, ts=now)
    return rec


def drift(db, specs: dict) -> list:
    cur = active(db)
    if not cur:
        return ["no active strategy"]
    spec = specs.get(cur["strategy_id"])
    if spec is None:
        return [f"active strategy {cur['strategy_id']} has no YAML"]
    out = []
    if spec["fingerprint"] != cur["fingerprint"]:
        out.append(f"{cur['strategy_id']} YAML fingerprint {spec['fingerprint'][:19]} != activated {cur['fingerprint'][:19]}")
    msha = REGISTRY[spec["implementation"]].source_sha256
    if msha != cur["module_sha256"]:
        out.append(f"{cur['strategy_id']} module source {msha[:12]} != activated {cur['module_sha256'][:12]}")
    if not spec["enabled"]:
        out.append(f"{cur['strategy_id']} is now disabled in its YAML")
    return out


def list_strategies(db, specs: dict) -> list:
    cur = active(db) or {}
    out = []
    for sid, s in sorted(specs.items()):
        e = REGISTRY[s["implementation"]]
        out.append({"id": sid, "implementation": s["implementation"], "version": s["version"],
                    "enabled": s["enabled"], "live_eligible": s["live_eligible"], "module_status": e.status,
                    "active": sid == cur.get("strategy_id"), "fingerprint": s["fingerprint"][:23],
                    "file": s["_file_name"]})
    return out


class Switcher:
    def __init__(self, engine):
        self.e = engine

    def preview(self, to_id: str, now: dt.datetime) -> dict:
        e = self.e
        if getattr(e, "multi", False):
            raise SwitchError("portfolio.mode is multi_strategy: sleeves are changed with `portfolio "
                              "reallocate-preview` / `reallocate`, not by a strategy switch")
        nows = now.astimezone(dt.timezone.utc).isoformat()
        cur = active(e.db)
        spec = e.specs.get(to_id)
        problems = []
        if spec is None:
            raise SwitchError(f"unknown strategy {to_id}")
        if cur and cur["strategy_id"] == to_id:
            raise SwitchError(f"{to_id} is already active")
        if not spec["enabled"]:
            problems.append("target strategy is disabled")
        if REGISTRY[spec["implementation"]].status != "AVAILABLE":
            problems.append(f"implementation {spec['implementation']} is unavailable")
        unresolved = e.db.q(f"SELECT intent_id FROM intents WHERE status IN ({','.join('?' * len(UNRESOLVED))})",
                            tuple(UNRESOLVED))
        if unresolved:
            problems.append(f"{len(unresolved)} unresolved order intent(s)")
        if halts.active(e.db):
            problems.append("active halt(s): " + ", ".join(h["kind"] for h in halts.active(e.db)))
        if problems:
            raise SwitchError("preview refused: " + "; ".join(problems))
        r = e.reconcile("switch_preview", now)
        if not r.clean:
            raise SwitchError("preview refused: reconciliation not clean: " + "; ".join(i["kind"] for i in r.issues))
        session = e.cal.last_completed_session(now)
        module, decision = e.compute_decision(spec, session, now, persist=False)
        alloc = allocate(decision, spec, r.strategy_equity)
        positions = e.engine_positions(r)
        quotes = e.quotes_for(positions, alloc)
        legs, notes = make_legs(alloc, positions, quotes, spec["rebalancing"], e.cfg["risk"]["min_order_usd"])
        cost = 0.0
        for leg in legs:
            q = quotes.get(leg.instrument_id)
            if q and q.ask > 0 and leg.amount:
                cost += leg.amount * (q.ask - q.bid) / ((q.ask + q.bid) / 2) / 2
        state_hash = fingerprint({"credit": r.snapshot.credit, "positions": sorted((p.position_id, p.units) for p in r.snapshot.positions)})
        body = {"from": cur["strategy_id"] if cur else None, "to": to_id, "to_fingerprint": spec["fingerprint"],
                "to_module_sha256": REGISTRY[spec["implementation"]].source_sha256, "decision": decision.to_dict(),
                "allocation": alloc.to_dict(), "legs": [l.to_dict() for l in legs], "notes": notes,
                "estimated_spread_cost_usd": round(cost, 2), "strategy_equity": r.strategy_equity,
                "broker_state_hash": state_hash, "created_at": nows, "mode": e.mode,
                "risk_preview": [f"{l.action} {l.symbol} {l.amount}" for l in legs]}
        ph = fingerprint(body)[7:23]
        with e.db.tx():
            e.db.con.execute("INSERT OR REPLACE INTO switch_plans VALUES(?,?,?,?,?,?,?,?)",
                             (ph, body["from"] or "", to_id, nows, "PREVIEWED", canonical_json(body), None, None))
            e.db.audit("SWITCH_PREVIEWED", {"plan_hash": ph, "from": body["from"], "to": to_id,
                                            "legs": body["risk_preview"], "cost": body["estimated_spread_cost_usd"]},
                       ts=nows)
        return {"plan_hash": ph, **body}

    def activate(self, to_id: str, plan_hash: str, approved_by: str, now: dt.datetime) -> dict:
        e = self.e
        nows = now.astimezone(dt.timezone.utc).isoformat()
        if not approved_by.strip():
            raise SwitchError("--approved-by is required")
        row = e.db.one("SELECT * FROM switch_plans WHERE plan_hash=?", (plan_hash,))
        if not row or row["status"] != "PREVIEWED" or row["to_strategy"] != to_id:
            raise SwitchError(f"no PREVIEWED switch plan {plan_hash} to {to_id}")
        body = json.loads(row["plan"])
        if now - dt.datetime.fromisoformat(body["created_at"]) > PREVIEW_TTL:
            raise SwitchError("preview expired; run preview-switch again")
        spec = e.specs[to_id]
        if spec["fingerprint"] != body["to_fingerprint"]:
            raise SwitchError("target YAML changed since the preview")
        r = e.reconcile("switch_before_execution", now)
        if not r.clean:
            raise SwitchError("reconciliation not clean: " + "; ".join(i["kind"] for i in r.issues))
        state_hash = fingerprint({"credit": r.snapshot.credit, "positions": sorted((p.position_id, p.units) for p in r.snapshot.positions)})
        if state_hash != body["broker_state_hash"]:
            raise SwitchError("broker state changed since the preview; run preview-switch again")
        old = active(e.db)
        with e.db.tx():
            e.db.con.execute("UPDATE switch_plans SET status='APPROVED', approved_by=? WHERE plan_hash=?",
                             (approved_by, plan_hash))
            e.db.audit("SWITCH_APPROVED", {"plan_hash": plan_hash, "from": body["from"], "to": to_id,
                                           "approved_by": approved_by}, ts=nows)
        from .order_planner import Leg
        legs = [Leg(**l) for l in body["legs"]]
        try:
            result = e.execute_legs("S" + plan_hash, legs, spec, body, now, plan_kind="switch")
            failed = result.get("status") not in ("DONE", "SHADOW", "NO_LEGS")
        except Exception as exc:  # noqa: BLE001 -- any failure is a failed switch, reported, never swallowed
            result, failed = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}, True
        executed = e.db.q("SELECT DISTINCT i.intent_id FROM intents i JOIN order_events v ON v.intent_id=i.intent_id "
                          "WHERE i.plan_id=? AND v.to_status IN ('FILLED','PARTIALLY_FILLED')", ("S" + plan_hash,))
        rejected = e.db.q("SELECT intent_id FROM intents WHERE plan_id=? AND status IN ('REJECTED','BLOCKED',"
                          "'UNKNOWN','ABANDONED')", ("S" + plan_hash,))
        if rejected:
            failed = True
        if not failed:
            rec = {**_record(spec), "activated_at": nows, "approved_by": approved_by, "via": f"switch {plan_hash}"}
            with e.db.tx():
                e.db.put("active_strategy", rec)
                e.db.con.execute("UPDATE ownership SET strategy_id=? WHERE owner='ENGINE' AND closed_at IS NULL", (to_id,))
                e.db.con.execute("UPDATE switch_plans SET status='COMPLETED', finished_at=? WHERE plan_hash=?",
                                 (nows, plan_hash))
                e.db.audit("STRATEGY_ACTIVATED", {**rec, "previous": old}, ts=nows)
            return {"status": "COMPLETED", "active": to_id, "result": result}
        status = "ROLLED_BACK" if not executed else "FAILED_PARTIAL"
        with e.db.tx():
            e.db.con.execute("UPDATE switch_plans SET status=?, finished_at=? WHERE plan_hash=?", (status, nows, plan_hash))
            e.db.audit("SWITCH_" + status, {"plan_hash": plan_hash, "active_remains": old["strategy_id"] if old else None,
                                            "executed_intents": [r["intent_id"] for r in executed],
                                            "result": result}, ts=nows)
        if status == "FAILED_PARTIAL":
            halts.raise_halt(e.db, "SWITCH_FAILED", "ALL", f"switch {plan_hash} to {to_id} partly executed; "
                             f"{old['strategy_id'] if old else '-'} remains active; operator review required", nows)
        return {"status": status, "active": old["strategy_id"] if old else None, "result": result}
