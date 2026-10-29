"""Scenarios 21 (switch #60 -> #529) and 22 (failed switch rollback / partial failure)."""
import datetime as dt
import json

import pytest

from conftest import Env
from utengine.core import halts, strategy_manager
from utengine.core.errors import SwitchError

UTC = dt.timezone.utc
SWITCH_AT = dt.datetime(2026, 10, 27, 14, 0, tzinfo=UTC)          # 10:00 New York, market open


def started(tmp_path):
    env = Env(tmp_path)
    env.engine()
    env.decide_and_execute()
    env.clock.set(SWITCH_AT)
    return env


def test_s21_switch_60_to_529(tmp_path):
    """INVARIANT: preview builds a hashed transition plan from a clean reconciliation; activation requires that
    hash and an approver, executes sells before buys through the normal risk path, reconciles, re-assigns engine
    positions to the new strategy and records the switch; capital and history are not reset."""
    env = started(tmp_path)
    e = env.e
    before_flows = e.db.one("SELECT COUNT(*) c FROM cash_flows")["c"]
    pv = e.switcher.preview("QC_529", env.clock())
    actions = [(l["action"], l["symbol"]) for l in pv["legs"]]
    assert actions[0] == ("PARTIAL_CLOSE", "TQQQ") and {s for a, s in actions if a == "OPEN"} == {"SQQQ", "TLT", "TECL"}
    assert pv["estimated_spread_cost_usd"] > 0 and "SOXL" in pv["allocation"]["blocked"]
    with pytest.raises(SwitchError):
        e.switcher.activate("QC_529", "deadbeefdeadbeef", "owner", env.clock())
    with pytest.raises(SwitchError):
        e.switcher.activate("QC_529", pv["plan_hash"], " ", env.clock())
    env.clock.advance(minutes=1)
    res = e.switcher.activate("QC_529", pv["plan_hash"], "owner", env.clock())
    assert res["status"] == "COMPLETED", res
    assert strategy_manager.active(e.db)["strategy_id"] == "QC_529"
    held = {e.id_to_symbol[p["instrument_id"]] for p in env.broker.s["positions"].values()}
    assert held == {"TQQQ", "SQQQ", "TLT", "TECL"}
    assert {r["strategy_id"] for r in e.db.q("SELECT strategy_id FROM ownership WHERE owner='ENGINE' AND closed_at IS NULL")} == {"QC_529"}
    kinds = [r["kind"] for r in e.db.q("SELECT kind FROM audit_log")]
    assert {"SWITCH_PREVIEWED", "SWITCH_APPROVED", "STRATEGY_ACTIVATED"} <= set(kinds)
    assert e.db.one("SELECT COUNT(*) c FROM cash_flows WHERE kind='INITIAL_CAPITAL'")["c"] == 1
    assert e.db.one("SELECT COUNT(*) c FROM cash_flows")["c"] > before_flows
    e.cycle()                                                     # 529 has no decision for 10-26: nothing to do
    assert e.reconcile("check", env.clock()).clean and e.status()["healthy"]


def test_s21_activation_refused_when_state_changed_or_expired(tmp_path):
    """INVARIANT: a preview is bound to the broker state and expires; a deposit after preview, or waiting past the
    TTL, forces a new preview."""
    env = started(tmp_path)
    e = env.e
    pv = e.switcher.preview("QC_529", env.clock())
    env.broker.deposit(10.0, evidence="d")
    with pytest.raises(SwitchError, match="changed"):
        e.switcher.activate("QC_529", pv["plan_hash"], "owner", env.clock())
    pv = e.switcher.preview("QC_529", env.clock())
    env.clock.advance(minutes=20)
    with pytest.raises(SwitchError, match="expired"):
        e.switcher.activate("QC_529", pv["plan_hash"], "owner", env.clock())
    assert strategy_manager.active(e.db)["strategy_id"] == "QC_60_SQQQ"


def test_s22_failed_switch_rolls_back(tmp_path):
    """INVARIANT: if the first transition order is rejected and nothing executes, the switch is ROLLED_BACK: the old
    strategy stays active, positions are unchanged, no halt is needed, and the failure is in the audit log."""
    env = started(tmp_path)
    e = env.e
    pos = json.dumps(env.broker.s["positions"], sort_keys=True)
    pv = e.switcher.preview("QC_529", env.clock())
    env.broker.inject("http_reject")
    res = e.switcher.activate("QC_529", pv["plan_hash"], "owner", env.clock())
    assert res["status"] == "ROLLED_BACK"
    assert strategy_manager.active(e.db)["strategy_id"] == "QC_60_SQQQ"
    assert json.dumps(env.broker.s["positions"], sort_keys=True) == pos
    assert not halts.active(e.db)
    assert e.db.one("SELECT status FROM switch_plans WHERE plan_hash=?", (pv["plan_hash"],))["status"] == "ROLLED_BACK"


def test_s22_partially_executed_switch_halts(tmp_path):
    """INVARIANT: if the transition fails after some orders executed, the switch is FAILED_PARTIAL: the old
    strategy stays recorded as active, a SWITCH_FAILED halt stops all new orders, and an operator must review."""
    env = started(tmp_path)
    e = env.e
    pv = e.switcher.preview("QC_529", env.clock())
    env.broker.inject("pending")                                  # the close fills; the first open hangs
    res = e.switcher.activate("QC_529", pv["plan_hash"], "owner", env.clock())
    assert res["status"] == "FAILED_PARTIAL"
    assert strategy_manager.active(e.db)["strategy_id"] == "QC_60_SQQQ"
    assert {"SWITCH_FAILED"} <= {h["kind"] for h in halts.active(e.db)}
