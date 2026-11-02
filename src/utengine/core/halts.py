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
    if open_exception(db, kind, subject or hid, "CRITICAL", reason, now, sleeve=sleeve, exception_id=hid) \
            and kind not in DEFERRED_NOTIFY:           # broker availability: notified by escalate(), see below
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
            if h["kind"] in DEFERRED_NOTIFY:
                _recovered(db, h["kind"], h["ts"], now)


# ----------------------------------------------------------------------------- notification policy (2026-11-02)
# SAFETY never waits: a failed or stale broker read raises its ALL-scope halt at once (raise_halt above) and nothing
# trades until a good read clears it. Only the NOTIFICATION of these self-clearing halts is deferred, so a transient
# failure that recovers by itself does not page the owner:
#   still unavailable after WARN_AFTER_S        -> one WARNING            (kind <KIND>_PERSISTENT)
#   still unavailable after CRIT_AFTER_S        -> one CRITICAL           (kind <KIND>_PERSISTENT)
#   >= FLAP_COUNT raises with gaps < FLAP_GAP_S  -> one WARNING per run    (kind <KIND>_INTERMITTENT)
#   recovery                                    -> one RECOVERED notice, only if an alert of that incident was
#                                                  DELIVERED; undelivered escalations are superseded, never sent late
# A missed or incomplete execution caused by broker unavailability is CRITICAL at once (engine.record_missed).
# Every other halt (unknown order outcome, unexpected positions, reconciliation mismatches, kill switch, loss limits,
# drift, ...) still alerts CRITICAL immediately. "Already sent" is read from the alerts table, so restarts and the
# health timer (which also escalates, so alerts continue if the engine stops cycling) never duplicate an alert.
DEFERRED_NOTIFY = {"BROKER_UNAVAILABLE", "BROKER_STATE_STALE"}
WARN_AFTER_S, CRIT_AFTER_S = 180, 300
FLAP_COUNT, FLAP_GAP_S = 3, 900
_LABEL = {"BROKER_UNAVAILABLE": "Broker reads failing", "BROKER_STATE_STALE": "Broker data stale"}


def _t(s: str):
    import datetime as dt
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _hm(s: str) -> str:
    return _t(s).strftime("%H:%M:%S UTC")


def _alerts_since(db, kind: str, since: str, delivered_only: bool = False) -> list:
    q = "SELECT * FROM alerts WHERE kind=? AND suppressed=0 AND ts>=?"
    if delivered_only:
        q += " AND delivered_at IS NOT NULL AND last_error IS NULL"
    return [dict(r) for r in db.q(q, (kind, since))]


def _last_alert(db, kind: str):
    r = db.one("SELECT * FROM alerts WHERE kind=? AND suppressed=0 ORDER BY id DESC LIMIT 1", (kind,))
    return dict(r) if r else None


def _raises(db, kind: str, since: str) -> list:
    """Timestamps of HALT_RAISED events of `kind` since `since` (the halts table keeps only the latest raise)."""
    import json as _json
    out = []
    for r in db.q("SELECT ts, payload FROM audit_log WHERE kind='HALT_RAISED' AND ts>=? ORDER BY seq", (since,)):
        p = _json.loads(r["payload"])
        if p.get("kind") == kind and not p.get("sleeve"):
            out.append(r["ts"])
    return out


def _flap_run(db, kind: str, now: str) -> list:
    """The current run of raises of `kind`: consecutive gaps < FLAP_GAP_S, the last one < FLAP_GAP_S ago."""
    import datetime as dt
    rs = _raises(db, kind, (_t(now) - dt.timedelta(days=1)).isoformat())
    if not rs or (_t(now) - _t(rs[-1])).total_seconds() >= FLAP_GAP_S:
        return []
    run = [rs[-1]]
    for ts in reversed(rs[:-1]):
        if (_t(run[0]) - _t(ts)).total_seconds() >= FLAP_GAP_S:
            break
        run.insert(0, ts)
    return run


def _recovered(db, kind: str, start: str, now: str) -> None:
    """Called when a deferred halt clears. RECOVERED only if this incident's escalation was DELIVERED; an escalation
    still waiting for delivery is superseded (it would only arrive after the problem is gone)."""
    pk = f"{kind}_PERSISTENT"
    delivered = _alerts_since(db, pk, start, delivered_only=True)
    db.x("UPDATE alerts SET delivered_at=?, last_error='superseded: recovered before delivery' "
         "WHERE kind=? AND ts>=? AND delivered_at IS NULL AND suppressed=0", (now, pk, start))
    if delivered and not _alerts_since(db, f"{kind}_RECOVERED", start):
        mins = (_t(now) - _t(start)).total_seconds() / 60
        db.alert("INFO", f"{kind}_RECOVERED", f"{_LABEL[kind]} since {_hm(start)}; recovered at {_hm(now)} "
                 f"after {mins:.0f} min. Trading may resume; nothing to do.", ts=now, dedupe_minutes=0)


def escalate(db, now: str) -> list:
    """Run every cycle and by the health timer. At most one alert per threshold per incident."""
    out = []
    for h in active(db):
        if h["kind"] not in DEFERRED_NOTIFY:
            continue
        kind, start = h["kind"], h["ts"]
        age = (_t(now) - _t(start)).total_seconds()
        pk = f"{kind}_PERSISTENT"
        sent = {a["severity"] for a in _alerts_since(db, pk, start)}
        if age >= CRIT_AFTER_S and "CRITICAL" not in sent:
            db.alert("CRITICAL", pk, f"{_LABEL[kind]} for {age / 60:.0f} min (since {_hm(start)}). All new orders "
                     f"are blocked until a good read; scheduled orders may be missed. Last error: {h['reason']}",
                     ts=now, dedupe_minutes=0)
            out.append(("CRITICAL", pk))
        elif WARN_AFTER_S <= age < CRIT_AFTER_S and not sent:
            db.alert("WARNING", pk, f"{_LABEL[kind]} for {age / 60:.0f} min (since {_hm(start)}). New orders are "
                     f"blocked; the engine retries every cycle and clears this itself. Last error: {h['reason']}",
                     ts=now, dedupe_minutes=0)
            out.append(("WARNING", pk))
    for kind in sorted(DEFERRED_NOTIFY):
        ik = f"{kind}_INTERMITTENT"
        run = _flap_run(db, kind, now)
        if len(run) >= FLAP_COUNT and not _alerts_since(db, ik, run[0]):
            db.alert("WARNING", ik, f"{_LABEL[kind]} intermittently: {len(run)} short failures since "
                     f"{_hm(run[0])}. Each one blocked trading until it recovered; the engine keeps retrying.",
                     ts=now, dedupe_minutes=0)
            out.append(("WARNING", ik))
        last = _last_alert(db, ik)
        if last and not run and not any(h["kind"] == kind for h in active(db)) \
                and last["delivered_at"] and last["last_error"] is None \
                and not _alerts_since(db, f"{ik}_RECOVERED", last["ts"]):
            db.alert("INFO", f"{ik}_RECOVERED", f"{_LABEL[kind]}: no failure for {FLAP_GAP_S // 60} min since the "
                     f"intermittent warning of {_hm(last['ts'])}. Nothing to do.", ts=now, dedupe_minutes=0)
            out.append(("INFO", f"{ik}_RECOVERED"))
    return out


def deferred_active_during(db, start: str, end: str) -> bool:
    """Was a broker-availability halt active at any moment in [start, end]? Reconstructed from the audit log."""
    import json as _json
    raised = {}
    for r in db.q("SELECT ts, kind, payload FROM audit_log WHERE kind IN ('HALT_RAISED','HALT_CLEARED') AND ts<=? "
                  "ORDER BY seq", (end,)):
        p = _json.loads(r["payload"])
        if p.get("kind") not in DEFERRED_NOTIFY or p.get("sleeve"):
            continue
        if r["kind"] == "HALT_RAISED":
            raised[p.get("halt_id")] = r["ts"]
        elif p.get("halt_id") in raised:
            raised.pop(p.get("halt_id"))
            if r["ts"] >= start:                       # cleared inside the window: it was active during it
                return True
    return bool(raised)                                # still active at the window's end
