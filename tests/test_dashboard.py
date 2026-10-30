"""Read-only, authenticated dashboard (src/utengine/dashboard). Simulated engine state; the server runs in a thread on
an ephemeral loopback port. No broker, no network beyond 127.0.0.1."""
import datetime as dt
import hashlib
import http.client
import json
import pathlib
import threading
import urllib.parse

import pytest

from conftest import FULL_CAPITAL, IDS, multi_env
from utengine.brokers.simulated import SimulatedBroker
from utengine.core.errors import BrokerUnavailable
from utengine.dashboard import server as S
from utengine.dashboard.data import Data

A, B = "QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"
PW = "correct horse battery staple"


def setup(tmp_path, **kw):
    env = multi_env(tmp_path, capital=FULL_CAPITAL, broker_kwargs={"cash_tx_capability": False}, **kw)
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(200.0, record=False)              # no evidence: suspense
    env.clock.advance(minutes=1)
    e.reconcile("t", env.clock())
    return env, e, Data(e.cfg, e.specs, env.home, clock=env.clock)


def test_financial_metrics_reconcile_to_the_engine(tmp_path):
    """INVARIANT: every overview number is the engine's own: account equity = sum of sleeves + suspense + foreign;
    sleeve equity equals the engine's sleeve valuation; lifetime P&L equals the recorded daily P&L."""
    env, e, d = setup(tmp_path)
    o = d.overview()
    a = o["account"]
    assert a["status"] == "VERIFIED" and a["suspense_unclassified"] == 200.0
    assert a["account_equity"] == pytest.approx(a["sum_of_sleeve_equity"] + 200.0, abs=1e-4)
    v = e.sleeve_valuation(e.reconcile("t", env.clock()))
    for s in (A, B):
        assert o["sleeves"][s]["equity"] == pytest.approx(v["sleeves"][s]["equity"], abs=1e-6)
    pnl = sum(r["pnl"] or 0 for r in e.db.q("SELECT pnl FROM daily_equity"))
    assert o["pnl"]["lifetime"] == pytest.approx(round(pnl, 2))
    assert o["data_label"] == "SIMULATED" and o["mode"] == "SIMULATION"


def test_both_sleeves_and_shared_instrument_attribution(tmp_path):
    """INVARIANT: both sleeves are shown with their own holdings; the shared TQQQ appears in the overlap with each
    sleeve's value, and the combined exposure equals the broker's TQQQ value."""
    env, e, d = setup(tmp_path)
    s = d.strategies()
    assert set(s["sleeves"]) == {A, B}
    assert "TQQQ" in s["overlap"] and set(s["overlap"]["TQQQ"]) == {A, B}
    r = e.reconcile("t", env.clock())
    broker_tqqq = sum(p.value for p in r.snapshot.positions if p.instrument_id == IDS["TQQQ"])
    assert s["exposure_by_instrument"]["TQQQ"] == pytest.approx(broker_tqqq, abs=0.01)
    assert s["sleeves"][A]["status"]["crash_guard_active"] is False
    assert s["sleeves"][B]["status"]["regime"].startswith(("BULL", "BEAR"))
    assert "SOXL" in s["sleeves"][A]["cash_from_unavailable_instruments"] or \
        s["sleeves"][A]["decision"]["targets"].get("SOXL") is None


def test_missing_broker_pnl_is_unavailable_never_estimated(tmp_path, monkeypatch):
    """INVARIANT: when the broker gives no unrealized P&L, account equity is None (INCOMPLETE), not a number."""
    env, e, d = setup(tmp_path)
    real = SimulatedBroker.account_snapshot

    def no_pnl(self):
        from dataclasses import replace
        s = real(self)
        return replace(s, positions=tuple(replace(p, unrealized_pnl=None) for p in s.positions))
    monkeypatch.setattr(SimulatedBroker, "account_snapshot", no_pnl)
    env.clock.advance(minutes=1)
    e.reconcile("t", env.clock())
    o = d.overview()
    assert o["account"]["account_equity"] is None and o["account"]["status"] == "INCOMPLETE"
    assert all(v["equity"] is None for v in o["sleeves"].values())


def test_stale_state_and_broker_outage_are_labelled(tmp_path, monkeypatch):
    """INVARIANT: an old snapshot is STALE; a failed broker read shows STALE data and UNAVAILABLE connectivity."""
    env, e, d = setup(tmp_path)
    env.clock.advance(hours=2)
    assert d.overview()["data_label"] == "STALE"

    def down(self):
        raise BrokerUnavailable("simulated outage")
    monkeypatch.setattr(SimulatedBroker, "account_snapshot", down)
    e.reconcile("t", env.clock())
    op = d.operations()
    assert op["broker"]["connectivity"] == "UNAVAILABLE" and op["broker"]["label"] == "STALE"
    assert any(x["kind"] == "BROKER_UNAVAILABLE" for x in op["exceptions_open"])


def test_dashboard_survives_engine_restart(tmp_path):
    """INVARIANT: the dashboard reads the durable state: after an engine restart the numbers are identical."""
    env, e, d = setup(tmp_path)
    before = d.overview()["sleeves"]
    env.engine()
    assert Data(e.cfg, e.specs, env.home, clock=env.clock).overview()["sleeves"] == before


# ----------------------------------------------------------------------------------------------- HTTP
@pytest.fixture
def srv(tmp_path):
    env, e, d = setup(tmp_path)
    S.set_password(env.home, PW)
    box = {}
    t = threading.Thread(target=S.serve, args=(d, env.home), kwargs={"port": 0, "env": {},
                         "ready": lambda h: box.update(h=h)}, daemon=True)
    t.start()
    while "h" not in box:
        pass
    yield env, e, box["h"]
    box["h"].shutdown()


def req(h, method, path, body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", h.server_address[1], timeout=10)
    hd = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
    if cookie:
        hd["Cookie"] = cookie
    c.request(method, path, body=body, headers=hd)
    r = c.getresponse()
    data = r.read()
    return r.status, dict(r.getheaders()), data


def login(h, pw=PW):
    st, hd, _ = req(h, "POST", "/login", body=urllib.parse.urlencode({"password": pw}))
    return st, hd.get("Set-Cookie", "").split(";")[0]


def test_authentication_sessions_lockout_and_one_time_links(srv):
    """INVARIANT: no data without a session; a wrong password fails and 5 failures lock the address out; a valid
    session reads every section; logout ends it; a login link works once and cannot redirect off-site."""
    env, e, h = srv
    assert req(h, "GET", "/api/overview")[0] == 401
    assert req(h, "GET", "/")[0] == 303
    st, ck = login(h)
    assert st == 303 and ck.startswith("ute_session=")
    for name in S.DATA_ROUTES:
        st, hd, body = req(h, "GET", f"/api/{name}", cookie=ck)
        assert st == 200, (name, body[:200])
        assert hd["Cache-Control"] == "no-store" and "frame-ancestors 'none'" in hd["Content-Security-Policy"]
    assert "HttpOnly" in req(h, "POST", "/login", body="password=" + urllib.parse.quote(PW))[1]["Set-Cookie"]
    req(h, "POST", "/logout", cookie=ck)
    assert req(h, "GET", "/api/overview", cookie=ck)[0] == 401
    tok = S.new_login_link(env.home)
    st, hd, _ = req(h, "GET", f"/login?otp={tok}&next=//evil.example/")
    assert st == 303 and hd["Location"] == "/"
    assert req(h, "GET", f"/login?otp={tok}")[0] == 401                      # single use
    for _ in range(4):                       # the reused link above was failure 1 of 5 for this address
        assert login(h, "wrong password!!")[0] == 401
    assert login(h)[0] == 429                                                 # locked out, even with the right one


def test_read_only_enforcement(srv):
    """INVARIANT: no write method reaches any data route; viewing every page changes nothing in the engine's
    database (file hash, audit length, alerts); the read connection itself refuses writes."""
    env, e, h = srv
    _, ck = login(h)
    dbp = pathlib.Path(env.home) / "engine.sqlite"
    e.db.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    sha = hashlib.sha256(dbp.read_bytes()).hexdigest()
    n_audit = e.db.one("SELECT COUNT(*) n FROM audit_log")["n"]
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        for path in ("/api/overview", "/api/trades", "/api/orders", "/"):
            assert req(h, m, path, body="x=1", cookie=ck)[0] in (404, 405)
    for _ in range(3):
        for name in S.DATA_ROUTES:
            req(h, "GET", f"/api/{name}", cookie=ck)
    assert hashlib.sha256(dbp.read_bytes()).hexdigest() == sha
    assert e.db.one("SELECT COUNT(*) n FROM audit_log")["n"] == n_audit
    from utengine.dashboard.data import _RO
    ro = _RO(dbp)
    with pytest.raises(Exception):
        ro.con.execute("INSERT INTO engine_state VALUES('x','1','t')")


def test_no_order_path_and_no_secret_exposure():
    """INVARIANT: the dashboard package imports no executor, no broker writer and no live-trading module, and has no
    route other than login/logout/health/page and the read APIs."""
    src = "".join(p.read_text(encoding="utf-8") for p in pathlib.Path(S.__file__).parent.glob("*.py"))
    for bad in ("core.execution", "Executor(", "etoro_live", "submit_open", "submit_close", "obtain_writer",
                "authorize_live", "SimulatedBroker", "takeover.advance", "approve(", "TradingEngine"):
        assert bad not in src, bad
    assert S.DATA_ROUTES == ("meta", "overview", "strategies", "trades", "operations", "migration")


def test_refuses_unsafe_startup(tmp_path):
    """INVARIANT: no password -> refused; a non-loopback bind without --allow-remote -> refused; a broker WRITE
    credential in the environment -> refused (the dashboard never runs next to a write key)."""
    env, e, d = setup(tmp_path)
    with pytest.raises(SystemExit, match="no dashboard password"):
        S.serve(d, env.home, port=0, env={})
    S.set_password(env.home, PW)
    with pytest.raises(SystemExit, match="loopback"):
        S.serve(d, env.home, host="0.0.0.0", port=0, env={})
    with pytest.raises(SystemExit, match="WRITE credential"):
        S.serve(d, env.home, port=0, env={"UTE_ETORO_WRITE_API_KEY": "x" * 20})
    with pytest.raises(ValueError):
        S.set_password(env.home, "short")
    doc = json.loads(S.auth_path(env.home).read_text())
    assert PW not in json.dumps(doc) and doc["scheme"].startswith("scrypt")


def test_migration_view_shows_takeover_and_retirement_without_acting(tmp_path):
    """INVARIANT: the migration view shows the takeover preview, approvals, blockers and the Gold Miner readiness
    checklist, and reading it never advances anything."""
    from test_takeover import GM
    from utengine.core import takeover as T
    env = multi_env(tmp_path, takeover=GM, capital=FULL_CAPITAL)
    env.broker.external_open_position(IDS["GDX"], 300.0, is_buy=False, leverage=5, open_rate=81.43)
    e = env.engine()
    pv = T.preview(e, env.clock())
    m = Data(e.cfg, e.specs, env.home, clock=env.clock).migration()
    assert m["takeover"]["run_id"] == pv["run_id"] and m["takeover"]["state"] == "APPROVAL_REQUIRED"
    assert m["foreign_positions"][0]["owner"] == "PRE_EXISTING"
    assert m["gold_miner_retirement"]["steps"]["activation_gates"]["status"] == "BLOCKED"
    assert T.run(e.db, pv["run_id"])["state"] == "APPROVAL_REQUIRED"


def test_login_link_cannot_be_shadowed_or_burned_by_a_preview(srv, tmp_path, capsys):
    """Observed 2026-10-29 on Windows: a second dashboard bound the same port (SO_REUSEADDR) and answered links minted
    for another state directory ("invalid or expired"). INVARIANT: a second bind of a listening port is refused; a
    HEAD (link preview) never consumes a link; `login-link` refuses when the port serves another state directory."""
    env, e, h = srv
    port = h.server_address[1]
    with pytest.raises(SystemExit, match="cannot bind"):
        S.serve(None, env.home, port=port, env={})
    tok = S.new_login_link(env.home)
    assert req(h, "HEAD", f"/login?otp={tok}")[0] == 200
    st, hd, _ = req(h, "GET", f"/login?otp={tok}")
    assert st == 303 and hd["Set-Cookie"].startswith("ute_session=")       # still valid after the preview
    assert S.probe_instance(port) == (True, S.instance_id(env.home))
    other = tmp_path / "other-home"
    S.set_password(other, PW)
    from utengine.cli import main as cli
    assert cli(["--config", str(env.cfg_path), "--home", str(other), "dashboard",
                "login-link", "--port", str(port)]) == 2
    assert "different state directory" in capsys.readouterr().err
    assert len(json.loads(S.auth_path(other).read_text()).get("otps", [])) == 0   # no link minted for the wrong one


def test_migration_labels_follow_recorded_evidence(tmp_path):
    """INVARIANT: before takeover the migration view says the engine has no write capability (not "DONE"), shows the
    last recorded key check, labels Gold Miner's positions as foreign with broker exposure and fees, and lists only
    blockers that the recorded state still supports."""
    from test_takeover import GM
    env = multi_env(tmp_path, takeover=GM, capital=FULL_CAPITAL)
    env.broker.external_open_position(IDS["GDX"], 300.0, is_buy=False, leverage=5, open_rate=81.43)
    e = env.engine()
    e.db.audit("BROKER_KEY_CHECK", {"status": "READ_ONLY", "scopes": ["etoro-public:trade.real:read"]})
    m = Data(e.cfg, e.specs, env.home, clock=env.clock).migration()
    assert m["pre_takeover"] is True    # (this simulated broker is itself writer-capable: two_writer_risk is True)
    assert m["broker_key"]["status"] == "READ_ONLY"
    assert not any("read-only eToro key" in b for b in m["blockers"])
    assert any("Gold Miner is the live writer" in b for b in m["blockers"])
    assert any("not owned by the engine" in b for b in m["blockers"])
    f = m["foreign_positions"][0]
    assert f["owner"] == "PRE_EXISTING" and {"exposure_usd", "fees", "close_rate", "quote_at"} <= set(f)
    two = S.page.APP.split("Two-writer risk now")[1][:300]
    assert 'badge("DONE")' not in two and 'badge("NONE")' in two and "Gold Miner remains the only writer" in two


def test_log_and_backup_dirs_can_live_outside_the_state_directory(tmp_path, monkeypatch):
    """VPS layout (/home/ute/data, /home/ute/logs, /home/ute/backups/nightly): UTE_LOG_DIR moves the engine log and
    UTE_BACKUP_DIR is where the dashboard lists backups; without them both stay under the state directory."""
    monkeypatch.setenv("UTE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("UTE_BACKUP_DIR", str(tmp_path / "nightly"))
    env, e, d = setup(tmp_path / "s")
    assert (tmp_path / "logs" / "engine.jsonl").exists() and not (env.home / "logs").exists()
    (tmp_path / "nightly").mkdir()
    (tmp_path / "nightly" / "engine-20261030T000000Z.manifest.json").write_text("{}")
    assert d.operations()["backups"] == ["engine-20261030T000000Z.manifest.json"]


@pytest.mark.parametrize("trust_proxy", [True, False])
def test_lockout_is_per_real_client_behind_the_local_proxy(tmp_path, trust_proxy):
    """INVARIANT (single login behind nginx): with --trust-proxy, failed logins are counted per X-Real-IP (set by the
    local proxy), so an attacker locks out only itself, never the owner; without it the header is ignored (every
    request counts against the socket address). A malformed header falls back to the socket address."""
    env, e, d = setup(tmp_path)
    S.set_password(env.home, PW)
    box = {}
    threading.Thread(target=S.serve, args=(d, env.home), kwargs={"port": 0, "env": {}, "trust_proxy": trust_proxy,
                     "ready": lambda h: box.update(h=h)}, daemon=True).start()
    while "h" not in box:
        pass
    h = box["h"]

    def post(pw, real_ip):
        c = http.client.HTTPConnection("127.0.0.1", h.server_address[1], timeout=10)
        c.request("POST", "/login", body=urllib.parse.urlencode({"password": pw}),
                  headers={"Content-Type": "application/x-www-form-urlencoded", "X-Real-IP": real_ip})
        r = c.getresponse()
        r.read()
        return r.status
    try:
        for _ in range(5):
            assert post("wrong password!!", "203.0.113.5") == 401
        assert post(PW, "203.0.113.5") == 429                        # the attacker's address is locked out
        owner = post(PW, "198.51.100.7")
        assert owner == (303 if trust_proxy else 429)                # the owner logs in (only behind a trusted proxy)
        if trust_proxy:
            assert post(PW, "not-an-address") == 303                 # malformed: socket address (not locked)
    finally:
        h.shutdown()


def test_snapshot_is_every_dashboard_route_and_writes_nothing(tmp_path, capsys):
    """INVARIANT (read-only audit account): `dashboard snapshot` returns every data route of the dashboard as JSON from
    the read-only data layer; the engine database is unchanged; no credential or password hash appears in it."""
    env, e, d = setup(tmp_path)
    db = sorted(p for p in env.home.rglob("*") if p.is_file() and not p.name.endswith("-shm"))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in db}
    S.set_password(env.home, PW)
    auth = S.auth_path(env.home).read_text()
    from utengine.cli import main as cli
    assert cli(["--config", str(env.cfg_path), "--home", str(env.home), "dashboard", "snapshot"]) == 0
    out = capsys.readouterr().out
    snap = json.loads(out)
    assert set(snap) == set(S.DATA_ROUTES)
    assert snap["overview"]["account"]["account_equity"] == pytest.approx(d.overview()["account"]["account_equity"])
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in db} == before
    assert json.loads(auth)["hash"] not in out and "API_KEY" not in out and "USER_KEY" not in out


def test_performance_values_are_labelled_as_the_last_close_and_history_is_untouched(tmp_path):
    """2026-10-30: `status` showed the pre-takeover close next to the live equity without saying it was a close. INVARIANT: the performance blocks name their equity values last_close_*
    and say which close they are; they equal the recorded rows; new capital after the close changes the live
    valuation, never the recorded history."""
    from utengine.core import accounting, sleeves
    env, e, d = setup(tmp_path)
    rows = [dict(r) for r in e.db.q("SELECT * FROM daily_equity ORDER BY session")]
    assert rows, "the scenario records a daily close"
    p = accounting.performance(e.db)
    assert p["last_close_strategy_equity"] == rows[-1]["strategy_equity"] and "equity" not in p
    assert p["last_close_account_equity"] == rows[-1]["account_equity"] and "account_equity" not in p
    assert p["values_as_of"].startswith(f"close of session {rows[-1]['session']}") and "not live" in p["values_as_of"]
    for s, v in sleeves.performance(e.db).items():
        assert "equity" not in v and "not live" in v["values_as_of"] and v["last_close_equity"] is not None
    env.broker.deposit(150.0, record=False)
    env.clock.advance(minutes=1)
    e.reconcile("t", env.clock())
    assert [dict(r) for r in e.db.q("SELECT * FROM daily_equity ORDER BY session")] == rows
    assert accounting.performance(e.db)["last_close_strategy_equity"] == rows[-1]["strategy_equity"]
