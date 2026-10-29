"""Halts and durable exceptions. A halt never liquidates anything: it only stops NEW orders.

scope ALL           integrity problems (unknown cash, external activity, unresolved orders, config drift,
                    broker unreadable, failed switch): no order of any kind is planned or submitted.
scope NEW_EXPOSURE  risk limits and the operator's emergency halt: no opening orders; risk-reducing closes only
                    if risk.halt_allows_reductions is true (default false: nothing trades, holdings stay).
sleeve              (multi-strategy) a halt may be confined to ONE sleeve: that sleeve holds (no decision, no
                    order) while the other sleeves continue. Account-level halts (sleeve NULL) stop every sleeve.
Only BROKER_UNAVAILABLE / BROKER_STATE_STALE clear themselves (on the next good read). Every other halt is cleared by
an operator command that records who cleared it and why (audit log).

Exceptions (docs/AUTOMATIC_RECONCILIATION_POLICY.md): every halt and every alert-worthy reconciliation issue is one
durable exception row keyed by (kind, subject, sleeve). The operator is notified when an exception OPENS (or
re-opens after it was resolved), never again while it stays open: re-detection only bumps `occurrences` and
`last_seen`. A subject is the thing the issue is about (a discrepancy id, a position id, an intent id), so the same
unresolved discrepancy reported by every later reconciliation is still ONE exception and ONE notification.
"""
from __future__ import annotations

from .canonical import short_id
from .errors import EngineError

AUTO_CLEAR = {"BROKER_UNAVAILABLE", "BROKER_STATE_STALE"}


def _hid(kind: str, reason: str, subject: str | None, sleeve: str | None) -> str:
    if subject is None and sleeve is None:
        return short_id(kind, reason, n=16)                     # identity used before migration 3, unchanged
    return short_id(kind, "subject", subject or reason, sleeve or "", n=16)


def open_exception(db, kind: str, subject: str, severity: str, message: str, now: str, *,
                   sleeve: str | None = None, exception_id: str | None = None) -> bool:
    """Upsert an exception. True when it is NEW or RE-OPENED (the caller notifies); False when it was already open
    (silently counted)."""
    from ..monitoring.notify import action_for
    eid = exception_id or short_id("exc", kind, subject, sleeve or "", n=16)
    with db.tx():
        row = db.one("SELECT status FROM exceptions WHERE exception_id=?", (eid,))
        if row and row["status"] == "OPEN":
            db.con.execute("UPDATE exceptions SET last_seen=?, occurrences=occurrences+1 WHERE exception_id=?",
                           (now, eid))
            return False
        if row:
            db.con.execute("UPDATE exceptions SET status='OPEN', last_seen=?, occurrences=occurrences+1, message=?, "
                           "notified_at=?, resolved_at=NULL, resolved_by=NULL WHERE exception_id=?",
                           (now, message, now, eid))
        else:
            db.con.execute("INSERT INTO exceptions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (eid, kind, subject, sleeve, severity, "OPEN", now, now, 1, message, action_for(kind), now,
                            None, None))
        db.audit("EXCEPTION_OPENED", {"exception_id": eid, "kind": kind, "subject": subject, "sleeve": sleeve,
                                      "severity": severity, "message": message}, ts=now)
    return True


def resolve_exception(db, exception_id: str, by: str, now: str) -> None:
    with db.tx():
        row = db.one("SELECT status FROM exceptions WHERE exception_id=?", (exception_id,))
        if not row or row["status"] != "OPEN":
            return
        db.con.execute("UPDATE exceptions SET status='RESOLVED', resolved_at=?, resolved_by=? WHERE exception_id=?",
                       (now, by, exception_id))
        db.audit("EXCEPTION_RESOLVED", {"exception_id": exception_id, "by": by}, ts=now)


def open_exceptions(db) -> list:
    return [dict(r) for r in db.q("SELECT * FROM exceptions WHERE status='OPEN' ORDER BY first_seen")]


def raise_halt(db, kind: str, scope: str, reason: str, now: str, *, subject: str | None = None,
               sleeve: str | None = None) -> str:
    hid = _hid(kind, reason, subject, sleeve)
    row = db.one("SELECT status FROM halts WHERE halt_id=?", (hid,))
    if row and row["status"] == "ACTIVE":
        open_exception(db, kind, subject or hid, "CRITICAL", reason, now, sleeve=sleeve, exception_id=hid)
        return hid
    with db.tx():
        db.con.execute("INSERT INTO halts(halt_id,ts,kind,scope,reason,status,cleared_at,cleared_by,subject,sleeve) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(halt_id) DO UPDATE SET "
                       "status='ACTIVE', ts=excluded.ts, reason=excluded.reason, cleared_at=NULL, cleared_by=NULL",
                       (hid, now, kind, scope, reason, "ACTIVE", None, None, subject, sleeve))
        db.audit("HALT_RAISED", {"halt_id": hid, "kind": kind, "scope": scope, "reason": reason, "subject": subject,
                                 "sleeve": sleeve}, ts=now)
    if open_exception(db, kind, subject or hid, "CRITICAL", reason, now, sleeve=sleeve, exception_id=hid):
        db.alert("CRITICAL", kind, reason if not sleeve else f"[sleeve {sleeve}] {reason}", ts=now)
    return hid


def active(db, scope: str | None = None) -> list:
    """ACCOUNT-level active halts (sleeve IS NULL): what every single-strategy caller means by 'halted'."""
    rows = db.q("SELECT * FROM halts WHERE status='ACTIVE' AND sleeve IS NULL ORDER BY ts")
    return [dict(r) for r in rows if scope is None or r["scope"] == scope]


def active_sleeve(db, sleeve: str | None = None) -> list:
    rows = db.q("SELECT * FROM halts WHERE status='ACTIVE' AND sleeve IS NOT NULL ORDER BY ts")
    return [dict(r) for r in rows if sleeve is None or r["sleeve"] == sleeve]


def clear(db, halt_id: str, operator: str, reason: str, now: str, automatic: bool = False) -> None:
    row = db.one("SELECT * FROM halts WHERE halt_id=?", (halt_id,))
    if not row or row["status"] != "ACTIVE":
        raise EngineError(f"no active halt {halt_id}")
    if not automatic and not operator.strip():
        raise EngineError("clearing a halt requires --operator")
    with db.tx():
        db.con.execute("UPDATE halts SET status='CLEARED', cleared_at=?, cleared_by=? WHERE halt_id=?",
                       (now, operator, halt_id))
        db.audit("HALT_CLEARED", {"halt_id": halt_id, "kind": row["kind"], "operator": operator, "reason": reason,
                                  "automatic": automatic, "sleeve": row["sleeve"]}, ts=now)
        resolve_exception(db, halt_id, operator, now)


def auto_clear(db, kinds: set, now: str) -> None:
    for h in active(db):
        if h["kind"] in kinds and h["kind"] in AUTO_CLEAR:
            clear(db, h["halt_id"], "engine", "condition no longer present", now, automatic=True)
