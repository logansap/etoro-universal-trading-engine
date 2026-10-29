"""Scenario 3: #529 multi-instrument targets (parity with the championship's recorded P529-ORIG decisions)."""
import datetime as dt
import json

import pytest

from conftest import FIX, ROOT, Env, write_engine_yaml
from utengine.core.calendar import Calendar
from utengine.core.configuration import load_strategy_spec
from utengine.core.market_data import CsvBarStore, build_snapshot
from utengine.strategy_modules import REGISTRY, create

CAL = Calendar(["2025-01-09"])
NOW = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)


def test_s03_parity_with_recorded_multi_instrument_decisions():
    """INVARIANT: replaying the recorded sessions in order (state carried between them, fresh at the first, as the
    paper engine did), qc529's weights equal the recorded target units x close / NAV for every instrument, and the
    no-trade-on-unchanged-label rule matches (all three recorded sessions traded)."""
    m = create(load_strategy_spec(ROOT / "strategies" / "529.yaml", REGISTRY))
    rec = [r for r in json.loads((FIX / "recorded_decisions.json").read_text()) if r["portfolio_id"] == "P529-ORIG"]
    assert [r["session"] for r in rec] == ["2026-10-22", "2026-10-23", "2026-10-26"]
    state = {}
    for r in rec:
        snap = build_snapshot(CsvBarStore(FIX / "snapshots" / r["snapshot"]), CAL, dt.date.fromisoformat(r["session"]),
                              m.required_symbols(), m.optional_symbols(), lookback_sessions=760, now=NOW)
        d = m.decide(snap, state, NOW)
        state = d.strategy_state
        expected = {t: u * snap.close[t][-1] / r["nav_at_close"] for t, u in r["targets"].items() if u}
        assert d.rebalance is True
        assert set(d.target_weights) == set(expected)
        for t, w in expected.items():
            assert d.target_weights[t] == pytest.approx(w, abs=1e-9), (r["session"], t)
        assert len(d.target_weights) >= 3
        assert d.cash_weight == pytest.approx(1.0 - sum(d.target_weights.values()))


def test_s03_unchanged_label_does_not_rebalance():
    """INVARIANT: deciding the same session again with the state it produced reports rebalance=False and keeps the
    previous weights (the source trades only when the sleeve label changes)."""
    m = create(load_strategy_spec(ROOT / "strategies" / "529.yaml", REGISTRY))
    snap = build_snapshot(CsvBarStore(FIX / "snapshots" / "930795e32531bedc"), CAL, dt.date(2026, 10, 26),
                          m.required_symbols(), m.optional_symbols(), lookback_sessions=760, now=NOW)
    d1 = m.decide(snap, {}, NOW)
    d2 = m.decide(snap, d1.strategy_state, NOW)
    assert d1.rebalance and not d2.rebalance and d2.target_weights == d1.target_weights


def test_s03_multi_instrument_plan_executes_eligible_legs(tmp_path):
    """INVARIANT: an engine running #529 in simulation opens one position per ELIGIBLE target instrument, leaves
    ineligible weight (SOXL: no verified eToro id) in cash, and reconciles clean."""
    env = Env(tmp_path, cfg_path=write_engine_yaml(tmp_path / "c2" / "engine.yaml", initial="QC_529"))
    e = env.engine()
    d, x = env.decide_and_execute()
    assert d["decision"]["status"] == "DECIDED"
    assert x["execution"]["status"] == "DONE", x
    held = {env.e.id_to_symbol[p["instrument_id"]] for p in env.broker.s["positions"].values()}
    assert held == {"SQQQ", "TLT", "TECL", "TQQQ"}
    plan = json.loads(e.db.one("SELECT plan FROM plans")["plan"])
    assert "SOXL" in plan["allocation_blocked"] and "SOXL" not in held
    assert e.status()["healthy"]
