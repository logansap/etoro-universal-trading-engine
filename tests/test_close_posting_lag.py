"""2026-11-16 fixes for the 2026-11-12 halt, against the real eToro adapter and the contract fake.

2026-11-12 14:31: the engine's PARTIAL_CLOSE of SQQQ position 3634154364 (0.325914 units, +13.75) filled according to
the order lookup, but the 14:31:37 portfolio still showed the pre-close state: 9.370983 units and the old cash. rc2
halted ALL (POSITION_MISMATCH + CASH_DISCREPANCY -13.75), parked the +13.75 that posted later in suspense, skipped
two execution windows and needed a manual `account resolve` -- which then failed in mode=live because the CLI built
the order writer and asked for the trading key. The dashboard meanwhile said "live trading DISABLED".
"""
import json

import pytest

from test_autonomous_live import ENV, _live_yaml
from test_etoro_live_contract import TQQQ, Rig, open_leg
from utengine.core import attribution, halts
from utengine.core.order_planner import Leg
from utengine.core.security import live_trading_state

STALE = 5     # > the executor's re-reads (VISIBILITY_READS): reconciliation still sees the pre-close portfolio


def _setup(tmp_path):
    rig = Rig(tmp_path)
    rig.run("P0", [open_leg(0, TQQQ, "TQQQ", 1500.0)])
    pid = next(p["positionID"] for p in rig.fake.positions.values() if p["instrumentID"] == TQQQ)
    assert rig.recon().clean
    return rig, pid


def _partial(rig, pid, plan="P1"):
    units = round(rig.fake.positions[pid]["units"] / 3, 6)
    return rig.run(plan, [Leg(0, 1, "PARTIAL_CLOSE", "TQQQ", TQQQ, 500.0, units, str(pid), "reduce")])


def _open_discrepancies(rig):
    return rig.db.q("SELECT * FROM discrepancies WHERE status='OPEN'")


def test_nov12_partial_close_with_a_stale_portfolio_waits_instead_of_halting(tmp_path):
    """REGRESSION 2026-11-12. INVARIANT: while the portfolio still shows a filled partial close's pre-close units and
    lacks exactly its proceeds, reconciliation is not clean (no new opens) but raises no halt, books no discrepancy
    and no suspense, and values the strategy without a phantom loss; once the portfolio is current it is CLEAN."""
    rig, pid = _setup(tmp_path)
    eq0 = rig.recon().strategy_equity
    rig.fake.close_stale = STALE
    out = _partial(rig, pid)
    assert out["phase1"] == ["FILLED"]
    assert attribution.markers(rig.db, attribution.AWAIT_REDUCED)
    r = rig.recon()
    assert [i["kind"] for i in r.issues] == ["AWAITING_VISIBILITY"], r.issues
    assert not halts.active(rig.db) and not _open_discrepancies(rig)
    assert r.strategy_equity == pytest.approx(eq0, abs=1.0)            # no phantom -proceeds
    while rig.fake.stale:
        rig.recon()
    r = rig.recon()
    assert r.clean, r.issues
    assert not halts.active(rig.db) and not _open_discrepancies(rig)
    assert not attribution.markers(rig.db, attribution.AWAIT_REDUCED)
    assert r.strategy_equity == pytest.approx(eq0, abs=1.0)
    own = rig.db.one("SELECT units FROM ownership WHERE position_id=?", (str(pid),))["units"]
    assert own == pytest.approx(rig.fake.positions[pid]["units"])


def test_full_close_with_a_stale_portfolio_and_cash_waits_without_a_phantom_loss(tmp_path):
    rig, pid = _setup(tmp_path)
    eq0 = rig.recon().strategy_equity
    rig.fake.close_stale = STALE
    out = rig.run("P1", [Leg(0, 1, "CLOSE", "TQQQ", TQQQ, 1500.0, None, str(pid), "exit")])
    assert out["phase1"] == ["FILLED"]
    r = rig.recon()
    assert [i["kind"] for i in r.issues] == ["AWAITING_VISIBILITY"], r.issues
    assert not halts.active(rig.db) and not _open_discrepancies(rig)
    assert r.strategy_equity == pytest.approx(eq0, abs=1.0)            # the listed position, valued as listed
    while rig.fake.stale:
        rig.recon()
    r = rig.recon()
    assert r.clean, r.issues
    assert r.strategy_equity == pytest.approx(eq0, abs=1.0)


def test_a_partial_close_the_broker_never_shows_halts_after_the_grace(tmp_path):
    """Fail closed: the wait is bounded by the same grace as the other lag cases."""
    rig, pid = _setup(tmp_path)
    rig.fake.close_stale = 10 ** 6
    _partial(rig, pid)
    assert not rig.recon().clean and not halts.active(rig.db)
    rig.clock.advance(seconds=attribution.VISIBILITY_GRACE_S + 1)
    r = rig.recon()
    kinds = {i["kind"] for i in r.issues}
    assert {"POSITION_MISMATCH", "CASH_UNEXPLAINED"} <= kinds, r.issues
    assert {"POSITION_MISMATCH", "CASH_DISCREPANCY"} <= {h["kind"] for h in halts.active(rig.db)}


def test_any_other_cash_change_while_waiting_is_still_a_discrepancy(tmp_path):
    """Fail closed: only a shortfall of exactly the in-flight proceeds is waited out; nothing is auto-classified."""
    rig, pid = _setup(tmp_path)
    rig.fake.close_stale = STALE
    _partial(rig, pid)
    rig.fake.cache["clientPortfolio"]["credit"] -= 5.0                  # an unrelated cash movement
    r = rig.recon()
    assert "CASH_UNEXPLAINED" in {i["kind"] for i in r.issues}
    assert "CASH_DISCREPANCY" in {h["kind"] for h in halts.active(rig.db)}


def test_a_current_portfolio_after_a_partial_close_needs_no_wait(tmp_path):
    rig, pid = _setup(tmp_path)
    assert _partial(rig, pid)["phase1"] == ["FILLED"]
    assert not attribution.markers(rig.db, attribution.AWAIT_REDUCED)
    assert rig.recon().clean


# ------------------------------------------------------------------------------------------------ operator CLI
def test_account_resolve_and_status_run_in_live_mode_without_the_trading_key(tmp_path, monkeypatch):
    """REGRESSION 2026-11-16: `account resolve` in mode=live failed with "trading credential (UTE_ETORO_TRADING_*)
    not provisioned". Operator bookkeeping never submits an order: it runs monitor-only."""
    from utengine.cli import main as cli
    from utengine.core.engine import TradingEngine
    for k in list(ENV) + ["UTE_ETORO_READONLY_API_KEY", "UTE_ETORO_READONLY_USER_KEY"]:
        monkeypatch.delenv(k, raising=False)
    cfg, home = _live_yaml(tmp_path), tmp_path / "home"
    m = TradingEngine(cfg, home, env={}, monitor_only=True)
    m.db.con.execute("INSERT INTO discrepancies VALUES(?,?,?,?,?,?,?)",
                     ("d1", m.nows(), "CASH_UNEXPLAINED", -13.75, json.dumps({"snapshot_id": None}), "OPEN", None))
    m.db.con.commit()
    base = ["--config", str(cfg), "--home", str(home)]
    assert cli(base + ["account", "resolve", "--discrepancy", "d1", "--classify", "ADJUSTMENT",
                       "--evidence", "test", "--operator", "owner"]) == 0
    assert m.db.one("SELECT status FROM discrepancies WHERE disc_id='d1'")["status"] == "RESOLVED"
    assert cli(base + ["account", "status"]) == 0
    assert m.writer is None


# ------------------------------------------------------------------------------------------------ banner / status
@pytest.mark.parametrize("mode,first", [("live", "ENABLED"), ("supervised_live", "SUPERVISED"),
                                        ("simulation", "SIMULATED"), ("shadow", "DISABLED")])
def test_live_trading_state_reflects_the_mode(mode, first):
    assert live_trading_state(mode).split(":")[0] == first


def test_status_and_dashboard_no_longer_say_disabled_in_live_mode(tmp_path):
    from utengine.core.engine import TradingEngine
    from utengine.dashboard.data import Data
    cfg = _live_yaml(tmp_path)
    m = TradingEngine(cfg, tmp_path / "home", env={}, monitor_only=True)
    assert m.status()["live_execution"].startswith("ENABLED")
    meta = Data(m.cfg, m.specs, tmp_path / "home").meta()
    assert meta["mode"] == "LIVE" and meta["live_trading"].split(":")[0] == "ENABLED"
