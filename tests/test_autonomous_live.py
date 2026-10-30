"""AUTONOMOUS live release (0.11.x): no per-order approval; the SAME hard limits, send-time quote/spread checks, kill
switch, fill-based reconciliation and recovery as every other mode. `simulation` with a live_controls section runs
exactly the autonomous execution path with the simulated broker (faults: partial fills, broker outages, lost
responses, crashes). The one-time activation gate (authorize_live) is tested without network."""
import datetime as dt
import json

import pytest
import yaml

from conftest import DECIDE_AT, EXECUTE_AT, Env, extended_store, write_engine_yaml
from utengine.brokers.simulated import SimulatedCrash
from utengine.core import halts, sleeves, supervision
from utengine.core.configuration import load_engine_config
from utengine.core.errors import ConfigError, LiveExecutionNotAvailable
from utengine.core.security import LIVE_ACTIVATION_PHRASE, authorize_live, key_hash

A = "QC_529_ETORO_RESTRICTED"
UTC = dt.timezone.utc
ONE = {"mode": "multi_strategy", "funding_policy": "proportional", "rebalance_policy": "strategy_independent",
       "aggregate_risk": "enforced", "sleeves": [{"strategy": A, "weight": 1.0}]}


def controls(tmp_path, **over):
    c = {"max_order_usd": 1500.0, "max_gross_usd": 2000.0, "max_orders_per_session": 12, "daily_loss_stop_usd": 400.0,
         "cumulative_loss_stop_usd": 800.0, "activation_file": str(tmp_path / "activation.json"),
         "forbidden_hashes_file": str(tmp_path / "forbidden.sha256")}
    c.update(over)
    return c


def auto_env(tmp_path, bar_store=None, risk=None, **over):
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml", portfolio=ONE, initial=A, **(risk or {}))
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["live_controls"] = controls(tmp_path, **over)
    cfg.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    env = Env(tmp_path, cfg_path=cfg, bar_store=bar_store)
    env.engine()
    assert env.e.executor.supervisor is not None            # the guard is active (autonomous path)
    return env


def sent(env):
    return [s for s in env.broker.s["submissions"] if s["client_ref"] != "EXTERNAL"]


def statuses(e):
    return [r["status"] for r in e.db.q("SELECT status FROM intents ORDER BY created_at")]


# ------------------------------------------------------------------------------------- autonomous execution
def test_autonomous_first_acquisition_without_any_approval_and_no_duplicates_on_restart(tmp_path):
    """INVARIANT: in the autonomous path every routine order is sent without approval, exactly once; a restart and
    later cycles resend nothing; sleeve units == broker units after the plan."""
    env = auto_env(tmp_path)
    d, x = env.decide_and_execute()
    n = len(sent(env))
    assert n > 0 and set(statuses(env.e)) <= {"FILLED", "RECONCILED"}
    env.engine()
    for m in (5, 20, 50):
        env.clock.set(EXECUTE_AT + dt.timedelta(minutes=m))
        env.e.cycle()
    assert len(sent(env)) == n
    from utengine.core.reconciliation import engine_position_ids
    r = env.e.reconcile("check", env.clock())
    assert r.clean and not sleeves.check_invariants(env.e.db, r.snapshot, engine_position_ids(env.e.db))


def test_autonomous_caps_block_and_kill_switch_stops_everything(tmp_path):
    env = auto_env(tmp_path, max_order_usd=600.0, max_gross_usd=700.0)
    env.decide_and_execute()
    total = sum(env.broker.s["orders"][s["order_id"]]["amount"] for s in sent(env))
    reasons = [json.loads(r["detail"]).get("reason", "") for r in env.e.db.q(
        "SELECT detail FROM order_events WHERE to_status='BLOCKED'")]
    assert total <= 700.0 + 1e-6 and any("max_order_usd" in x or "max_gross_usd" in x for x in reasons)
    env2 = auto_env(tmp_path / "k")
    supervision.engage_kill_switch(env2.e.cfg, env2.e.home, "owner", "emergency drill", env2.e.nows(), env2.e.db)
    env2.decide_and_execute()
    assert sent(env2) == [] and "KILL_SWITCH" in {h["kind"] for h in halts.active(env2.e.db)}
    assert env2.e.db.one("SELECT COUNT(*) c FROM alerts WHERE kind='KILL_SWITCH'")["c"] == 1


def test_stale_quote_holds_then_sends_when_fresh_within_the_window(tmp_path, monkeypatch):
    """INVARIANT: a stale broker quote at send time HOLDs the order (status HELD, not sent); when quotes are fresh
    again within the execution window it is sent once; nothing is sent after the window."""
    env = auto_env(tmp_path)
    real = env.broker.quotes

    def stale(ids):
        out = real(ids)
        for k, q in out.items():
            out[k] = q.__class__(q.instrument_id, q.bid, q.ask, (env.clock() - dt.timedelta(hours=2)).isoformat())
        return out
    monkeypatch.setattr(env.broker, "quotes", stale)
    env.clock.set(DECIDE_AT)
    env.e.cycle()
    env.clock.set(EXECUTE_AT)
    env.e.cycle()
    assert sent(env) == [] and "HELD" in [r["status"] for r in env.e.db.q("SELECT status FROM plans")]
    monkeypatch.setattr(env.broker, "quotes", real)
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=5))
    env.e.cycle()
    n = len(sent(env))
    assert n > 0
    env.clock.set(EXECUTE_AT + dt.timedelta(hours=3))
    env.e.cycle()
    assert len(sent(env)) == n


@pytest.mark.parametrize("fault,final", [("partial:0.5", {"FILLED", "RECONCILED", "PARTIALLY_FILLED"}),
                                         ("timeout_after_accept", {"FILLED", "RECONCILED"}),
                                         ("reject:746", {"RECONCILED", "FILLED", "REJECTED"})])
def test_broker_faults_are_resolved_from_evidence_never_resubmitted(tmp_path, fault, final):
    env = auto_env(tmp_path)
    env.clock.set(DECIDE_AT)
    env.e.cycle()
    env.broker.inject(fault)
    env.clock.set(EXECUTE_AT)
    env.e.cycle()
    if fault.startswith("partial"):
        env.broker.advance()
        env.clock.set(EXECUTE_AT + dt.timedelta(minutes=5))
        env.e.cycle()
    ids = [s["client_ref"] for s in sent(env)]
    assert len(ids) == len(set(ids)), "an intent was submitted twice"
    assert set(statuses(env.e)) <= final | {"BLOCKED"}


def test_lost_response_without_evidence_fails_closed_with_an_alert(tmp_path):
    """INVARIANT: a connection loss with no broker evidence leaves the order UNKNOWN, halts ALL, raises a CRITICAL
    alert, and is never resubmitted on later cycles or restarts."""
    env = auto_env(tmp_path)
    env.clock.set(DECIDE_AT)
    env.e.cycle()
    env.broker.inject("network_before")
    env.clock.set(EXECUTE_AT)
    env.e.cycle()
    assert "UNKNOWN" in statuses(env.e) and "UNKNOWN_ORDER" in {h["kind"] for h in halts.active(env.e.db)}
    assert env.e.db.one("SELECT COUNT(*) c FROM alerts WHERE severity='CRITICAL'")["c"] >= 1
    n = len(sent(env))
    env.engine()
    for m in (5, 10):
        env.clock.set(EXECUTE_AT + dt.timedelta(minutes=m))
        env.e.cycle()
    assert len(sent(env)) == n


def test_broker_outage_at_execution_retries_inside_the_window(tmp_path):
    """INVARIANT: an unreadable broker before execution blocks the plan (nothing sent); the next cycle inside the
    window executes it normally."""
    env = auto_env(tmp_path)
    env.clock.set(DECIDE_AT)
    env.e.cycle()
    env.broker.inject("read_unavailable")
    env.clock.set(EXECUTE_AT)
    env.e.cycle()
    assert sent(env) == []
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=3))
    env.e.cycle()
    assert len(sent(env)) > 0


def test_crash_after_broker_accept_recovers_on_restart_without_duplicates(tmp_path):
    env = auto_env(tmp_path)
    env.clock.set(DECIDE_AT)
    env.e.cycle()
    env.broker.inject("crash_after_submit")
    env.clock.set(EXECUTE_AT)
    with pytest.raises(SimulatedCrash):
        env.e.cycle()
    env.engine()
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=2))
    env.e.cycle()
    ids = [s["client_ref"] for s in sent(env)]
    assert len(ids) == len(set(ids)) and "UNKNOWN" not in statuses(env.e)


def _force_hold(e, session):
    row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session=?", (A, session))
    d = json.loads(row["decision"])
    d["rebalance"] = False
    e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), A, session))
    return d


def test_missed_window_then_hold_carries_the_standing_target_forward_once(tmp_path):
    """INVARIANT (blocker 1): the engine is down for the whole 10-27 window, so the 10-26 rebalance never executes and
    no plan for it is ever created later (never late). The 10-27 decision is a HOLD (same standing weights): at the
    10-28 open, inside a fresh valid window, the standing target is carried forward with fresh quotes and every risk
    check, sent exactly once; the next HOLD session (target now executed) trades nothing."""
    bars = extended_store({dt.date(2026, 10, 27): 1.0, dt.date(2026, 10, 28): 1.0})
    env = auto_env(tmp_path, bar_store=bars)
    e = env.e
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.clock.set(dt.datetime(2026, 10, 27, 18, 0, tzinfo=UTC))        # down through the whole 10-27 window
    e.cycle()
    assert sent(env) == [] and e.db.one("SELECT COUNT(*) c FROM plans")["c"] == 0
    env.clock.set(dt.datetime(2026, 10, 27, 20, 55, tzinfo=UTC))
    e.cycle()
    d = _force_hold(e, "2026-10-27")
    env.clock.set(dt.datetime(2026, 10, 28, 13, 35, tzinfo=UTC))
    e.cycle()
    n = len(sent(env))
    assert n > 0 and e.db.one("SELECT COUNT(*) c FROM audit_log WHERE kind='UNEXECUTED_TARGET_CARRIED_FORWARD'")["c"] == 1
    assert {r["session"] for r in e.db.q("SELECT session FROM plans")} == {"2026-10-27"}   # never a 10-26 plan
    bought = {env.broker.s["instruments"][str(env.broker.s["orders"][x["order_id"]]["instrument_id"])] for x in sent(env)}
    assert bought <= {t for t, w in d["target_weights"].items() if w > 0}
    env.engine()
    env.clock.set(dt.datetime(2026, 10, 28, 13, 50, tzinfo=UTC))
    env.e.cycle()
    env.clock.set(dt.datetime(2026, 10, 28, 20, 55, tzinfo=UTC))
    env.e.cycle()
    _force_hold(env.e, "2026-10-28")
    env.clock.set(dt.datetime(2026, 10, 29, 13, 35, tzinfo=UTC))
    x = env.e.cycle()["execution"]["2026-10-28"]
    assert x["status"] == "NO_REBALANCE" and len(sent(env)) == n


def test_held_plan_abandoned_at_window_end_is_carried_forward_next_session(tmp_path, monkeypatch):
    """INVARIANT: an order HELD for stale quotes during its whole window is ABANDONED at the window end (never sent
    late); the next session's HOLD carries the standing target forward into its own fresh window."""
    bars = extended_store({dt.date(2026, 10, 27): 1.0})
    env = auto_env(tmp_path, bar_store=bars)
    e = env.e
    real = env.broker.quotes

    def stale(ids):
        return {k: q.__class__(q.instrument_id, q.bid, q.ask, (env.clock() - dt.timedelta(hours=2)).isoformat())
                for k, q in real(ids).items()}
    monkeypatch.setattr(env.broker, "quotes", stale)
    env.clock.set(DECIDE_AT)
    e.cycle()
    for m in (0, 20, 40):
        env.clock.set(EXECUTE_AT + dt.timedelta(minutes=m))
        e.cycle()
    env.clock.set(dt.datetime(2026, 10, 27, 18, 0, tzinfo=UTC))
    e.cycle()
    assert sent(env) == [] and set(statuses(e)) == {"ABANDONED"}
    monkeypatch.setattr(env.broker, "quotes", real)
    env.clock.set(dt.datetime(2026, 10, 27, 20, 55, tzinfo=UTC))
    e.cycle()
    _force_hold(e, "2026-10-27")
    env.clock.set(dt.datetime(2026, 10, 28, 13, 35, tzinfo=UTC))
    e.cycle()
    assert len(sent(env)) > 0 and len({x["client_ref"] for x in sent(env)}) == len(sent(env))


def test_friday_hold_weekend_capital_transfer_monday_open_autonomous(tmp_path):
    """Friday 16:50 ET HOLD; Saturday audited capital transfer; nothing before Monday's open; Monday 09:35 EST the
    standing weights are bought automatically (no approval), exactly once."""
    fri = dt.date(2026, 10, 30)
    bars = extended_store({dt.date(2026, 10, 27): 1.0, dt.date(2026, 10, 28): 1.0, dt.date(2026, 10, 29): 1.0,
                           fri: 1.0})
    env = auto_env(tmp_path, bar_store=bars)
    e = env.e
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.clock.set(dt.datetime(2026, 10, 27, 18, 0, tzinfo=UTC))       # 10-26 plan window missed (never sent)
    e.cycle()
    env.clock.set(dt.datetime(2026, 10, 30, 20, 55, tzinfo=UTC))
    e.cycle()
    row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session=?", (A, fri.isoformat()))
    d = json.loads(row["decision"])
    if d["rebalance"]:
        d["rebalance"] = False
        e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), A, fri.isoformat()))
    env.clock.set(dt.datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    sleeves.request_catch_up(e.db, A, "pool allocation", "test:transfer", e.nows())
    env.clock.set(dt.datetime(2026, 11, 2, 13, 40, tzinfo=UTC))       # Monday pre-open (EST)
    e.cycle()
    assert sent(env) == []
    env.clock.set(dt.datetime(2026, 11, 2, 14, 35, tzinfo=UTC))
    e.cycle()
    n = len(sent(env))
    assert n > 0 and sleeves.catch_up_pending(e.db, A) is None
    env.engine()
    env.clock.set(dt.datetime(2026, 11, 2, 14, 50, tzinfo=UTC))
    env.e.cycle()
    assert len(sent(env)) == n


# ------------------------------------------------------------------------------------- configuration and activation
def _live_yaml(tmp_path, **mut):
    p = write_engine_yaml(tmp_path / "live.yaml", mode="shadow", portfolio=ONE, initial=A)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw["runtime"]["mode"] = "live"
    raw["security"]["live_execution_enabled"] = True
    raw["broker"]["name"], raw["broker"]["access"] = "etoro", "live_read_only"
    raw["live_controls"] = controls(tmp_path)
    for k, v in mut.items():
        raw[k] = v
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return p


def test_live_configuration_validates_only_with_both_switches_and_controls(tmp_path):
    cfg = load_engine_config(_live_yaml(tmp_path))
    assert cfg["runtime"]["mode"] == "live" and cfg["security"]["live_execution_enabled"] is True
    with pytest.raises(ConfigError):
        load_engine_config(_live_yaml(tmp_path, live_controls=None))
    raw = yaml.safe_load(_live_yaml(tmp_path).read_text(encoding="utf-8"))
    raw["security"]["live_execution_enabled"] = False
    p = tmp_path / "x.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_engine_config(p)


ENV = {"UTE_ETORO_TRADING_API_KEY": "trade-api", "UTE_ETORO_TRADING_USER_KEY": "new-trading-user-key",
       "UTE_ETORO_READONLY_API_KEY": "ro-api", "UTE_ETORO_READONLY_USER_KEY": "ro-user"}
NOW = dt.datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
WRITE = lambda a, u: ["etoro-public:trade.real:read", "etoro-public:trade.real:write"]  # noqa: E731


def _activation(tmp_path, cfg, **over):
    a = {"kind": "AUTONOMOUS_LIVE", "phrase": LIVE_ACTIVATION_PHRASE, "release_sha": "r1",
         "config_fingerprint": cfg["_fingerprint"], "approved_by": "owner", "expires_at": None}
    a.update(over)
    (tmp_path / "activation.json").write_text(json.dumps(a), encoding="utf-8")


def _gate(tmp_path, env=ENV, probe=WRITE, release="r1", cfg=None):
    cfg = cfg or load_engine_config(_live_yaml(tmp_path))
    return authorize_live(release_sha=release, config=cfg, activation_path=tmp_path / "activation.json",
                          kill_switch_paths=[tmp_path / "KILL"], env=env, now=NOW, scope_probe=probe)


def test_one_time_activation_passes_with_every_gate_and_no_expiry(tmp_path):
    cfg = load_engine_config(_live_yaml(tmp_path))
    (tmp_path / "forbidden.sha256").write_text(key_hash("old-gold-miner-user-key") + "\n", encoding="utf-8")
    _activation(tmp_path, cfg)
    auth = _gate(tmp_path, cfg=cfg)
    assert auth.approved_by == "owner" and auth.expires_at is None


@pytest.mark.parametrize("case,match", [
    ("release", "different release"), ("config_changed", "different release or configuration"),
    ("phrase", "phrase"), ("kind", "phrase"), ("expired", "expired"), ("no_creds", "not provisioned"),
    ("same_as_ro", "READ-ONLY"), ("gm_key", "FORBIDDEN"), ("no_forbidden_file", "forbidden-key file"),
    ("kill", "kill switch"), ("no_write_scope", "write scope"), ("no_file", "no owner approval file")])
def test_activation_refusals(tmp_path, case, match):
    cfg = load_engine_config(_live_yaml(tmp_path))
    (tmp_path / "forbidden.sha256").write_text(key_hash("old-gold-miner-user-key") + "\n", encoding="utf-8")
    _activation(tmp_path, cfg)
    env, probe, release = dict(ENV), WRITE, "r1"
    if case == "release":
        release = "r2"
    elif case == "config_changed":
        _activation(tmp_path, cfg, config_fingerprint="sha256:other")
    elif case == "phrase":
        _activation(tmp_path, cfg, phrase="yes")
    elif case == "kind":
        _activation(tmp_path, cfg, kind="SUPERVISED_PILOT")
    elif case == "expired":
        _activation(tmp_path, cfg, expires_at=NOW.isoformat())
    elif case == "no_creds":
        env.pop("UTE_ETORO_TRADING_API_KEY")
    elif case == "same_as_ro":
        env["UTE_ETORO_TRADING_USER_KEY"] = env["UTE_ETORO_READONLY_USER_KEY"]
    elif case == "gm_key":
        env["UTE_ETORO_TRADING_USER_KEY"] = "old-gold-miner-user-key"
    elif case == "no_forbidden_file":
        (tmp_path / "forbidden.sha256").unlink()
    elif case == "kill":
        (tmp_path / "KILL").write_text("x")
    elif case == "no_write_scope":
        probe = lambda a, u: ["etoro-public:trade.real:read"]  # noqa: E731
    elif case == "no_file":
        (tmp_path / "activation.json").unlink()
    with pytest.raises(LiveExecutionNotAvailable, match=match):
        _gate(tmp_path, env=env, probe=probe, release=release, cfg=cfg)


def test_operator_cli_in_live_modes(tmp_path, capsys):
    """INVARIANT: the activation template binds this release + config; the kill-switch CLI works in the autonomous
    path (any mode with live_controls) and blocks the next execution."""
    from utengine.cli import main
    from utengine.core.takeover import release_id
    p = _live_yaml(tmp_path)
    assert main(["--config", str(p), "--home", str(tmp_path / "h"), "live-activation-template"]) == 0
    t = json.loads(capsys.readouterr().out)
    assert t["kind"] == "AUTONOMOUS_LIVE" and t["release_sha"] == release_id() and t["expires_at"] is None
    assert t["config_fingerprint"] == load_engine_config(p)["_fingerprint"] and t["phrase"] == LIVE_ACTIVATION_PHRASE
    env = auto_env(tmp_path / "sim")
    base = ["--config", str(env.cfg_path), "--home", str(env.home)]
    assert main(base + ["kill-switch", "--operator", "owner", "--reason", "drill"]) == 0
    env.engine()
    env.decide_and_execute()
    assert sent(env) == [] and "KILL_SWITCH" in {h["kind"] for h in halts.active(env.e.db)}
