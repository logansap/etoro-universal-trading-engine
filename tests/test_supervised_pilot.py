"""SUPERVISED live pilot (pilot release): every order needs a human approval bound to that intent; hard limits are
checked when an order would be sent; kill switch; no duplicate on restart; unknown-order recovery unchanged; first
acquisition from cash (Friday HOLD -> Monday). `supervised_simulation` = the identical supervision layer with the
simulated broker as writer. The real writer's gate (authorize_supervised) is tested separately; no network."""
import datetime as dt
import json

import pytest
import yaml

from conftest import (DECIDE_AT, EXECUTE_AT, Env, extended_store, write_engine_yaml)
from utengine.brokers.base import AccountSnapshot, Position
from utengine.core import halts, sleeves, supervision
from utengine.core.errors import EngineError, LiveExecutionNotAvailable
from utengine.core.security import (LIVE_RELEASE_ENABLED, PILOT_APPROVAL_PHRASE, SUPERVISED_PILOT_RELEASE,
                                    authorize_supervised, key_hash, obtain_writer)

A = "QC_529_ETORO_RESTRICTED"
UTC = dt.timezone.utc
ONE_SLEEVE = {"mode": "multi_strategy", "funding_policy": "proportional", "rebalance_policy": "strategy_independent",
              "aggregate_risk": "enforced", "sleeves": [{"strategy": A, "weight": 1.0}]}


def pilot_cfg(tmp_path, **over):
    p = {"max_order_usd": 1500.0, "max_gross_usd": 2000.0, "max_orders_per_session": 12, "daily_loss_stop_usd": 200.0,
         "cumulative_loss_stop_usd": 400.0, "approval_ttl_minutes": 15.0,
         "activation_file": str(tmp_path / "pilot-approval.json"),
         "forbidden_hashes_file": str(tmp_path / "forbidden.sha256")}
    p.update(over)
    return p


def supervised_env(tmp_path, bar_store=None, risk=None, **over):
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml", portfolio=ONE_SLEEVE, initial=A, **(risk or {}))
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["runtime"]["mode"] = "supervised_simulation"
    raw["live_controls"] = pilot_cfg(tmp_path, **over)
    cfg.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    env = Env(tmp_path, cfg_path=cfg, bar_store=bar_store)
    env.engine()
    return env


def sent(env):
    return [s for s in env.broker.s["submissions"] if s["client_ref"] != "EXTERNAL"]


def approve_all(env, by="owner"):
    out = []
    for p in supervision.pending(env.e.db):
        out.append(supervision.approve(env.e.db, env.e.cfg, p["intent_id"], by, env.clock()))
    return out


def plan_status(e):
    return [r["status"] for r in e.db.q("SELECT status FROM plans ORDER BY created_at")]


# ------------------------------------------------------------------------------------------- approval and idempotency
def test_no_order_is_sent_without_approval_then_each_approved_order_exactly_once(tmp_path):
    """INVARIANT: without approvals nothing reaches the broker (plan AWAITING_APPROVAL); after approval each intent is
    sent once; a restart and further cycles never resend; every approval is consumed."""
    env = supervised_env(tmp_path)
    env.decide_and_execute()
    assert sent(env) == [] and plan_status(env.e) == ["AWAITING_APPROVAL"]
    pend = supervision.pending(env.e.db)
    assert pend and all(p["approval"] is None for p in pend)
    approve_all(env)
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=2))
    env.e.cycle()
    n = len(sent(env))
    assert n == len(pend) and plan_status(env.e) == ["DONE"]
    env.engine()                                                      # restart
    for m in (5, 10):
        env.clock.set(EXECUTE_AT + dt.timedelta(minutes=m))
        env.e.cycle()
    assert len(sent(env)) == n
    assert all(env.e.db.get(f"approval:{p['intent_id']}")["consumed_at"] for p in pend)
    r = env.e.reconcile("check", env.clock())
    assert r.clean and not sleeves.check_invariants(env.e.db, r.snapshot,
                                                   __import__("utengine.core.reconciliation",
                                                              fromlist=["x"]).engine_position_ids(env.e.db))


def test_unapproved_orders_are_abandoned_when_the_window_ends_never_sent_late(tmp_path):
    env = supervised_env(tmp_path)
    env.decide_and_execute()
    env.clock.set(EXECUTE_AT + dt.timedelta(hours=3))                 # window (60 min) long gone
    env.e.cycle()
    st = {r["status"] for r in env.e.db.q("SELECT status FROM intents")}
    assert st == {"ABANDONED"} and sent(env) == []
    with pytest.raises(EngineError):
        approve_all(env) or supervision.approve(env.e.db, env.e.cfg, env.e.db.one("SELECT intent_id FROM intents")
                                                ["intent_id"], "owner", env.clock())


def test_expired_approval_holds_and_a_tampered_intent_is_blocked(tmp_path):
    env = supervised_env(tmp_path, approval_ttl_minutes=5.0)
    env.decide_and_execute()
    approve_all(env)
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=8))               # approvals expired
    env.e.cycle()
    assert sent(env) == [] and any("expired" in json.loads(r["payload"])["reason"]
                                   for r in env.e.db.q("SELECT payload FROM audit_log WHERE kind='ORDER_HELD'"))
    approve_all(env)
    iid = supervision.pending(env.e.db)[0]["intent_id"]
    env.e.db.x("UPDATE intents SET amount=amount+1 WHERE intent_id=?", (iid,))   # what would be sent changed
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=9))
    env.e.cycle()
    assert env.e.db.one("SELECT status FROM intents WHERE intent_id=?", (iid,))["status"] == "BLOCKED"


def test_reject_blocks_the_intent(tmp_path):
    env = supervised_env(tmp_path)
    env.decide_and_execute()
    iid = supervision.pending(env.e.db)[0]["intent_id"]
    supervision.reject(env.e.db, iid, "owner", "not today", env.e.nows())
    assert env.e.db.one("SELECT status FROM intents WHERE intent_id=?", (iid,))["status"] == "BLOCKED"


# ------------------------------------------------------------------------------------------- hard limits
def test_order_cap_refuses_approval_and_blocks_a_forced_one(tmp_path):
    """INVARIANT: an order above max_order_usd cannot be approved, and even a (forged) approval is BLOCKED at
    submit time with the reason recorded."""
    env = supervised_env(tmp_path, max_order_usd=600.0, max_gross_usd=2000.0)
    env.decide_and_execute()
    pend = supervision.pending(env.e.db)
    big = [p for p in pend if p["amount_usd"] > 600.0]
    assert big, "fixture must produce an order above the cap"
    for p in big:
        with pytest.raises(EngineError, match="max_order_usd"):
            supervision.approve(env.e.db, env.e.cfg, p["intent_id"], "owner", env.clock())
        row = dict(env.e.db.one("SELECT * FROM intents WHERE intent_id=?", (p["intent_id"],)))
        env.e.db.put(f"approval:{p['intent_id']}", {"digest": supervision.intent_digest(row), "approved_by": "forged",
                                                     "expires_at": (env.clock() + dt.timedelta(minutes=10)).isoformat()})
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=2))
    env.e.cycle()
    for p in big:
        assert env.e.db.one("SELECT status FROM intents WHERE intent_id=?", (p["intent_id"],))["status"] == "BLOCKED"
    reasons = [json.loads(r["detail"]).get("reason", "") for r in env.e.db.q(
        "SELECT detail FROM order_events WHERE to_status='BLOCKED'")]
    assert any("max_order_usd" in x for x in reasons)
    assert not any(env.broker.s["orders"][s["order_id"]]["amount"] > 600.0 for s in sent(env))


def test_gross_cap_blocks_the_order_that_would_exceed_it(tmp_path):
    """INVARIANT: with every order approved, the first order that would take engine exposure above max_gross_usd is
    BLOCKED; total sent stays within the cap."""
    env = supervised_env(tmp_path, max_order_usd=500.0, max_gross_usd=500.0)
    env.decide_and_execute()
    for p in supervision.pending(env.e.db):
        if p["amount_usd"] <= 500.0:
            supervision.approve(env.e.db, env.e.cfg, p["intent_id"], "owner", env.clock())
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=2))
    env.e.cycle()
    total = sum(env.broker.s["orders"][s["order_id"]]["amount"] for s in sent(env))
    reasons = [json.loads(r["detail"]).get("reason", "") for r in env.e.db.q(
        "SELECT detail FROM order_events WHERE to_status='BLOCKED'")]
    assert total <= 500.0 + 1e-6 and any("max_gross_usd" in x for x in reasons), (total, reasons)


def test_kill_switch_blocks_approved_orders_and_halts(tmp_path):
    env = supervised_env(tmp_path)
    env.decide_and_execute()
    approve_all(env)
    supervision.engage_kill_switch(env.e.cfg, env.e.home, "owner", "test", env.e.nows(), env.e.db)
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=2))
    env.e.cycle()
    assert sent(env) == []
    assert "KILL_SWITCH" in {h["kind"] for h in halts.active(env.e.db)}


def test_wide_spread_holds_until_the_window_ends(tmp_path):
    env = supervised_env(tmp_path, risk={"max_spread_bp": 50.0})
    env.decide_and_execute()
    approve_all(env)
    for iid, (bid, ask) in list(env.broker.s["prices"].items()):
        m = (bid + ask) / 2
        env.broker.set_price(int(iid), round(m * 0.995, 6), round(m * 1.005, 6))      # 100 bp
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=2))
    env.e.cycle()
    assert sent(env) == [] and any("spread" in json.loads(r["payload"])["reason"]
                                   for r in env.e.db.q("SELECT payload FROM audit_log WHERE kind='ORDER_HELD'"))


def test_supervision_check_limits_unit(tmp_path):
    """Unit test of supervision.check: daily and cumulative loss stops, order count and kill switch."""
    env = supervised_env(tmp_path, daily_loss_stop_usd=50.0, cumulative_loss_stop_usd=100.0, max_orders_per_session=1)
    e = env.e
    env.decide_and_execute()
    row = dict(e.db.one("SELECT * FROM intents ORDER BY leg_index LIMIT 1"))
    supervision.approve(e.db, e.cfg, row["intent_id"], "owner", env.clock())

    def snap(credit):
        return AccountSnapshot(as_of=env.clock().isoformat(), credit=credit, pending_open_amount=0.0, positions=(),
                               pending_orders=(), currency="USD", source="t", raw_sha256="x", provenance={})
    q = env.broker.quotes
    assert supervision.check(e.db, e.cfg, e.home, row, snap(2000.0), q, env.clock()) == (None, None)
    kind, why = supervision.check(e.db, e.cfg, e.home, row, snap(1940.0), q, env.clock())
    assert kind == "block" and "daily loss" in why
    kind, why = supervision.check(e.db, e.cfg, e.home, row, snap(1890.0), q, env.clock())
    assert kind == "block" and "cumulative" in why and "PILOT_LOSS_STOP" in {h["kind"] for h in halts.active(e.db)}


# ------------------------------------------------------------------------------------------- recovery and ordering
def test_unknown_order_recovery_still_never_resubmits(tmp_path):
    env = supervised_env(tmp_path)
    env.decide_and_execute()
    approve_all(env)
    env.broker.inject("timeout_after_accept")
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=2))
    env.e.cycle()
    n = len(sent(env))
    env.engine()
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=6))
    env.e.cycle()
    assert len(sent(env)) == n and env.e.db.one(
        "SELECT COUNT(*) c FROM audit_log WHERE kind='ORDER_FOUND_BY_REFERENCE'")["c"] >= 1


def test_friday_hold_restructure_monday_first_acquisition_supervised(tmp_path):
    """First live acquisition from reconciled cash: Friday decision HOLD (state from an earlier decision), weekend
    capital transfer, Monday open -> catch-up plan AWAITING_APPROVAL -> approved -> executed once; the catch-up is
    consumed only when the plan completes."""
    fri = dt.date(2026, 10, 30)
    bars = extended_store({dt.date(2026, 10, 27): 1.0, dt.date(2026, 10, 28): 1.0, dt.date(2026, 10, 29): 1.0,
                           fri: 1.0})
    env = supervised_env(tmp_path, bar_store=bars)
    e = env.e
    env.clock.set(DECIDE_AT)
    e.cycle()                                                          # 10-26 decision: never approved
    env.clock.set(dt.datetime(2026, 10, 27, 15, 0, tzinfo=UTC))
    e.cycle()                                                          # its window passes: abandoned
    env.clock.set(dt.datetime(2026, 10, 30, 20, 55, tzinfo=UTC))
    e.cycle()
    row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session=?", (A, fri.isoformat()))
    d = json.loads(row["decision"])
    if d["rebalance"]:
        d["rebalance"] = False
        e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), A, fri.isoformat()))
    env.clock.set(dt.datetime(2026, 10, 31, 12, 0, tzinfo=UTC))       # Saturday: an audited capital transfer
    sleeves.request_catch_up(e.db, A, "pool allocation", "test:transfer", e.nows())
    env.clock.set(dt.datetime(2026, 11, 2, 14, 35, tzinfo=UTC))       # Monday open + delay
    e.cycle()
    assert sent(env) == [] and plan_status(e)[-1] == "AWAITING_APPROVAL" and sleeves.catch_up_pending(e.db, A)
    approve_all(env)
    env.clock.set(dt.datetime(2026, 11, 2, 14, 40, tzinfo=UTC))
    e.cycle()
    n = len(sent(env))
    assert n > 0 and plan_status(e)[-1] == "DONE" and sleeves.catch_up_pending(e.db, A) is None
    env.engine()
    env.clock.set(dt.datetime(2026, 11, 2, 14, 50, tzinfo=UTC))
    env.e.cycle()
    assert len(sent(env)) == n


# ------------------------------------------------------------------------------------------- the real writer's gate
def _approval(tmp_path, cfg, release="r1", **over):
    a = {"kind": "SUPERVISED_PILOT", "phrase": PILOT_APPROVAL_PHRASE, "release_sha": release,
         "config_fingerprint": cfg["_fingerprint"], "approved_by": "owner",
         "expires_at": (dt.datetime(2026, 10, 30, 12, tzinfo=UTC) + dt.timedelta(hours=4)).isoformat()}
    a.update(over)
    p = tmp_path / "pilot-approval.json"
    p.write_text(json.dumps(a), encoding="utf-8")
    return p


def _live_cfg(tmp_path):
    cfg = {"runtime": {"mode": "supervised_live"}, "_fingerprint": "fp", "live_controls": pilot_cfg(tmp_path),
           "broker": {"credentials_env": {"api_key": "RO_API", "user_key": "RO_USER"}}}
    (tmp_path / "forbidden.sha256").write_text(key_hash("gold-miner-user-key") + "\n", encoding="utf-8")
    return cfg


ENV = {"UTE_ETORO_TRADING_API_KEY": "trade-api", "UTE_ETORO_TRADING_USER_KEY": "new-trading-user-key",
       "RO_API": "ro-api", "RO_USER": "ro-user"}
NOW = dt.datetime(2026, 10, 30, 12, tzinfo=UTC)
WRITE_SCOPES = lambda a, u: ["etoro-public:trade.real:read", "etoro-public:trade.real:write"]  # noqa: E731


def test_authorize_supervised_passes_only_with_every_gate(tmp_path):
    cfg = _live_cfg(tmp_path)
    _approval(tmp_path, cfg)
    auth = authorize_supervised(release_sha="r1", config=cfg, approval_path=tmp_path / "pilot-approval.json",
                                kill_switch_paths=[tmp_path / "KILL"], env=ENV, now=NOW, scope_probe=WRITE_SCOPES)
    assert auth.approved_by == "owner"


@pytest.mark.parametrize("case,match", [
    ("wrong_release", "different release"), ("expired", "expired"), ("phrase", "phrase"),
    ("no_creds", "not provisioned"), ("same_as_ro", "READ-ONLY"), ("gm_key", "FORBIDDEN"),
    ("no_forbidden_file", "forbidden-key file"), ("kill", "kill switch"), ("no_write_scope", "write scope"),
    ("mode", "not a supervised_live")])
def test_authorize_supervised_refusals(tmp_path, case, match):
    cfg = _live_cfg(tmp_path)
    env, probe, release, kill = dict(ENV), WRITE_SCOPES, "r1", [tmp_path / "KILL"]
    _approval(tmp_path, cfg)
    if case == "wrong_release":
        release = "r2"
    elif case == "expired":
        _approval(tmp_path, cfg, expires_at=NOW.isoformat())
    elif case == "phrase":
        _approval(tmp_path, cfg, phrase="yes")
    elif case == "no_creds":
        env.pop("UTE_ETORO_TRADING_USER_KEY")
    elif case == "same_as_ro":
        env["UTE_ETORO_TRADING_USER_KEY"] = env["RO_USER"]
    elif case == "gm_key":
        env["UTE_ETORO_TRADING_USER_KEY"] = "gold-miner-user-key"
    elif case == "no_forbidden_file":
        (tmp_path / "forbidden.sha256").unlink()
    elif case == "kill":
        (tmp_path / "KILL").write_text("x")
    elif case == "no_write_scope":
        probe = lambda a, u: ["etoro-public:trade.real:read"]  # noqa: E731
    elif case == "mode":
        cfg["runtime"]["mode"] = "shadow"
    with pytest.raises(LiveExecutionNotAvailable, match=match):
        authorize_supervised(release_sha=release, config=cfg, approval_path=tmp_path / "pilot-approval.json",
                             kill_switch_paths=kill, env=env, now=NOW, scope_probe=probe)


def test_live_writers_need_their_authorization_closure():
    """Autonomous-live release: both gates are open in code, but no writer exists without its authorize closure."""
    assert LIVE_RELEASE_ENABLED is True and SUPERVISED_PILOT_RELEASE is True
    with pytest.raises(LiveExecutionNotAvailable):
        obtain_writer("live", object())
    with pytest.raises(LiveExecutionNotAvailable):
        obtain_writer("supervised_live", object())                    # no authorize() closure -> refused


def test_session_order_count_holds_further_orders(tmp_path):
    """INVARIANT: once pilot.max_orders_per_session orders were sent this session, further approved orders HOLD."""
    env = supervised_env(tmp_path, max_orders_per_session=2)
    env.decide_and_execute()
    approve_all(env)
    env.clock.set(EXECUTE_AT + dt.timedelta(minutes=2))
    env.e.cycle()
    assert len(sent(env)) == 2
    assert any("max_orders_per_session" in json.loads(r["payload"])["reason"]
               for r in env.e.db.q("SELECT payload FROM audit_log WHERE kind='ORDER_HELD'"))


def test_operator_cli_pending_approve_kill_switch(tmp_path, capsys):
    """INVARIANT: the operator path works through the CLI without any broker credential: `orders pending`,
    `orders approve` (one intent), then the next cycle sends exactly that intent; `kill-switch` then blocks the rest."""
    from utengine.cli import main
    env = supervised_env(tmp_path)
    base = ["--config", str(env.cfg_path), "--home", str(env.home)]
    assert main(base + ["--now", DECIDE_AT.isoformat(), "run", "--once"]) == 0
    assert main(base + ["--now", EXECUTE_AT.isoformat(), "run", "--once"]) == 0
    capsys.readouterr()
    assert main(base + ["orders", "pending"]) == 0
    pend = json.loads(capsys.readouterr().out)
    assert len(pend) >= 2 and all(p["approval"] is None for p in pend)
    first = sorted(pend, key=lambda p: p["intent_id"])[0]
    # the CLI approval uses wall-clock time; give it the simulated clock's frame by approving through the module
    supervision.approve(env.e.db, env.e.cfg, first["intent_id"], "owner", EXECUTE_AT + dt.timedelta(minutes=1))
    assert main(base + ["--now", (EXECUTE_AT + dt.timedelta(minutes=2)).isoformat(), "run", "--once"]) == 0
    sim = json.loads((env.home / "sim_broker.json").read_text(encoding="utf-8"))
    assert [s["client_ref"] for s in sim["submissions"]] == [first["intent_id"]]
    assert main(base + ["kill-switch", "--operator", "owner", "--reason", "test"]) == 0
    assert (env.home / "KILL_SWITCH").exists()
    capsys.readouterr()
    assert main(base + ["orders", "approve", "--intent", pend[-1]["intent_id"], "--approved-by", "owner"]) == 0
    assert main(base + ["--now", (EXECUTE_AT + dt.timedelta(minutes=3)).isoformat(), "run", "--once"]) == 0
    sim = json.loads((env.home / "sim_broker.json").read_text(encoding="utf-8"))
    assert len(sim["submissions"]) == 1


def test_pilot_approval_template_binds_release_and_config(tmp_path, capsys):
    """INVARIANT: the printed skeleton carries this release's id and this configuration's fingerprint, so a filled
    approval passes authorize_supervised's binding check (and any later code/config change invalidates it)."""
    from utengine.cli import main
    from utengine.core.configuration import load_engine_config
    from utengine.core.takeover import release_id
    cfgp = write_engine_yaml(tmp_path / "engine.yaml", portfolio=ONE_SLEEVE, initial=A)
    raw = yaml.safe_load(cfgp.read_text(encoding="utf-8"))
    raw["runtime"]["mode"] = "supervised_live"
    raw["broker"]["name"], raw["broker"]["access"] = "etoro", "live_read_only"
    raw["live_controls"] = pilot_cfg(tmp_path)
    cfgp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert main(["--config", str(cfgp), "--home", str(tmp_path / "h"), "pilot-approval-template"]) == 0
    t = json.loads(capsys.readouterr().out)
    assert t["release_sha"] == release_id() and t["config_fingerprint"] == load_engine_config(cfgp)["_fingerprint"]
    assert t["phrase"] == PILOT_APPROVAL_PHRASE and t["kind"] == "SUPERVISED_PILOT"
