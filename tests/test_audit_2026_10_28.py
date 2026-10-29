"""Independent release audit 2026-10-28: adversarial regression tests for the defects it found (F1-F4) and the
switching / ledger-continuity scenarios it required. Simulated broker only; no real broker is involved."""
import datetime as dt
import json

import pytest

from conftest import DECIDE_AT, EXECUTE_AT, IDS, Env
from utengine.brokers.simulated import SimulatedBroker, SimulatedCrash
from utengine.core import accounting, halts, strategy_manager
from utengine.core.errors import SwitchError
from utengine.core.reconciliation import acknowledge_external

UTC = dt.timezone.utc
SWITCH_AT = dt.datetime(2026, 10, 27, 14, 0, tzinfo=UTC)


def kinds(e):
    return {h["kind"] for h in halts.active(e.db)}


def clear_all(e, why):
    for h in halts.active(e.db):
        halts.clear(e.db, h["halt_id"], "auditor", why, e.nows())


def test_f1_missing_unrealized_pnl_blocks_sizing_instead_of_crashing(tmp_path, monkeypatch):
    """INVARIANT (F1): if the broker reports an ENGINE position without unrealized P&L, strategy equity is unknown;
    the engine halts ACCOUNT_SIZING_BLOCKED and submits nothing (it previously raised TypeError in allocate())."""
    env = Env(tmp_path)
    env.engine()
    env.decide_and_execute()
    real = SimulatedBroker.account_snapshot

    def no_pnl(self):
        s = real(self)
        from dataclasses import replace
        return replace(s, positions=tuple(replace(p, unrealized_pnl=None) for p in s.positions))
    monkeypatch.setattr(SimulatedBroker, "account_snapshot", no_pnl)
    e = env.e
    e.db.x("DELETE FROM plans")                                     # force a fresh planning attempt (test only)
    before = len(env.broker.s["submissions"])
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=5))
    out = e.execute(e.active_spec(), dt.date(2026, 10, 26), env.clock())
    assert out["status"] == "BLOCKED_EQUITY_UNAVAILABLE"
    assert "ACCOUNT_SIZING_BLOCKED" in kinds(e) and len(env.broker.s["submissions"]) == before


def test_f2_interrupted_switch_is_detected_at_restart(tmp_path):
    """INVARIANT (F2): a crash in the middle of an approved switch leaves the old strategy active and, at the next
    start, the switch is marked INTERRUPTED with a SWITCH_FAILED halt; the executed leg is attributed from broker
    evidence and nothing is re-executed automatically."""
    env = Env(tmp_path)
    env.engine()
    env.decide_and_execute()
    env.clock.set(SWITCH_AT)
    e = env.e
    pv = e.switcher.preview("QC_529", env.clock())
    env.broker.inject("crash_after_submit")
    with pytest.raises(SimulatedCrash):
        e.switcher.activate("QC_529", pv["plan_hash"], "owner", env.clock())
    subs = len(env.broker.s["submissions"])
    e2 = env.engine()                                                # restart
    assert strategy_manager.active(e2.db)["strategy_id"] == "QC_60_SQQQ"
    assert e2.db.one("SELECT status FROM switch_plans WHERE plan_hash=?", (pv["plan_hash"],))["status"] == "INTERRUPTED"
    assert "SWITCH_FAILED" in kinds(e2)
    assert len(env.broker.s["submissions"]) == subs                  # nothing re-sent
    st = {r["status"] for r in e2.db.q("SELECT status FROM intents WHERE plan_id=?", ("S" + pv["plan_hash"],))}
    assert st == {"FILLED"}


def test_f3_foreign_position_change_can_be_acknowledged(tmp_path):
    """INVARIANT (F3): a pre-existing (e.g. Gold Miner) position trimmed outside the engine halts with EXTERNAL_CHANGE
    and an unexplained cash change; the operator can accept the new foreign baseline and classify the cash as
    EXTERNAL_TRADE; reconciliation is then CLEAN and the foreign position is still never touched."""
    env = Env(tmp_path)
    env.broker.external_open(IDS["GDX"], 500.0)
    e = env.engine()
    pid = next(iter(env.broker.s["positions"]))
    units = env.broker.s["positions"][pid]["units"]
    env.broker.submit_close(pid, IDS["GDX"], round(units / 4, 6), client_ref="GOLD-MINER")
    r = e.reconcile("t", env.clock())
    assert {"EXTERNAL_CHANGE", "CASH_UNEXPLAINED"} <= {i["kind"] for i in r.issues}
    acknowledge_external(e.db, pid, "auditor", "Gold Miner trimmed its own position", e.nows())
    d = e.db.one("SELECT disc_id FROM discrepancies WHERE status='OPEN'")["disc_id"]
    accounting.resolve_discrepancy(e.db, d, "EXTERNAL_TRADE", "GM close order (audit snapshot)", "auditor", e.nows())
    clear_all(e, "verified")
    r = e.reconcile("t2", env.clock())
    assert r.clean, r.issues
    assert e.db.one("SELECT owner FROM ownership WHERE position_id=?", (pid,))["owner"] == "PRE_EXISTING"


def test_f4_log_write_failure_does_not_crash_the_engine(tmp_path):
    """INVARIANT (F4): an unwritable log file is counted and reported unhealthy; the cycle and the DB record go on."""
    env = Env(tmp_path)
    e = env.engine()
    e.log.path.unlink(missing_ok=True)
    e.log.path.mkdir()                                               # writing a file at this path now fails
    env.clock.set(DECIDE_AT)
    out = e.cycle()
    assert out["decision"]["status"] == "DECIDED"
    st = e.status()
    assert st["log_write_failures"] > 0 and st["healthy"] is False


def test_switch_refused_while_an_order_is_outstanding(tmp_path):
    """INVARIANT: no strategy switch can be previewed while an order intent is unresolved at the broker."""
    env = Env(tmp_path)
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.broker.inject("pending")
    env.clock.set(EXECUTE_AT)
    e.cycle()
    for h in halts.active(e.db):                                     # even with the halt cleared
        halts.clear(e.db, h["halt_id"], "auditor", "test", e.nows())
    with pytest.raises(SwitchError, match="unresolved"):
        e.switcher.preview("QC_529", env.clock())


def test_roundtrip_60_529_60_keeps_one_continuous_ledger(tmp_path):
    """INVARIANT: #60 -> #529 (first attempt fails and rolls back) -> deposit -> #529 -> #60, with a pre-existing
    foreign position: one INITIAL_CAPITAL, one DEPOSIT, every cash change explained (flows = broker cash), the
    foreign position untouched, the audit chain valid, no duplicate submission, history never reset."""
    env = Env(tmp_path)
    env.broker.external_open(IDS["GDX"], 300.0)
    e = env.engine()
    gdx = next(iter(env.broker.s["positions"]))
    env.decide_and_execute()
    env.clock.set(SWITCH_AT)
    pv = e.switcher.preview("QC_529", env.clock())
    env.broker.inject("http_reject")                                 # the first transition order fails
    assert e.switcher.activate("QC_529", pv["plan_hash"], "owner", env.clock())["status"] == "ROLLED_BACK"
    env.broker.deposit(500.0, evidence="bank transfer during migration")
    env.clock.advance(minutes=1)
    assert e.reconcile("dep", env.clock()).clean
    pv = e.switcher.preview("QC_529", env.clock())
    assert e.switcher.activate("QC_529", pv["plan_hash"], "owner", env.clock())["status"] == "COMPLETED"
    env.clock.advance(minutes=2)
    pv = e.switcher.preview("QC_60_SQQQ", env.clock())
    res = e.switcher.activate("QC_60_SQQQ", pv["plan_hash"], "owner", env.clock())
    assert res["status"] == "COMPLETED", res
    assert strategy_manager.active(e.db)["strategy_id"] == "QC_60_SQQQ"
    flows = {r["kind"]: r["n"] for r in e.db.q("SELECT kind, COUNT(*) n FROM cash_flows GROUP BY kind")}
    assert flows["INITIAL_CAPITAL"] == 1 and flows["DEPOSIT"] == 1
    total = e.db.one("SELECT SUM(amount) s FROM cash_flows")["s"]
    assert abs(total - env.broker.s["credit"]) <= 0.05
    assert gdx in env.broker.s["positions"] and env.broker.s["positions"][gdx]["amount"] == 300.0
    assert e.db.one("SELECT owner FROM ownership WHERE position_id=?", (gdx,))["owner"] == "PRE_EXISTING"
    assert e.db.verify_audit_chain()[0]
    refs = [s["client_ref"] for s in env.broker.s["submissions"] if s["client_ref"] != "EXTERNAL"]
    assert len(refs) == len(set(refs))
    plans = [r["status"] for r in e.db.q("SELECT status FROM switch_plans ORDER BY created_at")]
    assert plans.count("ROLLED_BACK") == 1 and plans.count("COMPLETED") == 2
    held = {e.id_to_symbol[p["instrument_id"]] for k, p in env.broker.s["positions"].items() if k != gdx}
    assert held == {"TQQQ"}
    fund = accounting.funding_summary(e.db)
    assert fund["INITIAL_CAPITAL"] == 1700.0 and fund["DEPOSIT"] == 500.0
    assert e.reconcile("end", env.clock()).clean
    kinds_ = [r["kind"] for r in e.db.q("SELECT kind FROM audit_log")]
    assert kinds_.count("BOOTSTRAP") == 1 and json.dumps(kinds_).count("STRATEGY_ACTIVATED") == 3


def test_f5_data_check_works_before_first_start(tmp_path):
    """INVARIANT (F5): `data check` on a never-started state directory checks the configured initial strategy
    instead of crashing (no active strategy exists yet)."""
    from utengine.core.engine import TradingEngine
    env = Env(tmp_path)
    e = TradingEngine(env.cfg_path, env.home, clock=env.clock, broker=env.broker, bar_store=env.bar_store)
    assert strategy_manager.active(e.db) is None
    assert e.data_check(now=DECIDE_AT)["status"] == "OK"
