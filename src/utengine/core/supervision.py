"""SUPERVISED pilot execution (pilot release only; docs/LIVE_PILOT_DESIGN.md).

In `supervised_simulation` and `supervised_live` modes every order intent needs a HUMAN APPROVAL bound to that exact
intent before the executor may send it, and every send is re-checked against hard pilot limits AT SUBMIT TIME:

  hold   (the intent stays INTENT_PERSISTED, re-checked every cycle, abandoned when the plan window ends; never sent
          late): no approval yet, approval expired, quote stale or too wide now, session order count reached
  block  (the intent becomes BLOCKED; no approval can override it): kill switch, order above max_order_usd, gross
          exposure above max_gross_usd, daily loss stop (opens), cumulative loss stop (raises a halt of ALL)

An approval is used once: it is consumed at the SUBMITTING transition, so a restart can never resend it (the
executor only queries an intent that left INTENT_PERSISTED; it never resubmits). The limits are OPERATIONAL limits,
not loss guarantees: they are evaluated when an order would be sent and close nothing.
"""
from __future__ import annotations

import datetime as dt
import pathlib

from . import halts
from .canonical import canonical_json, sha256_text
from .errors import EngineError

SUPERVISED_MODES = ("supervised_simulation", "supervised_live")
HOLD, BLOCK = "hold", "block"


def is_supervised(mode: str) -> bool:
    return mode in SUPERVISED_MODES


def guard_active(mode: str, cfg: dict) -> bool:
    """The execution guard (hard limits, send-time quote and spread checks, kill switch) runs in every live mode and
    in simulation whenever live_controls are configured (so simulation rehearses exactly the autonomous path)."""
    return mode in SUPERVISED_MODES or mode == "live" or (mode == "simulation" and bool(cfg.get("live_controls")))


def intent_digest(row: dict) -> str:
    """The approval is bound to WHAT will be sent: action, instrument, amount / units, position and plan."""
    return sha256_text(canonical_json({k: row.get(k) for k in ("intent_id", "plan_id", "action", "instrument_id",
                                                                "symbol", "amount", "units", "position_id")}))


def kill_switch_paths(cfg: dict, home) -> list:
    p = cfg["live_controls"]
    return [pathlib.Path(home) / "KILL_SWITCH"] + ([pathlib.Path(p["kill_switch_file"])] if p.get("kill_switch_file")
                                                    else [])


def kill_switch_engaged(cfg: dict, home) -> str | None:
    for p in kill_switch_paths(cfg, home):
        if p.exists():
            return str(p)
    return None


def engage_kill_switch(cfg: dict, home, operator: str, reason: str, now: str, db) -> str:
    if not operator.strip() or not reason.strip():
        raise EngineError("--operator and --reason are required")
    p = kill_switch_paths(cfg, home)[0]
    p.write_text(f"{now} {operator}: {reason}\n", encoding="utf-8")
    db.audit("KILL_SWITCH_ENGAGED", {"path": str(p), "operator": operator, "reason": reason}, ts=now)
    halts.raise_halt(db, "KILL_SWITCH", "ALL", f"kill switch engaged by {operator}: {reason}", now)
    return str(p)


# ----------------------------------------------------------------------------------------------- approvals
def pending(db) -> list:
    out = []
    for r in db.q("SELECT i.*, p.status AS plan_status FROM intents i JOIN plans p ON p.plan_id=i.plan_id "
                  "WHERE i.status='INTENT_PERSISTED' ORDER BY i.created_at, i.leg_index"):
        a = db.get(f"approval:{r['intent_id']}")
        out.append({"intent_id": r["intent_id"], "plan_id": r["plan_id"], "plan_status": r["plan_status"],
                    "phase": r["phase"], "action": r["action"], "symbol": r["symbol"],
                    "instrument_id": r["instrument_id"], "amount_usd": r["amount"], "units": r["units"],
                    "position_id": r["position_id"], "created_at": r["created_at"],
                    "approval": None if not a else {k: a[k] for k in ("approved_by", "expires_at", "consumed_at")
                                                    if k in a}})
    return out


def approve(db, cfg: dict, intent_id: str, approved_by: str, now: dt.datetime, ttl_minutes: float | None = None) -> dict:
    if not approved_by.strip():
        raise EngineError("--approved-by is required")
    row = db.one("SELECT * FROM intents WHERE intent_id=?", (intent_id,))
    if row is None or row["status"] != "INTENT_PERSISTED":
        raise EngineError(f"intent {intent_id} is not waiting for approval")
    row = dict(row)
    p = cfg["live_controls"]
    if row["action"] == "OPEN" and (row["amount"] or 0) > p["max_order_usd"] + 1e-9:
        raise EngineError(f"amount {row['amount']:.2f} exceeds pilot.max_order_usd {p['max_order_usd']}: it can "
                          "never be sent; it will be BLOCKED at submit time")
    ttl = min(float(ttl_minutes or p.get("approval_ttl_minutes", 15.0)), float(p.get("approval_ttl_minutes", 15.0)))
    rec = {"intent_id": intent_id, "digest": intent_digest(row), "approved_by": approved_by,
           "approved_at": now.isoformat(), "expires_at": (now + dt.timedelta(minutes=ttl)).isoformat(),
           "summary": {k: row[k] for k in ("action", "symbol", "amount", "units", "position_id")}}
    db.put(f"approval:{intent_id}", rec)
    db.audit("ORDER_APPROVED", rec, ts=now.isoformat())
    return rec


def reject(db, intent_id: str, operator: str, reason: str, now: str) -> None:
    from .order_state import transition
    if not operator.strip() or not reason.strip():
        raise EngineError("--operator and --reason are required")
    row = db.one("SELECT status FROM intents WHERE intent_id=?", (intent_id,))
    if row is None or row["status"] != "INTENT_PERSISTED":
        raise EngineError(f"intent {intent_id} is not waiting for approval")
    transition(db, intent_id, "BLOCKED", now, {"reason": f"rejected by {operator}: {reason}"})
    db.audit("ORDER_REJECTED_BY_OPERATOR", {"intent_id": intent_id, "operator": operator, "reason": reason}, ts=now)


def consume(db, intent_id: str, now: str) -> None:
    a = db.get(f"approval:{intent_id}")
    if a and not a.get("consumed_at"):
        db.put(f"approval:{intent_id}", {**a, "consumed_at": now})


# ----------------------------------------------------------------------------------------------- the submit gate
def _engine_exposure(db, snap) -> float:
    owned = {r["position_id"] for r in db.q("SELECT position_id FROM ownership WHERE owner='ENGINE' AND closed_at "
                                             "IS NULL")}
    return round(sum(p.amount + (p.unrealized_pnl or 0.0) for p in snap.positions if p.position_id in owned)
                 + snap.pending_open_amount, 6)


def _strategy_equity(db, snap) -> float:
    owned = {r["position_id"] for r in db.q("SELECT position_id FROM ownership WHERE owner='ENGINE'")}
    from .funding import suspense_total
    return round(snap.credit + sum(p.amount + (p.unrealized_pnl or 0.0) for p in snap.positions
                                   if p.position_id in owned) - suspense_total(db), 6)


def _external_flows_since(db, since: str) -> float:
    return float(db.one("SELECT COALESCE(SUM(amount),0) s FROM cash_flows WHERE external=1 AND ts>?", (since,))["s"])


def _submitted_in_session(db, session: str) -> int:
    return int(db.one("SELECT COUNT(DISTINCT intent_id) c FROM order_events WHERE to_status='SUBMITTING' AND "
                      "substr(ts,1,10)=?", (session,))["c"])


def check(db, cfg: dict, home, row: dict, snap, quotes_fn, now: dt.datetime) -> tuple:
    """(None, None) = may be sent now; (HOLD, reason) or (BLOCK, reason)."""
    p, r = cfg["live_controls"], cfg["risk"]
    nows = now.isoformat()
    ks = kill_switch_engaged(cfg, home)
    if ks:
        return BLOCK, f"kill switch engaged ({ks})"
    if is_supervised(cfg["runtime"]["mode"]):            # per-order approval: supervised modes only
        a = db.get(f"approval:{row['intent_id']}")
        if not a:
            return HOLD, "awaiting human approval"
        if a.get("consumed_at"):
            return BLOCK, "approval already used (an intent is never sent twice)"
        if a["digest"] != intent_digest(row):
            return BLOCK, "approval does not match this intent"
        if dt.datetime.fromisoformat(a["expires_at"]) <= now:
            return HOLD, "approval expired: approve again"
    session = now.date().isoformat()
    if _submitted_in_session(db, session) >= p["max_orders_per_session"]:
        return HOLD, f"pilot.max_orders_per_session {p['max_orders_per_session']} reached"
    equity = _strategy_equity(db, snap)
    base = db.get("pilot:baseline")
    if base is None:
        base = {"equity": equity, "at": nows}
        db.put("pilot:baseline", base)
        db.audit("PILOT_BASELINE", base, ts=nows)
    cum = equity - base["equity"] - _external_flows_since(db, base["at"])
    if cum <= -p["cumulative_loss_stop_usd"]:
        halts.raise_halt(db, "PILOT_LOSS_STOP", "ALL", f"pilot cumulative P&L {cum:+.2f} <= "
                         f"-{p['cumulative_loss_stop_usd']} USD", nows)
        return BLOCK, f"pilot cumulative loss stop ({cum:+.2f} USD)"
    if row["action"] == "OPEN":
        amt = float(row["amount"] or 0.0)
        if amt > p["max_order_usd"] + 1e-9:
            return BLOCK, f"amount {amt:.2f} > pilot.max_order_usd {p['max_order_usd']}"
        expo = _engine_exposure(db, snap)
        if expo + amt > p["max_gross_usd"] + 1e-6:
            return BLOCK, f"exposure {expo:.2f} + {amt:.2f} > pilot.max_gross_usd {p['max_gross_usd']}"
        day = db.get(f"pilot:day_start:{session}")
        if day is None:
            prev = db.one("SELECT strategy_equity FROM daily_equity WHERE session < ? ORDER BY session DESC LIMIT 1",
                          (session,))
            day = {"equity": float(prev["strategy_equity"]) if prev else equity, "at": nows}
            db.put(f"pilot:day_start:{session}", day)
        dl = equity - day["equity"] - _external_flows_since(db, day["at"])
        if dl <= -p["daily_loss_stop_usd"]:
            return BLOCK, f"pilot daily loss stop ({dl:+.2f} USD): no new exposure today"
        q = (quotes_fn([row["instrument_id"]]) or {}).get(int(row["instrument_id"]))
        if q is None or not (q.bid > 0 and q.ask >= q.bid):
            return HOLD, "no valid broker quote now"
        age = (now - dt.datetime.fromisoformat(str(q.quote_time).replace("Z", "+00:00"))).total_seconds()
        if age > r["max_quote_age_seconds"]:
            return HOLD, f"quote {age:.0f}s old > {r['max_quote_age_seconds']}s"
        bp = (q.ask - q.bid) / ((q.ask + q.bid) / 2) * 1e4
        if r.get("max_spread_bp") is not None and bp > r["max_spread_bp"]:
            return HOLD, f"spread {bp:.1f} bp > max_spread_bp {r['max_spread_bp']}"
    return None, None
