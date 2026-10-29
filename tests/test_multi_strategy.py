"""Multi-strategy virtual sleeves (50/50 QC_529_ETORO_RESTRICTED + QC_60_SQQQ) on ONE simulated account.

Every test states the invariant it protects. Simulated broker only; no real broker is involved. Where a test needs a
specific rebalance (crossing, one sleeve going to cash) it rewrites the sleeve's persisted decision for the next
session -- a test-only device that exercises the planner, executor and ledger exactly as a real decision would.
"""
import datetime as dt
import json
import sqlite3

import pytest

from conftest import (DECIDE_AT, EXECUTE_AT, FIFTY_FIFTY, IDS, NEXT_DECIDE, NEXT_EXECUTE, extended_store, multi_env)
from utengine.brokers.simulated import SimulatedBroker, SimulatedCrash
from utengine.core import accounting, halts, sleeves
from utengine.core.multi_planner import plan_portfolio
from utengine.core.reconciliation import engine_position_ids

A, B = "QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"
UTC = dt.timezone.utc
D2 = dt.date(2026, 10, 27)


def day2_env(tmp_path, **kw):
    return multi_env(tmp_path, bar_store=extended_store({D2: 1.0}), **kw)


def val(e):
    r = e.reconcile("t", e.now())
    assert r.clean, r.issues
    return r, e.sleeve_valuation(r)


def kinds(e):
    return {h["kind"] for h in halts.active(e.db) + halts.active_sleeve(e.db)}


def clear_all(e, why="test"):
    for h in halts.active(e.db) + halts.active_sleeve(e.db):
        halts.clear(e.db, h["halt_id"], "tester", why, e.nows())


def force_decision(e, sid, session, weights: dict):
    """TEST ONLY: replace a persisted decision's targets (the strategy module is not involved)."""
    row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session=?", (sid, session))
    d = json.loads(row["decision"])
    d["target_weights"] = {k: float(v) for k, v in weights.items()}
    d["signal_target"] = dict(d["target_weights"])
    d["cash_weight"] = 1.0 - sum(weights.values())
    d["rebalance"] = True
    e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), sid, session))


def day2(env, forced=None):
    """Decide after the 10-27 close (optionally forcing targets), execute at the 10-28 open."""
    e = env.e
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    for sid, w in (forced or {}).items():
        force_decision(e, sid, D2.isoformat(), w)
    env.clock.set(NEXT_EXECUTE)
    return e.cycle()


def plan_of(out):
    ex = out["execution"][D2.isoformat()]
    return next(v for k, v in ex.items() if k.startswith("P"))


# ----------------------------------------------------------------------------------------------- 1, 2, 5
def test_two_sleeves_share_one_tqqq_position_with_exact_attribution(tmp_path):
    """INVARIANT: both sleeves decide independently; their TQQQ targets become ONE broker order whose units and
    cost are split exactly; sum of sleeve units == broker units and sum of sleeve equity == account equity."""
    env = multi_env(tmp_path)
    e = env.engine()
    d, x = env.decide_and_execute()
    assert set(d["decisions"]["2026-10-26"]) == {A, B}
    assert e.db.one("SELECT COUNT(*) c FROM decisions")["c"] == 2
    tqqq = [s for s in env.broker.s["submissions"] if s["kind"] == "open"
            and env.broker.s["orders"][s["order_id"]]["instrument_id"] == IDS["TQQQ"]]
    assert len(tqqq) == 1                                              # aggregated at the broker
    r, v = val(e)
    s = v["sleeves"]
    assert s[A]["holdings"][IDS["TQQQ"]]["units"] > 0 and s[B]["holdings"][IDS["TQQQ"]]["units"] > 0
    broker_tqqq = sum(p["units"] for p in env.broker.s["positions"].values() if p["instrument_id"] == IDS["TQQQ"])
    assert abs(s[A]["holdings"][IDS["TQQQ"]]["units"] + s[B]["holdings"][IDS["TQQQ"]]["units"] - broker_tqqq) < 1e-6
    assert abs(sum(x["equity"] for x in s.values()) - r.strategy_equity) < 1e-4
    assert abs(s[A]["equity"] - s[B]["equity"]) < 2.0                 # 50/50 of USD 2,000 minus spread
    held = {e.id_to_symbol[i] for i in s[A]["holdings"]}
    assert "SOXL" not in held and s[A]["cash"] > 100                  # unverified weight stays in 529's cash
    assert not sleeves.check_invariants(e.db, r.snapshot, engine_position_ids(e.db))


# ----------------------------------------------------------------------------------------------- 3
def test_verified_deposit_is_allocated_50_50_and_excluded_from_pnl(tmp_path):
    """INVARIANT: a broker-evidenced deposit (transaction id) is split by the sleeve weights, recorded as external
    capital (P&L and TWR unaffected) and invested only at the next rebalance."""
    env = day2_env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    _r, v0 = val(e)
    env.broker.deposit(1000.0, evidence="bank transfer ref T-77")
    env.clock.advance(minutes=1)
    _r, v1 = val(e)
    for sid in (A, B):
        assert abs(v1["sleeves"][sid]["cash"] - v0["sleeves"][sid]["cash"] - 500.0) < 1e-6
    row = e.db.one("SELECT * FROM cash_flows WHERE kind='DEPOSIT'")
    assert row["source"].startswith("broker:") and row["ref"].startswith("tx:")
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    daily = {r["sleeve"]: dict(r) for r in e.db.q("SELECT * FROM sleeve_daily WHERE session=?", (D2.isoformat(),))}
    for sid in (A, B):
        assert abs(daily[sid]["external_flow"] - 500.0) < 1e-6
        # prices unchanged since the open: P&L is only the first fills' spread (cents), never the USD 500 deposit
        assert -2.0 < daily[sid]["pnl"] <= 0.0
    env.clock.set(NEXT_EXECUTE)
    out = e.cycle()
    _r, v2 = val(e)
    assert v2["sleeves"][B]["cash"] < 10                               # #60 invested its share at the rebalance
    assert plan_of(out)["status"].startswith("DONE")


def test_unverified_cash_increase_is_one_exception_never_a_deposit(tmp_path):
    """INVARIANT: cash that appears WITHOUT broker deposit evidence is never classified automatically; it is ONE
    exception / notification however many reconciliations see it; only an audited classification allocates it."""
    env = multi_env(tmp_path, broker_kwargs={"cash_tx_capability": False})
    e = env.engine()
    env.decide_and_execute()
    env.broker.adjust_cash(300.0, record=False)
    for _ in range(5):
        env.clock.advance(minutes=1)
        r = e.reconcile("t", env.clock())
        assert not r.clean
    exc = [x for x in halts.open_exceptions(e.db) if x["kind"] == "CASH_DISCREPANCY"]
    assert len(exc) == 1 and exc[0]["occurrences"] >= 5
    sent = e.db.q("SELECT * FROM alerts WHERE kind='CASH_DISCREPANCY' AND suppressed=0")
    assert len(sent) == 1 and "possible deposit, UNVERIFIED" in sent[0]["message"]
    assert not e.db.one("SELECT 1 FROM cash_flows WHERE kind='DEPOSIT'")
    disc = e.db.one("SELECT disc_id FROM discrepancies WHERE status='OPEN'")["disc_id"]
    accounting.resolve_discrepancy(e.db, disc, "DEPOSIT", "bank statement 2026-10-27", "owner", e.nows())
    clear_all(e)
    _r, v = val(e)
    assert abs(v["sleeves"][A]["cash"] + v["sleeves"][B]["cash"] - env.broker.s["credit"]) < 1e-6
    assert not halts.open_exceptions(e.db)


# ----------------------------------------------------------------------------------------------- 4
def test_independent_pnl_per_sleeve(tmp_path):
    """INVARIANT: each sleeve's P&L comes from its own holdings only; the two P&Ls add up to the account's."""
    env = day2_env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    r0, v0 = val(e)
    bid, ask = env.broker.s["prices"][str(IDS["TQQQ"])]
    env.broker.set_price(IDS["TQQQ"], bid * 1.10, ask * 1.10)          # TQQQ +10%, everything else flat
    r1, v1 = val(e)
    for sid in (A, B):
        u = v0["sleeves"][sid]["holdings"][IDS["TQQQ"]]["units"]
        gain = v1["sleeves"][sid]["equity"] - v0["sleeves"][sid]["equity"]
        assert abs(gain - u * bid * 0.10) < 1e-4
    assert v1["sleeves"][B]["equity"] - v0["sleeves"][B]["equity"] > v1["sleeves"][A]["equity"] - v0["sleeves"][A]["equity"]
    total = sum(v1["sleeves"][s]["equity"] - v0["sleeves"][s]["equity"] for s in (A, B))
    assert abs(total - (r1.strategy_equity - r0.strategy_equity)) < 1e-4
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    perf = sleeves.performance(e.db)
    assert set(perf) == {A, B} and perf[B]["twr_return"] > perf[A]["twr_return"] > 0


# ----------------------------------------------------------------------------------------------- 6
def test_one_sleeve_going_to_cash_never_sells_the_other_sleeves_units(tmp_path):
    """INVARIANT: #529's rebalance to cash closes only #529's units of the shared TQQQ position (a PARTIAL_CLOSE);
    #60's TQQQ units, value and cash are untouched."""
    env = day2_env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    _r, v0 = val(e)
    out = day2(env, forced={A: {}})
    p = plan_of(out)
    assert p["status"].startswith("DONE"), p
    _r, v1 = val(e)
    assert v1["sleeves"][A]["holdings"] == {} or all(abs(h["units"]) < 1e-6 for h in v1["sleeves"][A]["holdings"].values())
    assert abs(v1["sleeves"][B]["holdings"][IDS["TQQQ"]]["units"] - v0["sleeves"][B]["holdings"][IDS["TQQQ"]]["units"]) < 1e-9
    assert abs(v1["sleeves"][B]["cash"] - v0["sleeves"][B]["cash"]) < 1e-9
    acts = {r["action"] for r in e.db.q("SELECT action FROM intents WHERE symbol='TQQQ' AND plan_id LIKE 'P%' AND phase=1")}
    assert acts == {"PARTIAL_CLOSE"}
    assert abs(v1["sleeves"][A]["cash"] - v0["sleeves"][A]["equity"]) < 1.0   # 529 is now (almost) all cash


def test_opposite_trades_cross_internally_and_net_at_the_broker(tmp_path):
    """INVARIANT: #60 selling TQQQ while #529 buys it is crossed between the sleeves at the broker mark (no broker
    order for the overlap); only the net residual reaches the broker; the crossing changes no sleeve's equity."""
    env = day2_env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    _r, v0 = val(e)
    subs0 = len(env.broker.s["submissions"])
    out = day2(env, forced={A: {"TQQQ": 1.0}, B: {"SQQQ": 1.0}})
    p = plan_of(out)
    assert p["crossings"] >= 1 and p["status"].startswith("DONE"), p
    cross = [dict(r) for r in e.db.q("SELECT * FROM sleeve_ledger WHERE kind LIKE 'CROSS_%'")]
    assert {c["symbol"] for c in cross} >= {"TQQQ"}
    by = {}
    for c in cross:
        by.setdefault(c["symbol"], 0.0)
        by[c["symbol"]] += c["units"]
    assert all(abs(u) < 1e-9 for u in by.values())                       # units only change hands
    assert abs(sum(c["cash"] for c in cross)) < 1e-6                      # cash only changes hands
    pid = next(k for k in out["execution"][D2.isoformat()] if k.startswith("P"))
    tq = [r for r in e.db.q("SELECT action FROM intents WHERE symbol='TQQQ' AND plan_id=?", (pid,))]
    sub = env.broker.s["submissions"][subs0:]
    instruments = [env.broker.s["orders"][s["order_id"]]["instrument_id"] for s in sub]
    assert instruments.count(IDS["TQQQ"]) <= 1                           # at most ONE net TQQQ order
    _r, v1 = val(e)
    assert v1["sleeves"][A]["holdings"][IDS["TQQQ"]]["value"] > 0.9 * v0["sleeves"][A]["equity"]
    assert IDS["TQQQ"] not in v1["sleeves"][B]["holdings"] or v1["sleeves"][B]["holdings"][IDS["TQQQ"]]["units"] < 1e-6
    assert v1["sleeves"][B]["holdings"][IDS["SQQQ"]]["value"] > 0.9 * v0["sleeves"][B]["equity"]
    assert len(tq) <= 1


def test_aggregate_instrument_limit_is_enforced_across_sleeves(tmp_path):
    """INVARIANT (aggregate_risk enforced): two sleeves each within their own limits cannot together exceed the
    ACCOUNT instrument limit; the aggregated TQQQ open is blocked."""
    from conftest import Env, write_engine_yaml
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml", portfolio=FIFTY_FIFTY, max_instrument_weight=0.6)
    env = Env(tmp_path, cfg_path=cfg)
    e = env.engine()
    env.decide_and_execute()
    blocked = json.loads(e.db.one("SELECT plan FROM plans WHERE strategy_id='PORTFOLIO'")["plan"])["blocked"]
    assert any(l["symbol"] == "TQQQ" and "instrument limit" in why for l, why in blocked)
    assert not any(p["instrument_id"] == IDS["TQQQ"] for p in env.broker.s["positions"].values())


# ----------------------------------------------------------------------------------------------- 7
def test_restart_mid_plan_never_double_orders(tmp_path):
    """INVARIANT: a crash right after the broker accepted an open; at restart the open is attributed from broker
    evidence (reference lookup) to the right sleeves, nothing is re-sent, later cycles do not re-execute the plan."""
    env = multi_env(tmp_path)
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.broker.inject("crash_after_submit")
    env.clock.set(EXECUTE_AT)
    with pytest.raises(SimulatedCrash):
        e.cycle()
    subs = len(env.broker.s["submissions"])
    e2 = env.engine()                                                   # restart
    env.clock.advance(minutes=2)
    for _ in range(3):
        e2.cycle()
        env.clock.advance(minutes=1)
    refs = [s["client_ref"] for s in env.broker.s["submissions"]]
    assert len(refs) == len(set(refs))                                 # one submission per intent, ever
    assert len(env.broker.s["submissions"]) >= subs
    r, v = val(e2)
    assert not sleeves.check_invariants(e2.db, r.snapshot, engine_position_ids(e2.db))
    assert abs(sum(x["equity"] for x in v["sleeves"].values()) - r.strategy_equity) < 1e-4
    assert e2.db.one("SELECT COUNT(*) c FROM plans WHERE strategy_id='PORTFOLIO'")["c"] == 1


# ----------------------------------------------------------------------------------------------- 8
def test_lost_close_response_without_proceeds_halts_and_needs_audited_resolution(tmp_path, monkeypatch):
    """INVARIANT (blocker B3 preserved): the broker accepted a close but the response was lost, and -- as on eToro --
    there is no way to look up the close's proceeds. The units leave the sleeve, the cash does not appear, the engine
    halts (CLOSE_PROCEEDS_UNKNOWN + CASH_DISCREPANCY), opens are not started, nothing is retried. Only an audited
    TRADE_CLOSE classification of the OBSERVED cash (no typed number) restores the sleeve ledger."""
    env = day2_env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    monkeypatch.setattr(SimulatedBroker, "closed_position_proceeds", lambda self, pid: None)
    env.broker.inject("timeout_after_accept")
    out = day2(env, forced={A: {}, B: {"SQQQ": 1.0}})
    p = plan_of(out)
    assert p["status"] in ("HALTED_RECONCILIATION", "HALTED_PHASE1"), p
    assert {"CLOSE_PROCEEDS_UNKNOWN", "CASH_DISCREPANCY"} <= kinds(e)
    assert not e.db.one("SELECT 1 FROM intents WHERE phase=2 AND plan_id LIKE 'P%' AND status NOT IN "
                        "('INTENT_PERSISTED','BLOCKED','ABANDONED') AND created_at>=?", (NEXT_EXECUTE.isoformat(),))
    subs = len(env.broker.s["submissions"])
    env.clock.advance(minutes=5)
    e.cycle()
    assert len(env.broker.s["submissions"]) == subs                    # halted: nothing is retried or sent
    it = e.db.one("SELECT intent_id FROM intents WHERE action IN ('CLOSE','PARTIAL_CLOSE') AND status='FILLED'")
    disc = e.db.one("SELECT disc_id, amount FROM discrepancies WHERE status='OPEN'")
    accounting.resolve_discrepancy(e.db, disc["disc_id"], "TRADE_CLOSE", "eToro trade history row", "owner",
                                   e.nows(), intent_id=it["intent_id"])
    clear_all(e, "proceeds verified in eToro")
    r, v = val(e)
    assert not sleeves.check_invariants(e.db, r.snapshot, engine_position_ids(e.db))
    assert abs(sum(x["cash"] for x in v["sleeves"].values()) - env.broker.s["credit"]) < 0.06


def test_unknown_open_is_never_retried(tmp_path):
    """INVARIANT: an open whose request may or may not have reached the broker (no evidence either way) is UNKNOWN,
    halts all new orders and is never resubmitted by later cycles."""
    env = multi_env(tmp_path)
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.broker.inject("network_before")
    env.clock.set(EXECUTE_AT)
    e.cycle()
    assert "UNKNOWN_ORDER" in kinds(e)
    unknown = e.db.one("SELECT intent_id FROM intents WHERE status='UNKNOWN'")["intent_id"]
    for _ in range(3):
        env.clock.advance(minutes=1)
        e.cycle()
    assert not any(s["client_ref"] == unknown for s in env.broker.s["submissions"])


# ----------------------------------------------------------------------------------------------- isolation
def test_sleeve_halt_holds_that_sleeve_only(tmp_path):
    """INVARIANT: a halt confined to one sleeve stops that sleeve's orders; the other sleeve trades normally."""
    env = multi_env(tmp_path)
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    halts.raise_halt(e.db, "EMERGENCY_HALT", "ALL", "owner holds #529", e.nows(), subject="owner", sleeve=A)
    env.clock.set(EXECUTE_AT)
    e.cycle()
    r, v = val(e)
    assert v["sleeves"][A]["holdings"] == {} and v["sleeves"][B]["holdings"]
    assert v["sleeves"][A]["cash"] == pytest.approx(1000.0)


def test_partial_close_minimums_never_spill_into_another_sleeve():
    """INVARIANT (planner): a sale too small for an eToro partial close is left unsold (drift note); it is never
    rounded up into a full close that would sell the other sleeve's units."""
    from utengine.brokers.base import Position, Quote
    from utengine.core.portfolio import Allocation
    pos = [Position("p1", 4465, "TQQQ", True, 10.0, 680.0, 1, 68.0, "CFD", 0.0, 0.0, None)]
    v = {"marks": {4465: 68.0}, "sleeves": {
        "X": {"equity": 1000.0, "available_cash": 932.0, "holdings": {4465: {"units": 1.0}}},
        "Y": {"equity": 680.0 - 68.0, "available_cash": 0.0, "holdings": {4465: {"units": 9.0}}}}}
    spec = {"rebalancing": {"min_order_usd": 10.0, "min_order_pct_nav": 0.0}}
    al = Allocation(1000.0, {"TQQQ": 68.0 * 0.9}, {}, {"TQQQ": 4465})                 # X sells 0.1 unit = 6.8 USD
    pp = plan_portfolio([{"sleeve": "X", "spec": spec, "alloc": al, "rebalance": True}], v, pos,
                        {4465: Quote(4465, 68.0, 68.02, "t")}, {4465: "TQQQ"}, 10.0)
    assert pp["legs"] == []                         # 6.8 USD is below the minimum order: not traded at all
    al = Allocation(1000.0, {}, {}, {})                                                # X liquidates its 1 unit
    pp = plan_portfolio([{"sleeve": "X", "spec": spec, "alloc": al, "rebalance": True}], v, pos,
                        {4465: Quote(4465, 68.0, 68.02, "t")}, {4465: "TQQQ"}, 10.0)
    assert [l.action for l in pp["legs"]] == ["PARTIAL_CLOSE"] and pp["legs"][0].units == 1.0
    assert dict(pp["legs"][0].sleeves) == {"X": 1.0}


# ----------------------------------------------------------------------------------------------- reallocation
def test_controlled_reallocation_moves_equity_without_broker_orders(tmp_path):
    """INVARIANT: an approved 50/50 -> 60/40 reallocation transfers sleeve capital (external to sleeve P&L) with no
    broker order; a stale or changed preview is refused."""
    env = multi_env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    r, v = val(e)
    subs = len(env.broker.s["submissions"])
    pv = sleeves.reallocation_preview(e.db, {A: 0.6, B: 0.4}, v, e.nows())
    env.clock.advance(minutes=20)
    with pytest.raises(Exception, match="expired"):
        sleeves.reallocate(e.db, pv["preview_hash"], "owner", v, e.nows())
    pv = sleeves.reallocation_preview(e.db, {A: 0.6, B: 0.4}, v, e.nows())
    res = sleeves.reallocate(e.db, pv["preview_hash"], "owner", v, e.nows())
    assert res["status"] == "APPLIED" and len(env.broker.s["submissions"]) == subs
    r, v2 = val(e)
    tot = sum(x["equity"] for x in v2["sleeves"].values())
    assert v2["sleeves"][A]["equity"] / tot == pytest.approx(0.6, abs=1e-6)
    assert sleeves.weights(e.db) == {A: 0.6, B: 0.4}


# ----------------------------------------------------------------------------------------------- migrations
def test_schema_upgrade_from_version_2_keeps_data_and_audit_chain(tmp_path, monkeypatch):
    """INVARIANT: a version-2 database (the audited release) upgrades to version 3 in place: rows kept, audit chain
    still valid, halts gain the subject/sleeve columns, new tables exist."""
    from utengine.persistence import db as dbm
    monkeypatch.setattr(dbm, "MIGRATIONS", dbm.MIGRATIONS[:2])
    old = dbm.DB(tmp_path / "engine.sqlite")
    old.audit("TEST", {"x": 1})
    old.x("INSERT INTO halts VALUES(?,?,?,?,?,?,?,?)", ("h1", "t", "K", "ALL", "r", "ACTIVE", None, None))
    assert old.schema_version() == 2
    old.close()
    monkeypatch.undo()
    new = dbm.DB(tmp_path / "engine.sqlite")
    assert new.schema_version() == 3
    assert new.verify_audit_chain()[0]
    assert new.one("SELECT subject, sleeve FROM halts WHERE halt_id='h1'")["sleeve"] is None
    tables = {r[0] for r in new.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sleeve_ledger", "exceptions", "takeover_runs", "takeover_approvals"} <= tables
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        new.x("INSERT INTO sleeve_ledger(entry_id,ts,sleeve,kind,cash,units,external,detail) VALUES('e','t','s','k',"
              "1,0,0,'{}')")
        new.x("DELETE FROM sleeve_ledger")
