"""Scenario 26: shadow mode cannot execute live -- including after restarts -- with a recording fake eToro."""
import json

import pytest

from conftest import DECIDE_AT, EXECUTE_AT, Clock, write_engine_yaml
from utengine.brokers.etoro import EtoroReadOnly, check_read_only
from utengine.brokers.base import BrokerWriter
from utengine.core.configuration import load_engine_config
from utengine.core.engine import TradingEngine
from utengine.core.errors import KeyNotReadOnly, ReadOnlyViolation
from utengine.core.market_data import CsvBarStore
from conftest import SNAP_ALL

ENV = {"UTE_ETORO_READONLY_API_KEY": "test-api-key-123456", "UTE_ETORO_READONLY_USER_KEY": "test-user-key-654321"}


class FakeEtoro:
    """Records every request; answers the read endpoints with a small account (cash only)."""

    def __init__(self, credit=2000.0):
        self.calls, self.credit = [], credit

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        path = url.replace("https://public-api.etoro.com", "")
        if path == "/api/v1/trading/info/real/pnl":
            return 200, json.dumps({"clientPortfolio": {"credit": self.credit, "positions": [], "ordersForOpen": [],
                                                        "orders": []}}).encode()
        if path.startswith("/api/v1/market-data/instruments/rates"):
            ids = path.split("=")[1].split(",")
            return 200, json.dumps({"rates": [{"instrumentID": int(i), "bid": 68.0, "ask": 68.02,
                                               "date": EXECUTE_AT.isoformat()} for i in ids]}).encode()
        if path == "/api/v1/me":
            return 200, json.dumps({"realCid": 1, "scopes": ["etoro-public:trade.real:read"]}).encode()
        return 404, b"{}"


def make(tmp_path, transport, clock):
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml", mode="shadow")
    broker = EtoroReadOnly(load_engine_config(cfg), transport=transport, env=ENV)
    e = TradingEngine(cfg, tmp_path / "home", clock=clock, broker=broker, bar_store=CsvBarStore(SNAP_ALL), env=ENV)
    e.startup()
    return e


def test_s26_shadow_cycle_never_writes_and_survives_restart(tmp_path):
    """INVARIANT: a full shadow decision + execution window -- run twice with a restart in between -- sends only
    allowlisted read requests, creates no writer, records SHADOW_RECORDED intents, and never logs a credential."""
    fake, clock = FakeEtoro(), Clock(DECIDE_AT)
    e = make(tmp_path, fake, clock)
    assert e.writer is None and not isinstance(e.broker, BrokerWriter)
    assert e.cycle()["decision"]["status"] == "DECIDED"
    clock.set(EXECUTE_AT)
    x = e.cycle()["execution"]
    assert x["status"] == "SHADOW", x
    e2 = make(tmp_path, fake, clock)                         # restart
    assert e2.writer is None
    assert e2.cycle()["execution"]["status"] == "ALREADY_EXECUTED"
    for method, url, _h, _b in fake.calls:
        check_read_only(method, url.replace("https://public-api.etoro.com", ""))
        assert "/trading/execution" not in url
    assert {m for m, *_ in fake.calls} <= {"GET"}
    st = [r["status"] for r in e2.db.q("SELECT status FROM intents")]
    assert st and set(st) == {"SHADOW_RECORDED"}
    logs = (tmp_path / "home" / "logs" / "engine.jsonl").read_text(encoding="utf-8")
    assert ENV["UTE_ETORO_READONLY_API_KEY"] not in logs and ENV["UTE_ETORO_READONLY_USER_KEY"] not in logs
    db_dump = "\n".join(str(tuple(r)) for r in e2.db.q("SELECT * FROM audit_log"))
    assert ENV["UTE_ETORO_READONLY_API_KEY"] not in db_dump


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v2/trading/execution/orders"),
    ("POST", "/api/v1/trading/execution/market-close-orders/positions/123"),
    ("PATCH", "/api/v2/trading/positions/123"),
    ("DELETE", "/api/v1/trading/info/real/pnl"),
    ("GET", "/api/v1/trading/info/real/pnl/../../execution/orders"),
])
def test_s26_transport_refuses_writes_before_network(method, path):
    """INVARIANT: any non-allowlisted request is refused BEFORE the transport is called."""
    called = []
    b = EtoroReadOnly({"broker": {"access": "live_read_only", "credentials_env": {"api_key": "A_KEY", "user_key": "U_KEY"},
                                  "request_timeout_seconds": 5}},
                      transport=lambda *a: called.append(a), env={"A_KEY": "x" * 10, "U_KEY": "y" * 10})
    with pytest.raises(ReadOnlyViolation):
        b._request(method, path)
    assert called == []


def test_s26_shadow_without_credentials_fails_closed(tmp_path):
    """INVARIANT: with no credentials the shadow engine cannot read the account -> BROKER_UNAVAILABLE halt; it
    still never obtains a writer."""
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml", mode="shadow")
    e = TradingEngine(cfg, tmp_path / "home", clock=Clock(DECIDE_AT), bar_store=CsvBarStore(SNAP_ALL), env={})
    e.startup()
    assert e.writer is None
    assert any(h["kind"] == "BROKER_UNAVAILABLE" for h in e.status()["halts"])


GM_LIKE_SCOPES = ["etoro-public:trade.real:read", "etoro-public:trade.real:write", "etoro-public:market-data:read"]


class ScopedEtoro(FakeEtoro):
    """FakeEtoro whose /api/v1/me grants `scopes` (None = no scope list); `me_down` makes /me unreachable."""

    def __init__(self, scopes, me_down=False):
        super().__init__()
        self.scopes, self.me_down = scopes, me_down

    def __call__(self, method, url, headers, body):
        if url.endswith("/api/v1/me"):
            self.calls.append((method, url, dict(headers), body))
            if self.me_down:
                raise TimeoutError("simulated outage")
            return 200, json.dumps({"gcid": 7} if self.scopes is None else {"gcid": 7, "scopes": self.scopes}).encode()
        return super().__call__(method, url, headers, body)


def _pnl_reads(fake):
    return [u for _m, u, *_ in fake.calls if u.endswith("/trading/info/real/pnl")]


@pytest.mark.parametrize("scopes", [GM_LIKE_SCOPES, ["etoro-public:real:write"], None, []])
def test_migration_write_capable_or_unprovable_key_is_refused_before_any_account_read(tmp_path, scopes):
    """INVARIANT (migration 2026-10-29): a key whose GRANTED scopes include any write scope -- e.g. Gold Miner's,
    which has trade.real:write -- or whose scopes cannot be read is refused at startup: CRITICAL alert, audit entry,
    no account read, no writer."""
    fake = ScopedEtoro(scopes)
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml", mode="shadow")
    broker = EtoroReadOnly(load_engine_config(cfg), transport=fake, env=ENV)
    e = TradingEngine(cfg, tmp_path / "home", clock=Clock(DECIDE_AT), broker=broker, bar_store=CsvBarStore(SNAP_ALL),
                      env=ENV)
    with pytest.raises(KeyNotReadOnly):
        e.startup()
    assert e.writer is None and _pnl_reads(fake) == []
    kinds = [r["kind"] for r in e.db.q("SELECT kind FROM audit_log")]
    assert "BROKER_KEY_REFUSED" in kinds
    assert any(r["kind"] == "BROKER_KEY_NOT_READ_ONLY" for r in e.db.q("SELECT kind FROM alerts"))
    with pytest.raises(KeyNotReadOnly):                      # every later read is refused as well
        broker.account_snapshot()
    assert _pnl_reads(fake) == []


def test_migration_read_only_key_is_verified_and_recorded(tmp_path):
    fake = ScopedEtoro(["etoro-public:trade.real:read", "etoro-public:money.balance:read"])
    e = make(tmp_path, fake, Clock(DECIDE_AT))
    chk = [json.loads(r["payload"]) for r in e.db.q("SELECT payload FROM audit_log WHERE kind='BROKER_KEY_CHECK'")]
    assert chk and chk[-1]["status"] == "READ_ONLY"
    assert _pnl_reads(fake)                                  # the account was read only after the check


def test_migration_scope_check_outage_never_lets_an_unverified_key_read(tmp_path):
    """/me unreachable at startup -> UNVERIFIED (no halt on the outage itself), and the next account read checks
    the scopes FIRST: a write-capable key is still refused before the account is read."""
    fake = ScopedEtoro(GM_LIKE_SCOPES, me_down=True)
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml", mode="shadow")
    broker = EtoroReadOnly(load_engine_config(cfg), transport=fake, env=ENV)
    e = TradingEngine(cfg, tmp_path / "home", clock=Clock(DECIDE_AT), broker=broker, bar_store=CsvBarStore(SNAP_ALL),
                      env=ENV)
    e.startup()
    chk = [json.loads(r["payload"]) for r in e.db.q("SELECT payload FROM audit_log WHERE kind='BROKER_KEY_CHECK'")]
    assert chk[-1]["status"] == "UNVERIFIED" and _pnl_reads(fake) == []
    fake.me_down = False
    with pytest.raises(KeyNotReadOnly):
        broker.account_snapshot()
    assert _pnl_reads(fake) == [] and e.writer is None


def test_migration_transport_sends_an_identifying_user_agent(monkeypatch):
    """eToro's edge answers 403 'error code: 1010' to urllib's default 'Python-urllib/x.y' signature (observed
    2026-10-29 with the engine's own read-only key): every request must carry the engine's own User-Agent."""
    import urllib.request
    from utengine.brokers import etoro as E
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    E.UrllibTransport(5)("GET", E.BASE + "/api/v1/me", {"x-request-id": "r"}, None)
    assert seen["user-agent"] == E.USER_AGENT and "python-urllib" not in seen["user-agent"].lower()


@pytest.mark.parametrize("path,ok", [
    ("/api/v1/market-data/search?internalSymbolFull=SOXL", True),
    ("/api/v1/market-data/search?internalSymbolFull=LABU.US", True),
    ("/api/v1/market-data/search?internalSymbolFull=SOXL&fields=all", False),
    ("/api/v1/market-data/search?internalSymbolFull=soxl", False),
    ("/api/v1/market-data/search", False),
])
def test_migration_instrument_search_is_an_exact_read_only_route(path, ok):
    if ok:
        check_read_only("GET", path)
    else:
        with pytest.raises(ReadOnlyViolation):
            check_read_only("GET", path)
    with pytest.raises(ReadOnlyViolation):
        check_read_only("POST", "/api/v1/market-data/search?internalSymbolFull=SOXL")


def test_migration_real_position_payload_carries_financing_exposure_and_quote_time():
    """A real eToro GDX x5 short (2026-10-29, ids replaced) reports overnight financing in totalExternalFees while
    totalFees is 0.0, and its exposure, close rate and rate timestamp inside unrealizedPnL: all are read, none
    estimated."""
    pos = {"positionID": 1, "instrumentID": 3002, "isBuy": False, "units": 45.5, "amount": 740.0,
           "leverage": 5, "openRate": 81.44, "totalFees": 0.0, "totalExternalFees": 5.9, "openDateTime": "x",
           "unrealizedPnL": {"pnL": -4.12, "exposureInAccountCurrency": -3700.0, "closeRate": 81.53,
                             "timestamp": "2026-10-29T17:00:00.0000000Z"}}

    def transport(method, url, headers, body):
        if url.endswith("/api/v1/me"):
            return 200, json.dumps({"scopes": ["etoro-public:trade.real:read"]}).encode()
        return 200, json.dumps({"clientPortfolio": {"credit": 530.0, "positions": [pos], "ordersForOpen": [],
                                                    "orders": []}}).encode()
    b = EtoroReadOnly({"broker": {"access": "live_read_only", "credentials_env": {"api_key": "A", "user_key": "U"},
                                  "request_timeout_seconds": 5}}, transport=transport, env={"A": "a", "U": "u"})
    p = b.account_snapshot().positions[0]
    assert p.fees == 5.9 and p.exposure_usd == 3700.0 and p.close_rate == 81.53
    assert p.quote_at == "2026-10-29T17:00:00.0000000Z" and p.unrealized_pnl == -4.12
