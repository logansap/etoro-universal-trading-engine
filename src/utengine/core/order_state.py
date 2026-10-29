"""Order-intent state machine. Every transition is persisted (intents row + order_events + audit) BEFORE the
next broker action; illegal transitions raise IllegalTransition.

  INTENT_PERSISTED --> SUBMITTING --> ACKNOWLEDGED --> PENDING / PARTIALLY_FILLED --> FILLED
         |                 |                |                                    \\-> REJECTED / CANCELED
         |                 |                \\--> FILLED / REJECTED / UNKNOWN
         |                 \\--> REJECTED (definite broker error) / UNKNOWN (timeout, connection loss, crash)
         \\--> ABANDONED (window passed, never sent) / BLOCKED (risk) / SHADOW_RECORDED (shadow: never sent)
  UNKNOWN --> resolved ONLY by evidence from broker reads (or an audited operator resolution):
              FILLED / PARTIALLY_FILLED / PENDING / REJECTED / NOT_SUBMITTED; otherwise it stays UNKNOWN and the
              engine halts. An UNKNOWN intent is NEVER resubmitted.
  FILLED / REJECTED / CANCELED / NOT_SUBMITTED --> RECONCILED (after the post-execution reconciliation).
"""
from __future__ import annotations

from .canonical import canonical_json
from .errors import IllegalTransition

TRANSITIONS = {
    "INTENT_PERSISTED": {"SUBMITTING", "ABANDONED", "BLOCKED", "SHADOW_RECORDED"},
    "SUBMITTING": {"ACKNOWLEDGED", "REJECTED", "UNKNOWN"},
    "ACKNOWLEDGED": {"PENDING", "PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCELED", "UNKNOWN"},
    "PENDING": {"PENDING", "PARTIALLY_FILLED", "FILLED", "REJECTED", "CANCELED", "UNKNOWN"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELED", "UNKNOWN"},
    "UNKNOWN": {"UNKNOWN", "FILLED", "PARTIALLY_FILLED", "PENDING", "REJECTED", "CANCELED", "NOT_SUBMITTED"},
    "FILLED": {"RECONCILED"},
    "REJECTED": {"RECONCILED"},
    "CANCELED": {"RECONCILED"},
    "NOT_SUBMITTED": {"RECONCILED"},
}
TERMINAL = {"RECONCILED", "ABANDONED", "BLOCKED", "SHADOW_RECORDED"}
DONE = {"FILLED", "REJECTED", "CANCELED", "NOT_SUBMITTED"} | TERMINAL
UNRESOLVED = {"SUBMITTING", "ACKNOWLEDGED", "PENDING", "PARTIALLY_FILLED", "UNKNOWN"}


def transition(db, intent_id: str, to: str, now: str, detail: dict | None = None, **fields) -> None:
    with db.tx():
        row = db.one("SELECT status FROM intents WHERE intent_id=?", (intent_id,))
        if row is None:
            raise IllegalTransition(f"unknown intent {intent_id}")
        frm = row["status"]
        if to not in TRANSITIONS.get(frm, set()):
            raise IllegalTransition(f"{intent_id}: {frm} -> {to} is not allowed")
        sets, args = ["status=?", "updated_at=?"], [to, now]
        for k, v in fields.items():
            if k not in ("broker_order_id", "pre_submit", "result", "amount", "units"):
                raise IllegalTransition(f"field {k} cannot be set by a transition")
            sets.append(f"{k}=?")
            args.append(canonical_json(v) if isinstance(v, (dict, list)) else v)
        db.con.execute(f"UPDATE intents SET {', '.join(sets)} WHERE intent_id=?", (*args, intent_id))
        db.con.execute("INSERT INTO order_events(intent_id,ts,from_status,to_status,detail) VALUES(?,?,?,?,?)",
                       (intent_id, now, frm, to, canonical_json(detail or {})))
        db.audit("ORDER_TRANSITION", {"intent_id": intent_id, "from": frm, "to": to, "detail": detail or {}}, ts=now)
