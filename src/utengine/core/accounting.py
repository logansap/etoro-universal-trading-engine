"""Broker-authoritative accounting.

The broker's numbers are the truth; this ledger only EXPLAINS changes in the broker's cash (`credit`):

    credit_now - credit_prev  ==  sum(unreconciled cash flows)  (+/- cash_tolerance_usd)

Cash flows are evidence-backed rows, never balancing entries:
  TRADE_OPEN   -amount of an engine open that the broker reports filled
  TRADE_CLOSE  +proceeds of an engine close that the broker reports filled
  DEPOSIT / WITHDRAWAL               external capital (broker cash-transaction record, or an audited operator
                                     classification of an OBSERVED unexplained change -- see resolve_discrepancy)
  DIVIDEND / FEE / ADJUSTMENT        broker-reported, or operator-classified observed change
  INITIAL_CAPITAL                    the available cash found at bootstrap (exclusive-ownership rule)
  TAKEOVER_PROCEEDS                  +proceeds of closing a FOREIGN position in an approved takeover: capital that
                                     enters the engine's scope (external), never strategy P&L
Anything left unexplained becomes a DISCREPANCY and halts all new orders (unknown_balance_policy: halt).

Performance separates capital from return:
  strategy P&L(t) = E(t) - E(t-1) - F(t)          F = external flows since the previous record
  TWR index(t)    = TWR(t-1) * (E(t) - F(t)) / E(t-1)
where E = strategy equity (available cash + engine-owned positions at broker value). A deposit raises E and F
by the same amount and leaves P&L and the TWR index unchanged.
"""
from __future__ import annotations

import json

from .canonical import canonical_json, short_id
from .errors import EngineError

EXTERNAL_KINDS = {"DEPOSIT", "WITHDRAWAL", "INITIAL_CAPITAL", "EXTERNAL_TRADE", "TAKEOVER_PROCEEDS", "DEPOSIT_REVERSAL"}
CLASSIFIABLE = {"DEPOSIT", "WITHDRAWAL", "DIVIDEND", "FEE", "ADJUSTMENT", "EXTERNAL_TRADE", "TRADE_CLOSE"}


def record_flow(db, kind: str, amount: float, *, source: str, evidence: str, ref: str, now: str,
                recon_snapshot_id: int | None = None) -> str:
    """Idempotent by (source, ref): replaying the same evidence never double-books."""
    fid = short_id("flow", source, ref)
    with db.tx():
        if db.one("SELECT 1 FROM cash_flows WHERE flow_id=?", (fid,)):
            return fid
        db.con.execute("INSERT INTO cash_flows VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (fid, now, kind, round(float(amount), 6), int(kind in EXTERNAL_KINDS), source, evidence,
                        ref, recon_snapshot_id, None))
        db.audit("CASH_FLOW", {"flow_id": fid, "kind": kind, "amount": round(float(amount), 6), "source": source,
                               "ref": ref, "evidence": evidence}, ts=now)
    return fid


def resolve_discrepancy(db, disc_id: str, classify: str, evidence: str, operator: str, now: str,
                        intent_id: str | None = None) -> str:
    """Operator classification of an OBSERVED unexplained cash change. The amount is the discrepancy's own observed
    amount -- an operator cannot type a number, so nothing can be manufactured. Recorded with the snapshot the
    discrepancy was found in, so the cash reconciliation is not affected twice.

    TRADE_CLOSE (the lost-close-response case, blocker B3): the close is proven by broker position evidence but the
    broker gave no proceeds. It must name the close intent (`intent_id`), which must be a filled close without any
    recorded proceeds; in multi-strategy mode the amount is then attributed to the selling sleeves by their units."""
    if classify not in CLASSIFIABLE:
        raise EngineError(f"classification must be one of {sorted(CLASSIFIABLE)}")
    if not evidence.strip() or not operator.strip():
        raise EngineError("--evidence and --operator are required")
    if classify == "TRADE_CLOSE":
        it = db.one("SELECT * FROM intents WHERE intent_id=?", (intent_id or "",))
        if not it or it["action"] not in ("CLOSE", "PARTIAL_CLOSE") or it["status"] not in ("FILLED", "RECONCILED"):
            raise EngineError("TRADE_CLOSE needs --intent naming a FILLED close intent")
        if db.one("SELECT 1 FROM cash_flows WHERE kind IN ('TRADE_CLOSE','TAKEOVER_PROCEEDS') AND ref IN (?,?)",
                  (f"close:{it['broker_order_id']}", f"close:{intent_id}")) or                 db.one("SELECT 1 FROM cash_flows WHERE kind='TRADE_CLOSE' AND ref=?", (f"intent:{intent_id}",)):
            raise EngineError(f"intent {intent_id} already has recorded proceeds")
    d = db.one("SELECT * FROM discrepancies WHERE disc_id=?", (disc_id,))
    if not d or d["status"] != "OPEN":
        raise EngineError(f"no open discrepancy {disc_id}")
    from .funding import SUSPENSE, _exc_id
    if d["kind"] == SUSPENSE and classify not in ("EXTERNAL_TRADE", "DEPOSIT"):
        # a suspense item is a positive unexplained credit: e.g. the proceeds of a foreign (pre-existing) position
        # closed outside the engine (EXTERNAL_TRADE), or a deposit without typed broker evidence (DEPOSIT)
        raise EngineError("a suspense credit can only be classified as EXTERNAL_TRADE or DEPOSIT")
    if d["kind"] not in ("CASH_UNEXPLAINED", SUSPENSE):
        raise EngineError(f"{d['kind']} is not a cash discrepancy; resolve it with its own command")
    amt = float(d["amount"])
    if classify in ("DEPOSIT", "TRADE_CLOSE") and amt <= 0 or classify == "WITHDRAWAL" and amt >= 0:
        raise EngineError(f"{classify} contradicts the observed sign of {amt:+.2f}")
    detail = json.loads(d["detail"])
    with db.tx():
        fid = record_flow(db, classify, amt, source="operator", evidence=f"{operator}: {evidence}",
                          ref=f"intent:{intent_id}" if classify == "TRADE_CLOSE" else f"disc:{disc_id}", now=now,
                          recon_snapshot_id=detail.get("snapshot_id"))
        if classify == "TRADE_CLOSE":
            from .sleeves import post_pending_proceeds
            post_pending_proceeds(db, intent_id, amt, fid, now)
        db.con.execute("UPDATE discrepancies SET status='RESOLVED', resolution=? WHERE disc_id=?",
                       (canonical_json({"classified_as": classify, "flow_id": fid, "operator": operator,
                                        "evidence": evidence, "at": now}), disc_id))
        db.audit("DISCREPANCY_RESOLVED", {"disc_id": disc_id, "classified_as": classify, "amount": amt,
                                          "operator": operator, "evidence": evidence}, ts=now)
    if d["kind"] == SUSPENSE:
        from . import halts
        halts.resolve_exception(db, _exc_id(disc_id), f"operator {operator} ({classify})", now)
    return fid


def record_daily(db, session: str, *, account_equity, strategy_equity, credit, exposure: dict, strategy_id: str,
                 now: str, foreign_value: float | None = None) -> dict:
    prev = db.one("SELECT * FROM daily_equity WHERE session < ? ORDER BY session DESC LIMIT 1", (session,))
    if db.one("SELECT 1 FROM daily_equity WHERE session=?", (session,)):
        r = dict(db.one("SELECT * FROM daily_equity WHERE session=?", (session,)))
        return {"session": session, "strategy_equity": r["strategy_equity"], "external_flow": r["external_flow"],
                "pnl": r["pnl"], "twr_index": r["twr_index"], "drawdown": r["drawdown"], "existing": True}
    flows = db.q("SELECT flow_id, amount FROM cash_flows WHERE external=1 AND performance_ts IS NULL")
    f = round(sum(r["amount"] for r in flows), 6)
    if prev and prev["strategy_equity"]:
        pnl = round(strategy_equity - prev["strategy_equity"] - f, 6)
        twr = prev["twr_index"] * (strategy_equity - f) / prev["strategy_equity"]
        hwm = max(prev["hwm"], twr)
    else:
        pnl, twr, hwm = 0.0, 1.0, 1.0
    dd = twr / hwm - 1.0 if hwm else 0.0
    with db.tx():
        db.con.execute("INSERT INTO daily_equity(session,ts,account_equity,strategy_equity,credit,external_flow,pnl,"
                       "twr_index,hwm,drawdown,exposure,strategy_id,foreign_value) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(session) DO NOTHING",
                       (session, now, account_equity, strategy_equity, credit, f, pnl, twr, hwm, dd,
                        canonical_json(exposure), strategy_id, foreign_value))
        for r in flows:
            db.con.execute("UPDATE cash_flows SET performance_ts=? WHERE flow_id=?", (now, r["flow_id"]))
    return {"session": session, "strategy_equity": strategy_equity, "external_flow": f, "pnl": pnl,
            "twr_index": twr, "drawdown": dd}


def performance(db) -> dict:
    rows = [dict(r) for r in db.q("SELECT * FROM daily_equity ORDER BY session")]
    ext = db.one("SELECT COALESCE(SUM(amount),0) s FROM cash_flows WHERE external=1")["s"]
    if not rows:
        return {"sessions": 0, "net_external_capital": ext}
    last = rows[-1]
    return {"sessions": len(rows), "first": rows[0]["session"], "last": last["session"],
            "strategy_equity": last["strategy_equity"], "account_equity": last["account_equity"],
            "net_external_capital": ext, "cumulative_pnl": round(sum(r["pnl"] or 0 for r in rows), 6),
            "twr_return": last["twr_index"] - 1.0, "max_drawdown": min(r["drawdown"] for r in rows)}


def funding_summary(db) -> dict:
    """Capital in and out of the strategy's scope, by kind (evidence-backed flows only)."""
    out = {k: 0.0 for k in ("INITIAL_CAPITAL", "DEPOSIT", "WITHDRAWAL", "DEPOSIT_REVERSAL", "EXTERNAL_TRADE",
                            "TAKEOVER_PROCEEDS", "DIVIDEND", "FEE", "ADJUSTMENT")}
    for r in db.q("SELECT kind, SUM(amount) s FROM cash_flows GROUP BY kind"):
        if r["kind"] in out:
            out[r["kind"]] = round(r["s"], 6)
    out["net_contributions"] = round(out["INITIAL_CAPITAL"] + out["DEPOSIT"] + out["WITHDRAWAL"]
                                     + out["DEPOSIT_REVERSAL"] + out["EXTERNAL_TRADE"] + out["TAKEOVER_PROCEEDS"], 6)
    return out


def account_report(db, recon) -> dict:
    """Actual account equity vs strategy-allocated equity, and funding-adjusted performance.

    account equity   broker equity (eToro guide formula); None when the broker gave no unrealized P&L for some
                     position (never estimated)
    foreign value    pre-existing / external positions (amount + unrealized when known) -- NOT strategy capital
    strategy equity  available cash + ENGINE positions (what the strategy may allocate)
    trading P&L      strategy equity - net contributions (initial capital + deposits - withdrawals - cash moved
                     into foreign positions); dividends, fees and adjustments are part of trading P&L
    TWR              time-weighted, funding-adjusted return (deposits neither help nor hurt it)
    """
    snap = recon.snapshot
    owners = {r["position_id"]: r["owner"] for r in db.q("SELECT position_id, owner FROM ownership")}
    rows = []
    for p in snap.positions if snap else []:
        rows.append({"position_id": p.position_id, "instrument_id": p.instrument_id, "symbol": p.symbol,
                     "owner": owners.get(p.position_id, "UNCLASSIFIED"), "is_buy": p.is_buy, "leverage": p.leverage,
                     "units": p.units, "amount": p.amount, "unrealized_pnl": p.unrealized_pnl,
                     "value": None if p.unrealized_pnl is None else round(p.value, 2)})
    foreign = [r for r in rows if r["owner"] != "ENGINE"]
    foreign_value = (None if any(r["unrealized_pnl"] is None for r in foreign)
                     else round(sum(r["value"] for r in foreign), 2))
    fund = funding_summary(db)
    se = recon.strategy_equity
    perf = performance(db)
    return {
        "as_of": snap.as_of if snap else None, "reconciliation": recon.status,
        "broker": {"credit": snap.credit if snap else None, "available_cash": snap.available_cash if snap else None,
                   "pending_open_amount": snap.pending_open_amount if snap else None,
                   "account_equity": recon.account_equity,
                   "account_equity_note": None if recon.account_equity is not None else
                   "unavailable: the broker returned no unrealized P&L for at least one position (never estimated)"},
        "positions": rows, "foreign_positions_value": foreign_value,
        "foreign_positions_amount": round(sum(r["amount"] for r in foreign), 2),
        "strategy_equity": se,
        "funding": fund,
        "trading_pnl": None if se is None else round(se - fund["net_contributions"], 2),
        "simple_return_on_contributions": (None if se is None or fund["net_contributions"] <= 0 else
                                           round(se / fund["net_contributions"] - 1, 6)),
        "twr_return": perf.get("twr_return"), "max_drawdown": perf.get("max_drawdown"),
        "sessions_recorded": perf.get("sessions", 0),
    }
