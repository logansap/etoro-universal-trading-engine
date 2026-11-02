"""Notification policy for broker availability (2026-11-02, owner-approved) and monitor-only engines.

SAFETY is unchanged and immediate: a failed or stale broker read halts ALL new orders at once. Only the NOTIFICATION
of these self-clearing halts is deferred: silent if it recovers within 3 minutes, one WARNING at 3 minutes, one
CRITICAL at 5 minutes, one RECOVERED only after a delivered alert, one WARNING for an intermittent run, and an
immediate CRITICAL when an execution window closes missed or incomplete because the broker was unavailable. Every
other halt still alerts CRITICAL at once. Monitoring commands build a monitor-only engine (no writer, cannot trade),
so the health timer and crash hook work in live mode without the trading key."""
import datetime as dt
import json

import pytest

from conftest import DECIDE_AT, EXECUTE_AT
from test_autonomous_live import ENV, WRITE, _activation, _live_yaml, auto_env, sent
from utengine.brokers.simulated import SimulatedBroker
from utengine.core import halts
from utengine.core.engine import TradingEngine
from utengine.core.errors import BrokerUnavailable, EngineError, LiveExecutionNotAvailable

BROKER = ("BROKER_UNAVAILABLE", "BROKER_UNAVAILABLE_PERSISTENT", "BROKER_UNAVAILABLE_INTERMITTENT",
          "BROKER_UNAVAILABLE_RECOVERED", "BROKER_UNAVAILABLE_INTERMITTENT_RECOVERED",
          "MISSED_EXECUTION_BROKER_UNAVAILABLE", "INCOMPLETE_EXECUTION_BROKER_UNAVAILABLE")


def created(e, kinds=BROKER):
    """Non-suppressed alerts of the broker-availability kinds, in order: (severity, kind)."""
    return [(r["severity"], r["kind"]) for r in e.db.q("SELECT * FROM alerts WHERE suppressed=0 ORDER BY id")
            if r["kind"] in kinds]


def delivered(e, kinds=BROKER):
    return [(r["severity"], r["kind"]) for r in e.db.q(
        "SELECT * FROM alerts WHERE suppressed=0 AND delivered_at IS NOT NULL AND last_error IS NULL ORDER BY id")
        if r["kind"] in kinds]


class Outage:
    """Every broker account read fails while on; restores the simulated broker when off."""
    def __init__(self, monkeypatch):
        self.mp, self.real = monkeypatch, SimulatedBroker.account_snapshot

    def on(self):
        def down(_self):
            raise BrokerUnavailable("GET /api/v1/trading/info/real/pnl: TimeoutError")
        self.mp.setattr(SimulatedBroker, "account_snapshot", down)

    def off(self):
        self.mp.setattr(SimulatedBroker, "account_snapshot", self.real)


def run(env, start, minutes, step_s=60):
    t = start
    while t <= start + dt.timedelta(minutes=minutes):
        env.clock.set(t)
        env.e.cycle()
        t += dt.timedelta(seconds=step_s)
    return t


def idle(minutes=-90):
    return EXECUTE_AT + dt.timedelta(minutes=minutes)        # before the open: no decision or execution window


# ------------------------------------------------------------------------------------------------ safety, unchanged
def test_first_failed_read_blocks_all_new_orders_immediately_and_nothing_is_sent(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    env.clock.set(DECIDE_AT)
    env.e.cycle()
    out = Outage(monkeypatch)
    out.on()
    env.clock.set(EXECUTE_AT)
    env.e.cycle()
    assert [(h["kind"], h["scope"]) for h in halts.active(env.e.db)] == [("BROKER_UNAVAILABLE", "ALL")]
    assert sent(env) == []
    assert created(env.e) == []                              # blocked at once; nobody paged for a blip


# ------------------------------------------------------------------------------------------------ transient
def test_short_outage_inside_the_execution_window_is_silent_when_execution_then_completes(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    env.clock.set(DECIDE_AT)
    env.e.cycle()
    out = Outage(monkeypatch)
    out.on()
    for s in (0, 30, 60):                                    # 09:31:00 .. 09:32:00 down
        env.clock.set(EXECUTE_AT + dt.timedelta(seconds=s))
        env.e.cycle()
        assert [h["kind"] for h in halts.active(env.e.db)] == ["BROKER_UNAVAILABLE"]   # really halted
    assert sent(env) == []
    out.off()
    run(env, EXECUTE_AT + dt.timedelta(seconds=110), 70)      # recovered after ~110 s; window runs to its end
    assert len(sent(env)) > 0
    assert halts.active(env.e.db) == []
    assert created(env.e) == []                              # no warning, no critical, no recovered, no missed
    assert created(env.e, ("MISSED_EXECUTION", "INCOMPLETE_EXECUTION")) == []


def test_transient_outage_while_idle_is_silent_and_is_re_read_every_cycle(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    out = Outage(monkeypatch)
    out.on()
    env.clock.set(idle())
    env.e.reconcile("idle", env.clock())                     # the failed read (any read point)
    out.off()
    env.clock.set(idle() + dt.timedelta(seconds=70))
    env.e.cycle()                                            # retried at once, not 15 minutes later
    assert halts.active(env.e.db) == [] and created(env.e) == []


# ------------------------------------------------------------------------------------------------ persistent
def test_four_minute_outage_one_warning_then_one_recovered_in_readable_ntfy_text(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    posts = []
    env.e.notifier.url = "https://ntfy.sh/ute-test-topic"
    env.e.notifier.transport = lambda url, body, timeout: posts.append((url, body)) or 200
    out = Outage(monkeypatch)
    out.on()
    t = run(env, idle(), 4)
    assert delivered(env.e) == [("WARNING", "BROKER_UNAVAILABLE_PERSISTENT")]
    out.off()
    run(env, t, 2)
    assert delivered(env.e) == [("WARNING", "BROKER_UNAVAILABLE_PERSISTENT"),
                                ("INFO", "BROKER_UNAVAILABLE_RECOVERED")]
    assert len(posts) == 2
    for url, body in posts:
        text = body.decode("utf-8")
        assert not text.lstrip().startswith("{") and "What to do:" in text      # human-readable, not raw JSON
        assert "title=" in url and "priority=" in url
    assert "priority=4" in posts[0][0] and "priority=2" in posts[1][0]


def test_six_minute_outage_one_warning_one_critical_one_recovered_across_a_restart(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    out = Outage(monkeypatch)
    out.on()
    t = run(env, idle(), 4)
    env.engine()                                             # restart mid-incident: state comes from the database
    out.on()
    t = run(env, t, 3)
    out.off()
    run(env, t, 3)
    assert created(env.e) == [("WARNING", "BROKER_UNAVAILABLE_PERSISTENT"),
                              ("CRITICAL", "BROKER_UNAVAILABLE_PERSISTENT"),
                              ("INFO", "BROKER_UNAVAILABLE_RECOVERED")]


# ------------------------------------------------------------------------------------------------ intermittent
def test_repeated_short_outages_are_one_intermittent_incident(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    out = Outage(monkeypatch)
    t = idle(-120)
    for _ in range(4):                                       # 4 failures of ~1 minute, 2 minutes apart
        out.on()
        t = run(env, t, 1)
        out.off()
        t = run(env, t, 2)
    assert created(env.e) == [("WARNING", "BROKER_UNAVAILABLE_INTERMITTENT")]
    run(env, t, 16)                                          # quiet for longer than the run gap
    assert created(env.e) == [("WARNING", "BROKER_UNAVAILABLE_INTERMITTENT"),
                              ("INFO", "BROKER_UNAVAILABLE_INTERMITTENT_RECOVERED")]


# ------------------------------------------------------------------------------------------------ delivery failures
def test_escalation_not_delivered_before_recovery_is_superseded_and_no_recovered_is_sent(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    env.e.notifier.url = "https://ntfy.sh/ute-test-topic"

    def fail(url, body, timeout):
        raise OSError("network down")
    env.e.notifier.transport = fail
    out = Outage(monkeypatch)
    out.on()
    t = run(env, idle(), 4)
    rows = [dict(r) for r in env.e.db.q("SELECT * FROM alerts WHERE kind='BROKER_UNAVAILABLE_PERSISTENT'")]
    assert len(rows) == 1 and rows[0]["delivered_at"] is None and rows[0]["attempts"] >= 1   # retried, one row
    out.off()
    run(env, t, 2)
    row = dict(env.e.db.one("SELECT * FROM alerts WHERE kind='BROKER_UNAVAILABLE_PERSISTENT'"))
    assert row["last_error"].startswith("superseded")         # never arrives after the problem is gone
    assert ("INFO", "BROKER_UNAVAILABLE_RECOVERED") not in created(env.e)


def test_failed_delivery_is_retried_until_it_succeeds_without_duplicates(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    env.e.notifier.url = "https://ntfy.sh/ute-test-topic"
    calls = {"n": 0}

    def flaky(url, body, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("network down")
        return 200
    env.e.notifier.transport = flaky
    out = Outage(monkeypatch)
    out.on()
    run(env, idle(), 4)
    assert delivered(env.e) == [("WARNING", "BROKER_UNAVAILABLE_PERSISTENT")]
    assert env.e.db.one("SELECT COUNT(*) c FROM alerts WHERE kind='BROKER_UNAVAILABLE_PERSISTENT'")["c"] == 1


# ------------------------------------------------------------------------------------------------ execution window
def test_outage_through_the_whole_window_is_one_immediate_critical_for_the_missed_execution(tmp_path, monkeypatch):
    env = auto_env(tmp_path)
    env.clock.set(DECIDE_AT)
    env.e.cycle()
    out = Outage(monkeypatch)
    out.on()
    run(env, EXECUTE_AT - dt.timedelta(minutes=1), 63)        # the window (60 min) closes while still down
    assert sent(env) == []
    kinds = created(env.e)
    assert kinds.count(("CRITICAL", "MISSED_EXECUTION_BROKER_UNAVAILABLE")) == 1
    assert kinds.count(("WARNING", "BROKER_UNAVAILABLE_PERSISTENT")) == 1
    assert kinds.count(("CRITICAL", "BROKER_UNAVAILABLE_PERSISTENT")) == 1


# ------------------------------------------------------------------------------------------------ engine stalls / crash
def test_health_timer_escalates_when_the_engine_stops_cycling(tmp_path, monkeypatch):
    from utengine.cli import main as cli
    env = auto_env(tmp_path)
    out = Outage(monkeypatch)
    out.on()
    env.clock.set(idle())
    env.e.cycle()                                            # the last cycle before the engine stalls
    later = (idle() + dt.timedelta(minutes=7)).isoformat()
    cli(["--config", str(env.cfg_path), "--home", str(env.home), "--now", later, "health", "--notify"])
    kinds = [(r["severity"], r["kind"]) for r in env.e.db.q("SELECT * FROM alerts WHERE suppressed=0 ORDER BY id")]
    assert ("CRITICAL", "BROKER_UNAVAILABLE_PERSISTENT") in kinds and ("CRITICAL", "HEARTBEAT_STALE") in kinds


# ------------------------------------------------------------------------------------------------ immediate criticals
@pytest.mark.parametrize("kind", ["UNKNOWN_ORDER", "EXTERNAL_POSITION", "POSITION_MISMATCH", "CASH_DISCREPANCY"])
def test_urgent_conditions_still_alert_critical_immediately(tmp_path, kind):
    env = auto_env(tmp_path)
    halts.raise_halt(env.e.db, kind, "ALL", f"{kind} detail", env.e.nows(), subject="x")
    assert [(r["severity"], r["kind"]) for r in env.e.db.q("SELECT * FROM alerts WHERE suppressed=0")] == [
        ("CRITICAL", kind)]


# ------------------------------------------------------------------------------------------------ monitor-only engines
def test_monitor_only_engine_works_in_live_mode_without_the_trading_key_and_cannot_trade(tmp_path):
    cfg = _live_yaml(tmp_path)
    ro = {k: v for k, v in ENV.items() if "READONLY" in k}
    with pytest.raises(LiveExecutionNotAvailable):
        TradingEngine(cfg, tmp_path / "home", env=ro)          # a full engine needs the trading key and activation
    m = TradingEngine(cfg, tmp_path / "home", env=ro, monitor_only=True)
    assert m.writer is None and m.status()["write_capability"] is False and m.status()["monitor_only"] is True
    with pytest.raises(EngineError):
        m.cycle()
    with pytest.raises(EngineError):
        m.startup()
    with pytest.raises(LiveExecutionNotAvailable):
        m.live_gate(ro)                                     # no trading key: the gate refuses


def test_live_gate_check_passes_with_every_gate_and_records_nothing(tmp_path, monkeypatch):
    from utengine.core import security, takeover
    from utengine.core.security import key_hash
    cfg_path = _live_yaml(tmp_path)
    from utengine.core.configuration import load_engine_config
    cfg = load_engine_config(cfg_path)
    (tmp_path / "forbidden.sha256").write_text(key_hash("old-gold-miner-user-key") + "\n", encoding="utf-8")
    _activation(tmp_path, cfg, release_sha=takeover.release_id())
    m = TradingEngine(cfg_path, tmp_path / "home", env=dict(ENV), monitor_only=True)
    real = security.authorize_live

    def no_network(**kw):
        return real(**{**kw, "scope_probe": WRITE})          # the scope probe is the only network call
    monkeypatch.setattr(security, "authorize_live", no_network)
    before = m.db.one("SELECT COUNT(*) c FROM audit_log")["c"]
    auth, _tr, _env = m.live_gate(dict(ENV))
    assert auth.approved_by == "owner" and m.writer is None
    assert m.db.one("SELECT COUNT(*) c FROM audit_log")["c"] == before      # no LIVE_WRITER_AUTHORIZED recorded


# ------------------------------------------------------------------------------------------------ the real health unit
def test_health_unit_path_in_live_mode_without_any_credentials_stopped_hung_and_restarted(tmp_path, monkeypatch):
    """The installed etoro-ute-health.service loads ONLY ops.env: no trading key, no read-only key. INVARIANT: in a
    live configuration `health --notify` still runs (monitor-only engine), pages the stalled engine and the broker
    outage once per incident (not every 5-minute run), and never submits or plans an order. A restarted trading
    engine continues the same incident without duplicating its alerts."""
    from utengine.cli import main as cli
    for k in list(ENV) + ["UTE_ETORO_READONLY_API_KEY", "UTE_ETORO_READONLY_USER_KEY"]:
        monkeypatch.delenv(k, raising=False)
    cfg = _live_yaml(tmp_path)
    home = tmp_path / "home"
    t0 = dt.datetime(2026, 11, 3, 14, 40, tzinfo=dt.timezone.utc)
    m = TradingEngine(cfg, home, env={}, monitor_only=True)
    # the trading engine's last state before it stopped / hung: a heartbeat and an active broker outage
    m.db.heartbeat("engine", {"phase": "cycle", "mode": "live"}, ts=t0.isoformat())
    halts.raise_halt(m.db, "BROKER_UNAVAILABLE", "ALL", "account read failed: TimeoutError", t0.isoformat(),
                     subject="broker-read")
    for minutes in (2, 7, 12, 17):                            # successive 5-minute health timer runs
        rc = cli(["--config", str(cfg), "--home", str(home), "--now", (t0 + dt.timedelta(minutes=minutes)).isoformat(),
                  "health", "--notify"])
        assert rc == 1                                        # unhealthy, reported -- but it ran
    rows = [(r["severity"], r["kind"]) for r in m.db.q("SELECT * FROM alerts WHERE suppressed=0 ORDER BY id")]
    assert rows.count(("CRITICAL", "HEARTBEAT_STALE")) == 1              # once per incident, not every run
    assert rows.count(("WARNING", "BROKER_UNAVAILABLE_PERSISTENT")) == 0  # first escalation run was already > 5 min
    assert rows.count(("CRITICAL", "BROKER_UNAVAILABLE_PERSISTENT")) == 1
    assert m.db.one("SELECT COUNT(*) c FROM intents")["c"] == 0          # monitor-only: nothing planned or sent
    assert m.db.one("SELECT COUNT(*) c FROM plans")["c"] == 0
    # restarted trading engine (simulation path), same incident shape: no duplicate alerts, no duplicate orders
    env = auto_env(tmp_path / "restart")
    out = Outage(monkeypatch)
    out.on()
    t = run(env, idle(), 6)
    env.engine()
    out.on()
    run(env, t, 3)
    kinds = created(env.e)
    assert kinds.count(("WARNING", "BROKER_UNAVAILABLE_PERSISTENT")) == 1
    assert kinds.count(("CRITICAL", "BROKER_UNAVAILABLE_PERSISTENT")) == 1
    assert sent(env) == []
