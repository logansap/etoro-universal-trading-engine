"""Virtual sleeves: several strategies sharing ONE broker account (docs/VIRTUAL_SLEEVE_ACCOUNTING.md).

The broker stays authoritative for the ACCOUNT (cash, positions, values). This module only divides what the broker
reports among the sleeves, with an append-only ledger (`sleeve_ledger`, SQL triggers refuse UPDATE/DELETE):

  sleeve cash      sum of the sleeve's ledger cash entries
  sleeve units     per instrument, sum of the sleeve's ledger unit entries (unit-pooled: a broker position is not
                   owned by a sleeve; the sleeve owns UNITS of the instrument, so two sleeves holding TQQQ share
                   the broker's TQQQ positions)
  mark             broker value per unit of an instrument = sum(position value) / sum(position units) over the
                   engine's broker positions in it (x1 long CFD: value = units x bid, so every unit has the same
                   value; financing and fees already in the broker value are shared pro rata)
  sleeve equity    cash - reserved (unfilled opens in flight) + sum(units x mark)

Invariants checked after every clean reconciliation (a breach is SLEEVE_LEDGER_MISMATCH, halt ALL):
  * every broker cash flow (cash_flows) is posted to sleeves exactly once, and sum(sleeve cash) == sum(posted flows);
  * per instrument, sum(sleeve units) == units of the engine's broker positions.
So sum(sleeve equity) == account strategy equity: nothing is created, lost or double-counted by attribution.

Postings and their sources (never a balancing entry):
  OPEN_FILL / CLOSE_FILL    a broker fill, split by the intent's persisted sleeve attribution (fill hook)
  CROSS_OUT / CROSS_IN      internal crossing: one sleeve's sale and another's purchase of the same instrument in the
                            same plan, transferred at the broker mark with no broker order (equity-neutral)
  INITIAL_CAPITAL / DEPOSIT / WITHDRAWAL / EXTERNAL_TRADE   funding_policy proportional: split by sleeve weights
  DIVIDEND / FEE / ADJUSTMENT   split by sleeve equity (part of trading P&L)
  TAKEOVER_PROCEEDS         to the _UNALLOCATED pool until the takeover's ALLOCATE step
  ALLOCATE / REALLOC        audited transfers between the pool / sleeves (cash; external to the sleeve's P&L)
  SEED                      once, when a non-empty single-strategy database is converted to sleeves
"""
from __future__ import annotations

import datetime as dt
import json

from . import halts
from .canonical import canonical_json, fingerprint, short_id
from .errors import EngineError
from .order_state import UNRESOLVED

UNALLOCATED = "_UNALLOCATED"
FUNDING_KINDS = {"INITIAL_CAPITAL", "DEPOSIT", "WITHDRAWAL", "DEPOSIT_REVERSAL", "EXTERNAL_TRADE"}
INCOME_KINDS = {"DIVIDEND", "FEE", "ADJUSTMENT"}
TRADE_KINDS = {"TRADE_OPEN", "TRADE_CLOSE"}
POOL_KINDS = {"TAKEOVER_PROCEEDS"}
EXTERNAL_LEDGER_KINDS = FUNDING_KINDS | POOL_KINDS | {"ALLOCATE_IN", "ALLOCATE_OUT", "REALLOC_IN", "REALLOC_OUT",
                                                       "SEED"}
REALLOC_TTL = dt.timedelta(minutes=15)
UNIT_TOL = 1e-6
CASH_TOL = 1e-4


# ----------------------------------------------------------------------------- portfolio record (DB, not YAML)
def portfolio(db) -> dict | None:
    return db.get("portfolio")


def weights(db) -> dict:
    p = portfolio(db)
    return {s["strategy"]: float(s["weight"]) for s in p["sleeves"]} if p else {}


def bootstrap(db, cfg: dict, specs: dict, registry: dict, now: str) -> dict:
    """First start in multi_strategy mode: the configured sleeves and weights are recorded in the DATABASE (like
    the active strategy). Later YAML edits change nothing; `portfolio reallocate` is the audited path."""
    cur = portfolio(db)
    if cur:
        return cur
    pf = cfg["portfolio"]
    recs = {}
    for s in pf["sleeves"]:
        spec = specs.get(s["strategy"])
        if spec is None or not spec["enabled"]:
            raise EngineError(f"portfolio sleeve {s['strategy']} has no enabled strategy YAML")
        if registry[spec["implementation"]].status != "AVAILABLE":
            raise EngineError(f"portfolio sleeve {s['strategy']}: implementation unavailable")
        recs[s["strategy"]] = {"fingerprint": spec["fingerprint"], "version": spec["version"],
                               "module_sha256": registry[spec["implementation"]].source_sha256}
    seed = bool(db.one("SELECT 1 FROM cash_flows LIMIT 1"))
    rec = {"sleeves": [{"strategy": s["strategy"], "weight": s["weight"]} for s in pf["sleeves"]],
           "yaml_fingerprint": fingerprint(pf), "strategies": recs, "activated_at": now,
           "approved_by": "engine.yaml portfolio (first start)", "funding_policy": pf["funding_policy"],
           "rebalance_policy": pf["rebalance_policy"], "internal_crossing": pf.get("internal_crossing", True),
           "seed_required": seed}
    db.put("portfolio", rec)
    db.audit("PORTFOLIO_ACTIVATED", rec, ts=now)
    return rec


def drift(db, specs: dict, registry: dict) -> dict:
    """Per-sleeve configuration drift (YAML fingerprint / module source / disabled) -> {sleeve: [reasons]}."""
    out = {}
    p = portfolio(db) or {"strategies": {}}
    for sid, rec in p["strategies"].items():
        spec = specs.get(sid)
        why = []
        if spec is None:
            why.append(f"{sid} has no YAML")
        else:
            if spec["fingerprint"] != rec["fingerprint"]:
                why.append(f"{sid} YAML fingerprint {spec['fingerprint'][:19]} != activated {rec['fingerprint'][:19]}")
            if registry[spec["implementation"]].source_sha256 != rec["module_sha256"]:
                why.append(f"{sid} module source changed since activation")
            if not spec["enabled"]:
                why.append(f"{sid} is now disabled in its YAML")
        if why:
            out[sid] = why
    return out


# ----------------------------------------------------------------------------- ledger primitives
def post(db, sleeve: str, kind: str, now: str, *, ref: str, cash: float = 0.0, units: float = 0.0,
         instrument_id: int | None = None, symbol: str | None = None, flow_id: str | None = None,
         intent_id: str | None = None, plan_id: str | None = None, detail: dict | None = None) -> bool:
    """Idempotent by (ref, sleeve, kind): replaying the same evidence never double-posts."""
    eid = short_id("sl", ref, sleeve, kind)
    with db.tx():
        cur = db.con.execute(
            "INSERT OR IGNORE INTO sleeve_ledger(entry_id,ts,sleeve,kind,cash,instrument_id,symbol,units,external,"
            "flow_id,intent_id,plan_id,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, now, sleeve, kind, round(float(cash), 6), instrument_id, symbol, round(float(units), 9),
             int(kind in EXTERNAL_LEDGER_KINDS), flow_id, intent_id, plan_id, canonical_json(detail or {})))
        return cur.rowcount == 1


def mark_posted(db, flow_id: str, policy: str, now: str) -> None:
    db.x("INSERT OR IGNORE INTO sleeve_flow_posts VALUES(?,?,?)", (flow_id, now, policy))


def request_catch_up(db, sleeve: str, reason: str, ref: str, now: str) -> None:
    """New capital reached `sleeve` through an AUDITED transfer: a restructure or a reallocation (approved preview hash),
    a takeover-pool allocation, or operator-classified proceeds of a foreign position (EXTERNAL_TRADE > 0). Strategies such as #529 trade only when their signal label changes, so without
    this the new cash would sit idle until then. The next execution of this sleeve re-targets the decision's
    standing target weights on the sleeve's equity (logged as CAPITAL_CATCH_UP). Ordinary deposits are NOT included."""
    if sleeve == UNALLOCATED:
        return
    cur = db.get(f"catch_up:{sleeve}")
    if cur and not cur.get("consumed"):
        return
    rec = {"sleeve": sleeve, "reason": reason, "ref": ref, "requested_at": now}
    db.put(f"catch_up:{sleeve}", rec)
    db.audit("CATCH_UP_REQUESTED", rec, ts=now)


def catch_up_pending(db, sleeve: str) -> dict | None:
    cur = db.get(f"catch_up:{sleeve}")
    return cur if cur and not cur.get("consumed") else None


def consume_catch_up(db, sleeve: str, plan_id: str, now: str) -> None:
    cur = catch_up_pending(db, sleeve)
    if cur:
        db.put(f"catch_up:{sleeve}", {**cur, "consumed": now, "plan_id": plan_id})
        db.audit("CATCH_UP_CONSUMED", {"sleeve": sleeve, "plan_id": plan_id, "ref": cur["ref"]}, ts=now)


def split(total: float, shares: dict, nd: int) -> dict:
    """Split `total` by non-negative `shares` exactly: rounded to nd decimals, remainder to the last key."""
    keys = sorted(k for k, v in shares.items() if v > 0)
    if not keys:
        return {}
    s = sum(shares[k] for k in keys)
    out, acc = {}, 0.0
    for k in keys[:-1]:
        out[k] = round(total * shares[k] / s, nd)
        acc += out[k]
    out[keys[-1]] = round(total - acc, nd)
    return out


def balances(db) -> dict:
    out = {}
    for r in db.q("SELECT sleeve, SUM(cash) c FROM sleeve_ledger GROUP BY sleeve"):
        out.setdefault(r["sleeve"], {"cash": 0.0, "units": {}})["cash"] = round(r["c"], 6)
    for r in db.q("SELECT sleeve, instrument_id, symbol, SUM(units) u FROM sleeve_ledger WHERE instrument_id IS NOT "
                  "NULL GROUP BY sleeve, instrument_id"):
        if abs(r["u"]) > 1e-12:
            out.setdefault(r["sleeve"], {"cash": 0.0, "units": {}})["units"][int(r["instrument_id"])] = r["u"]
    for s in weights(db):
        out.setdefault(s, {"cash": 0.0, "units": {}})
    return out


def _sleeve_shares(row) -> list:
    return (json.loads(row["result"] or "{}") or {}).get("sleeves") or []


def reserved(db) -> dict:
    """Cash committed to OPEN intents that are in flight (not final): share of the order amount not yet posted as a
    fill. Derived from durable intent rows, so it survives a restart."""
    out = {}
    rows = db.q(f"SELECT intent_id, amount, result FROM intents WHERE action='OPEN' AND status IN "
                f"({','.join('?' * len(UNRESOLVED))})", tuple(UNRESOLVED))
    for r in rows:
        shares = {s: float(q) for s, q in _sleeve_shares(r)}
        if not shares:
            continue
        per = split(float(r["amount"] or 0.0), shares, 6)
        for s, amt in per.items():
            spent = -(db.one("SELECT COALESCE(SUM(cash),0) c FROM sleeve_ledger WHERE intent_id=? AND sleeve=? AND "
                             "kind='OPEN_FILL'", (r["intent_id"], s))["c"])
            out[s] = round(out.get(s, 0.0) + max(0.0, amt - spent), 6)
    return out


def marks(snapshot, engine_ids: set) -> dict:
    """instrument id -> broker value per unit over the engine's positions (None when a value is unknown)."""
    agg = {}
    for p in snapshot.positions:
        if p.position_id not in engine_ids:
            continue
        a = agg.setdefault(p.instrument_id, [0.0, 0.0, True])
        a[0] += p.units
        if p.unrealized_pnl is None:
            a[2] = False
        else:
            a[1] += p.value
    return {iid: (v / u if ok and u > 0 else None) for iid, (u, v, ok) in agg.items()}


def broker_units(snapshot, engine_ids: set) -> dict:
    out = {}
    for p in snapshot.positions:
        if p.position_id in engine_ids:
            out[p.instrument_id] = out.get(p.instrument_id, 0.0) + p.units
    return out


def valuation(db, snapshot, engine_ids: set, id_to_symbol: dict | None = None) -> dict:
    """Per-sleeve equity at the broker's marks. `complete` is False when a held instrument has no broker value."""
    bal, res, mk = balances(db), reserved(db), marks(snapshot, engine_ids)
    out, complete = {}, True
    for s, b in sorted(bal.items()):
        hold, hv = {}, 0.0
        for iid, u in sorted(b["units"].items()):
            m = mk.get(iid)
            if m is None:
                complete = False
                hold[iid] = {"symbol": (id_to_symbol or {}).get(iid), "units": u, "mark": None, "value": None}
                continue
            hold[iid] = {"symbol": (id_to_symbol or {}).get(iid), "units": u, "mark": m, "value": round(u * m, 6)}
            hv += u * m
        r = res.get(s, 0.0)
        out[s] = {"cash": b["cash"], "reserved": r, "available_cash": round(b["cash"] - r, 6), "holdings": hold,
                  "holdings_value": round(hv, 6), "equity": round(b["cash"] - r + hv, 6)}
    if not complete:
        for v in out.values():
            v["equity"] = None
    return {"sleeves": out, "complete": complete, "marks": mk}


# ----------------------------------------------------------------------------- flows -> sleeves
def _equity_shares(db, snapshot, engine_ids) -> dict:
    v = valuation(db, snapshot, engine_ids) if snapshot is not None else {"complete": False}
    w = weights(db)
    if v["complete"]:
        eq = {s: max(0.0, x["equity"]) for s, x in v["sleeves"].items() if s in w}
        if sum(eq.values()) > 0:
            return eq
    return w


def seed_from_snapshot(db, snapshot, engine_ids: set, ownership_strategy: dict, now: str) -> dict:
    """Convert a non-empty single-strategy history: open engine holdings go to the sleeve of the strategy that
    opened them (else split by weight); the broker cash is split so that sleeve equity matches the weights as
    closely as possible without negative cash. All earlier flows are marked as covered by the seed."""
    w = weights(db)
    mk = marks(snapshot, engine_ids)
    units = {}
    for p in snapshot.positions:
        if p.position_id not in engine_ids:
            continue
        owner = ownership_strategy.get(p.position_id)
        tgt = {owner: 1.0} if owner in w else w
        for s, u in split(p.units, tgt, 9).items():
            units.setdefault(s, {}).setdefault(p.instrument_id, 0.0)
            units[s][p.instrument_id] += u
    hv = {s: sum(u * (mk.get(i) or 0.0) for i, u in units.get(s, {}).items()) for s in w}
    cash_total = round(sum(r["amount"] for r in db.q("SELECT amount FROM cash_flows")), 6)
    total = cash_total + sum(hv.values())
    want = {s: max(0.0, w[s] * total - hv[s]) for s in w}
    cash = split(cash_total, want if sum(want.values()) > 0 else w, 6)
    with db.tx():
        for s in sorted(w):
            post(db, s, "SEED", now, ref="seed", cash=cash.get(s, 0.0),
                 detail={"units": {str(i): u for i, u in units.get(s, {}).items()}})
            for iid, u in sorted(units.get(s, {}).items()):
                post(db, s, "SEED_UNITS", now, ref=f"seed:{iid}", units=u, instrument_id=iid)
        for r in db.q("SELECT flow_id FROM cash_flows"):
            mark_posted(db, r["flow_id"], "SEED", now)
        rec = portfolio(db)
        rec["seed_required"] = False
        db.put("portfolio", rec)
        db.audit("SLEEVES_SEEDED", {"cash": cash, "units": {s: {str(i): u for i, u in d.items()}
                                                             for s, d in units.items()}}, ts=now)
    return {"cash": cash, "units": units}


def sync_flows(db, snapshot, engine_ids: set, now: str) -> list:
    """Post every not-yet-posted broker cash flow by policy. Trade flows are posted by the fill hook; one found
    here unposted has no attribution -> issue (never guessed)."""
    w = weights(db)
    issues = []
    rows = db.q("SELECT * FROM cash_flows WHERE flow_id NOT IN (SELECT flow_id FROM sleeve_flow_posts) ORDER BY ts, "
                "flow_id")
    for f in rows:
        kind, amt = f["kind"], float(f["amount"])
        if kind in TRADE_KINDS:
            issues.append({"kind": "SLEEVE_LEDGER_MISMATCH", "subject": f"flow:{f['flow_id']}",
                           "detail": f"{kind} flow {f['flow_id']} ({amt:+.2f}) has no sleeve attribution"})
            continue
        if kind in POOL_KINDS:
            parts, policy = {UNALLOCATED: amt}, "POOL"
        elif kind in INCOME_KINDS:
            parts, policy = split(amt, _equity_shares(db, snapshot, engine_ids), 6), "EQUITY_SHARE"
        else:
            parts, policy = split(amt, w, 6), "WEIGHTS"
        with db.tx():
            for s, a in parts.items():
                post(db, s, kind, now, ref=f"flow:{f['flow_id']}", cash=a, flow_id=f["flow_id"],
                     detail={"policy": policy, "evidence": f["evidence"]})
            mark_posted(db, f["flow_id"], policy, now)
        if kind == "EXTERNAL_TRADE" and amt > 0:      # proceeds of a foreign position, classified by an operator
            for s, a in parts.items():
                if a > 0:
                    request_catch_up(db, s, "external-position proceeds", f"flow:{f['flow_id']}", now)
    return issues


# ----------------------------------------------------------------------------- fills (Executor hook)
def on_fill(db, row: dict, info: dict, now: str) -> None:
    """Attribute a broker fill to the sleeves recorded on the intent BEFORE it was submitted."""
    shares = {s: float(q) for s, q in _sleeve_shares(row)}
    if not shares:
        return                                   # single-strategy intent or takeover close (pool, via sync_flows)
    iid, sym = row["instrument_id"], row["symbol"]
    if row["action"] == "OPEN":
        for pid, units, amount, flow_id in info.get("opened", []):
            a, u = split(amount, shares, 6), split(units, shares, 9)
            with db.tx():
                for s in sorted(a):
                    post(db, s, "OPEN_FILL", now, ref=f"open:{pid}", cash=-a[s], units=u[s], instrument_id=iid,
                         symbol=sym, flow_id=flow_id, intent_id=row["intent_id"], detail={"position_id": pid})
                if flow_id:
                    mark_posted(db, flow_id, "INTENT", now)
        return
    closed, proceeds, flow_id = info.get("closed_units"), info.get("proceeds"), info.get("flow_id")
    if not closed:
        return
    u = split(closed, shares, 9)
    p = split(proceeds, shares, 6) if proceeds is not None else {}
    with db.tx():
        for s in sorted(u):
            post(db, s, "CLOSE_FILL", now, ref=f"close:{row['intent_id']}", cash=p.get(s, 0.0), units=-u[s],
                 instrument_id=iid, symbol=sym, flow_id=flow_id, intent_id=row["intent_id"],
                 detail={"position_id": row["position_id"], "proceeds_pending": proceeds is None})
        if flow_id:
            mark_posted(db, flow_id, "INTENT", now)
    if proceeds is None:
        halts.raise_halt(db, "CLOSE_PROCEEDS_UNKNOWN", "ALL",
                         f"close intent {row['intent_id']} filled (units left the account) but the broker gave no "
                         "proceeds; sleeve cash for it is pending. Verify the credited amount in eToro, then "
                         "`account resolve --classify TRADE_CLOSE --intent <id>`", now, subject=row["intent_id"])


def post_pending_proceeds(db, intent_id: str, amount: float, flow_id: str, now: str) -> dict:
    rows = db.q("SELECT sleeve, units FROM sleeve_ledger WHERE intent_id=? AND kind='CLOSE_FILL'", (intent_id,))
    shares = {r["sleeve"]: -r["units"] for r in rows}
    if not shares:
        mark_posted(db, flow_id, "SINGLE", now)
        return {}
    p = split(amount, shares, 6)
    with db.tx():
        for s, a in p.items():
            post(db, s, "CLOSE_PROCEEDS", now, ref=f"proceeds:{intent_id}", cash=a, flow_id=flow_id,
                 intent_id=intent_id, detail={"source": "operator-classified observed discrepancy"})
        mark_posted(db, flow_id, "INTENT", now)
    return p


# ----------------------------------------------------------------------------- crossing
def apply_crossings(db, plan_id: str, crossings: list, now: str) -> int:
    n = 0
    with db.tx():
        for c in crossings:
            iid, m = c["instrument_id"], c["mark"]
            cash_out = {s: round(u * m, 6) for s, u in c["from"]}
            pay = split(sum(cash_out.values()), {s: u for s, u in c["to"]}, 6)
            for s, u in c["from"]:
                n += post(db, s, "CROSS_OUT", now, ref=f"cross:{plan_id}:{iid}", cash=cash_out[s], units=-u,
                          instrument_id=iid, symbol=c["symbol"], plan_id=plan_id, detail={"mark": m})
            for s, u in c["to"]:
                n += post(db, s, "CROSS_IN", now, ref=f"cross:{plan_id}:{iid}", cash=-pay[s], units=u,
                          instrument_id=iid, symbol=c["symbol"], plan_id=plan_id, detail={"mark": m})
        if n:
            db.audit("SLEEVE_CROSSING", {"plan_id": plan_id, "crossings": crossings}, ts=now)
    return n


# ----------------------------------------------------------------------------- invariants
def check_invariants(db, snapshot, engine_ids: set) -> list:
    issues = []
    unposted = db.one("SELECT COUNT(*) c FROM cash_flows WHERE flow_id NOT IN (SELECT flow_id FROM sleeve_flow_posts)")["c"]
    if unposted:
        issues.append({"kind": "SLEEVE_LEDGER_MISMATCH", "subject": "unposted-flows",
                       "detail": f"{unposted} broker cash flow(s) not attributed to any sleeve"})
    led = db.one("SELECT COALESCE(SUM(cash),0) c FROM sleeve_ledger")["c"]
    posted = db.one("SELECT COALESCE(SUM(amount),0) c FROM cash_flows WHERE flow_id IN (SELECT flow_id FROM "
                    "sleeve_flow_posts)")["c"]
    if abs(led - posted) > CASH_TOL:
        issues.append({"kind": "SLEEVE_LEDGER_MISMATCH", "subject": "cash",
                       "detail": f"sum of sleeve cash {led:.6f} != attributed broker cash flows {posted:.6f}"})
    bu = broker_units(snapshot, engine_ids)
    su = {}
    for b in balances(db).values():
        for i, u in b["units"].items():
            su[i] = su.get(i, 0.0) + u
            if u < -UNIT_TOL:
                issues.append({"kind": "SLEEVE_LEDGER_MISMATCH", "subject": f"neg:{i}",
                               "detail": f"a sleeve holds negative units {u:.6f} of instrument {i}"})
    for i in sorted(set(bu) | set(su)):
        if abs(bu.get(i, 0.0) - su.get(i, 0.0)) > UNIT_TOL * max(1, len(snapshot.positions)):
            issues.append({"kind": "SLEEVE_LEDGER_MISMATCH", "subject": f"units:{i}",
                           "detail": f"instrument {i}: sleeves hold {su.get(i, 0.0):.6f} units, the engine's broker "
                                     f"positions {bu.get(i, 0.0):.6f}"})
    return issues


# ----------------------------------------------------------------------------- performance
def record_daily(db, session: str, val: dict, now: str) -> dict:
    out = {}
    top = db.one("SELECT COALESCE(MAX(seq),0) s FROM sleeve_ledger")["s"]
    for s, v in sorted(val["sleeves"].items()):
        if v["equity"] is None or db.one("SELECT 1 FROM sleeve_daily WHERE session=? AND sleeve=?", (session, s)):
            continue
        last = db.get(f"sleeve_daily_seq:{s}", 0)
        f = db.one("SELECT COALESCE(SUM(cash),0) c FROM sleeve_ledger WHERE sleeve=? AND external=1 AND seq>? AND "
                   "seq<=?", (s, last, top))["c"]
        prev = db.one("SELECT * FROM sleeve_daily WHERE sleeve=? AND session<? ORDER BY session DESC LIMIT 1",
                      (s, session))
        e = v["equity"]
        if prev and prev["equity"]:
            pnl = e - prev["equity"] - f
            twr = prev["twr_index"] * (e - f) / prev["equity"]
            hwm = max(prev["hwm"], twr)
        else:
            pnl, twr, hwm = 0.0, 1.0, 1.0
        dd = twr / hwm - 1.0
        hold = {str(i): {"units": h["units"], "value": h["value"]} for i, h in v["holdings"].items()}
        db.x("INSERT INTO sleeve_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (session, s, now, e, v["cash"], canonical_json(hold), round(f, 6), round(pnl, 6), twr, hwm, dd))
        db.put(f"sleeve_daily_seq:{s}", top)
        out[s] = {"equity": e, "external_flow": round(f, 6), "pnl": round(pnl, 6), "twr_index": twr, "drawdown": dd}
    return out


def performance(db) -> dict:
    out = {}
    for s in sorted({r["sleeve"] for r in db.q("SELECT DISTINCT sleeve FROM sleeve_daily")}):
        rows = [dict(r) for r in db.q("SELECT * FROM sleeve_daily WHERE sleeve=? ORDER BY session", (s,))]
        ext = db.one("SELECT COALESCE(SUM(cash),0) c FROM sleeve_ledger WHERE sleeve=? AND external=1", (s,))["c"]
        out[s] = {"sessions": len(rows), "first": rows[0]["session"], "last": rows[-1]["session"],
                  "equity": rows[-1]["equity"], "net_external_capital": round(ext, 6),
                  "cumulative_pnl": round(sum(r["pnl"] for r in rows), 6), "twr_return": rows[-1]["twr_index"] - 1,
                  "max_drawdown": min(r["drawdown"] for r in rows)}
    return out


def limit_checks(db, cfg: dict, session: str, val: dict, now: str) -> list:
    """Per-sleeve daily-loss and drawdown limits -> a NEW_EXPOSURE halt confined to that sleeve.

    The reference is the sleeve's MOST RECENT daily record (the last close, possibly of `session` itself) and the
    external flows posted after that record. (Rehearsal finding 2026-10-29: comparing with an older record while the
    flows since it had already been absorbed by a newer record turned a 50/50 -> 30/70 reallocation into a false -40%
    'daily loss' and halted the sleeve.)"""
    r, out = cfg["risk"], []
    for s, v in val["sleeves"].items():
        last = db.get(f"sleeve_daily_seq:{s}", 0)
        prev = db.one("SELECT * FROM sleeve_daily WHERE sleeve=? AND session<=? ORDER BY session DESC LIMIT 1",
                      (s, session))
        if not prev or not prev["equity"] or v["equity"] is None:
            continue
        f = db.one("SELECT COALESCE(SUM(cash),0) c FROM sleeve_ledger WHERE sleeve=? AND external=1 AND seq>?",
                   (s, last))["c"]
        ret = (v["equity"] - f) / prev["equity"] - 1.0
        if ret <= -r["daily_loss_halt_pct"]:
            out.append(halts.raise_halt(db, "DAILY_LOSS_LIMIT", "NEW_EXPOSURE", f"sleeve {s} return since last close "
                                        f"{ret:.2%} <= -{r['daily_loss_halt_pct']:.0%}", now, subject=session,
                                        sleeve=s))
        twr = prev["twr_index"] * (v["equity"] - f) / prev["equity"]
        if twr / max(prev["hwm"], twr) - 1.0 <= -r["drawdown_halt_pct"]:
            out.append(halts.raise_halt(db, "DRAWDOWN_LIMIT", "NEW_EXPOSURE", f"sleeve {s} drawdown "
                                        f"{twr / max(prev['hwm'], twr) - 1:.2%}", now, subject="drawdown", sleeve=s))
    return out


# ----------------------------------------------------------------------------- pool allocation / reallocation
def allocate_pool(db, approved_by: str, now: str, ref: str) -> dict:
    """Move the _UNALLOCATED pool (e.g. takeover proceeds) into the sleeves by weight. Audited, idempotent."""
    pool = balances(db).get(UNALLOCATED, {"cash": 0.0, "units": {}})
    if pool["units"]:
        raise EngineError("the unallocated pool holds units; it can only hold cash")
    amt = round(pool["cash"], 6)
    if amt <= 0:
        return {}
    parts = split(amt, weights(db), 6)
    with db.tx():
        post(db, UNALLOCATED, "ALLOCATE_OUT", now, ref=ref, cash=-amt, detail={"approved_by": approved_by})
        for s, a in parts.items():
            post(db, s, "ALLOCATE_IN", now, ref=ref, cash=a, detail={"approved_by": approved_by})
        db.audit("POOL_ALLOCATED", {"amount": amt, "parts": parts, "approved_by": approved_by, "ref": ref}, ts=now)
    for s, a in parts.items():
        if a > 0:
            request_catch_up(db, s, "pool allocation", ref, now)
    return parts


def reallocation_preview(db, new_weights: dict, val: dict, now: str) -> dict:
    """Controlled reallocation: new target weights -> cash transfers between sleeves. A donor without enough cash
    goes negative and its own next rebalance sells to restore it (the recipient's opens are capped by the broker's
    available cash). No broker order is created by the reallocation itself."""
    cur = weights(db)
    if sorted(new_weights) != sorted(cur):
        raise EngineError(f"reallocation must keep the same sleeves {sorted(cur)} (adding/removing a sleeve is a new "
                          "portfolio activation)")
    if abs(sum(new_weights.values()) - 1.0) > 1e-9 or any(v <= 0 for v in new_weights.values()):
        raise EngineError("new weights must be > 0 and sum to 1")
    if not val["complete"]:
        raise EngineError("sleeve equity is incomplete (missing broker values); reallocation refused")
    eq = {s: val["sleeves"][s]["equity"] for s in cur}
    tot = sum(eq.values())
    transfers = {s: round(new_weights[s] * tot - eq[s], 6) for s in cur}
    body = {"from_weights": cur, "to_weights": new_weights, "equity": eq, "transfers": transfers, "created_at": now,
            "state": fingerprint({s: (v["cash"], v["holdings_value"]) for s, v in val["sleeves"].items()})}
    h = fingerprint(body)[7:23]
    db.put(f"realloc_preview:{h}", body)
    db.audit("REALLOCATION_PREVIEWED", {"hash": h, **body}, ts=now)
    return {"preview_hash": h, **body}


def restructure_preview(db, new_weights: dict, val: dict, specs: dict, registry: dict, now: str) -> dict:
    """A NEW portfolio activation inside the same database: sleeves may be added or retired (e.g. the 50/50 portfolio
    -> one 100% sleeve). Only cash moves: a retiring sleeve must hold no units, and nothing may be in flight
    (unresolved intents, reserved opens). The ledger, decisions and audit history of every sleeve are kept; a retired
    sleeve simply ends at zero cash. No broker order is created; a new sleeve starts with fresh strategy state, so its
    first decision is a full acquisition from its cash."""
    cur = weights(db)
    if not cur:
        raise EngineError("no multi-strategy portfolio is active")
    if abs(sum(new_weights.values()) - 1.0) > 1e-9 or any(v <= 0 for v in new_weights.values()):
        raise EngineError("new weights must be > 0 and sum to 1")
    for sid in new_weights:
        spec = specs.get(sid)
        if spec is None or not spec["enabled"]:
            raise EngineError(f"{sid} has no enabled strategy YAML")
        if registry[spec["implementation"]].status != "AVAILABLE":
            raise EngineError(f"{sid}: implementation unavailable")
    if not val["complete"]:
        raise EngineError("sleeve equity is incomplete (missing broker values); restructure refused")
    bal = balances(db)
    retired = sorted(s for s in cur if s not in new_weights)
    held = {s: bal.get(s, {}).get("units") for s in retired if bal.get(s, {}).get("units")}
    if held:
        raise EngineError(f"retiring sleeve(s) still hold units {held}: they must be sold by their own strategy first")
    if any(abs(v) > CASH_TOL for v in reserved(db).values()):
        raise EngineError("opens are in flight (reserved cash); restructure refused")
    if db.one(f"SELECT 1 FROM intents WHERE status IN ({','.join('?' * len(UNRESOLVED))})", tuple(UNRESOLVED)):
        raise EngineError("an order intent is unresolved; restructure refused")
    if abs(bal.get(UNALLOCATED, {"cash": 0.0})["cash"]) > CASH_TOL:
        raise EngineError("the unallocated pool is not empty; allocate it first")
    union = sorted(set(cur) | set(new_weights))
    eq = {s: (val["sleeves"][s]["equity"] if s in val["sleeves"] else 0.0) for s in union}
    tot = sum(eq.values())
    transfers = {s: round(new_weights.get(s, 0.0) * tot - eq[s], 6) for s in union}
    body = {"kind": "RESTRUCTURE", "from_weights": cur, "to_weights": dict(new_weights), "retired": retired,
            "added": sorted(s for s in new_weights if s not in cur), "equity": eq, "transfers": transfers,
            "strategies": {s: {"fingerprint": specs[s]["fingerprint"], "version": specs[s]["version"],
                               "module_sha256": registry[specs[s]["implementation"]].source_sha256}
                           for s in new_weights},
            "created_at": now,
            "state": fingerprint({s: (v["cash"], v["holdings_value"]) for s, v in val["sleeves"].items()})}
    h = fingerprint(body)[7:23]
    db.put(f"restructure_preview:{h}", body)
    db.audit("RESTRUCTURE_PREVIEWED", {"hash": h, **body}, ts=now)
    return {"preview_hash": h, **body}


def restructure(db, preview_hash: str, approved_by: str, val: dict, now: str) -> dict:
    if not approved_by.strip():
        raise EngineError("--approved-by is required")
    body = db.get(f"restructure_preview:{preview_hash}")
    if not body or body.get("applied"):
        raise EngineError(f"no unapplied restructure preview {preview_hash}")
    if dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(body["created_at"]) > REALLOC_TTL:
        raise EngineError("restructure preview expired; preview again")
    state = fingerprint({s: (v["cash"], v["holdings_value"]) for s, v in val["sleeves"].items()})
    if state != body["state"] or weights(db) != body["from_weights"]:
        raise EngineError("sleeve state changed since the preview; preview again")
    bal = balances(db)
    if any(bal.get(s, {}).get("units") for s in body["retired"]):
        raise EngineError("a retiring sleeve holds units; preview again")
    give = {s: -x for s, x in body["transfers"].items() if x < 0}
    get = {s: x for s, x in body["transfers"].items() if x > 0}
    amt = round(min(sum(give.values()), sum(get.values())), 6)
    ref = f"restructure:{preview_hash}"
    with db.tx():
        if amt > 0:
            for s, a in split(amt, give, 6).items():
                post(db, s, "REALLOC_OUT", now, ref=ref, cash=-a, detail={"approved_by": approved_by})
            for s, a in split(amt, get, 6).items():
                post(db, s, "REALLOC_IN", now, ref=ref, cash=a, detail={"approved_by": approved_by})
        rec = portfolio(db)
        history = list(rec.get("history", []))
        history.append({"sleeves": rec["sleeves"], "strategies": rec["strategies"], "until": now,
                        "replaced_by": ref, "approved_by": approved_by})
        rec["sleeves"] = [{"strategy": s, "weight": body["to_weights"][s]} for s in sorted(body["to_weights"])]
        rec["strategies"] = {s: rec["strategies"].get(s) or body["strategies"][s] for s in body["to_weights"]}
        rec["history"] = history
        rec["restructured_at"], rec["restructured_by"] = now, approved_by
        db.put("portfolio", rec)
        db.put(f"restructure_preview:{preview_hash}", {**body, "applied": now})
        db.audit("PORTFOLIO_RESTRUCTURED", {"hash": preview_hash, "amount": amt, "from_weights": body["from_weights"],
                                            "to_weights": body["to_weights"], "retired": body["retired"],
                                            "added": body["added"], "approved_by": approved_by}, ts=now)
    for s, x in get.items():
        if x > 0:
            request_catch_up(db, s, "restructure", ref, now)
    return {"status": "APPLIED", "amount": amt, "weights": body["to_weights"], "retired": body["retired"]}


def reallocate(db, preview_hash: str, approved_by: str, val: dict, now: str) -> dict:
    if not approved_by.strip():
        raise EngineError("--approved-by is required")
    body = db.get(f"realloc_preview:{preview_hash}")
    if not body or body.get("applied"):
        raise EngineError(f"no unapplied reallocation preview {preview_hash}")
    if dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(body["created_at"]) > REALLOC_TTL:
        raise EngineError("reallocation preview expired; preview again")
    state = fingerprint({s: (v["cash"], v["holdings_value"]) for s, v in val["sleeves"].items()})
    if state != body["state"]:
        raise EngineError("sleeve state changed since the preview; preview again")
    give = {s: -x for s, x in body["transfers"].items() if x < 0}
    get = {s: x for s, x in body["transfers"].items() if x > 0}
    amt = round(min(sum(give.values()), sum(get.values())), 6)
    with db.tx():
        for s, a in split(amt, give, 6).items():
            post(db, s, "REALLOC_OUT", now, ref=f"realloc:{preview_hash}", cash=-a, detail={"approved_by": approved_by})
        for s, a in split(amt, get, 6).items():
            post(db, s, "REALLOC_IN", now, ref=f"realloc:{preview_hash}", cash=a, detail={"approved_by": approved_by})
        rec = portfolio(db)
        rec["sleeves"] = [{"strategy": s, "weight": body["to_weights"][s]} for s in sorted(body["to_weights"])]
        rec["reallocated_at"], rec["reallocated_by"] = now, approved_by
        db.put("portfolio", rec)
        db.put(f"realloc_preview:{preview_hash}", {**body, "applied": now})
        db.audit("REALLOCATED", {"hash": preview_hash, "amount": amt, "to_weights": body["to_weights"],
                                 "approved_by": approved_by}, ts=now)
    for s, x in get.items():
        if x > 0:
            request_catch_up(db, s, "reallocation", f"realloc:{preview_hash}", now)
    return {"status": "APPLIED", "amount": amt, "weights": body["to_weights"]}
