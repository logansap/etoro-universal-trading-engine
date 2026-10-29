"""Scenarios 4-9, 23, 28: broker-authoritative accounting and reconciliation."""
import datetime as dt
import json

from conftest import (DECIDE_AT, EXECUTE_AT, IDS, NEXT_DECIDE, NEXT_EXECUTE, Env, extended_store)
from utengine.core import accounting, halts
from utengine.core.reconciliation import acknowledge_external

UTC = dt.timezone.utc


def kinds(e):
    return {h["kind"] for h in halts.active(e.db)}


def test_s04_existing_positions_at_startup_are_excluded(tmp_path):
    """INVARIANT: a position that exists before the first start is PRE_EXISTING: excluded from strategy equity,
    never closed or resized, and the strategy invests only the available cash."""
    env = Env(tmp_path)
    env.broker.external_open(IDS["GDX"], 500.0)                  # e.g. another engine's / manual position
    e = env.engine()
    own = {r["position_id"]: r["owner"] for r in e.db.q("SELECT * FROM ownership")}
    assert list(own.values()) == ["PRE_EXISTING"]
    d, x = env.decide_and_execute()
    assert x["execution"]["status"] == "DONE"
    tq = [p for p in env.broker.s["positions"].values() if p["instrument_id"] == IDS["TQQQ"]]
    assert len(tq) == 1 and tq[0]["amount"] == round(1500.0 * 0.9975, 2)
    assert any(p["instrument_id"] == IDS["GDX"] and p["amount"] == 500.0 for p in env.broker.s["positions"].values())
    assert e.status()["healthy"]


def _run_two_sessions(tmp_path, deposit=None):
    env = Env(tmp_path, bar_store=extended_store({dt.date(2026, 10, 27): 1.03}))
    env.engine()
    env.decide_and_execute()
    if deposit:
        env.clock.set(dt.datetime(2026, 10, 27, 15, 0, tzinfo=UTC))
        env.broker.deposit(deposit, evidence="bank transfer 2026-10-27")
        env.e.cycle()                                            # idle reconciliation picks it up, no restart
    env.price_all(1.03)
    env.clock.set(NEXT_DECIDE)
    env.e.cycle()
    env.clock.set(NEXT_EXECUTE)
    x = env.e.cycle()
    return env, x


def test_s05_deposit_detected_and_invested_without_restart(tmp_path):
    """INVARIANT: a deposit evidenced by the broker's cash transactions is classified DEPOSIT automatically (no
    restart, no manual capital edit) and the next rebalance invests it."""
    env, x = _run_two_sessions(tmp_path, deposit=1000.0)
    e = env.e
    flows = {r["kind"]: r for r in e.db.q("SELECT * FROM cash_flows")}
    assert flows["DEPOSIT"]["amount"] == 1000.0 and flows["DEPOSIT"]["source"].startswith("broker:")
    assert x["execution"]["status"] == "DONE", x
    opens = [p for p in env.broker.s["positions"].values() if p["instrument_id"] == IDS["TQQQ"]]
    assert len(opens) == 2 and opens[1]["amount"] > 990
    assert e.status()["healthy"] and not e.db.q("SELECT * FROM discrepancies")


def test_s06_deposit_excluded_from_pnl_and_twr(tmp_path):
    """INVARIANT: the deposit raises equity and external flow by the same amount; P&L and the TWR index are
    identical to the same history without the deposit."""
    a, _ = _run_two_sessions(tmp_path / "a", deposit=1000.0)
    b, _ = _run_two_sessions(tmp_path / "b", deposit=None)
    ra = dict(a.e.db.one("SELECT * FROM daily_equity WHERE session='2026-10-27'"))
    rb = dict(b.e.db.one("SELECT * FROM daily_equity WHERE session='2026-10-27'"))
    assert ra["external_flow"] == 1000.0 and rb["external_flow"] == 0.0
    assert abs(ra["strategy_equity"] - rb["strategy_equity"] - 1000.0) < 0.01
    assert abs(ra["pnl"] - rb["pnl"]) < 0.01 and ra["pnl"] > 0
    assert abs(ra["twr_index"] - rb["twr_index"]) < 1e-6


def test_s07_unknown_cash_discrepancy_blocks_orders(tmp_path):
    """INVARIANT: an unexplained cash change halts all new orders; nothing is booked to balance it; only an
    audited operator classification of the OBSERVED amount (sign-checked) resolves it."""
    env = Env(tmp_path)
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.broker.adjust_cash(25.0, record=False)                   # no broker evidence
    env.clock.set(EXECUTE_AT)
    x = e.cycle()["execution"]
    assert x["status"] == "BLOCKED_RECONCILIATION" and "CASH_DISCREPANCY" in kinds(e)
    assert e.db.one("SELECT COUNT(*) c FROM intents")["c"] == 0 and env.broker.s["positions"] == {}
    disc = e.db.one("SELECT * FROM discrepancies WHERE status='OPEN'")
    assert disc["amount"] == 25.0
    import pytest
    from utengine.core.errors import EngineError
    with pytest.raises(EngineError):
        accounting.resolve_discrepancy(e.db, disc["disc_id"], "WITHDRAWAL", "wrong sign", "ops", e.nows())
    accounting.resolve_discrepancy(e.db, disc["disc_id"], "DEPOSIT", "bank statement ref 123", "ops", e.nows())
    assert e.db.one("SELECT amount FROM cash_flows WHERE kind='DEPOSIT'")["amount"] == 25.0
    for h in halts.active(e.db):
        halts.clear(e.db, h["halt_id"], "ops", "deposit verified", e.nows())
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=5))
    assert e.cycle()["execution"]["status"] == "DONE"
    assert e.status()["healthy"]


def test_s08_broker_cash_differs_from_internal_after_fill(tmp_path):
    """INVARIANT: if the broker charges something the engine cannot see (an undisclosed fee at fill), the
    post-execution reconciliation reports the exact difference and halts; the plan is DONE_UNRECONCILED."""
    env = Env(tmp_path)
    e = env.engine()
    real_fill = env.broker._fill_open

    def fill_with_hidden_fee(o, amount):
        real_fill(o, amount)
        env.broker.s["credit"] = round(env.broker.s["credit"] - 1.25, 6)
    env.broker._fill_open = fill_with_hidden_fee
    d, x = env.decide_and_execute()
    assert x["execution"]["status"] == "DONE_UNRECONCILED"
    disc = e.db.one("SELECT * FROM discrepancies WHERE status='OPEN'")
    assert abs(disc["amount"] + 1.25) < 1e-6 and "CASH_DISCREPANCY" in kinds(e)


def test_s09_position_mismatch_halts(tmp_path):
    """INVARIANT: an engine position whose broker units differ from the engine's record halts all new orders."""
    env = Env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    pid = next(iter(env.broker.s["positions"]))
    env.broker.s["positions"][pid]["units"] *= 0.5
    env.broker._save()
    r = e.reconcile("test", env.clock())
    assert not r.clean and "POSITION_MISMATCH" in kinds(e)


def test_s09_engine_position_vanishing_halts(tmp_path):
    """INVARIANT: an engine position closed outside the engine (manual close, broker stop) halts."""
    env = Env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    pid = next(iter(env.broker.s["positions"]))
    env.broker.submit_close(pid, IDS["TQQQ"], None, client_ref="MANUAL")
    r = e.reconcile("test", env.clock())
    assert "POSITION_MISSING" in kinds(e) and not r.clean


def test_s23_external_trade_detected_and_never_touched(tmp_path):
    """INVARIANT: a manual trade is detected (EXTERNAL_POSITION halt), its cash use is explained by the broker's
    own position amount (no cash discrepancy), it is excluded from strategy equity after acknowledgement, and the
    engine never closes it."""
    env = Env(tmp_path, bar_store=extended_store({dt.date(2026, 10, 27): 1.0}))
    e = env.engine()
    env.decide_and_execute()
    env.clock.set(dt.datetime(2026, 10, 27, 15, 0, tzinfo=UTC))
    env.broker.deposit(100.0, evidence="test top-up")
    env.broker.external_open(IDS["UVXY"], 50.0)
    e.cycle()
    assert "EXTERNAL_POSITION" in kinds(e) and not e.db.q("SELECT * FROM discrepancies")
    pid = e.db.one("SELECT position_id FROM ownership WHERE owner='EXTERNAL_UNACK'")["position_id"]
    acknowledge_external(e.db, pid, "ops", "owner bought UVXY by hand", e.nows())
    for h in halts.active(e.db):
        halts.clear(e.db, h["halt_id"], "ops", "acknowledged", e.nows())
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    env.clock.set(NEXT_EXECUTE)
    x = e.cycle()["execution"]
    assert x["status"] in ("DONE", "NO_LEGS"), x
    assert pid in env.broker.s["positions"]                           # never touched
    closes = e.db.q("SELECT * FROM intents WHERE position_id=?", (pid,))
    assert closes == []
    assert e.status()["healthy"]


def test_s28_ledger_consistency_across_restarts(tmp_path):
    """INVARIANT: across restarts between every step: the audit chain verifies; INITIAL_CAPITAL + every cash flow
    equals the broker's cash; flows are unique; ENGINE ownership equals the broker's engine positions; no intent
    reached the broker more than once."""
    env = Env(tmp_path, bar_store=extended_store({dt.date(2026, 10, 27): 0.97}))
    env.engine()
    env.clock.set(DECIDE_AT)
    env.engine().cycle()
    env.clock.set(EXECUTE_AT)
    env.engine().cycle()
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=3))
    env.engine().cycle()                                              # duplicate cycle after restart
    env.broker = env.broker.__class__(env.broker_path, clock=env.clock)
    env.broker.deposit(250.0, evidence="deposit")
    env.price_all(0.97)
    env.clock.set(NEXT_DECIDE)
    env.engine().cycle()
    env.clock.set(NEXT_EXECUTE)
    env.engine().cycle()
    e = env.engine()
    ok, msg = e.db.verify_audit_chain()
    assert ok, msg
    total = e.db.one("SELECT SUM(amount) s FROM cash_flows")["s"]
    assert abs(total - env.broker.s["credit"]) <= 0.05
    assert e.db.one("SELECT COUNT(*) c FROM cash_flows")["c"] == e.db.one("SELECT COUNT(DISTINCT ref) c FROM cash_flows")["c"]
    eng = {r["position_id"] for r in e.db.q("SELECT position_id FROM ownership WHERE owner='ENGINE' AND closed_at IS NULL")}
    assert eng == set(env.broker.s["positions"])
    refs = [s["client_ref"] for s in env.broker.s["submissions"]]
    assert len(refs) == len(set(refs))
    assert e.status()["healthy"], json.dumps(e.status()["halts"])
