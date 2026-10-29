"""Full-account takeover: a read-only PREVIEW and a durable, approval-gated state machine.

    DISCOVER -> PREVIEW -> APPROVAL_REQUIRED -> QUIESCE_OLD_WRITER -> VERIFY_ACCOUNT -> CLOSE_EXISTING -> RECONCILE
             -> ALLOCATE -> ACTIVATE -> MONITOR
    side exits: CANCELED (operator), INVALIDATED (the account changed materially after approval), HALTED (a close
    failed or its outcome is unknown); all three are final: a new preview and new approvals are required.

What this module will and will not do (docs/TAKEOVER_STATE_MACHINE.md):
  * preview() reads the account through ReadOnlyView (any write method raises before reaching the broker); it
    mutates nothing at the broker and never claims, adopts or closes a position;
  * every transition is one explicit operator command (`takeover advance`), persisted (takeover_runs,
    takeover_events, audit chain) BEFORE its effect; the service loop never advances a takeover;
  * separate owner approvals, each bound to {account, release, plan hash, positions + units, max cost, max
    exposure, expiry}: DISABLE_OLD_WRITER, CLOSE_POSITIONS, ACCEPT_LOSSES_AND_FEES, ACTIVATE_STRATEGY, ENABLE_LIVE.
    Approving a preview authorizes nothing by itself; each approval is consumed by exactly one transition;
  * the engine never disables Gold Miner (or any other writer): QUIESCE_OLD_WRITER waits for recorded evidence
    that the owner disabled it; running this service is never evidence of ownership;
  * closing real positions needs TAKEOVER_EXECUTION_RELEASED (a code constant, False in this release) AND a write
    capability; in this release CLOSE_EXISTING can only run against the simulated broker (tests and rehearsals);
  * ENABLE_LIVE is recorded but always refused (LiveExecutionNotAvailable).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib

from . import halts, sleeves
from .canonical import canonical_json, fingerprint, short_id
from .configuration import takeover_settings
from .errors import BrokerError, BrokerUnavailable, DataError, EngineError, LiveExecutionNotAvailable, \
    ShadowWriteViolation, StrategyError
from .order_planner import Leg
from .order_state import UNRESOLVED

TAKEOVER_EXECUTION_RELEASED = False     # real-account closes: a separately reviewed release, never configuration
STATES = ("DISCOVER", "PREVIEW", "APPROVAL_REQUIRED", "QUIESCE_OLD_WRITER", "VERIFY_ACCOUNT", "CLOSE_EXISTING",
          "RECONCILE", "ALLOCATE", "ACTIVATE", "MONITOR")
FINAL = ("CANCELED", "INVALIDATED", "HALTED", "MONITOR")
TRANSITIONS = {
    "DISCOVER": {"PREVIEW"}, "PREVIEW": {"APPROVAL_REQUIRED"},
    "APPROVAL_REQUIRED": {"QUIESCE_OLD_WRITER"}, "QUIESCE_OLD_WRITER": {"VERIFY_ACCOUNT"},
    "VERIFY_ACCOUNT": {"CLOSE_EXISTING"}, "CLOSE_EXISTING": {"RECONCILE"}, "RECONCILE": {"ALLOCATE"},
    "ALLOCATE": {"ACTIVATE"}, "ACTIVATE": {"MONITOR"},
}
APPROVAL_KINDS = ("DISABLE_OLD_WRITER", "CLOSE_POSITIONS", "ACCEPT_LOSSES_AND_FEES", "ACTIVATE_STRATEGY",
                  "ENABLE_LIVE")
READ_METHODS = ("inventory", "account_snapshot", "quotes", "account_identity", "capabilities", "eligibility",
                "order_status", "cash_transactions", "name")


class TakeoverError(EngineError):
    """A takeover step was refused (the reasons are in the message and the audit log)."""


class ReadOnlyView:
    """The preview's only handle on the broker: read methods pass through, anything else raises BEFORE I/O."""

    def __init__(self, reader):
        self._r = reader

    def __getattr__(self, name):
        if name not in READ_METHODS:
            raise ShadowWriteViolation(f"takeover preview is read-only: {name} refused")
        return getattr(self._r, name)


def release_id() -> str:
    """Hash of this release's engine source (line-ending normalised): approvals are bound to the exact code."""
    root = pathlib.Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        h.update(str(p.relative_to(root)).replace("\\", "/").encode())
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def _age(t, now: dt.datetime):
    try:
        x = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return (now - x).total_seconds() if x.tzinfo else None
    except ValueError:
        return None


# ----------------------------------------------------------------------------- preview
def preview(e, now: dt.datetime, *, context_quotes: dict | None = None, reader=None) -> dict:
    """Build, store and return a takeover preview. `context_quotes` ({instrument_id: (bid, ask, as_of)}) may supply
    MARKET-CONTEXT prices when the broker gives neither P&L nor a quote (labelled, never broker evidence)."""
    nows = now.astimezone(dt.timezone.utc).isoformat()
    t = takeover_settings(e.cfg)
    v = ReadOnlyView(reader or e.broker)
    try:
        inv = v.inventory()
    except (BrokerUnavailable, BrokerError) as exc:
        raise TakeoverError(f"DISCOVER failed: broker account unreadable ({exc}); no preview") from exc
    try:
        acct = v.account_identity()
    except (BrokerUnavailable, BrokerError, AttributeError):
        acct = None
    iids = sorted({p.instrument_id for p in inv.positions})
    try:
        quotes = v.quotes(iids) if iids else {}
    except (BrokerUnavailable, BrokerError):
        quotes = {}
    owners = {r["position_id"]: dict(r) for r in e.db.q("SELECT * FROM ownership")}
    max_q = e.cfg["risk"]["max_quote_age_seconds"]
    slip = t["close_slippage_bp"] / 1e4
    fin = t["financing_annual_rate_observed"]
    rows, risks = [], []
    for p in sorted(inv.positions, key=lambda p: p.position_id):
        sym = e.id_to_symbol.get(p.instrument_id) or p.symbol or str(p.instrument_id)
        q, basis, age = quotes.get(p.instrument_id), None, None
        bid = ask = None
        if q is not None and q.bid > 0 and q.ask >= q.bid:
            bid, ask, age = q.bid, q.ask, _age(q.quote_time, now)
            basis = "BROKER_QUOTE"
        elif context_quotes and p.instrument_id in context_quotes:
            bid, ask, asof = context_quotes[p.instrument_id]
            age, basis = _age(asof, now), "MARKET_CONTEXT_NOT_BROKER"
        own = owners.get(p.position_id)
        owner = own["owner"] if own else "UNCLASSIFIED"
        sign = 1.0 if p.is_buy else -1.0
        if p.unrealized_pnl is not None:
            proceeds, pbasis = p.amount + p.unrealized_pnl, "BROKER_UNREALIZED_PNL"
        elif bid is not None:
            px = bid if p.is_buy else ask
            proceeds, pbasis = p.amount + sign * p.units * (px - p.open_rate), basis
        else:
            proceeds, pbasis = None, "UNAVAILABLE"
        mid = (bid + ask) / 2 if bid is not None else None
        notional = p.units * (mid if mid else p.open_rate)
        spread = p.units * (ask - bid) / 2 if bid is not None else None
        slippage = notional * slip
        lev_or_short = p.leverage > 1 or not p.is_buy
        rate = fin.get(sym)
        financing = ({"daily_usd_estimate": round(notional * rate / 365, 4), "annual_rate_observed": rate}
                     if lev_or_short and rate is not None else
                     {"note": "leveraged/short CFD: overnight fees apply; no per-position rate from the API"}
                     if lev_or_short else {"note": "x1 long CFD: eToro publishes 0 overnight fee (observed 0 on "
                                                    "SOXS/SQQQ/QQQ)"})
        separate = []
        if owner == "ENGINE":
            action = "KEEP_ENGINE_SLEEVE"
        else:
            if proceeds is None:
                separate.append("no broker P&L and no quote: proceeds cannot be estimated")
            if pbasis == "MARKET_CONTEXT_NOT_BROKER":
                separate.append("estimate uses market-context prices, not broker evidence; re-preview with a broker "
                                "quote before approval")
            if age is not None and age > max_q:
                separate.append(f"quote {age:.0f}s old (> {max_q}s): a fresh broker quote is required")
            action = "SEPARATE_HANDLING" if separate else "CLOSE"
        rows.append({"position_id": p.position_id, "instrument_id": p.instrument_id, "symbol": sym,
                     "direction": "LONG" if p.is_buy else "SHORT", "leverage": p.leverage, "units": p.units,
                     "amount": p.amount, "open_rate": p.open_rate, "exposure_usd": round(notional, 2),
                     "settlement": p.settlement, "owner": owner, "unrealized_pnl": p.unrealized_pnl,
                     "fees_to_date": p.fees, "quote": {"bid": bid, "ask": ask, "age_s": age, "basis": basis},
                     "est_close_proceeds": None if proceeds is None else round(proceeds, 2),
                     "proceeds_basis": pbasis,
                     "est_realized_pnl": None if proceeds is None else round(proceeds - p.amount, 2),
                     "est_spread_cost": None if spread is None else round(spread, 2),
                     "slippage_allowance": round(slippage, 2),
                     "est_proceeds_worst": None if proceeds is None else round(proceeds - slippage, 2),
                     "financing": financing, "action": action, "separate_reasons": separate})
        if lev_or_short and owner != "ENGINE":
            risks.append(f"{sym} {p.position_id}: {'short' if not p.is_buy else 'long'} x{p.leverage} CFD, exposure "
                         f"~{notional:,.0f} USD: closing at the open can gap; overnight financing accrues until closed")
    mirrors = [{"mirror_id": m.mirror_id, "parent_username": m.parent_username, "invested": m.invested,
                "value": m.value, "positions": m.positions, "action": "SEPARATE_HANDLING",
                "separate_reasons": ["copy-trading mirror: stop copying in the eToro app (no API path in this engine)"]}
               for m in inv.mirrors]
    pending = [{"order_id": o.order_id, "instrument_id": o.instrument_id, "amount": o.amount, "kind": o.kind,
                "action": "SEPARATE_HANDLING", "separate_reasons": ["pending order: cancel in the eToro app (this "
                                                                    "engine has no cancel capability)"]}
               for o in inv.pending_orders]
    writers = [dict(w) for w in t["known_writers"]]
    foreign = [r for r in rows if r["owner"] != "ENGINE"]
    if foreign and not writers:
        writers = [{"name": "unidentified-foreign-writer", "description": "foreign positions exist but no writer is "
                                                                          "declared in takeover.known_writers"}]
    recent = e.db.q("SELECT payload FROM audit_log WHERE kind='HALT_RAISED' AND ts>=? ORDER BY seq",
                    ((now - dt.timedelta(days=7)).isoformat(),))
    ext_events = sum(1 for r in recent if json.loads(r["payload"])["kind"].startswith("EXTERNAL"))
    for w in writers:
        w["status"] = "ACTIVE (assumed: no disable evidence recorded)"
        w["external_events_7d"] = ext_events
    closes = [r for r in rows if r["action"] == "CLOSE"]
    cash = inv.available_cash
    est = round(cash + sum(r["est_close_proceeds"] for r in closes), 2)
    worst = round(cash + sum(r["est_proceeds_worst"] for r in closes), 2)
    sep_est = [r for r in rows if r["action"] == "SEPARATE_HANDLING" and r["est_close_proceeds"] is not None]
    indicative = round(est + sum(r["est_close_proceeds"] for r in sep_est), 2)
    remaining = {"positions": [r["position_id"] for r in rows if r["action"] != "CLOSE"],
                 "exposure_usd": round(sum(r["exposure_usd"] for r in rows if r["action"] != "CLOSE"), 2),
                 "mirrors_invested": round(sum(m["invested"] for m in mirrors), 2),
                 "pending_orders_amount": round(sum(o["amount"] for o in pending), 2)}
    investable = round(max(worst, 0.0) * (1 - t["investable_reserve_pct"]), 2)
    w = sleeves.weights(e.db) or ({s["strategy"]: s["weight"] for s in e.cfg["portfolio"]["sleeves"]}
                                  if e.multi else {})
    allocation = {s: round(investable * x, 2) for s, x in w.items()} or {"single_strategy": investable}
    holdings = proposed_holdings(e, allocation, now)
    risks += [
        "lost close response (blocker B3): eToro returns no reference for a close order; a lost response is "
        "recovered from position evidence but its proceeds need an audited manual classification",
        "the old writer must be disabled by the owner and verified before any close; until then the account is shared",
    ]
    if any(r["proceeds_basis"] != "BROKER_UNREALIZED_PNL" for r in foreign):
        risks.append("broker unrealized P&L missing for some position: proceeds are quote/context estimates")
    loss = sum(min(0.0, r["est_realized_pnl"] or 0.0) for r in closes)
    costs = sum((r["est_spread_cost"] or 0.0) + r["slippage_allowance"] for r in closes)
    state_hash = fingerprint({"credit": inv.credit, "pending": sorted(o.order_id for o in inv.pending_orders),
                              "positions": sorted((p.position_id, p.units, p.amount) for p in inv.positions),
                              "mirrors": sorted(m.mirror_id for m in inv.mirrors)})
    body = {"created_at": nows, "account_id": acct, "release_id": release_id(), "mode": e.mode,
            "broker_source": inv.source, "snapshot_as_of": inv.as_of, "state_hash": state_hash,
            "cash": {"credit": inv.credit, "available_cash": cash, "pending_open_amount": inv.pending_open_amount},
            "positions": rows, "mirrors": mirrors, "pending_orders": pending, "writers": writers,
            "capital": {"est_cash_after_closes": est, "worst_case_cash_after_closes": worst,
                        "indicative_cash_if_separate_items_closed_at_estimate": indicative,
                        "indicative_realized_pnl_separate_items": round(sum(r["est_realized_pnl"] for r in sep_est), 2),
                        "investable_reserve_pct": t["investable_reserve_pct"], "investable": investable,
                        "est_realized_loss_usd": round(loss, 2), "est_costs_usd": round(costs, 2),
                        "max_cost_floor_usd": round(-loss + costs, 2)},
            "remaining_after_takeover": remaining, "allocation": allocation, "proposed_holdings": holdings,
            "risks": risks, "broker_mutations": 0}
    ph = fingerprint(body)[7:23]
    run_id = "TK" + short_id(ph, nows, n=14)
    with e.db.tx():
        e.db.con.execute("INSERT INTO takeover_runs VALUES(?,?,?,?,?,?,?,?,?)",
                         (run_id, nows, nows, "DISCOVER", acct, body["release_id"], ph, canonical_json(body), None))
        e.db.audit("TAKEOVER_DISCOVERED", {"run_id": run_id, "account_id": acct, "positions": len(rows),
                                           "mirrors": len(mirrors), "pending": len(pending)}, ts=nows)
    _move(e.db, run_id, "PREVIEW", nows, {"plan_hash": ph})
    _move(e.db, run_id, "APPROVAL_REQUIRED", nows, {"plan_hash": ph, "note": "a preview authorizes nothing"})
    return {"run_id": run_id, "plan_hash": ph, "state": "APPROVAL_REQUIRED", **body}


def proposed_holdings(e, allocation: dict, now: dt.datetime) -> dict:
    """Each sleeve's latest decision applied to its proposed capital (what the engine WOULD target next)."""
    from .portfolio import allocate
    out, agg = {}, {}
    session = e.cal.last_completed_session(now)
    for sid, cap in allocation.items():
        spec = e.specs.get(sid) or (e.active_spec() if sid == "single_strategy" else None)
        if spec is None:
            continue
        try:
            _m, dec = e.compute_decision(spec, session, now, persist=False)
        except (DataError, StrategyError) as exc:
            out[sid] = {"status": f"UNAVAILABLE: {exc}"}
            continue
        al = allocate(dec, spec, cap)
        out[sid] = {"decision_session": session.isoformat(), "targets_usd": al.targets_usd, "cash_usd": al.cash_usd,
                    "blocked_to_cash": al.blocked}
        for s, x in al.targets_usd.items():
            agg[s] = round(agg.get(s, 0.0) + x, 2)
    return {"by_sleeve": out, "aggregate_targets_usd": agg}


# ----------------------------------------------------------------------------- state machine primitives
def run(db, run_id: str) -> dict:
    r = db.one("SELECT * FROM takeover_runs WHERE run_id=?", (run_id,))
    if not r:
        raise TakeoverError(f"no takeover run {run_id}")
    d = dict(r)
    d["plan"] = json.loads(d["plan"]) if d["plan"] else None
    return d


def _move(db, run_id: str, to: str, now: str, detail: dict) -> None:
    with db.tx():
        cur = db.one("SELECT state FROM takeover_runs WHERE run_id=?", (run_id,))["state"]
        if to not in TRANSITIONS.get(cur, set()) and to not in ("CANCELED", "INVALIDATED", "HALTED"):
            raise TakeoverError(f"{run_id}: {cur} -> {to} is not allowed")
        if cur in FINAL:
            raise TakeoverError(f"{run_id} is {cur} (final)")
        db.con.execute("UPDATE takeover_runs SET state=?, updated_at=? WHERE run_id=?", (to, now, run_id))
        db.con.execute("INSERT INTO takeover_events(run_id,ts,from_state,to_state,detail) VALUES(?,?,?,?,?)",
                       (run_id, now, cur, to, canonical_json(detail)))
        db.audit("TAKEOVER_TRANSITION", {"run_id": run_id, "from": cur, "to": to, "detail": detail}, ts=now)


def _void_approvals(db, run_id: str, status: str, reason: str, now: str) -> None:
    with db.tx():
        db.con.execute("UPDATE takeover_approvals SET status=?, status_reason=? WHERE run_id=? AND status='VALID'",
                       (status, reason, run_id))
        db.audit("TAKEOVER_APPROVALS_VOIDED", {"run_id": run_id, "status": status, "reason": reason}, ts=now)


def _fail(e, run_id: str, to: str, reason: str, now: str):
    _move(e.db, run_id, to, now, {"reason": reason})
    _void_approvals(e.db, run_id, "INVALIDATED", f"run {to}: {reason}", now)
    raise TakeoverError(f"{run_id} {to}: {reason}")


def approve(e, run_id: str, kind: str, approved_by: str, now: dt.datetime, *, max_cost_usd: float | None = None,
            max_exposure_usd: float | None = None, ttl_hours: float | None = None) -> dict:
    nows = now.astimezone(dt.timezone.utc).isoformat()
    if kind not in APPROVAL_KINDS:
        raise TakeoverError(f"approval kind must be one of {APPROVAL_KINDS}")
    if not approved_by.strip():
        raise TakeoverError("--approved-by is required")
    r = run(e.db, run_id)
    if r["state"] in FINAL:
        raise TakeoverError(f"{run_id} is {r['state']}; no approval can be recorded")
    plan = r["plan"]
    if plan["account_id"] is None:
        raise TakeoverError("the broker account identity is unverified; approvals cannot be bound to an account")
    t = takeover_settings(e.cfg)
    ttl = min(ttl_hours or t["approval_ttl_hours"], t["approval_ttl_hours"])
    closes = [x for x in plan["positions"] if x["action"] == "CLOSE"]
    if kind == "ACCEPT_LOSSES_AND_FEES":
        if max_cost_usd is None:
            raise TakeoverError("ACCEPT_LOSSES_AND_FEES needs --max-cost (USD of realized loss + costs accepted)")
        if max_cost_usd + 1e-9 < plan["capital"]["max_cost_floor_usd"]:
            raise TakeoverError(f"--max-cost {max_cost_usd} is below the preview's estimated loss + costs "
                                f"{plan['capital']['max_cost_floor_usd']}")
    if kind == "CLOSE_POSITIONS":
        if not closes:
            raise TakeoverError("the plan has no position eligible for automatic closure")
        if max_exposure_usd is None:
            raise TakeoverError("CLOSE_POSITIONS needs --max-exposure (largest total exposure the closes may cover)")
    binding = {"account_id": plan["account_id"], "release_id": r["release_id"], "plan_hash": r["plan_hash"],
               "positions": [[x["position_id"], x["units"]] for x in closes], "max_cost_usd": max_cost_usd,
               "max_exposure_usd": max_exposure_usd,
               "expires_at": (now + dt.timedelta(hours=ttl)).astimezone(dt.timezone.utc).isoformat()}
    aid = "A" + short_id(run_id, kind, nows, n=15)
    with e.db.tx():
        e.db.con.execute("INSERT INTO takeover_approvals VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (aid, run_id, kind, r["plan_hash"], canonical_json(binding), approved_by, nows,
                          binding["expires_at"], "VALID", None))
        e.db.audit("TAKEOVER_APPROVAL", {"approval_id": aid, "run_id": run_id, "kind": kind,
                                         "approved_by": approved_by, "binding": binding}, ts=nows)
    return {"approval_id": aid, "kind": kind, "binding": binding}


def revoke(e, approval_id: str, by: str, reason: str, now: dt.datetime) -> None:
    nows = now.astimezone(dt.timezone.utc).isoformat()
    with e.db.tx():
        cur = e.db.con.execute("UPDATE takeover_approvals SET status='REVOKED', status_reason=? WHERE approval_id=? "
                               "AND status='VALID'", (f"{by}: {reason}", approval_id))
        if not cur.rowcount:
            raise TakeoverError(f"no VALID approval {approval_id}")
        e.db.audit("TAKEOVER_APPROVAL_REVOKED", {"approval_id": approval_id, "by": by, "reason": reason}, ts=nows)


def cancel(e, run_id: str, by: str, reason: str, now: dt.datetime) -> None:
    nows = now.astimezone(dt.timezone.utc).isoformat()
    if not by.strip():
        raise TakeoverError("--operator is required")
    _move(e.db, run_id, "CANCELED", nows, {"by": by, "reason": reason})
    _void_approvals(e.db, run_id, "REVOKED", f"run canceled by {by}: {reason}", nows)


def record_writer_evidence(e, run_id: str, writer: str, verdict: str, evidence: str, recorded_by: str,
                           now: dt.datetime) -> None:
    """The owner's evidence that an old writer is DISABLED (or still ACTIVE): e.g. the read-only Gold Miner audit
    showing the unit inactive + kill switch engaged. The engine cannot stop or inspect that service itself."""
    if verdict not in ("DISABLED_VERIFIED", "ACTIVE"):
        raise TakeoverError("verdict must be DISABLED_VERIFIED or ACTIVE")
    if not evidence.strip() or not recorded_by.strip():
        raise TakeoverError("--evidence and --recorded-by are required")
    r = run(e.db, run_id)
    if writer not in {w["name"] for w in r["plan"]["writers"]}:
        raise TakeoverError(f"{writer} is not a writer of this plan: {[w['name'] for w in r['plan']['writers']]}")
    nows = now.astimezone(dt.timezone.utc).isoformat()
    with e.db.tx():
        e.db.con.execute("INSERT INTO takeover_writer_evidence(run_id,writer,ts,verdict,evidence,recorded_by) "
                         "VALUES(?,?,?,?,?,?)", (run_id, writer, nows, verdict, evidence, recorded_by))
        e.db.audit("TAKEOVER_WRITER_EVIDENCE", {"run_id": run_id, "writer": writer, "verdict": verdict,
                                                "evidence": evidence, "recorded_by": recorded_by}, ts=nows)


def _approval(e, run_id: str, kind: str, now: dt.datetime, plan_hash: str) -> dict:
    a = e.db.one("SELECT * FROM takeover_approvals WHERE run_id=? AND kind=? AND status='VALID' ORDER BY created_at "
                 "DESC LIMIT 1", (run_id, kind))
    if not a:
        raise TakeoverError(f"approval {kind} is required (none VALID for {run_id})")
    b = json.loads(a["binding"])
    if dt.datetime.fromisoformat(b["expires_at"]) <= now:
        raise TakeoverError(f"approval {a['approval_id']} ({kind}) expired at {b['expires_at']}; preview again")
    if b["plan_hash"] != plan_hash or a["plan_hash"] != plan_hash:
        raise TakeoverError(f"approval {a['approval_id']} is bound to another plan")
    if b["release_id"] != release_id():
        raise TakeoverError(f"approval {a['approval_id']} is bound to release {b['release_id']}, running "
                            f"{release_id()}")
    return {**dict(a), "binding": b}


def _consume(e, approval_id: str, now: str) -> None:
    e.db.x("UPDATE takeover_approvals SET status='CONSUMED', status_reason=? WHERE approval_id=?",
           (f"used at {now}", approval_id))


def material_changes(e, plan: dict, now: dt.datetime) -> list:
    """Fresh read-only inventory vs the approved plan; any listed change invalidates the plan."""
    t = takeover_settings(e.cfg)
    v = ReadOnlyView(e.broker)
    inv = v.inventory()
    out = []
    try:
        acct = v.account_identity()
    except (BrokerUnavailable, BrokerError, AttributeError):
        acct = None
    if acct != plan["account_id"]:
        out.append(f"account identity {acct} != plan {plan['account_id']}")
    old = {x["position_id"]: x for x in plan["positions"]}
    new = {p.position_id: p for p in inv.positions}
    for pid in sorted(set(old) - set(new)):
        out.append(f"position {pid} is gone")
    for pid in sorted(set(new) - set(old)):
        out.append(f"new position {pid}")
    for pid in sorted(set(old) & set(new)):
        if abs(old[pid]["units"] - new[pid].units) > 1e-6:
            out.append(f"position {pid} units {old[pid]['units']} -> {new[pid].units}")
    if sorted(o.order_id for o in inv.pending_orders) != sorted(o["order_id"] for o in plan["pending_orders"]):
        out.append("pending orders changed")
    if sorted(m.mirror_id for m in inv.mirrors) != sorted(m["mirror_id"] for m in plan["mirrors"]):
        out.append("copy-trading mirrors changed")
    if abs(inv.available_cash - plan["cash"]["available_cash"]) > e.cfg["account"]["cash_tolerance_usd"]:
        out.append(f"available cash {plan['cash']['available_cash']} -> {inv.available_cash}")
    iids = sorted({p.instrument_id for p in inv.positions})
    quotes = v.quotes(iids) if iids else {}
    for pid in sorted(set(old) & set(new)):
        x, p, q = old[pid], new[pid], quotes.get(new[pid].instrument_id)
        if x["action"] != "CLOSE" or q is None or x["est_close_proceeds"] is None:
            continue
        px = q.bid if p.is_buy else q.ask
        now_proceeds = p.amount + (p.unrealized_pnl if p.unrealized_pnl is not None else
                                   (1 if p.is_buy else -1) * p.units * (px - p.open_rate))
        if abs(now_proceeds - x["est_close_proceeds"]) > t["price_tolerance_pct"] * max(x["exposure_usd"], 1.0):
            out.append(f"position {pid} estimated proceeds {x['est_close_proceeds']} -> {now_proceeds:.2f} (beyond "
                       f"{t['price_tolerance_pct']:.1%} of exposure)")
    return out


# ----------------------------------------------------------------------------- advance: one step per command
def advance(e, run_id: str, now: dt.datetime, operator: str) -> dict:
    if not operator.strip():
        raise TakeoverError("--operator is required")
    nows = now.astimezone(dt.timezone.utc).isoformat()
    r = run(e.db, run_id)
    st, plan, ph = r["state"], r["plan"], r["plan_hash"]
    if st in FINAL:
        raise TakeoverError(f"{run_id} is {st} (final)")
    try:
        if st == "APPROVAL_REQUIRED":
            a = _approval(e, run_id, "DISABLE_OLD_WRITER", now, ph)
            _consume(e, a["approval_id"], nows)
            _move(e.db, run_id, "QUIESCE_OLD_WRITER", nows, {"approval": a["approval_id"], "operator": operator,
                                                              "note": "the OWNER disables the old writer; the engine "
                                                                      "does not touch it"})
        elif st == "QUIESCE_OLD_WRITER":
            since = e.db.one("SELECT ts FROM takeover_events WHERE run_id=? AND to_state='QUIESCE_OLD_WRITER'",
                             (run_id,))["ts"]
            missing = []
            for w in plan["writers"]:
                ev = e.db.one("SELECT verdict FROM takeover_writer_evidence WHERE run_id=? AND writer=? AND ts>=? "
                              "ORDER BY id DESC LIMIT 1", (run_id, w["name"], since))
                if not ev or ev["verdict"] != "DISABLED_VERIFIED":
                    missing.append(w["name"])
            if missing:
                raise TakeoverError(f"no DISABLED_VERIFIED evidence (recorded after quiesce started) for {missing}")
            ch = material_changes(e, plan, now)
            if ch:
                _fail(e, run_id, "INVALIDATED", "; ".join(ch), nows)
            _move(e.db, run_id, "VERIFY_ACCOUNT", nows, {"operator": operator, "writers": plan["writers"]})
        elif st == "VERIFY_ACCOUNT":
            return _start_closes(e, run_id, plan, ph, now, operator)
        elif st == "CLOSE_EXISTING":
            return _run_closes(e, run_id, plan, now, operator)
        elif st == "RECONCILE":
            rec = e.reconcile("takeover_reconcile", now)
            if not rec.clean:
                raise TakeoverError("reconciliation not clean: " + "; ".join(i["kind"] for i in rec.issues))
            left = {p.position_id for p in rec.snapshot.positions} & {x["position_id"] for x in plan["positions"]
                                                                       if x["action"] == "CLOSE"}
            if left:
                raise TakeoverError(f"approved positions still open: {sorted(left)}")
            _move(e.db, run_id, "ALLOCATE", nows, {"operator": operator, "credit": rec.snapshot.credit})
        elif st == "ALLOCATE":
            a = _approval(e, run_id, "ACTIVATE_STRATEGY", now, ph)
            rec = e.reconcile("takeover_allocate", now)
            if not rec.clean:
                raise TakeoverError("reconciliation not clean: " + "; ".join(i["kind"] for i in rec.issues))
            foreign = [p.position_id for p in rec.snapshot.positions if p.position_id not in _engine_ids(e)]
            if rec.snapshot.mirrors or rec.snapshot.pending_orders or foreign:
                raise TakeoverError("separately handled items (foreign positions, mirrors, pending orders) are still "
                                    "on the account; exclusive ownership cannot be established")
            parts = sleeves.allocate_pool(e.db, a["approved_by"], nows, ref=f"takeover:{run_id}") if e.multi else {}
            _consume(e, a["approval_id"], nows)
            e.db.put("account_takeover", {"run_id": run_id, "exclusive_verified_at": nows, "by": a["approved_by"]})
            _move(e.db, run_id, "ACTIVATE", nows, {"allocated": parts, "approval": a["approval_id"]})
        elif st == "ACTIVATE":
            _move(e.db, run_id, "MONITOR", nows, {"mode": e.mode, "live": "NOT ENABLED (ENABLE_LIVE is refused in "
                                                                          "this release)"})
        return {"run_id": run_id, "state": run(e.db, run_id)["state"]}
    except TakeoverError as exc:
        e.db.audit("TAKEOVER_BLOCKED", {"run_id": run_id, "state": st, "reason": str(exc)}, ts=nows)
        raise


def _engine_ids(e) -> set:
    from .reconciliation import engine_position_ids
    return engine_position_ids(e.db)


def _execution_capability(e) -> str | None:
    from ..brokers.simulated import SimulatedBroker
    if e.writer is None:
        return f"mode {e.mode}: no write capability (closing positions is impossible in this mode)"
    if not TAKEOVER_EXECUTION_RELEASED and not isinstance(e.writer, SimulatedBroker):
        return "takeover execution against a real broker is not released (TAKEOVER_EXECUTION_RELEASED = False)"
    return None


def _start_closes(e, run_id, plan, ph, now, operator) -> dict:
    nows = now.astimezone(dt.timezone.utc).isoformat()
    cap = _execution_capability(e)
    if cap:
        raise TakeoverError(cap)
    a_close = _approval(e, run_id, "CLOSE_POSITIONS", now, ph)
    a_cost = _approval(e, run_id, "ACCEPT_LOSSES_AND_FEES", now, ph)
    closes = [x for x in plan["positions"] if x["action"] == "CLOSE"]
    bound = sorted(map(tuple, a_close["binding"]["positions"]))
    if bound != sorted((x["position_id"], x["units"]) for x in closes):
        raise TakeoverError("CLOSE_POSITIONS approval does not bind exactly the plan's positions")
    exposure = sum(x["exposure_usd"] for x in closes)
    if exposure > a_close["binding"]["max_exposure_usd"] + 1e-6:
        raise TakeoverError(f"exposure {exposure:.2f} > approved max {a_close['binding']['max_exposure_usd']}")
    if plan["capital"]["max_cost_floor_usd"] > a_cost["binding"]["max_cost_usd"] + 1e-6:
        raise TakeoverError("estimated loss + costs exceed the approved maximum")
    if halts.active(e.db, "ALL"):
        raise TakeoverError("an account halt is active: " + ", ".join(h["kind"] for h in halts.active(e.db, "ALL")))
    if e.db.q(f"SELECT 1 FROM intents WHERE status IN ({','.join('?' * len(UNRESOLVED))})", tuple(UNRESOLVED)):
        raise TakeoverError("unresolved order intents exist")
    if not e.cal.is_open(now):
        raise TakeoverError("market is not open")
    ch = material_changes(e, plan, now)
    if ch:
        _fail(e, run_id, "INVALIDATED", "; ".join(ch), nows)
    rec = e.reconcile("takeover_before_close", now)
    if not rec.clean:
        raise TakeoverError("reconciliation not clean: " + "; ".join(i["kind"] for i in rec.issues))
    for a in (a_close, a_cost):
        _consume(e, a["approval_id"], nows)
    _move(e.db, run_id, "CLOSE_EXISTING", nows, {"approvals": [a_close["approval_id"], a_cost["approval_id"]],
                                                 "operator": operator, "positions": [x["position_id"] for x in closes]})
    return _run_closes(e, run_id, plan, now, operator)


def _legs(plan) -> list:
    return [Leg(i, 1, "CLOSE", x["symbol"], x["instrument_id"], x["est_close_proceeds"], None, x["position_id"],
                "approved takeover close") for i, x in enumerate(x for x in plan["positions"] if x["action"] == "CLOSE")]


def _run_closes(e, run_id, plan, now, operator) -> dict:
    """Idempotent: re-running after a crash resolves (never resubmits) what was already sent."""
    nows = now.astimezone(dt.timezone.utc).isoformat()
    cap = _execution_capability(e)
    if cap:
        raise TakeoverError(cap)
    res = e.executor.execute("T" + run_id, _legs(plan), "TAKEOVER", lambda p: e.reconcile(p, e.now()))
    st = res["status"]
    if st == "HALTED_PHASE1":
        _fail(e, run_id, "HALTED", "a close outcome is unknown or unresolved; nothing is retried; resolve at the "
                                   "broker, then preview again", nows)
    if st in ("PHASE1_REJECTED", "DONE_WITH_REJECTIONS") or "REJECTED" in res.get("phase1", []):
        _fail(e, run_id, "HALTED", "the broker rejected a close (not retried); the remaining account must be "
                                   "previewed and approved again", nows)
    _move(e.db, run_id, "RECONCILE", nows, {"closes": res["phase1"], "operator": operator})
    return {"run_id": run_id, "state": "RECONCILE", "closes": res["phase1"]}


def enable_live(e, run_id: str, now: dt.datetime) -> None:
    raise LiveExecutionNotAvailable("ENABLE_LIVE is recorded as an approval only; live execution cannot be enabled "
                                    "in this release (LIVE_RELEASE_ENABLED = False, mode=live refused)")


# ----------------------------------------------------------------------------- report
def render(p: dict) -> str:
    L = [f"# Account takeover preview {p.get('run_id', '')}", "",
         f"- **State:** {p.get('state')} (a preview authorizes nothing; broker mutations: {p['broker_mutations']})",
         f"- **Account:** {p['account_id'] or 'UNVERIFIED (approvals impossible)'}; source {p['broker_source']}, "
         f"snapshot {p['snapshot_as_of']}; release {p['release_id']}; plan hash `{p.get('plan_hash')}`",
         f"- **Cash:** available {p['cash']['available_cash']:.2f} USD (credit {p['cash']['credit']:.2f}, pending "
         f"opens {p['cash']['pending_open_amount']:.2f})", "", "## Positions", "",
         "| Position | Instrument | Side x lev | Units | Invested | Exposure | Owner | Est. proceeds (basis) | "
         "Est. realized P&L | Spread + slippage | Financing | Action |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in p["positions"]:
        fin = r["financing"].get("daily_usd_estimate")
        fin_s = f"{fin:.2f}/day" if fin is not None else r["financing"].get("note", "")
        proc = "n/a" if r["est_close_proceeds"] is None else f"{r['est_close_proceeds']:.2f}"
        pnl = "n/a" if r["est_realized_pnl"] is None else f"{r['est_realized_pnl']:+.2f}"
        why = f" ({'; '.join(r['separate_reasons'])})" if r["separate_reasons"] else ""
        L.append(f"| {r['position_id']} | {r['symbol']} | {r['direction']} x{r['leverage']} | {r['units']:.6f} | "
                 f"{r['amount']:.2f} | {r['exposure_usd']:,.2f} | {r['owner']} | {proc} ({r['proceeds_basis']}) | "
                 f"{pnl} | {(r['est_spread_cost'] or 0) + r['slippage_allowance']:.2f} | {fin_s} | "
                 f"**{r['action']}**{why} |")
    for m in p["mirrors"]:
        L.append(f"| mirror {m['mirror_id']} | copy of {m['parent_username']} | - | {m['positions']} positions | "
                 f"{m['invested']:.2f} | - | COPY | - | - | - | - | **SEPARATE_HANDLING** |")
    for o in p["pending_orders"]:
        L.append(f"| order {o['order_id']} | {o['instrument_id']} | {o['kind']} | - | {o['amount']:.2f} | - | ? | - | "
                 f"- | - | - | **SEPARATE_HANDLING** |")
    c = p["capital"]
    L += ["", "## Capital after settlement", "",
          f"- Estimated cash after the approved closes: **{c['est_cash_after_closes']:.2f} USD** (worst case "
          f"{c['worst_case_cash_after_closes']:.2f})",
          f"- Estimated realized loss {c['est_realized_loss_usd']:.2f}, costs {c['est_costs_usd']:.2f}: an "
          f"ACCEPT_LOSSES_AND_FEES approval must cover at least {c['max_cost_floor_usd']:.2f} USD",
          f"- Investable after a {c['investable_reserve_pct']:.0%} reserve: **{c['investable']:.2f} USD**",
          f"- Indicative only: if the separately handled positions were also closed at their (non-broker) estimates, "
          f"cash would be about {c['indicative_cash_if_separate_items_closed_at_estimate']:.2f} USD (realized "
          f"{c['indicative_realized_pnl_separate_items']:+.2f}); this figure is not used for any approval", "",
          "## Remaining after takeover", "",
          f"- Positions not closed: {p['remaining_after_takeover']['positions'] or 'none'} (exposure "
          f"{p['remaining_after_takeover']['exposure_usd']:.2f} USD); mirrors "
          f"{p['remaining_after_takeover']['mirrors_invested']:.2f}; pending orders "
          f"{p['remaining_after_takeover']['pending_orders_amount']:.2f}", "",
          "## Proposed allocation and next holdings", ""]
    for s, x in p["allocation"].items():
        h = p["proposed_holdings"]["by_sleeve"].get(s, {})
        L.append(f"- **{s}: {x:.2f} USD** -> {h.get('targets_usd', h.get('status'))}"
                 + (f"; to cash (ineligible): {sorted(h['blocked_to_cash'])}" if h.get("blocked_to_cash") else ""))
    L.append(f"- Aggregate broker targets: {p['proposed_holdings']['aggregate_targets_usd']}")
    L += ["", "## Conflicting writers", ""]
    for w in p["writers"]:
        L.append(f"- **{w['name']}**: {w['status']} ({w.get('description', '')}; external events in 7 days: "
                 f"{w['external_events_7d']})")
    L += ["", "## Material execution risks", ""] + [f"- {x}" for x in p["risks"]]
    return "\n".join(L) + "\n"
