"""Reconciliation: what actually happened at the broker, compared with what the engine knows.

Runs before every decision, before execution, after every execution phase and after every restart. Each run
reads ONE account snapshot and:
  1. bootstrap (first run): every existing position is PRE_EXISTING (never claimed, never closed, excluded from
     strategy capital); pre-existing pending orders halt; the available cash becomes INITIAL_CAPITAL;
  2. classifies every broker position: ENGINE (opened by an engine intent), PRE_EXISTING, EXTERNAL_ACK
     (operator-acknowledged foreign position, still excluded) or a NEW unknown position -> EXTERNAL_UNACK + halt;
  3. checks engine positions: missing at the broker without an engine close, or different units -> halt;
  4. checks pending orders: every one must belong to an engine intent -> otherwise halt;
  5. imports broker cash-transaction evidence (when the adapter has that capability);
  6. explains the cash change since the previous snapshot with unreconciled cash flows; an unexplained
     difference beyond cash_tolerance_usd creates a DISCREPANCY and halts (nothing is booked to balance it);
  7. records the snapshot and computes strategy equity = available cash + engine-owned positions.
Every issue carries a `subject` (discrepancy id, position id, order id, ...), so an issue that persists across
reconciliations stays ONE halt and ONE exception (one notification), never a daily repeat.
Stale state: a snapshot older than account.max_broker_state_age_seconds (default 300 s) halts (BROKER_STATE_STALE,
clears itself on the next fresh read). Copy-trading mirrors are refused (ACCOUNT_HAS_MIRRORS).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from . import halts
from .accounting import record_flow
from .canonical import canonical_json, short_id
from .errors import BrokerError, BrokerUnavailable
from .order_state import UNRESOLVED

TX_KIND = {"DEPOSIT", "WITHDRAWAL", "DEPOSIT_REVERSAL", "DIVIDEND", "FEE", "ADJUSTMENT"}


@dataclass
class ReconResult:
    status: str                          # CLEAN | HALT | UNAVAILABLE
    point: str
    snapshot: object = None
    snapshot_id: int | None = None
    issues: list = field(default_factory=list)
    strategy_equity: float | None = None
    account_equity: float | None = None
    engine_positions: list = field(default_factory=list)
    unexplained_cash: float = 0.0

    @property
    def clean(self) -> bool:
        return self.status == "CLEAN"


def _save_snapshot(db, snap, point) -> int:
    with db.tx():
        cur = db.con.execute(
            "INSERT INTO account_snapshots(ts,point,source,credit,pending_open,equity,positions,pending_orders,"
            "raw_sha256,provenance) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (snap.as_of, point, snap.source, snap.credit, snap.pending_open_amount, snap.equity,
             canonical_json([p.__dict__ for p in snap.positions]),
             canonical_json([o.__dict__ for o in snap.pending_orders]), snap.raw_sha256,
             canonical_json(snap.provenance)))
        return cur.lastrowid


def engine_position_ids(db) -> set:
    return {r["position_id"] for r in db.q("SELECT position_id FROM ownership WHERE owner='ENGINE' AND closed_at IS NULL")}


def reconcile(db, reader, cfg: dict, point: str, now: str) -> ReconResult:
    tol = cfg["account"]["cash_tolerance_usd"]
    try:
        snap = reader.account_snapshot()
    except (BrokerUnavailable, BrokerError) as exc:
        halts.raise_halt(db, "BROKER_UNAVAILABLE", "ALL", f"account read failed: {exc}", now, subject="broker-read")
        db.audit("RECONCILIATION", {"point": point, "status": "UNAVAILABLE", "error": str(exc)}, ts=now)
        return ReconResult("UNAVAILABLE", point, issues=[{"kind": "BROKER_UNAVAILABLE", "detail": str(exc)}])
    issues = []
    max_age = cfg["account"].get("max_broker_state_age_seconds") or 300
    try:
        age = (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(str(snap.as_of).replace("Z", "+00:00"))
               ).total_seconds()
    except (TypeError, ValueError):
        age = None
    if age is None or age > max_age:
        shown = "unparseable" if age is None else f"{age:.0f}s old"
        issues.append({"kind": "BROKER_STATE_STALE", "subject": "broker-state",
                       "detail": f"broker snapshot as_of {snap.as_of} is {shown} (limit {max_age}s)"})
    else:
        halts.auto_clear(db, {"BROKER_STATE_STALE"}, now)
    halts.auto_clear(db, {"BROKER_UNAVAILABLE"}, now)
    if getattr(snap, "mirrors", ()):
        issues.append({"kind": "ACCOUNT_HAS_MIRRORS", "subject": "mirrors",
                       "detail": f"{len(snap.mirrors)} copy-trading mirror(s) on the account; not supported"})
    prev = db.one("SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1")
    snap_id = _save_snapshot(db, snap, point)
    owned = {r["position_id"]: dict(r) for r in db.q("SELECT * FROM ownership")}

    if prev is None:                                                          # 1. bootstrap
        with db.tx():
            for p in snap.positions:
                db.con.execute("INSERT INTO ownership VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (p.position_id, p.instrument_id, p.symbol, "PRE_EXISTING", None, None, p.units,
                                p.amount, now, None, None, "found at bootstrap; excluded, never touched"))
            record_flow(db, "INITIAL_CAPITAL", snap.available_cash, source="bootstrap",
                        evidence=f"available cash at bootstrap, snapshot {snap.raw_sha256[:16]}",
                        ref=f"snap:{snap_id}", now=now, recon_snapshot_id=snap_id)
            db.put("bootstrap_at", snap.as_of)
            pre_tx = reader.cash_transactions("")       # everything already in the account is INITIAL_CAPITAL
            db.put("pre_bootstrap_tx", sorted(x.tx_id for x in pre_tx or []))
            db.audit("BOOTSTRAP", {"snapshot_id": snap_id, "credit": snap.credit,
                                   "available_cash": snap.available_cash,
                                   "pre_existing_positions": [p.position_id for p in snap.positions]}, ts=now)
        for o in snap.pending_orders:
            issues.append({"kind": "EXTERNAL_PENDING_ORDER", "subject": o.order_id,
                           "detail": f"pending order {o.order_id} existed at bootstrap"})
        owned = {r["position_id"]: dict(r) for r in db.q("SELECT * FROM ownership")}
    else:
        broker_ids = {p.position_id for p in snap.positions}
        for p in snap.positions:                                              # 2. classify
            row = owned.get(p.position_id)
            if row is None:
                with db.tx():
                    db.con.execute("INSERT INTO ownership VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (p.position_id, p.instrument_id, p.symbol, "EXTERNAL_UNACK", None, None,
                                    p.units, p.amount, now, None, None, "appeared without an engine intent"))
                # the cash it consumed is evidenced by the broker's own position amount (not a balancing entry);
                # it leaves the strategy's scope like a withdrawal, so strategy P&L is unaffected
                record_flow(db, "EXTERNAL_TRADE", -p.amount, source="broker-position",
                            evidence=f"foreign position {p.position_id} amount {p.amount}", ref=f"pos:{p.position_id}",
                            now=now)
                issues.append({"kind": "EXTERNAL_POSITION", "subject": p.position_id,
                               "detail": f"position {p.position_id} (instrument {p.instrument_id}, {p.amount:.2f} USD) "
                                         "was not opened by this engine"})
            elif row["owner"] == "ENGINE" and row["closed_at"] is None and abs(row["units"] - p.units) > 1e-6:
                issues.append({"kind": "POSITION_MISMATCH", "subject": p.position_id,
                               "detail": f"engine position {p.position_id}: engine {row['units']} units, broker {p.units}"})
            elif row["owner"] == "EXTERNAL_UNACK":
                issues.append({"kind": "EXTERNAL_POSITION", "subject": p.position_id,
                               "detail": f"position {p.position_id} still unacknowledged"})
            elif row["owner"] in ("PRE_EXISTING", "EXTERNAL_ACK") and abs(row["units"] - p.units) > 1e-6:
                issues.append({"kind": "EXTERNAL_CHANGE", "subject": p.position_id,
                               "detail": f"foreign position {p.position_id} changed units {row['units']} -> {p.units}"})
        for pid, row in owned.items():                                         # 3. missing positions
            if row["closed_at"] is None and pid not in broker_ids:
                if row["owner"] == "ENGINE":
                    issues.append({"kind": "POSITION_MISSING", "subject": pid,
                                   "detail": f"engine position {pid} is gone without an engine close"})
                else:
                    with db.tx():
                        db.con.execute("UPDATE ownership SET closed_at=?, note=COALESCE(note,'')||' | closed externally' "
                                       "WHERE position_id=?", (now, pid))
                    issues.append({"kind": "EXTERNAL_CHANGE", "subject": pid,
                                   "detail": f"foreign position {pid} was closed externally"})
    engine_orders = {r["broker_order_id"] for r in db.q("SELECT broker_order_id FROM intents WHERE broker_order_id IS NOT NULL")}
    if prev is not None:
        for o in snap.pending_orders:                                          # 4. pending orders
            if o.order_id not in engine_orders:
                issues.append({"kind": "EXTERNAL_PENDING_ORDER", "subject": o.order_id,
                               "detail": f"pending order {o.order_id} is not an engine order"})

    # 5. broker cash evidence: re-read a 1-day overlap (posting lag); rows are deduplicated by transaction id
    since = ((dt.datetime.fromisoformat(prev["ts"]) - dt.timedelta(days=1)).isoformat() if prev is not None
             else snap.as_of)
    txs = reader.cash_transactions(since) if prev is not None else []
    pre_boot = set(db.get("pre_bootstrap_tx", []))
    from . import funding
    for t in txs or []:
        if t.tx_id in pre_boot:
            continue                               # already inside INITIAL_CAPITAL
        kind = t.kind if t.kind in TX_KIND else "ADJUSTMENT"
        # a statement row for a credit that was already put in suspense explains THAT credit (booked against the
        # snapshot that saw it), not a new cash change -- the delayed-statement case
        linked = (funding.link_tx_to_suspense(db, t, now) if funding.automatic(cfg) and not db.one(
            "SELECT 1 FROM cash_flows WHERE ref=?", (f"tx:{t.tx_id}",)) else None)
        record_flow(db, kind, t.amount, source=f"broker:{reader.name}", evidence=t.evidence, ref=f"tx:{t.tx_id}",
                    now=now, recon_snapshot_id=linked)
    if prev is not None:
        funding.investigate(db, reader, cfg, now)  # older suspense vs transfer / funding evidence (idempotent)

    unexplained = 0.0                                                          # 6. cash explanation
    if prev is not None:
        flows = db.q("SELECT flow_id, amount FROM cash_flows WHERE recon_snapshot_id IS NULL")
        expected = sum(r["amount"] for r in flows)
        observed = snap.credit - prev["credit"]
        unexplained = round(observed - expected, 6)
        with db.tx():
            for r in flows:
                db.con.execute("UPDATE cash_flows SET recon_snapshot_id=? WHERE flow_id=?", (snap_id, r["flow_id"]))
            if abs(unexplained) > tol and funding.classify_change(db, reader, cfg, unexplained, snap_id, now):
                unexplained = 0.0                  # matched to authorized funding evidence and booked now
            elif unexplained > tol and funding.automatic(cfg):
                # positive and unexplained: held in SUSPENSE (excluded from all capital), investigated every run
                funding.hold_in_suspense(db, short_id("disc", snap_id, n=16), unexplained,
                                         {"snapshot_id": snap_id, "prev_snapshot_id": prev["id"],
                                          "observed_change": round(observed, 6), "explained": round(expected, 6),
                                          "cash_tx_capability": txs is not None,
                                          "cross_currency_evidence": funding.cross_currency_candidates(
                                              reader, unexplained, dt.datetime.fromisoformat(now))}, now)
            elif abs(unexplained) > tol:
                did = short_id("disc", snap_id, n=16)
                db.con.execute("INSERT OR IGNORE INTO discrepancies VALUES(?,?,?,?,?,?,?)",
                               (did, now, "CASH_UNEXPLAINED", unexplained,
                                canonical_json({"snapshot_id": snap_id, "prev_snapshot_id": prev["id"],
                                                "observed_change": round(observed, 6),
                                                "explained": round(expected, 6),
                                                "cash_tx_capability": txs is not None}), "OPEN", None))
                hint = ("; possible deposit, UNVERIFIED (the broker gave no deposit evidence; never auto-classified)"
                        if unexplained > 0 and txs is None else "")
                issues.append({"kind": "CASH_UNEXPLAINED", "subject": did,
                               "detail": f"broker cash changed {observed:+.2f}, explained {expected:+.2f}, "
                                         f"unexplained {unexplained:+.2f} (discrepancy {did}){hint}"})
    for d in db.q("SELECT disc_id, amount FROM discrepancies WHERE status='OPEN' AND kind<>'CASH_UNCLASSIFIED'"):
        if not any(d["disc_id"] in i["detail"] for i in issues):
            issues.append({"kind": "CASH_UNEXPLAINED", "subject": d["disc_id"],
                           "detail": f"discrepancy {d['disc_id']} ({d['amount']:+.2f}) is open"})
    unresolved = db.q(f"SELECT intent_id, status FROM intents WHERE status IN ({','.join('?' * len(UNRESOLVED))})",
                      tuple(UNRESOLVED))
    for r in unresolved:
        issues.append({"kind": "UNRESOLVED_ORDER", "subject": r["intent_id"],
                       "detail": f"intent {r['intent_id']} is {r['status']}"})
    open_ids = {r["intent_id"] for r in unresolved}
    for x in db.q("SELECT exception_id, subject FROM exceptions WHERE kind='UNRESOLVED_ORDER' AND status='OPEN'"):
        if x["subject"] not in open_ids:
            halts.resolve_exception(db, x["exception_id"], "engine (order final)", now)

    eng_ids = engine_position_ids(db)                                          # 7. equity
    eng_pos = [p for p in snap.positions if p.position_id in eng_ids]
    pos_value = sum(p.value for p in eng_pos) if all(p.unrealized_pnl is not None for p in eng_pos) else None
    strategy_equity = None if pos_value is None else round(snap.available_cash + pos_value, 6)
    if strategy_equity is not None and funding.automatic(cfg):
        strategy_equity = round(strategy_equity - funding.suspense_total(db), 6)   # suspense is nobody's capital

    for i in issues:
        kind = "CASH_DISCREPANCY" if i["kind"] == "CASH_UNEXPLAINED" else i["kind"]
        if i["kind"] == "UNRESOLVED_ORDER":
            # blocks orders through risk; resolution is the executor's job; one WARNING exception per intent
            new = halts.open_exception(db, kind, i["subject"], "WARNING", i["detail"], now)
            if new and not any(h["kind"] == "UNRESOLVED_ORDER" for h in halts.active(db)):
                db.alert("WARNING", kind, i["detail"], ts=now)     # the executor's halt already notified otherwise
            continue
        halts.raise_halt(db, kind, "ALL", i["detail"], now, subject=i.get("subject"))
    status = "CLEAN" if not issues else "HALT"
    db.audit("RECONCILIATION", {"point": point, "status": status, "snapshot_id": snap_id, "credit": snap.credit,
                                "available_cash": snap.available_cash, "strategy_equity": strategy_equity,
                                "unexplained_cash": unexplained, "issues": issues}, ts=now)
    return ReconResult(status, point, snap, snap_id, issues, strategy_equity, snap.equity, eng_pos, unexplained)


def acknowledge_external(db, position_id: str, operator: str, note: str, now: str) -> None:
    """EXTERNAL_UNACK -> EXTERNAL_ACK; or, for a PRE_EXISTING / EXTERNAL_ACK position that changed outside the
    engine (e.g. Gold Miner trimming its own position on a shared account), accept the broker's current units and
    amount as the new baseline (audit 2026-10-28 finding F3: such a change previously had no operator path and
    would halt for ever). The position stays foreign: excluded and never touched. Any cash effect is a separate
    discrepancy, classified with `account resolve --classify EXTERNAL_TRADE`."""
    row = db.one("SELECT * FROM ownership WHERE position_id=?", (position_id,))
    if not operator.strip() or not note.strip():
        raise BrokerError("--operator and --note are required")
    if row and row["owner"] == "EXTERNAL_UNACK":
        with db.tx():
            db.con.execute("UPDATE ownership SET owner='EXTERNAL_ACK', note=? WHERE position_id=?",
                           (f"acknowledged by {operator}: {note}", position_id))
            db.audit("EXTERNAL_ACKNOWLEDGED", {"position_id": position_id, "operator": operator, "note": note}, ts=now)
        return
    if row and row["owner"] in ("PRE_EXISTING", "EXTERNAL_ACK"):
        snap = last_snapshot(db)
        cur = next((p for p in (snap or {}).get("positions", []) if p["position_id"] == position_id), None)
        with db.tx():
            if cur is None:
                db.con.execute("UPDATE ownership SET closed_at=COALESCE(closed_at, ?) WHERE position_id=?", (now, position_id))
            else:
                db.con.execute("UPDATE ownership SET units=?, amount=? WHERE position_id=?",
                               (cur["units"], cur["amount"], position_id))
            db.audit("FOREIGN_CHANGE_ACKNOWLEDGED", {"position_id": position_id, "operator": operator, "note": note,
                                                     "old_units": row["units"], "new_units": cur and cur["units"]},
                     ts=now)
        return
    raise BrokerError(f"{position_id} is not a foreign (external or pre-existing) position")


def ownership_report(db) -> list:
    return [dict(r) for r in db.q("SELECT * FROM ownership ORDER BY first_seen")]


def last_snapshot(db):
    r = db.one("SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    d = dict(r)
    d["positions"] = json.loads(d["positions"])
    d["pending_orders"] = json.loads(d["pending_orders"])
    return d
