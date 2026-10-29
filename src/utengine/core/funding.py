"""Full-account capital ownership and automatic, evidence-backed funding discovery (docs/CAPITAL_AND_FUNDING.md).

Enabled by the engine.yaml `capital` section (absent = the previous behaviour, unchanged):

    capital:
      ownership: full_designated_account      # the engine owns 100% of the designated trading account
      sizing_source: verified_broker_equity   # sizing only from broker-reported cash + broker-valued positions
      manual_capital_amount: null             # there is no manual budget; a number is refused
      funding_policy: automatic_evidence_backed
      external_flow_allocation: proportional  # verified net new capital split by the sleeve weights
      preserve_strategy_cash: true            # a deposit never redistributes existing sleeve profits or cash

What happens to a change in the broker's cash that the engine's own trades do not explain:
  1. evidence first: a broker cash-transaction record (`cash_transactions`) or a funding record (`funding_evidence`:
     on eToro, an eToro-Money cash-account internalTransfer to/from the trading account) with the same amount ->
     a DEPOSIT / WITHDRAWAL flow with the broker reference (idempotent by reference), allocated by weight;
  2. a POSITIVE amount without evidence -> SUSPENSE (discrepancy kind CASH_UNCLASSIFIED): kept out of every sleeve
     and out of strategy equity, so nobody can spend it; ONE warning exception; trading continues on the explained
     capital; every reconciliation re-investigates and classifies it automatically when evidence arrives (a delayed
     statement or transfer record) -- never by guessing;
  3. a NEGATIVE amount without evidence -> CASH_DISCREPANCY halt (as before): cash the sleeves believe they own
     has left the account, so nothing may be sized until it is explained.
Currency: only evidence in the trading account's currency (USD) can classify anything. An eToro-Money transfer in
EUR (e.g. EUR 1,000.00 out of eToro Money, USD 1,150.00 into trading) has no recorded conversion rate in the
API, so it is never matched by amount; it is cited on the suspense item (`cross_currency_evidence`, with the rate it
would imply) for the operator. There is no manual suspense classification command and no FX-tolerance rule yet:
either is an owner policy decision (docs/CAPITAL_AND_FUNDING.md).
Sale proceeds are never funding: they come only from the close order (TRADE_CLOSE) or an audited TRADE_CLOSE
classification; automatic funding classification is suspended while any close has unknown proceeds.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json

from . import halts
from .accounting import record_flow
from .canonical import canonical_json

SUSPENSE = "CASH_UNCLASSIFIED"
MATCH_TOL = 0.01
BASE_CURRENCY = "USD"            # configuration.py allows only base_currency USD
WINDOW_BEFORE = dt.timedelta(days=7)
WINDOW_AFTER = dt.timedelta(days=5)


def settings(cfg: dict) -> dict | None:
    return cfg.get("capital")


def automatic(cfg: dict) -> bool:
    c = settings(cfg)
    return bool(c and c["funding_policy"] == "automatic_evidence_backed")


def suspense_total(db) -> float:
    return round(db.one("SELECT COALESCE(SUM(amount),0) s FROM discrepancies WHERE kind=? AND status='OPEN'",
                        (SUSPENSE,))["s"], 6)


def open_suspense(db) -> list:
    return [dict(r) for r in db.q("SELECT * FROM discrepancies WHERE kind=? AND status='OPEN' ORDER BY ts",
                                  (SUSPENSE,))]


def hold_in_suspense(db, did: str, amount: float, detail: dict, now: str) -> None:
    """A positive unexplained credit: recorded, excluded from all capital, one warning exception."""
    with db.tx():
        db.con.execute("INSERT OR IGNORE INTO discrepancies VALUES(?,?,?,?,?,?,?)",
                       (did, now, SUSPENSE, amount, canonical_json(detail), "OPEN", None))
        db.audit("FUNDS_IN_SUSPENSE", {"disc_id": did, "amount": amount, **detail}, ts=now)
    msg = (f"unclassified credit {amount:+.2f} USD held in suspense (not allocated, not spendable); the engine "
           f"re-checks broker funding evidence every reconciliation (discrepancy {did})")
    if halts.open_exception(db, SUSPENSE, did, "WARNING", msg, now):
        db.alert("WARNING", SUSPENSE, msg, ts=now)


def _rows(reader, since_iso: str) -> list:
    out = []
    for name in ("cash_transactions", "funding_evidence"):
        fn = getattr(reader, name, None)
        if fn is None:
            continue
        try:
            rows = fn(since_iso)
        except Exception:  # noqa: BLE001 -- evidence sources are best effort; absence never classifies anything
            rows = None
        for r in rows or []:
            if r.kind in ("DEPOSIT", "WITHDRAWAL", "DEPOSIT_REVERSAL"):
                out.append((name, r))
    return out


def _same_currency(r) -> bool:
    return str(getattr(r, "currency", "USD") or "USD").upper() == BASE_CURRENCY


def cross_currency_candidates(reader, amount: float, t: dt.datetime) -> list:
    """Evidence rows in ANOTHER currency, same sign, inside the time window of an unexplained credit: reported for a
    manual classification, never matched (the API gives no conversion rate for them)."""
    out = []
    for n, r in _rows(reader, (t - WINDOW_BEFORE).isoformat()):
        if not _same_currency(r) and (r.amount > 0) == (amount > 0) and _in_window(r.posted_at, t):
            out.append({"source": n, "tx_id": r.tx_id, "amount": r.amount, "currency": r.currency,
                        "posted_at": r.posted_at, "implied_rate_if_this_alone": round(amount / r.amount, 6)
                        if r.amount else None})
    return out


def _match(target: float, rows: list):
    """Exact single match first, then the smallest subset (<= 3 rows) summing to the target (several deposits
    between two reconciliations)."""
    for k in (1, 2, 3):
        for combo in itertools.combinations(rows, k):
            if abs(sum(r.amount for _n, r in combo) - target) <= MATCH_TOL:
                return list(combo)
    return None


def investigate(db, reader, cfg: dict, now: str) -> list:
    """Re-examine every open suspense item against all authorized broker funding evidence. Returns the classified
    items. Idempotent: a broker record classifies at most one suspense item (flow reference fund:<source>:<id>)."""
    if not automatic(cfg):
        return []
    items = open_suspense(db)
    if not items:
        return []
    if any(h["kind"] == "CLOSE_PROCEEDS_UNKNOWN" for h in halts.active(db)):
        return []                    # a lost close's proceeds are NOT funding: no automatic classification now
    first = min(dt.datetime.fromisoformat(d["ts"]) for d in items)
    rows = _rows(reader, (first - WINDOW_BEFORE).isoformat())
    used = {r["ref"] for r in db.q("SELECT ref FROM cash_flows WHERE ref LIKE 'fund:%' OR ref LIKE 'tx:%'")}
    done = []
    for d in items:
        t = dt.datetime.fromisoformat(d["ts"])
        cand = [(n, r) for n, r in rows if f"fund:{n}:{r.tx_id}" not in used and f"tx:{r.tx_id}" not in used
                and _same_currency(r) and (r.amount > 0) == (d["amount"] > 0) and _in_window(r.posted_at, t)]
        m = _match(float(d["amount"]), cand)
        if not m:
            continue
        detail = json.loads(d["detail"])
        with db.tx():
            fids = []
            for n, r in m:
                kind = "DEPOSIT" if r.amount > 0 else "WITHDRAWAL"
                fids.append(record_flow(db, kind, r.amount, source=f"broker:{getattr(reader, 'name', 'broker')}:{n}",
                                        evidence=f"{r.evidence} (posted {r.posted_at}; matched suspense {d['disc_id']})",
                                        ref=f"fund:{n}:{r.tx_id}", now=now,
                                        recon_snapshot_id=detail.get("snapshot_id")))
                used.add(f"fund:{n}:{r.tx_id}")
            db.con.execute("UPDATE discrepancies SET status='RESOLVED', resolution=? WHERE disc_id=?",
                           (canonical_json({"classified_as": "DEPOSIT" if d["amount"] > 0 else "WITHDRAWAL",
                                            "automatic": True, "flows": fids,
                                            "evidence": [f"{n}:{r.tx_id}" for n, r in m], "at": now}), d["disc_id"]))
            db.audit("FUNDING_CLASSIFIED", {"disc_id": d["disc_id"], "amount": d["amount"], "flows": fids,
                                            "evidence": [f"{n}:{r.tx_id}" for n, r in m]}, ts=now)
            halts.resolve_exception(db, _exc_id(d["disc_id"]), "engine (broker funding evidence)", now)
        done.append(d["disc_id"])
    return done


def classify_change(db, reader, cfg: dict, amount: float, snap_id: int, now: str) -> list:
    """A NEW unexplained cash change (either sign) seen by the current reconciliation: if authorized funding evidence
    with the same amount and time exists, book it now (against this snapshot) and return the flow ids; else []."""
    if not automatic(cfg) or any(h["kind"] == "CLOSE_PROCEEDS_UNKNOWN" for h in halts.active(db)):
        return []
    t = dt.datetime.fromisoformat(now)
    rows = _rows(reader, (t - WINDOW_BEFORE).isoformat())
    used = {r["ref"] for r in db.q("SELECT ref FROM cash_flows WHERE ref LIKE 'fund:%' OR ref LIKE 'tx:%'")}
    cand = [(n, r) for n, r in rows if f"fund:{n}:{r.tx_id}" not in used and f"tx:{r.tx_id}" not in used
            and _same_currency(r) and (r.amount > 0) == (amount > 0) and _in_window(r.posted_at, t)]
    m = _match(amount, cand)
    if not m:
        return []
    fids = []
    with db.tx():
        for n, r in m:
            kind = "DEPOSIT" if r.amount > 0 else "WITHDRAWAL"
            fids.append(record_flow(db, kind, r.amount, source=f"broker:{getattr(reader, 'name', 'broker')}:{n}",
                                    evidence=f"{r.evidence} (posted {r.posted_at})", ref=f"fund:{n}:{r.tx_id}",
                                    now=now, recon_snapshot_id=snap_id))
        db.audit("FUNDING_CLASSIFIED", {"amount": amount, "flows": fids, "snapshot_id": snap_id,
                                        "evidence": [f"{n}:{r.tx_id}" for n, r in m]}, ts=now)
    return fids


def link_tx_to_suspense(db, tx, now: str) -> str | None:
    """A broker cash-transaction record arriving AFTER its credit was put in suspense explains that suspense item
    (not a new cash change): returns the suspense's snapshot id to book it against, and resolves the item."""
    for d in open_suspense(db):
        if abs(float(d["amount"]) - tx.amount) <= MATCH_TOL and tx.kind in ("DEPOSIT", "ADJUSTMENT", "DIVIDEND"):
            detail = json.loads(d["detail"])
            with db.tx():
                db.con.execute("UPDATE discrepancies SET status='RESOLVED', resolution=? WHERE disc_id=?",
                               (canonical_json({"classified_as": tx.kind, "automatic": True, "evidence": tx.tx_id,
                                                "at": now}), d["disc_id"]))
                db.audit("FUNDING_CLASSIFIED", {"disc_id": d["disc_id"], "amount": d["amount"],
                                                "evidence": f"tx:{tx.tx_id}", "delayed_statement": True}, ts=now)
                halts.resolve_exception(db, _exc_id(d["disc_id"]), "engine (delayed broker statement)", now)
            return detail.get("snapshot_id")
    return None


def _exc_id(disc_id: str) -> str:
    from .canonical import short_id
    return short_id("exc", SUSPENSE, disc_id, "", n=16)


def _in_window(posted_at, t: dt.datetime) -> bool:
    try:
        p = dt.datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    return t - WINDOW_BEFORE <= p <= t + WINDOW_AFTER


def capital_report(db, recon, sleeve_val: dict | None = None) -> dict:
    """Broker-verified capital picture: account equity vs what the engine attributes, with every gap named.
    Values the broker did not supply are None (never estimated)."""
    s = recon.snapshot
    if s is None:
        return {"status": "UNAVAILABLE", "reason": "no broker snapshot"}
    from .reconciliation import engine_position_ids
    eng = engine_position_ids(db)
    engine_pos = [p for p in s.positions if p.position_id in eng]
    foreign = [p for p in s.positions if p.position_id not in eng]
    unreal = s.unrealized_pnl
    susp = suspense_total(db)
    sleeves_eq = None
    if sleeve_val and sleeve_val.get("complete"):
        sleeves_eq = round(sum(v["equity"] for v in sleeve_val["sleeves"].values()), 6)
    foreign_val = None if any(p.unrealized_pnl is None for p in foreign) else round(sum(p.value for p in foreign), 6)
    out = {
        "as_of": s.as_of, "source": s.source, "reconciliation": recon.status,
        "account_equity": s.equity, "available_cash": round(s.available_cash, 6), "credit": s.credit,
        "reserved_pending_opens": s.pending_open_amount, "invested_amount": round(s.total_invested, 6),
        "unrealized_pnl": unreal, "engine_positions_value": None if any(p.unrealized_pnl is None for p in engine_pos)
        else round(sum(p.value for p in engine_pos), 6),
        "foreign_positions_value": foreign_val, "suspense_unclassified": susp,
        "strategy_equity": recon.strategy_equity, "sum_of_sleeve_equity": sleeves_eq,
        "leverage_policy": "x1 only (no margin borrowing; extra CFD leverage is never used)",
    }
    if s.equity is None:
        out.update(status="INCOMPLETE", reason="broker gave no unrealized P&L for some position: account equity "
                                               "cannot be verified (never estimated)")
        return out
    attributed = (sleeves_eq if sleeves_eq is not None else recon.strategy_equity or 0.0) + susp + (foreign_val or 0.0)
    out["unattributed"] = round(s.equity - attributed, 6)
    out["status"] = "VERIFIED" if abs(out["unattributed"]) <= 0.05 + 1e-9 * abs(s.equity) else "MISMATCH"
    return out
