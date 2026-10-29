"""Full-account takeover: read-only preview and the approval-gated state machine (simulated broker only).

Setup mirrors the real account: a foreign leveraged GDX CFD short (Gold Miner's), owned by another live writer, plus
optional mirrors / pending orders. The engine starts beside it (PRE_EXISTING, excluded, never touched) and a takeover
is previewed and, in simulation only, walked through every gate.
"""
import datetime as dt
import hashlib

import pytest

from conftest import EXECUTE_AT, IDS, multi_env, write_engine_yaml, Env, FIFTY_FIFTY
from utengine.brokers.simulated import SimulatedCrash
from utengine.core import halts, sleeves, takeover as T
from utengine.core.errors import LiveExecutionNotAvailable, ShadowWriteViolation

UTC = dt.timezone.utc
GM = {"known_writers": [{"name": "gold-miner", "description": "live Gold Miner engine (VPS)"}],
      "approval_ttl_hours": 4.0, "price_tolerance_pct": 0.02, "close_slippage_bp": 25.0,
      "investable_reserve_pct": 0.02, "financing_annual_rate_observed": {"GDX": 0.1005}}


def setup(tmp_path, *, mirror=False, pending=False, mode="simulation"):
    if mode == "simulation":
        env = multi_env(tmp_path, takeover=GM, clock_at=EXECUTE_AT)
    else:
        cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml", mode="shadow", portfolio=FIFTY_FIFTY, takeover=GM)
        env = Env(tmp_path, cfg_path=cfg, clock_at=EXECUTE_AT)
    gdx = env.broker.external_open_position(IDS["GDX"], 740.0, is_buy=False, leverage=5, open_rate=81.43)
    tlt = env.broker.external_open_position(IDS["TLT"], 150.0)
    if mirror:
        env.broker.add_mirror("M1", "some_trader", 200.0, 210.0)
    if pending:
        env.broker.inject("pending")
        env.broker.submit_open(IDS["QQQ"], 50.0, is_buy=True, leverage=1, client_ref="MANUAL")
    e = env.engine()
    return env, e, gdx, tlt


def broker_bytes(env):
    return hashlib.sha256(env.broker_path.read_bytes()).hexdigest()


def approve_all_to_close(env, e, pv):
    now = env.clock()
    T.approve(e, pv["run_id"], "DISABLE_OLD_WRITER", "owner", now)
    T.advance(e, pv["run_id"], now, "owner")
    T.record_writer_evidence(e, pv["run_id"], "gold-miner", "DISABLED_VERIFIED",
                             "gm audit: unit inactive+disabled, REAL kill switch engaged, 0 pending", "owner", now)
    T.advance(e, pv["run_id"], now, "owner")
    assert T.run(e.db, pv["run_id"])["state"] == "VERIFY_ACCOUNT"
    T.approve(e, pv["run_id"], "CLOSE_POSITIONS", "owner", now, max_exposure_usd=10_000)
    T.approve(e, pv["run_id"], "ACCEPT_LOSSES_AND_FEES", "owner", now,
              max_cost_usd=pv["capital"]["max_cost_floor_usd"] + 10)


def test_preview_reads_everything_and_mutates_nothing(tmp_path):
    """INVARIANT: the preview inventories positions, leverage, ownership, financing, proceeds and the 50/50 plan,
    and leaves the broker byte-identical (no order, no close, no adoption); the read-only view refuses writes."""
    env, e, gdx, tlt = setup(tmp_path)
    before, subs = broker_bytes(env), len(env.broker.s["submissions"])
    pv = T.preview(e, env.clock())
    assert broker_bytes(env) == before and len(env.broker.s["submissions"]) == subs
    assert pv["state"] == "APPROVAL_REQUIRED" and pv["broker_mutations"] == 0
    rows = {r["position_id"]: r for r in pv["positions"]}
    g = rows[gdx]
    assert g["direction"] == "SHORT" and g["leverage"] == 5 and g["owner"] == "PRE_EXISTING"
    assert g["exposure_usd"] > 3500 and g["financing"]["annual_rate_observed"] == 0.1005
    assert g["action"] == "CLOSE" and g["proceeds_basis"] == "BROKER_UNREALIZED_PNL"
    assert rows[tlt]["financing"]["note"].startswith("x1 long")
    assert set(pv["allocation"]) == {"QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"}
    assert abs(sum(pv["allocation"].values()) - pv["capital"]["investable"]) < 0.02
    assert pv["proposed_holdings"]["aggregate_targets_usd"]
    assert pv["writers"][0]["name"] == "gold-miner" and pv["writers"][0]["status"].startswith("ACTIVE")
    assert e.db.one("SELECT owner FROM ownership WHERE position_id=?", (gdx,))["owner"] == "PRE_EXISTING"
    with pytest.raises(ShadowWriteViolation):
        T.ReadOnlyView(env.broker).submit_close(gdx, IDS["GDX"], None, client_ref="x")
    md = T.render(pv)
    assert "SHORT x5" in md and "Material execution risks" in md


def test_takeover_stops_at_approval_without_executing(tmp_path):
    """INVARIANT: without the owner's approvals nothing advances; the run sits at APPROVAL_REQUIRED and the broker is
    untouched, however many times `advance` is attempted."""
    env, e, gdx, _ = setup(tmp_path)
    before = broker_bytes(env)
    pv = T.preview(e, env.clock())
    for _ in range(3):
        with pytest.raises(T.TakeoverError, match="DISABLE_OLD_WRITER"):
            T.advance(e, pv["run_id"], env.clock(), "owner")
    assert T.run(e.db, pv["run_id"])["state"] == "APPROVAL_REQUIRED"
    assert broker_bytes(env) == before
    blocked = e.db.q("SELECT 1 FROM audit_log WHERE kind='TAKEOVER_BLOCKED'")
    assert len(blocked) == 3


def test_writer_must_be_verified_disabled_before_any_close(tmp_path):
    """INVARIANT: approving DISABLE_OLD_WRITER does not disable anything; the run waits in QUIESCE_OLD_WRITER until
    evidence that the owner disabled the writer is recorded."""
    env, e, _gdx, _ = setup(tmp_path)
    pv = T.preview(e, env.clock())
    T.approve(e, pv["run_id"], "DISABLE_OLD_WRITER", "owner", env.clock())
    T.advance(e, pv["run_id"], env.clock(), "owner")
    with pytest.raises(T.TakeoverError, match="DISABLED_VERIFIED"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    T.record_writer_evidence(e, pv["run_id"], "gold-miner", "ACTIVE", "unit still running", "owner", env.clock())
    with pytest.raises(T.TakeoverError, match="DISABLED_VERIFIED"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    assert T.run(e.db, pv["run_id"])["state"] == "QUIESCE_OLD_WRITER"


def test_revoked_approval_and_canceled_run(tmp_path):
    """INVARIANT: a revoked approval authorizes nothing; a canceled run is final and accepts no approval."""
    env, e, _gdx, _ = setup(tmp_path)
    pv = T.preview(e, env.clock())
    a = T.approve(e, pv["run_id"], "DISABLE_OLD_WRITER", "owner", env.clock())
    T.revoke(e, a["approval_id"], "owner", "changed my mind", env.clock())
    with pytest.raises(T.TakeoverError, match="required"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    T.cancel(e, pv["run_id"], "owner", "not now", env.clock())
    assert T.run(e.db, pv["run_id"])["state"] == "CANCELED"
    with pytest.raises(T.TakeoverError, match="CANCELED"):
        T.approve(e, pv["run_id"], "DISABLE_OLD_WRITER", "owner", env.clock())
    with pytest.raises(T.TakeoverError, match="final"):
        T.advance(e, pv["run_id"], env.clock(), "owner")


def test_stale_approval_is_refused(tmp_path):
    """INVARIANT: approvals expire (approval_ttl_hours); an expired approval cannot start the closes."""
    env, e, _gdx, _ = setup(tmp_path)
    pv = T.preview(e, env.clock())
    approve_all_to_close(env, e, pv)
    env.clock.advance(hours=5)
    with pytest.raises(T.TakeoverError, match="expired"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    assert T.run(e.db, pv["run_id"])["state"] == "VERIFY_ACCOUNT"


def test_changed_broker_position_invalidates_the_plan(tmp_path):
    """INVARIANT: if the old writer (or anyone) changes a position after approval, the plan is INVALIDATED, every
    approval is void and nothing is closed."""
    env, e, gdx, _ = setup(tmp_path)
    pv = T.preview(e, env.clock())
    approve_all_to_close(env, e, pv)
    u = env.broker.s["positions"][gdx]["units"]
    env.broker.submit_close(gdx, IDS["GDX"], round(u / 10, 6), client_ref="GOLD-MINER")      # GM trims
    subs = len(env.broker.s["submissions"])
    with pytest.raises(T.TakeoverError, match="INVALIDATED"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    assert T.run(e.db, pv["run_id"])["state"] == "INVALIDATED"
    assert {r["status"] for r in e.db.q("SELECT status FROM takeover_approvals WHERE run_id=?", (pv["run_id"],))} \
        <= {"INVALIDATED", "CONSUMED"}
    assert len(env.broker.s["submissions"]) == subs


def test_failed_close_halts_the_run_and_is_not_retried(tmp_path):
    """INVARIANT: a rejected close stops the takeover (HALTED, approvals void); it is never retried."""
    env, e, gdx, tlt = setup(tmp_path)
    pv = T.preview(e, env.clock())
    approve_all_to_close(env, e, pv)
    env.broker.inject("http_reject")
    with pytest.raises(T.TakeoverError, match="HALTED"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    subs = len(env.broker.s["submissions"])
    with pytest.raises(T.TakeoverError, match="final"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    assert len(env.broker.s["submissions"]) == subs
    assert gdx in env.broker.s["positions"] or tlt in env.broker.s["positions"]


def test_restart_during_takeover_resolves_without_resubmitting_and_completes(tmp_path):
    """INVARIANT: the engine dies right after the broker accepted a takeover close; at restart the close is
    attributed from broker evidence, the next `advance` resumes without re-sending, and the run completes:
    proceeds are external capital (TAKEOVER_PROCEEDS), allocated 50/50 only after the ACTIVATE approval."""
    env, e, gdx, tlt = setup(tmp_path)
    pv = T.preview(e, env.clock())
    approve_all_to_close(env, e, pv)
    env.broker.inject("crash_after_submit")
    with pytest.raises(SimulatedCrash):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    assert T.run(e.db, pv["run_id"])["state"] == "CLOSE_EXISTING"
    e2 = env.engine()                                                   # restart
    assert T.advance(e2, pv["run_id"], env.clock(), "owner")["state"] == "RECONCILE"
    refs = [s["client_ref"] for s in env.broker.s["submissions"] if s["kind"] == "close"]
    assert len(refs) == len(set(refs)) == 2                             # GDX and TLT, each sent once
    assert gdx not in env.broker.s["positions"] and tlt not in env.broker.s["positions"]
    T.advance(e2, pv["run_id"], env.clock(), "owner")                   # -> ALLOCATE
    pool = sleeves.balances(e2.db)[sleeves.UNALLOCATED]["cash"]
    assert pool > 0
    with pytest.raises(T.TakeoverError, match="ACTIVATE_STRATEGY"):
        T.advance(e2, pv["run_id"], env.clock(), "owner")
    T.approve(e2, pv["run_id"], "ACTIVATE_STRATEGY", "owner", env.clock())
    T.advance(e2, pv["run_id"], env.clock(), "owner")                   # -> ACTIVATE (pool allocated)
    assert T.advance(e2, pv["run_id"], env.clock(), "owner")["state"] == "MONITOR"
    b = sleeves.balances(e2.db)
    assert abs(b[sleeves.UNALLOCATED]["cash"]) < 1e-6
    assert b["QC_529_ETORO_RESTRICTED"]["cash"] == pytest.approx(b["QC_60_SQQQ"]["cash"], abs=0.02)
    kinds = {r["kind"] for r in e2.db.q("SELECT kind FROM cash_flows")}
    assert "TAKEOVER_PROCEEDS" in kinds and "TRADE_CLOSE" not in kinds
    r = e2.reconcile("t", env.clock())
    assert r.clean, r.issues
    with pytest.raises(LiveExecutionNotAvailable):
        T.enable_live(e2, pv["run_id"], env.clock())


def test_mixed_external_items_need_separate_handling(tmp_path):
    """INVARIANT: copy-trading mirrors and foreign pending orders are never closed automatically: they are listed for
    separate handling, they keep the account halted, so no close can start, and exclusive ownership is never
    assumed while they exist."""
    env, e, gdx, _ = setup(tmp_path, mirror=True, pending=True)
    pv = T.preview(e, env.clock())
    assert [m["action"] for m in pv["mirrors"]] == ["SEPARATE_HANDLING"]
    assert [o["action"] for o in pv["pending_orders"]] == ["SEPARATE_HANDLING"]
    assert pv["remaining_after_takeover"]["mirrors_invested"] == 200.0
    approve_all_to_close(env, e, pv)
    subs = len(env.broker.s["submissions"])
    with pytest.raises(T.TakeoverError, match="halt"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    assert len(env.broker.s["submissions"]) == subs and gdx in env.broker.s["positions"]
    assert {"ACCOUNT_HAS_MIRRORS", "EXTERNAL_PENDING_ORDER"} <= {h["kind"] for h in halts.active(e.db)}
    assert e.db.get("account_takeover") is None


def test_shadow_mode_can_preview_but_never_close(tmp_path):
    """INVARIANT: in shadow mode (the real deployment mode) the full preview and every approval work, but CLOSE is
    refused: there is no write capability and real-account takeover execution is not released."""
    env, e, gdx, _ = setup(tmp_path, mode="shadow")
    assert e.writer is None
    pv = T.preview(e, env.clock())
    approve_all_to_close(env, e, pv)
    with pytest.raises(T.TakeoverError, match="no write capability"):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    assert gdx in env.broker.s["positions"] and T.TAKEOVER_EXECUTION_RELEASED is False


def test_approval_needs_explicit_cost_and_exposure_bounds(tmp_path):
    """INVARIANT: loss/fee acceptance must cover the preview's estimate and close approval must bound exposure."""
    env, e, _gdx, _ = setup(tmp_path)
    pv = T.preview(e, env.clock())
    with pytest.raises(T.TakeoverError, match="max-cost"):
        T.approve(e, pv["run_id"], "ACCEPT_LOSSES_AND_FEES", "owner", env.clock())
    with pytest.raises(T.TakeoverError, match="below"):
        T.approve(e, pv["run_id"], "ACCEPT_LOSSES_AND_FEES", "owner", env.clock(), max_cost_usd=0.0)
    with pytest.raises(T.TakeoverError, match="max-exposure"):
        T.approve(e, pv["run_id"], "CLOSE_POSITIONS", "owner", env.clock())
