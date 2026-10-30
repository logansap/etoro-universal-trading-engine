"""Portfolio restructure: the deployed 50/50 sleeves -> one 100% QC_529_ETORO_API2 sleeve inside the SAME database
(ledger, decisions and audit kept), the first acquisition from cash, refusals, and the reverse path (rollback)."""
import pytest

from conftest import IDS, last_close, multi_env
from utengine.core import sleeves
from utengine.core.errors import EngineError
from utengine.core.reconciliation import engine_position_ids
from utengine.strategy_modules import REGISTRY

A, B, C = "QC_529_ETORO_RESTRICTED", "QC_60_SQQQ", "QC_529_ETORO_API2"
API2_EXTRA = {"SOXL": 3226, "LABU": 3199, "SVXY": 3236, "BSV": 4296}


def env_with_api2_instruments(tmp_path, **kw):
    env = multi_env(tmp_path, **kw)
    for sym, iid in API2_EXTRA.items():
        env.broker.s["instruments"][str(iid)] = sym
        px = last_close(sym)
        env.broker.set_price(iid, round(px * 0.9998, 6), round(px * 1.0002, 6))
    return env


def val(e):
    r = e.reconcile("t", e.now())
    assert r.clean, r.issues
    return e.sleeve_valuation(r)


def restructure(e, w, by="owner"):
    p = sleeves.restructure_preview(e.db, w, val(e), e.specs, REGISTRY, e.nows())
    return p, sleeves.restructure(e.db, p["preview_hash"], by, val(e), e.nows())


def test_restructure_to_single_api2_sleeve_then_first_acquisition_from_cash(tmp_path):
    """INVARIANT: with no engine positions, 50/50 -> 100% QC_529_ETORO_API2 moves all sleeve cash (nothing created or
    lost), keeps the old sleeves' ledger and the portfolio history, and the new sleeve's FIRST decision (fresh state)
    is a full acquisition: it rebalances, buys only API2-tradable instruments, and the ledger matches the broker."""
    env = env_with_api2_instruments(tmp_path)
    e = env.engine()
    before = sleeves.balances(e.db)
    total = round(sum(b["cash"] for b in before.values()), 6)
    p, out = restructure(e, {C: 1.0})
    assert out["status"] == "APPLIED" and out["retired"] == [A, B] and sleeves.weights(e.db) == {C: 1.0}
    bal = sleeves.balances(e.db)
    assert abs(bal[C]["cash"] - total) < 1e-6 and abs(bal[A]["cash"]) < 1e-6 and abs(bal[B]["cash"]) < 1e-6
    rec = sleeves.portfolio(e.db)
    assert rec["history"][-1]["sleeves"] == [{"strategy": A, "weight": 0.5}, {"strategy": B, "weight": 0.5}]
    assert set(rec["strategies"]) == {C}
    assert e.db.one("SELECT COUNT(*) c FROM audit_log WHERE kind='PORTFOLIO_RESTRUCTURED'")["c"] == 1
    e = env.engine()                                               # restart: the DB record governs, not the YAML
    d, x = env.decide_and_execute()
    decided = d["decisions"]["2026-10-26"]
    assert set(decided) == {C}
    row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=?", (C,))
    import json
    dec = json.loads(row["decision"])
    assert dec["rebalance"] is True and dec["target_weights"]
    tradable = {s for s, i in e.specs[C]["instruments"].items() if i["eligibility"] == "VERIFIED_TRADABLE"}
    bought = {env.broker.s["instruments"][str(env.broker.s["orders"][s["order_id"]]["instrument_id"])]
              for s in env.broker.s["submissions"] if s["kind"] == "open"}
    assert bought and bought <= tradable
    r = e.reconcile("check", env.clock())
    assert r.clean and not sleeves.check_invariants(e.db, r.snapshot, engine_position_ids(e.db))
    v = e.sleeve_valuation(r)
    assert v["complete"] and abs(v["sleeves"][C]["equity"] - r.strategy_equity) < 0.01


def test_restructure_refused_while_a_retiring_sleeve_holds_units(tmp_path):
    """INVARIANT: a sleeve with holdings is never retired by a cash transfer (that would orphan units)."""
    env = env_with_api2_instruments(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    assert any(b["units"] for b in sleeves.balances(e.db).values())
    with pytest.raises(EngineError, match="still hold units"):
        sleeves.restructure_preview(e.db, {C: 1.0}, val(e), e.specs, REGISTRY, e.nows())
    assert sleeves.weights(e.db) == {A: 0.5, B: 0.5}


def test_restructure_refusals_and_stale_preview(tmp_path):
    """INVARIANT: unknown strategy, bad weights, a missing approver and a state change after the preview refuse."""
    env = env_with_api2_instruments(tmp_path)
    e = env.engine()
    with pytest.raises(EngineError):
        sleeves.restructure_preview(e.db, {"QC_NOPE": 1.0}, val(e), e.specs, REGISTRY, e.nows())
    with pytest.raises(EngineError):
        sleeves.restructure_preview(e.db, {C: 0.6}, val(e), e.specs, REGISTRY, e.nows())
    p = sleeves.restructure_preview(e.db, {C: 1.0}, val(e), e.specs, REGISTRY, e.nows())
    with pytest.raises(EngineError, match="approved-by"):
        sleeves.restructure(e.db, p["preview_hash"], " ", val(e), e.nows())
    env.broker.deposit(100.0, evidence="deposit")
    e.reconcile("t", e.now())
    with pytest.raises(EngineError, match="changed"):
        sleeves.restructure(e.db, p["preview_hash"], "owner", val(e), e.nows())
    assert sleeves.weights(e.db) == {A: 0.5, B: 0.5}


def test_restructure_rollback_restores_the_two_sleeves(tmp_path):
    """INVARIANT: the reverse restructure (rollback) restores 50/50 by cash only, with the same total."""
    env = env_with_api2_instruments(tmp_path)
    e = env.engine()
    total = round(sum(b["cash"] for b in sleeves.balances(e.db).values()), 6)
    restructure(e, {C: 1.0})
    _p, out = restructure(e, {A: 0.5, B: 0.5}, by="owner rollback")
    assert out["retired"] == [C] and sleeves.weights(e.db) == {A: 0.5, B: 0.5}
    bal = sleeves.balances(e.db)
    assert abs(bal[A]["cash"] + bal[B]["cash"] - total) < 1e-6 and abs(bal[C]["cash"]) < 1e-6
    assert len(sleeves.portfolio(e.db)["history"]) == 2
