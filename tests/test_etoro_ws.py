"""Read-only eToro WebSocket quote snapshots (blocker G14): operation allowlist, frame parsing, and REST/WS merging
by broker timestamp with REST as the fallback."""
import json

import pytest

from utengine.brokers.etoro import EtoroReadOnly
from utengine.brokers.etoro_ws import check_ws_read_only, parse_frames, snapshot_rows
from utengine.core.errors import ReadOnlyViolation

CFG = {"broker": {"access": "live_read_only", "request_timeout_seconds": 5,
                  "credentials_env": {"api_key": "A", "user_key": "U"}}}
REST = {"rates": [{"instrumentID": 1, "bid": 10.0, "ask": 10.1, "date": "2026-10-30T11:00:00Z"},
                  {"instrumentID": 2, "bid": 20.0, "ask": 20.1, "date": "2026-10-30T13:40:00.1234567Z"},
                  {"instrumentID": 3, "bid": 30.0, "ask": 30.1, "date": "2026-10-30T12:00:00Z"}]}


def reader(ws_fetch):
    a = EtoroReadOnly(CFG, transport=lambda m, u, h, b: (200, json.dumps(REST).encode()), env={"A": "x", "U": "y"},
                      ws_fetch=ws_fetch)
    a.key_scopes_verified = True
    return a


@pytest.mark.parametrize("op,data", [("Authenticate", {"userKey": "u", "apiKey": "a"}),
                                     ("Subscribe", {"topics": ["instrument:3002", "instrument:1"], "snapshot": True})])
def test_ws_allowlist_accepts_only_read_operations(op, data):
    check_ws_read_only(op, data)


@pytest.mark.parametrize("op,data", [("Subscribe", {"topics": ["private"], "snapshot": True}),
                                     ("Subscribe", {"topics": []}),
                                     ("Subscribe", {"topics": ["instrument:1"], "orders": True}),
                                     ("PlaceOrder", {"instrumentId": 1}),
                                     ("Authenticate", {"userKey": "u", "apiKey": "a", "extra": 1})])
def test_ws_allowlist_refuses_everything_else(op, data):
    with pytest.raises(ReadOnlyViolation):
        check_ws_read_only(op, data)


def test_snapshot_parsing_keeps_only_complete_positive_snapshots():
    good = {"topic": "instrument:7", "type": "Snapshot",
            "content": json.dumps({"Bid": "1.5", "Ask": 1.6, "Date": "2026-10-30T13:31:00Z"})}
    frames = parse_frames(json.dumps({"messages": [json.dumps(good),
                                                   json.dumps({"topic": "instrument:7", "type": "Trading.Instrument.Rate",
                                                               "content": "{}"})]}))
    assert [snapshot_rows(f) for f in frames] == [(7, 1.5, 1.6, "2026-10-30T13:31:00Z"), None]
    assert snapshot_rows({"topic": "instrument:7", "type": "Snapshot", "content": {"Bid": 0, "Ask": 1, "Date": "x"}}) is None
    assert snapshot_rows({"topic": "private", "type": "Snapshot", "content": {"Bid": 1, "Ask": 1, "Date": "x"}}) is None


def test_quotes_take_the_newer_broker_timestamp_per_instrument():
    """INVARIANT: per instrument the newer broker timestamp wins (WS newer -> WS; REST newer -> REST); instruments
    the WS did not return keep REST."""
    ws = {1: (10.5, 10.6, "2026-10-30T13:45:00.9999999Z"), 2: (19.0, 19.1, "2026-10-30T13:00:00Z")}
    a = reader(lambda ids, k, u: ws)
    q = a.quotes([1, 2, 3])
    assert (q[1].bid, q[2].bid, q[3].bid) == (10.5, 20.0, 30.0)
    assert a.last_quote_sources == {1: "ws", 2: "rest", 3: "rest"}


def test_ws_failure_falls_back_to_rest():
    def boom(ids, k, u):
        raise OSError("ws down")
    a = reader(boom)
    assert {i: x.bid for i, x in a.quotes([1, 2, 3]).items()} == {1: 10.0, 2: 20.0, 3: 30.0}
    assert set(a.last_quote_sources.values()) == {"rest"}
