"""Pending CLOSE orders (eToro ordersForClose / ordersForCloseMultiple): parsed from the portfolio, never duplicated,
and the handle that recovers a lost close response (blocker B3: a close carries no client reference)."""
import json

from conftest import Env
from utengine.brokers.etoro import EtoroReadOnly
from utengine.core.order_planner import Leg

CFG = {"broker": {"access": "live_read_only", "request_timeout_seconds": 5,
                  "credentials_env": {"api_key": "A", "user_key": "U"}}}
PNL = {"clientPortfolio": {
    "credit": 500.0, "positions": [], "mirrors": [], "orders": [],
    "ordersForOpen": [{"orderID": 11, "instrumentID": 3002, "amount": 40.0, "isBuy": True, "mirrorID": 0}],
    "ordersForClose": [{"orderId": 21, "orderType": 1, "statusId": 1, "cid": 1, "instrumentId": 3002,
                        "unitsToDeduct": 12.5, "lotsToDeduct": 0, "positionId": 9100000001,
                        "openDateTime": "2026-10-29T21:00:00Z", "lastUpdate": "2026-10-29T21:00:00Z"}],
    "ordersForCloseMultiple": [{"orderId": 22, "orderType": 1, "statusId": 1, "cid": 1, "instrumentId": 3002,
                                "unitsToDeduct": 0, "lotsToDeduct": 0,
                                "pendingClosePositionIds": [9100000002, 9100000003]}]}}


def test_etoro_parses_pending_close_orders_without_reserving_cash():
    """INVARIANT: documented pending-close orders become kind=close PendingOrders naming their position(s); they
    reserve no cash (pending_open_amount counts opens only)."""
    a = EtoroReadOnly(CFG, transport=lambda m, u, h, b: (200, json.dumps(PNL).encode()), env={"A": "x", "U": "y"})
    a.key_scopes_verified = True
    snap = a.account_snapshot()
    by = {o.order_id: o for o in snap.pending_orders}
    assert by["11"].kind == "open" and snap.pending_open_amount == 40.0
    assert by["21"].kind == "close" and by["21"].position_ids == ("9100000001",) and by["21"].units == 12.5
    assert by["22"].position_ids == ("9100000002", "9100000003") and by["22"].units is None


def _engine_position(tmp_path):
    env = Env(tmp_path)
    env.engine()
    env.decide_and_execute()
    pid, p = sorted(env.broker.s["positions"].items())[0]
    return env, pid, p


def _close_leg(pid, p):
    return Leg(leg_index=0, phase=1, action="CLOSE", symbol="X", instrument_id=p["instrument_id"],
               amount=p["amount"], units=None, position_id=pid, reason="test close")


def test_queued_close_is_never_duplicated(tmp_path):
    """INVARIANT: while a close for a position is pending at the broker, a second close intent for that position is
    ABANDONED before any broker call (no duplicate close)."""
    env, pid, p = _engine_position(tmp_path)
    ex = env.e.executor
    n0 = len(env.broker.s["submissions"])
    env.broker.inject("queued_close")
    assert ex.run_leg("planA", _close_leg(pid, p), "s") == "PENDING"
    snap = env.broker.account_snapshot()
    assert [o.position_ids for o in snap.pending_orders if o.kind == "close"] == [(pid,)]
    assert ex.run_leg("planB", _close_leg(pid, p), "s") == "ABANDONED"
    assert len(env.broker.s["submissions"]) == n0 + 1 and pid in env.broker.s["positions"]
    env.broker.advance()
    assert pid not in env.broker.s["positions"]


def test_lost_close_response_recovered_from_pending_close_order(tmp_path):
    """INVARIANT (B3): a close whose response is lost while the broker queues it is found by the pending close order
    naming the position, adopted by order id (never resubmitted), and resolves to FILLED when it executes."""
    env, pid, p = _engine_position(tmp_path)
    ex = env.e.executor
    n0 = len(env.broker.s["submissions"])
    env.broker.inject("queued_close_timeout")
    assert ex.run_leg("planA", _close_leg(pid, p), "s") == "PENDING"
    row = env.e.db.one("SELECT * FROM intents WHERE plan_id='planA'")
    assert row["broker_order_id"]
    assert env.e.db.one("SELECT COUNT(*) c FROM audit_log WHERE kind='ORDER_FOUND_BY_PENDING_CLOSE'")["c"] == 1
    env.broker.advance()
    assert ex.run_leg("planA", _close_leg(pid, p), "s") == "FILLED"
    assert len(env.broker.s["submissions"]) == n0 + 1 and pid not in env.broker.s["positions"]
