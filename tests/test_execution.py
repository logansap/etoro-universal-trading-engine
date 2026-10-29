"""Scenarios 10-18, 20, 24: order lifecycle, failures, recovery and data/risk blocks (simulated broker)."""
import datetime as dt

import pytest

from conftest import (DECIDE_AT, EXECUTE_AT, IDS, NEXT_DECIDE, NEXT_EXECUTE, SNAP_ALL, Env, extended_store,
                      write_engine_yaml)
from utengine.brokers.simulated import SimulatedCrash
from utengine.core import halts
from utengine.core.execution import Executor, operator_resolve
from utengine.core.market_data import CsvBarStore, DictBarStore

UTC = dt.timezone.utc


def decide(env):
    e = env.e or env.engine()
    env.clock.set(DECIDE_AT)
    assert e.cycle()["decision"]["status"] == "DECIDED"
    return e


def execute(env, minutes=0):
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=minutes))
    return env.e.cycle()["execution"]


def submissions(env):
    return [s for s in env.broker.s["submissions"] if s["client_ref"] != "EXTERNAL"]


def statuses(e):
    return [r["status"] for r in e.db.q("SELECT status FROM intents ORDER BY created_at")]


def kinds(e):
    return {h["kind"] for h in halts.active(e.db)}


def test_s10_partial_fill(tmp_path):
    """INVARIANT: a partial fill is tracked (PARTIALLY_FILLED, halt, no second submission); the remainder is
    attributed when the broker completes it; total invested equals the intent amount; cash reconciles."""
    env = Env(tmp_path)
    decide(env)
    env.broker.inject("partial:0.5")
    x = execute(env)
    assert x["status"] == "HALTED_PHASE2" and statuses(env.e) == ["PARTIALLY_FILLED"]
    assert "UNRESOLVED_ORDER" in kinds(env.e)
    env.broker.advance()
    x = execute(env, minutes=5)
    assert x["status"] == "HALTED_RESOLVE_ONLY" and statuses(env.e) == ["FILLED"]
    assert len(submissions(env)) == 1
    amounts = sorted(p["amount"] for p in env.broker.s["positions"].values())
    assert len(amounts) == 2 and abs(sum(amounts) - 1995.0) < 0.01
    for h in halts.active(env.e.db):
        halts.clear(env.e.db, h["halt_id"], "ops", "partial fill completed", env.e.nows())
    assert execute(env, minutes=6)["status"] == "DONE"
    assert env.e.reconcile("check", env.clock()).clean and env.e.status()["healthy"]


def test_s11_rejection_not_retried(tmp_path):
    """INVARIANT: a broker rejection is final (REJECTED -> RECONCILED), moves no cash, is never retried, and
    leaves the account reconciled."""
    env = Env(tmp_path)
    decide(env)
    env.broker.inject("reject:746")
    x = execute(env)
    assert x["status"] == "DONE_WITH_REJECTIONS"
    assert env.broker.s["positions"] == {} and env.broker.s["credit"] == 2000.0
    assert execute(env, minutes=10)["status"] == "ALREADY_EXECUTED"
    assert len(submissions(env)) == 1 and statuses(env.e) == ["RECONCILED"]


def test_s12_timeout_after_acceptance(tmp_path):
    """INVARIANT: a timeout after the broker accepted the order is NOT resubmitted; broker evidence (a new matching
    position absent pre-submit) resolves it to FILLED; exactly one submission."""
    env = Env(tmp_path)
    decide(env)
    env.broker.inject("timeout_after_accept")
    x = execute(env)
    assert x["status"] == "DONE", x
    assert len(submissions(env)) == 1 and len(env.broker.s["positions"]) == 1
    assert env.e.db.one("SELECT COUNT(*) c FROM audit_log WHERE kind='ORDER_FOUND_BY_REFERENCE'")["c"] == 1
    assert env.e.status()["healthy"]


def test_s12_timeout_after_acceptance_without_reference_lookup(tmp_path, monkeypatch):
    """INVARIANT: if the reference lookup is unavailable, broker position evidence (a new matching position absent
    pre-submit) still resolves the intent to FILLED with exactly one submission."""
    from utengine.brokers.simulated import SimulatedBroker
    from utengine.core.errors import BrokerUnavailable

    def down(self, ref, kind):
        raise BrokerUnavailable("lookup down")
    monkeypatch.setattr(SimulatedBroker, "order_status_by_client_ref", down)
    env = Env(tmp_path)
    decide(env)
    env.broker.inject("timeout_after_accept")
    assert execute(env)["status"] == "DONE"
    ev = [r["detail"] for r in env.e.db.q("SELECT detail FROM order_events WHERE to_status='FILLED'")]
    assert "absent pre-submit" in ev[0] and len(submissions(env)) == 1


def test_s13_network_interruption_halts_never_resubmits(tmp_path):
    """INVARIANT: a connection loss with no broker evidence leaves the intent UNKNOWN and halts; later cycles only
    re-query; an operator may record NOT_SUBMITTED only when the broker shows no candidate; nothing is resent."""
    env = Env(tmp_path)
    decide(env)
    env.broker.inject("network_before")
    execute(env)
    assert statuses(env.e) == ["UNKNOWN"] and "UNKNOWN_ORDER" in kinds(env.e)
    execute(env, minutes=5)
    execute(env, minutes=10)
    assert submissions(env) == [] and statuses(env.e) == ["UNKNOWN"]
    iid = env.e.db.one("SELECT intent_id FROM intents")["intent_id"]
    operator_resolve(env.e.db, env.broker, iid, "NOT_SUBMITTED", "ops", "eToro UI shows no order", env.e.nows())
    for h in halts.active(env.e.db):
        halts.clear(env.e.db, h["halt_id"], "ops", "verified", env.e.nows())
    execute(env, minutes=15)
    assert submissions(env) == [] and env.broker.s["positions"] == {}


def test_s14_crash_before_submit(tmp_path, monkeypatch):
    """INVARIANT: a crash after the intent is persisted but before the broker call leaves INTENT_PERSISTED; after a
    restart inside the window the intent is submitted exactly once."""
    env = Env(tmp_path)
    decide(env)
    real = Executor.submit
    calls = {"n": 0}

    def crash_once(self, row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SimulatedCrash("killed before the broker call")
        return real(self, row)
    monkeypatch.setattr(Executor, "submit", crash_once)
    with pytest.raises(SimulatedCrash):
        execute(env)
    assert statuses(env.e) == ["INTENT_PERSISTED"] and submissions(env) == []
    env.engine()                                                   # restart
    x = execute(env, minutes=3)
    assert x["status"] == "DONE" and len(submissions(env)) == 1 and len(env.broker.s["positions"]) == 1


def test_s15_crash_after_submit_before_ack(tmp_path):
    """INVARIANT: a crash after the broker accepted the request (no order id recorded) leaves SUBMITTING; the
    restart resolves it from broker evidence to FILLED without resubmitting."""
    env = Env(tmp_path)
    decide(env)
    env.broker.inject("crash_after_submit")
    with pytest.raises(SimulatedCrash):
        execute(env)
    assert statuses(env.e) == ["SUBMITTING"]
    env.engine()                                                   # restart: startup resolves
    assert statuses(env.e) == ["FILLED"] and len(submissions(env)) == 1
    x = execute(env, minutes=3)
    assert x["status"] == "DONE" and len(submissions(env)) == 1
    assert env.e.status()["healthy"]


def test_s16_restart_with_unresolved_orders(tmp_path):
    """INVARIANT: an accepted order still pending at restart is re-queried by its order id (never resubmitted)
    until the broker reports it filled."""
    env = Env(tmp_path)
    decide(env)
    env.broker.inject("pending")
    assert execute(env)["status"] == "HALTED_PHASE2" and statuses(env.e) == ["PENDING"]
    env.engine()
    assert statuses(env.e) == ["PENDING"]
    env.broker.advance()
    env.engine()
    assert statuses(env.e) == ["FILLED"] and len(submissions(env)) == 1
    assert len(env.broker.s["positions"]) == 1


def test_s17_duplicate_cycles(tmp_path):
    """INVARIANT: repeated cycles and restarts inside the same windows produce one decision, one plan and one
    submission."""
    env = Env(tmp_path)
    for k in range(3):
        env.clock.set(DECIDE_AT + dt.timedelta(minutes=k))
        env.engine().cycle()
    for k in range(3):
        env.clock.set(EXECUTE_AT + dt.timedelta(minutes=k))
        env.engine().cycle()
    e = env.e
    assert e.db.one("SELECT COUNT(*) c FROM decisions")["c"] == 1
    assert e.db.one("SELECT COUNT(*) c FROM plans")["c"] == 1
    assert len(submissions(env)) == 1 and len(env.broker.s["positions"]) == 1


def _store_without(sym, drop_dates):
    base = CsvBarStore(SNAP_ALL)
    data = {}
    for f in SNAP_ALL.glob("*.csv.gz"):
        s = f.name.split(".")[0]
        rows = base.bars(s, dt.date(2000, 1, 1), dt.date(2026, 10, 26))
        data[s] = [r for r in rows if not (s == sym and r[0] in drop_dates)]
    return DictBarStore(data)


@pytest.mark.parametrize("drop,code", [({dt.date(2026, 10, 26)}, "STALE_DATA"), ({dt.date(2026, 6, 1)}, "VENDOR_GAP")])
def test_s18_missing_or_stale_data_holds(tmp_path, drop, code):
    """INVARIANT: a missing last bar or a gap inside the indicator window yields NO_DECISION (reason recorded);
    the execution window then has nothing to execute; nothing is guessed."""
    env = Env(tmp_path, bar_store=_store_without("TECS", drop))
    e = env.engine()
    env.clock.set(DECIDE_AT)
    d = e.cycle()["decision"]
    assert d["status"] == "NO_DECISION" and code in d["error"]
    assert execute(env)["status"] == "NO_DECISION" and submissions(env) == []


def test_s18_stale_decision_blocked_by_risk(tmp_path):
    """INVARIANT: risk refuses to execute a decision that is not for the session just before execution."""
    from utengine.core.order_planner import Leg
    from utengine.core.portfolio import Allocation
    from utengine.core.risk import evaluate
    env = Env(tmp_path)
    e = env.engine()
    spec = e.active_spec()
    leg = Leg(0, 2, "OPEN", "TQQQ", IDS["TQQQ"], 100.0, None, None, "t")
    alloc = Allocation(1000.0, {"TQQQ": 100.0}, {"TQQQ": 0.1}, {"TQQQ": IDS["TQQQ"]})
    v = evaluate(e.db, e.cfg, spec, [leg], alloc, e.broker.quotes([IDS["TQQQ"]]), decision_session="2026-10-23",
                 execution_session=dt.date(2026, 10, 27), calendar=e.cal, now=EXECUTE_AT, available_cash=1000.0)
    assert not v.ok and any("stale decision" in b for b in v.plan_blocks)


def test_s20_oversized_order_blocked(tmp_path):
    """INVARIANT: an order above max_single_order_usd is BLOCKED by risk and never submitted."""
    env = Env(tmp_path, cfg_path=write_engine_yaml(tmp_path / "c" / "engine.yaml", max_single_order_usd=500.0))
    decide(env)
    x = execute(env)
    assert x["status"] == "ALL_LEGS_BLOCKED" and statuses(env.e) == ["BLOCKED"] and submissions(env) == []


def test_s20_unaffordable_opens_resized_or_blocked():
    """INVARIANT: opens never exceed the cash the broker reports available: they are scaled down, and a scaled
    open below the USD 10 minimum is dropped."""
    from utengine.core.order_planner import Leg, resize_opens
    legs = [Leg(0, 2, "OPEN", "A", 1, 600.0, None, None, "r"), Leg(1, 2, "OPEN", "B", 2, 400.0, None, None, "r"),
            Leg(2, 2, "OPEN", "C", 3, 12.0, None, None, "r")]
    out, notes = resize_opens(legs, 500.0)
    assert sum(l.amount for l in out) <= 499.99 and {l.symbol for l in out} == {"A", "B"} and notes


def test_s20_unaffordable_blocked_by_risk(tmp_path):
    """INVARIANT: risk blocks an open that exceeds available cash (buying power)."""
    from utengine.core.order_planner import Leg
    from utengine.core.portfolio import Allocation
    from utengine.core.risk import evaluate
    env = Env(tmp_path)
    e = env.engine()
    env.clock.set(EXECUTE_AT)
    leg = Leg(0, 2, "OPEN", "TQQQ", IDS["TQQQ"], 1500.0, None, None, "t")
    alloc = Allocation(1500.0, {"TQQQ": 1500.0}, {"TQQQ": 0.9975}, {"TQQQ": IDS["TQQQ"]})
    v = evaluate(e.db, e.cfg, e.active_spec(), [leg], alloc, e.broker.quotes([IDS["TQQQ"]]),
                 decision_session="2026-10-26", execution_session=dt.date(2026, 10, 27), calendar=e.cal,
                 now=EXECUTE_AT, available_cash=1000.0)
    assert v.blocked and "unaffordable" in v.blocked[0][1]


def test_s24_emergency_halt_blocks_new_exposure_without_liquidating(tmp_path):
    """INVARIANT: an emergency halt blocks new exposure (and, by default, every order); it liquidates nothing;
    clearing it (audited) lets the next session trade."""
    env = Env(tmp_path, bar_store=extended_store({dt.date(2026, 10, 27): 1.0, dt.date(2026, 10, 28): 1.0}))
    env.engine()
    env.decide_and_execute()
    pos_before = dict(env.broker.s["positions"])
    hid = halts.raise_halt(env.e.db, "EMERGENCY_HALT", "NEW_EXPOSURE", "operator test", env.e.nows())
    env.clock.set(dt.datetime(2026, 10, 27, 15, 0, tzinfo=UTC))
    env.broker.deposit(500.0, evidence="deposit")
    env.clock.set(NEXT_DECIDE)
    env.e.cycle()
    env.clock.set(NEXT_EXECUTE)
    x = env.e.cycle()["execution"]
    assert x["status"] == "ALL_LEGS_BLOCKED" and env.broker.s["positions"] == pos_before
    halts.clear(env.e.db, hid, "ops", "test over", env.e.nows())
    env.clock.set(dt.datetime(2026, 10, 28, 20, 55, tzinfo=UTC))
    env.e.cycle()
    env.clock.set(dt.datetime(2026, 10, 29, 13, 35, tzinfo=UTC))
    assert env.e.cycle()["execution"]["status"] == "DONE"
    assert len(env.broker.s["positions"]) == 2
