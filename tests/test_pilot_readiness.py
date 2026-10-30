"""Single-strategy pilot readiness (100% QC_529_ETORO_RESTRICTED): the owner's manual close of the foreign GDX
positions is booked from evidence, a HOLD sleeve still invests capital it received through an audited transfer, and
the spread gate. Simulated broker only."""
import datetime as dt
import json

import pytest

from conftest import (DECIDE_AT, EXECUTE_AT, FULL_CAPITAL, IDS, NEXT_DECIDE, NEXT_EXECUTE, extended_store, multi_env,
                      write_engine_yaml)
from utengine.core import accounting, funding, halts, sleeves
from utengine.core.errors import EngineError
from utengine.core.reconciliation import acknowledge_external, engine_position_ids
from utengine.strategy_modules import REGISTRY

A, B = "QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"
D2 = dt.date(2026, 10, 27)


def val(e):
    r = e.reconcile("t", e.now())
    assert r.clean, r.issues
    return e.sleeve_valuation(r)


def clear_all(e, why="test"):
    for h in halts.active(e.db) + halts.active_sleeve(e.db):
        halts.clear(e.db, h["halt_id"], "tester", why, e.nows())


def close_externally(env, pid, proceeds):
    """The owner closes a foreign position in the eToro app: the position disappears, the proceeds are credited."""
    del env.broker.s["positions"][pid]
    env.broker.s["credit"] = round(env.broker.s["credit"] + proceeds, 6)
    env.broker._save()


def to_restricted_only(e):
    p = sleeves.restructure_preview(e.db, {A: 1.0}, val(e), e.specs, REGISTRY, e.nows())
    return sleeves.restructure(e.db, p["preview_hash"], "owner", val(e), e.nows())


def test_manual_close_of_pre_existing_gdx_is_booked_from_evidence_then_reaches_the_sleeve(tmp_path):
    """INVARIANT: the proceeds of a PRE_EXISTING position closed outside the engine are never guessed: they wait in
    suspense (excluded from capital) until an operator classifies the OBSERVED amount as EXTERNAL_TRADE with
    evidence; then they are split to the sleeves, the suspense exception is resolved, the ledger matches the broker
    and the receiving sleeves are flagged for a capital catch-up."""
    env = multi_env(tmp_path, capital=FULL_CAPITAL, clock_at=EXECUTE_AT)
    pid = env.broker.external_open_position(IDS["GDX"], 740.0, is_buy=False, leverage=5, open_rate=81.43)
    e = env.engine()
    cash0 = round(sum(b["cash"] for b in sleeves.balances(e.db).values()), 6)
    close_externally(env, pid, 748.91)
    r = e.reconcile("after_owner_close", e.now())
    assert funding.suspense_total(e.db) == pytest.approx(748.91)
    assert any(i["kind"] == "EXTERNAL_CHANGE" for i in r.issues)
    d = funding.open_suspense(e.db)[0]
    with pytest.raises(EngineError):
        accounting.resolve_discrepancy(e.db, d["disc_id"], "FEE", "x", "owner", e.nows())
    accounting.resolve_discrepancy(e.db, d["disc_id"], "EXTERNAL_TRADE",
                                   f"eToro trade history: position {pid} closed by the owner, proceeds credited",
                                   "owner", e.nows())
    assert funding.suspense_total(e.db) == 0.0
    acknowledge_external(e.db, pid, "owner", "closed by the owner in the eToro app", e.nows())
    clear_all(e)
    r = e.reconcile("after_classification", e.now())
    assert r.clean, r.issues
    assert not sleeves.check_invariants(e.db, r.snapshot, engine_position_ids(e.db))
    cash1 = round(sum(b["cash"] for b in sleeves.balances(e.db).values()), 6)
    assert cash1 == pytest.approx(cash0 + 748.91) and r.strategy_equity == pytest.approx(env.broker.s["credit"])
    assert sleeves.catch_up_pending(e.db, A) and sleeves.catch_up_pending(e.db, B)


def test_hold_sleeve_invests_restructured_cash_from_a_cash_only_account(tmp_path):
    """INVARIANT (first acquisition, #529 reporting HOLD): a sleeve whose decision does not rebalance would leave the
    cash it received from an audited restructure idle; the capital catch-up re-targets the decision's STANDING
    weights once (same weights, no new signal), buys only the 11 restricted instruments, and is consumed."""
    env = multi_env(tmp_path, bar_store=extended_store({D2: 1.0}))
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()                                                      # 10-26 decision; its open is never executed
    env.clock.set(NEXT_DECIDE)
    e.cycle()                                                      # 10-27 decision
    row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session=?", (A, D2.isoformat()))
    d = json.loads(row["decision"])
    if d["rebalance"]:                                             # TEST ONLY: make it the HOLD case
        d["rebalance"] = False
        e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), A, D2.isoformat()))
    assert d["target_weights"] and env.broker.s["positions"] == {}
    out = to_restricted_only(e)
    assert out["retired"] == [B] and sleeves.catch_up_pending(e.db, A)
    env.clock.set(NEXT_EXECUTE)
    x = e.cycle()["execution"][D2.isoformat()]
    plan = next(v for k, v in x.items() if k.startswith("P"))
    assert any("CAPITAL_CATCH_UP" in n for n in x["notes"] + plan["notes"])
    bought = {env.broker.s["instruments"][str(env.broker.s["orders"][s["order_id"]]["instrument_id"])]
              for s in env.broker.s["submissions"] if s["kind"] == "open"}
    allowed = {s for s, i in e.specs[A]["instruments"].items() if i["eligibility"] == "VERIFIED_TRADABLE"}
    assert bought and bought <= allowed and len(allowed) == 11
    assert not ({"SOXL", "LABU", "SVXY", "BSV", "SVIX", "BTAL", "TECS", "UVIX"} & bought)
    assert sleeves.catch_up_pending(e.db, A) is None
    r = e.reconcile("check", env.clock())
    assert r.clean and not sleeves.check_invariants(e.db, r.snapshot, engine_position_ids(e.db))
    v = e.sleeve_valuation(r)
    invested = v["sleeves"][A]["holdings_value"] / v["sleeves"][A]["equity"]
    eligible = sum(w for t, w in d["target_weights"].items() if t in allowed)   # the rest is the restricted cash
    assert invested == pytest.approx(eligible, abs=0.02) and eligible < sum(d["target_weights"].values())


def test_hold_without_capital_change_still_holds(tmp_path):
    """INVARIANT: without an audited inflow nothing changes: a HOLD decision trades nothing (strategy semantics)."""
    env = multi_env(tmp_path, bar_store=extended_store({D2: 1.0}))
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    for sid in (A, B):
        row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session=?", (sid, D2.isoformat()))
        d = json.loads(row["decision"])
        d["rebalance"] = False
        e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), sid, D2.isoformat()))
    env.clock.set(NEXT_EXECUTE)
    assert e.cycle()["execution"][D2.isoformat()]["status"] == "NO_REBALANCE"
    assert [s for s in env.broker.s["submissions"] if s["kind"] == "open"] == []


def test_spread_gate_blocks_wide_quotes(tmp_path):
    """INVARIANT: with risk.max_spread_bp set, an open whose broker quote is wider is blocked (never sent)."""
    import yaml
    from conftest import Env
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml")
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["risk"]["max_spread_bp"] = 5.0
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    env = Env(tmp_path, cfg_path=cfg)
    for sym, iid in IDS.items():
        bid, ask = env.broker._price(iid)
        mid = (bid + ask) / 2
        env.broker.set_price(iid, round(mid * 0.999, 6), round(mid * 1.001, 6))      # 20 bp wide
    env.engine()
    env.decide_and_execute()
    assert [s for s in env.broker.s["submissions"] if s["kind"] == "open"] == []
    blocked = env.e.db.q("SELECT plan FROM plans")
    assert any("max_spread_bp" in r["plan"] for r in blocked)


def test_pilot_strategy_identity_is_frozen():
    """INVARIANT (owner decision 2026-10-30): the pilot strategy is QC_529_ETORO_RESTRICTED exactly as activated on
    the VPS: spec fingerprint, version, original-source hash, module hash and the 11-instrument executable universe.
    Any change here is a NEW strategy version, never an edit."""
    from utengine.core.configuration import load_all_strategies, load_engine_config
    from conftest import ROOT
    s = load_all_strategies(ROOT / "strategies", REGISTRY)[A]
    assert s["fingerprint"] == "sha256:e8af2cee1ee0ee92d1d941a1562e8a37f4ec0ed7eaa7e78777a82a628e5ba024"
    assert s["version"] == "1.0.0" and s["implementation"] == "qc529"
    assert s["source"]["sha256"] == "40c270b8fc67f47a253b5947a9e0f729a6dbf98dca7e62e4b2e3bc4439c74ec6"
    assert REGISTRY["qc529"].source_sha256 == "c79f2c9d4a435c02fbc7e62df9eef1ddfa737487716a081e20a811d666961b21"
    tradable = sorted(k for k, v in s["instruments"].items() if v["eligibility"] == "VERIFIED_TRADABLE")
    assert tradable == ["BIL", "PSQ", "QLD", "QQQ", "SOXS", "SPXL", "SQQQ", "TECL", "TLT", "TQQQ", "UVXY"]
    excluded = {"SOXL", "LABU", "SVXY", "BSV", "SVIX", "BTAL", "TECS", "UVIX"}
    assert excluded == {k for k, v in s["instruments"].items() if v["eligibility"] != "VERIFIED_TRADABLE"}
    assert not s["substitutions"] and s["eligibility"]["max_leverage"] == 1 and not s["eligibility"]["allow_short"]
    cfg = load_engine_config(ROOT / "config" / "engine.single529.yaml")
    assert cfg["portfolio"]["sleeves"] == [{"strategy": A, "weight": 1.0}] and cfg["runtime"]["mode"] == "shadow"
    assert cfg["security"]["live_execution_enabled"] is False


# ------------------------------------------------------------------------------------- capital catch-up semantics
def hold_env(tmp_path, extra=None):
    """Two sessions decided (10-26 never executed, 10-27 forced to HOLD): the state paper #529 reports as HOLD."""
    env = multi_env(tmp_path, bar_store=extended_store(extra or {D2: 1.0}))
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    for sid in (A, B):
        row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session=?", (sid, D2.isoformat()))
        d = json.loads(row["decision"])
        d["rebalance"] = False
        e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), sid, D2.isoformat()))
    return env, e


def opens(env):
    return [s for s in env.broker.s["submissions"] if s["kind"] == "open"]


def audits(e, kind):
    return e.db.one("SELECT COUNT(*) c FROM audit_log WHERE kind=?", (kind,))["c"]


def test_catch_up_is_idempotent_across_restarts(tmp_path):
    """INVARIANT: a catch-up survives restarts as ONE pending request (restarts and repeated cycles never add a
    second one), is used by exactly one executed plan, and after that a HOLD holds again (no repeated buying)."""
    env, e = hold_env(tmp_path)
    to_restricted_only(e)
    for _ in range(2):
        e = env.engine()                                            # restart: state comes from the database only
        e.reconcile("restart", e.now())
    assert audits(e, "CATCH_UP_REQUESTED") == 1 and sleeves.catch_up_pending(e.db, A)
    env.clock.set(NEXT_EXECUTE)
    e.cycle()
    n = len(opens(env))
    assert n > 0 and audits(e, "CATCH_UP_CONSUMED") == 1
    e = env.engine()
    env.clock.set(NEXT_EXECUTE + dt.timedelta(minutes=10))
    e.cycle()                                                       # same session, after a restart: nothing new
    assert len(opens(env)) == n and audits(e, "CATCH_UP_CONSUMED") == 1 and sleeves.catch_up_pending(e.db, A) is None


def test_catch_up_only_for_audited_transfers(tmp_path):
    """INVARIANT: eligible = restructure, reallocation, pool allocation, operator-classified positive EXTERNAL_TRADE.
    A deposit (even an evidenced one) or a foreign position opening (negative EXTERNAL_TRADE) requests nothing."""
    env = multi_env(tmp_path, capital=FULL_CAPITAL, broker_kwargs={"cash_tx_capability": True})
    e = env.engine()
    env.broker.deposit(300.0, evidence="evidenced deposit")
    e.reconcile("t", e.now())
    env.broker.external_open_position(IDS["GDX"], 100.0, is_buy=False, leverage=5, open_rate=81.0)
    e.reconcile("t", e.now())
    assert audits(e, "CATCH_UP_REQUESTED") == 0
    clear_all(e)
    for p in [x for x in e.db.q("SELECT position_id FROM ownership WHERE owner='EXTERNAL_UNACK'")]:
        acknowledge_external(e.db, p["position_id"], "owner", "test", e.nows())
    clear_all(e)
    pv = sleeves.reallocation_preview(e.db, {A: 0.6, B: 0.4}, val(e), e.nows())
    sleeves.reallocate(e.db, pv["preview_hash"], "owner", val(e), e.nows())
    assert sleeves.catch_up_pending(e.db, A) and sleeves.catch_up_pending(e.db, B) is None


def test_catch_up_stays_pending_when_the_plan_is_blocked(tmp_path):
    """INVARIANT: a plan that executes nothing (here a NEW_EXPOSURE halt blocks every open) does not use up the
    catch-up; the next execution after the halt is cleared invests the cash."""
    env, e = hold_env(tmp_path, {D2: 1.0, dt.date(2026, 10, 28): 1.0})
    to_restricted_only(e)
    hid = halts.raise_halt(e.db, "EMERGENCY_HALT", "NEW_EXPOSURE", "operator test", e.nows())
    env.clock.set(NEXT_EXECUTE)
    e.cycle()
    assert opens(env) == [] and sleeves.catch_up_pending(e.db, A) and audits(e, "CATCH_UP_KEPT") == 1
    halts.clear(e.db, hid, "ops", "test over", e.nows())
    env.clock.set(dt.datetime(2026, 10, 28, 20, 55, tzinfo=dt.timezone.utc))
    e.cycle()                                                       # 10-28 decision
    row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session='2026-10-28'", (A,))
    d = json.loads(row["decision"])
    d["rebalance"] = False                                         # still HOLD
    e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session='2026-10-28'", (json.dumps(d), A))
    env.clock.set(dt.datetime(2026, 10, 29, 13, 35, tzinfo=dt.timezone.utc))
    e.cycle()
    assert opens(env) and sleeves.catch_up_pending(e.db, A) is None


def test_friday_hold_restructure_over_the_weekend_executes_monday_open(tmp_path):
    """BEHAVIOUR (owner question): the Friday 16:50 ET decision is a HOLD; the restructure happens on Saturday; the
    first eligible execution is Monday's open (09:30 ET + delay). Nothing trades before it (weekend, Monday
    pre-open); at Monday's open the catch-up re-targets FRIDAY's standing weights with Monday's broker quotes."""
    fri, mon = dt.date(2026, 10, 30), dt.date(2026, 11, 2)
    env = multi_env(tmp_path, bar_store=extended_store({D2: 1.0, dt.date(2026, 10, 28): 1.0,
                                                        dt.date(2026, 10, 29): 1.0, fri: 1.0}))
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()                                                       # 10-26 decision (never executed)
    env.clock.set(dt.datetime(2026, 10, 30, 20, 55, tzinfo=dt.timezone.utc))
    e.cycle()                                                       # Friday 16:55 ET decision
    row = e.db.one("SELECT decision FROM decisions WHERE strategy_id=? AND session=?", (A, fri.isoformat()))
    d = json.loads(row["decision"])
    if d["rebalance"]:
        d["rebalance"] = False
        e.db.x("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), A, fri.isoformat()))
    env.clock.set(dt.datetime(2026, 10, 31, 12, 0, tzinfo=dt.timezone.utc))
    to_restricted_only(e)                                           # Saturday
    e.cycle()
    env.clock.set(dt.datetime(2026, 11, 2, 13, 40, tzinfo=dt.timezone.utc))   # Monday 08:40 EST: market closed
    e.cycle()
    assert opens(env) == [] and sleeves.catch_up_pending(e.db, A)
    env.clock.set(dt.datetime(2026, 11, 2, 14, 35, tzinfo=dt.timezone.utc))   # Monday 09:35 EST: open + delay
    x = e.cycle()["execution"]
    assert fri.isoformat() in x, x
    plan = next(v for k, v in x[fri.isoformat()].items() if k.startswith("P"))
    assert any("CAPITAL_CATCH_UP" in n for n in x[fri.isoformat()]["notes"] + plan["notes"])
    assert opens(env) and sleeves.catch_up_pending(e.db, A) is None
    assert mon.weekday() == 0
