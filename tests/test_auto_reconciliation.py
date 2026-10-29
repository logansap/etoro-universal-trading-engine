"""Automatic, exception-only reconciliation (docs/AUTOMATIC_RECONCILIATION_POLICY.md). Simulated broker only.

Routine, fully explained changes produce no alert; each material problem is ONE durable exception and ONE
notification however long it persists; nothing is ever booked to make the numbers balance."""
import datetime as dt

from conftest import IDS, Env, multi_env
from utengine.brokers.simulated import SimulatedBroker
from utengine.core import halts
from utengine.core.errors import BrokerUnavailable


def alerts(e, kind=None):
    q = "SELECT * FROM alerts WHERE suppressed=0 AND severity IN ('WARNING','CRITICAL')"
    rows = [dict(r) for r in e.db.q(q)]
    return [r for r in rows if kind is None or r["kind"] == kind]


def test_routine_explained_changes_are_silent(tmp_path):
    """INVARIANT: trades, a broker-evidenced deposit, dividend and fee, over many reconciliations, raise no alert and
    no exception; every cash change is explained by an evidence-backed flow."""
    env = multi_env(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(400.0, evidence="bank transfer T-1")
    env.broker.adjust_cash(1.25, kind="DIVIDEND", evidence="TLT dividend")
    env.broker.adjust_cash(-0.40, kind="FEE", evidence="overnight fee")
    for _ in range(10):
        env.clock.advance(minutes=1)
        assert e.reconcile("routine", env.clock()).clean
    assert alerts(e) == [] and halts.open_exceptions(e.db) == []
    kinds = {r["kind"] for r in e.db.q("SELECT kind FROM cash_flows")}
    assert {"INITIAL_CAPITAL", "TRADE_OPEN", "DEPOSIT", "DIVIDEND", "FEE"} <= kinds


def test_broker_outage_is_one_exception_and_clears_itself(tmp_path, monkeypatch):
    """INVARIANT: repeated read failures (with different error texts) are ONE halt / exception / notification; the
    first good read clears the halt and resolves the exception automatically."""
    env = Env(tmp_path)
    e = env.engine()
    real = SimulatedBroker.account_snapshot
    n = {"i": 0}

    def flaky(self):
        n["i"] += 1
        raise BrokerUnavailable(f"timeout #{n['i']}")
    monkeypatch.setattr(SimulatedBroker, "account_snapshot", flaky)
    for _ in range(4):
        env.clock.advance(minutes=1)
        assert e.reconcile("t", env.clock()).status == "UNAVAILABLE"
    assert len(alerts(e, "BROKER_UNAVAILABLE")) == 1
    assert [x["occurrences"] for x in halts.open_exceptions(e.db)] == [4]
    monkeypatch.setattr(SimulatedBroker, "account_snapshot", real)
    env.clock.advance(minutes=1)
    assert e.reconcile("t", env.clock()).clean
    assert halts.active(e.db) == [] and halts.open_exceptions(e.db) == []


def test_stale_broker_state_halts_until_fresh(tmp_path, monkeypatch):
    """INVARIANT: a broker snapshot older than max_broker_state_age_seconds is not trusted (halt), and the halt
    clears itself when fresh state returns."""
    env = Env(tmp_path)
    e = env.engine()
    real = SimulatedBroker.account_snapshot

    def old(self):
        from dataclasses import replace
        return replace(real(self), as_of=(env.clock() - dt.timedelta(minutes=30)).isoformat())
    monkeypatch.setattr(SimulatedBroker, "account_snapshot", old)
    env.clock.advance(minutes=1)
    assert "BROKER_STATE_STALE" in [i["kind"] for i in e.reconcile("t", env.clock()).issues]
    monkeypatch.setattr(SimulatedBroker, "account_snapshot", real)
    env.clock.advance(minutes=1)
    assert e.reconcile("t", env.clock()).clean


def test_external_trade_is_one_notification_however_often_seen(tmp_path):
    """INVARIANT: an unexpected external position is reported once; later reconciliations only count it."""
    env = Env(tmp_path)
    e = env.engine()
    env.broker.external_open(IDS["QQQ"], 50.0)
    for _ in range(6):
        env.clock.advance(minutes=1)
        e.reconcile("t", env.clock())
    assert len(alerts(e, "EXTERNAL_POSITION")) == 1
    exc = [x for x in halts.open_exceptions(e.db) if x["kind"] == "EXTERNAL_POSITION"]
    assert len(exc) == 1 and exc[0]["occurrences"] == 6


def test_pending_order_is_one_warning_resolved_when_final(tmp_path):
    """INVARIANT: an order still pending at the broker is one WARNING exception that resolves itself once the
    broker reports it final (no operator action needed for a routine fill)."""
    from conftest import DECIDE_AT, EXECUTE_AT
    env = Env(tmp_path)
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.broker.inject("pending")
    env.clock.set(EXECUTE_AT)
    e.cycle()
    for _ in range(3):
        env.clock.advance(minutes=1)
        e.reconcile("t", env.clock())
    assert len(alerts(e, "UNRESOLVED_ORDER")) == 1
    env.broker.advance()
    e.executor.resolve_all()
    env.clock.advance(minutes=1)
    e.reconcile("t", env.clock())
    assert not [x for x in halts.open_exceptions(e.db) if x["kind"] == "UNRESOLVED_ORDER"]


def test_cleared_issue_that_recurs_notifies_again(tmp_path):
    """INVARIANT: de-duplication never hides a NEW occurrence: after an exception is resolved, the same problem
    appearing again re-opens it and notifies once more."""
    env = Env(tmp_path, broker_kwargs={"cash_tx_capability": False})
    e = env.engine()
    from utengine.core import accounting
    env.broker.adjust_cash(10.0, record=False)
    env.clock.advance(minutes=1)
    e.reconcile("t", env.clock())
    d = e.db.one("SELECT disc_id FROM discrepancies WHERE status='OPEN'")["disc_id"]
    accounting.resolve_discrepancy(e.db, d, "DEPOSIT", "bank statement", "owner", e.nows())
    for h in halts.active(e.db):
        halts.clear(e.db, h["halt_id"], "owner", "classified", e.nows())
    env.clock.advance(minutes=1)
    assert e.reconcile("t", env.clock()).clean
    env.broker.adjust_cash(7.0, record=False)
    env.clock.advance(minutes=1)
    e.reconcile("t", env.clock())
    assert len(alerts(e, "CASH_DISCREPANCY")) == 2
