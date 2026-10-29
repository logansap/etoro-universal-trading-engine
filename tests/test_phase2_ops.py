"""Phase 2 operational hardening: notifications, crash detection, market-data health, account/funding report,
off-host backup with restore verification, and the real-account audit importer (on synthetic documents)."""
import datetime as dt
import http.server
import json
import os
import subprocess
import sys
import threading

import pytest

from conftest import DECIDE_AT, NEXT_DECIDE, NEXT_EXECUTE, ROOT, SNAP_ALL, Env, extended_store, write_engine_yaml
from utengine.core import accounting
from utengine.core.market_data import CsvBarStore, DictBarStore
from utengine.monitoring.notify import Notifier, action_for

UTC = dt.timezone.utc


class Hook(http.server.BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):  # noqa: N802
        n = int(self.headers["Content-Length"])
        Hook.received.append(json.loads(self.rfile.read(n)))
        self.send_response(Hook.status)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def webhook():
    Hook.received, Hook.status = [], 200
    srv = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/hook"
    srv.shutdown()


def test_notifications_are_actionable_deduplicated_and_retried(tmp_path, webhook):
    """INVARIANT: a halt produces one webhook message carrying the runbook ACTION; the same alert inside the dedupe
    window is recorded but not re-sent; a failing endpoint is retried on a later flush; the outbox file always has
    the message."""
    env = Env(tmp_path)
    e = env.engine()
    n = Notifier(e.db, e.cfg, e.home, env={"UTE_ALERT_WEBHOOK_URL": webhook})
    from utengine.core import halts
    halts.raise_halt(e.db, "CASH_DISCREPANCY", "ALL", "broker cash changed +25.00, explained +0.00", e.nows())
    e.db.alert("CRITICAL", "CASH_DISCREPANCY", "broker cash changed +25.00, explained +0.00", ts=e.nows())
    assert n.flush(e.nows()) == {"sent": 1, "failed": 0, "webhook": True}
    msg = Hook.received[0]
    assert msg["kind"] == "CASH_DISCREPANCY" and "account resolve" in msg["action"] and "ACTION:" in msg["text"]
    Hook.status = 500
    e.db.alert("CRITICAL", "UNKNOWN_ORDER", "intent I1 outcome unknown", ts=e.nows())
    assert n.flush(e.nows())["failed"] == 1
    Hook.status = 200
    assert n.flush(e.nows())["sent"] == 1 and Hook.received[-1]["kind"] == "UNKNOWN_ORDER"
    lines = (e.home / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"UNKNOWN_ORDER"' in ln for ln in lines)
    assert webhook not in (e.home / "alerts.jsonl").read_text(encoding="utf-8")
    assert all(action_for(k) for k in ("MISSED_DECISION", "CRASH", "UNRESOLVED_ORDER", "DATA_STALE"))


def test_missed_decision_and_unresolved_order_notify(tmp_path):
    """INVARIANT: a missed decision window and an unresolved order each create a deliverable alert."""
    env = Env(tmp_path, clock_at=dt.datetime(2026, 10, 26, 15, 0, tzinfo=UTC))
    e = env.engine()
    e.cycle()
    env.clock.set(dt.datetime(2026, 10, 27, 14, 0, tzinfo=UTC))
    e.cycle()
    kinds = {r["kind"] for r in e.db.q("SELECT kind FROM alerts")}
    assert "MISSED_DECISION" in kinds
    env2 = Env(tmp_path / "b")
    e2 = env2.engine()
    env2.clock.set(DECIDE_AT)
    e2.cycle()
    env2.broker.inject("pending")
    env2.clock.set(dt.datetime(2026, 10, 27, 13, 35, tzinfo=UTC))
    e2.cycle()
    assert {"UNRESOLVED_ORDER"} <= {r["kind"] for r in e2.db.q("SELECT kind FROM alerts")}
    assert e2.db.one("SELECT COUNT(*) c FROM alerts WHERE delivered_at IS NOT NULL")["c"] >= 1


def test_crash_is_detected_at_next_start(tmp_path):
    """INVARIANT: a start after a run that never recorded ENGINE_STOP raises a CRASH alert; a clean stop does not."""
    env = Env(tmp_path)
    env.engine()                                       # started, never stopped -> next start sees a crash
    e = env.engine()
    assert e.db.one("SELECT COUNT(*) c FROM alerts WHERE kind='CRASH'")["c"] == 1
    e.shutdown("test")
    e = env.engine()
    assert e.db.one("SELECT COUNT(*) c FROM alerts WHERE kind='CRASH'")["c"] == 1


def _store_with_bad_last_close(factor):
    base = CsvBarStore(SNAP_ALL)
    data = {}
    for f in SNAP_ALL.glob("*.csv.gz"):
        s = f.name.split(".")[0]
        rows = base.bars(s, dt.date(2000, 1, 1), dt.date(2026, 10, 26))
        if s == "TQQQ":
            d, o, h, l_, c, v = rows[-1]
            rows = rows[:-1] + [(d, o * factor, h * factor, l_ * factor, c * factor, v)]
        data[s] = rows
    return DictBarStore(data)


def test_market_data_health_and_suspect_price_fail_closed(tmp_path):
    """INVARIANT: `data check` reports OK on good data; an implausible one-day move (unadjusted split / bad print)
    is SUSPECT_PRICE: no decision, a DATA_STALE alert, and health is not healthy."""
    env = Env(tmp_path)
    e = env.engine()
    assert e.data_check(now=DECIDE_AT)["status"] == "OK"
    env2 = Env(tmp_path / "b", bar_store=_store_with_bad_last_close(0.25))
    e2 = env2.engine()
    out = e2.data_check(now=DECIDE_AT)
    assert out["status"] == "SUSPECT_PRICE"
    env2.clock.set(DECIDE_AT)
    d = e2.cycle()["decision"]
    assert d["status"] == "NO_DECISION" and "SUSPECT_PRICE" in d["error"]
    assert "DATA_STALE" in {r["kind"] for r in e2.db.q("SELECT kind FROM alerts")}
    assert e2.status()["healthy"] is False


def test_account_report_separates_funding_from_trading_pnl(tmp_path):
    """INVARIANT: the report shows broker cash, account equity, foreign (pre-existing) value, strategy equity,
    contributions (initial + deposits) and trading P&L = strategy equity - net contributions; a deposit is
    funding, not P&L."""
    from conftest import IDS
    env = Env(tmp_path, bar_store=extended_store({dt.date(2026, 10, 27): 1.03}))
    env.broker.external_open(IDS["GDX"], 300.0)              # a foreign position before the engine starts
    e = env.engine()
    env.decide_and_execute()
    env.clock.set(dt.datetime(2026, 10, 27, 15, 0, tzinfo=UTC))
    env.broker.deposit(1000.0, evidence="bank transfer")
    env.price_all(1.03)
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    rep = accounting.account_report(e.db, e.reconcile("report", env.clock()))
    f = rep["funding"]
    assert f["INITIAL_CAPITAL"] == 1700.0 and f["DEPOSIT"] == 1000.0 and f["net_contributions"] == 2700.0
    assert rep["foreign_positions_amount"] == 300.0
    assert rep["strategy_equity"] == pytest.approx(2700.0 + rep["trading_pnl"], abs=0.01)
    assert rep["trading_pnl"] > 0                                      # TQQQ +3%, not the deposit
    assert rep["broker"]["account_equity"] == pytest.approx(rep["strategy_equity"] + rep["foreign_positions_value"],
                                                            abs=0.01)
    row = e.db.one("SELECT * FROM daily_equity WHERE session='2026-10-27'")
    assert row["external_flow"] == 1000.0 and row["foreign_value"] is not None


def test_offsite_backup_pushes_and_verifies_pulled_copy(tmp_path):
    """INVARIANT: `backup --offsite` (copy method to a mounted remote path) pushes the backup + manifest, pulls the
    copy back and runs the restore drill on it; retention prunes only old engine backups."""
    import yaml
    cfgp = write_engine_yaml(tmp_path / "c" / "engine.yaml")
    cfg = yaml.safe_load(cfgp.read_text())
    cfg["backup"] = {"local_dir": "backups", "retain": 2, "offsite_method": "copy",
                     "offsite_target_env": "UTE_BACKUP_OFFSITE_TARGET"}
    cfgp.write_text(yaml.safe_dump(cfg, sort_keys=False))
    env = Env(tmp_path, cfg_path=cfgp)
    env.engine()
    env.decide_and_execute()
    remote = tmp_path / "remote-mount"
    run_env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "UTE_BACKUP_OFFSITE_TARGET": str(remote)}
    for _ in range(3):
        r = subprocess.run([sys.executable, "-m", "utengine", "--config", str(cfgp), "--home", str(env.home), "backup",
                            "--to", str(tmp_path / "bk"), "--offsite"], capture_output=True, text=True, cwd=ROOT,
                           env=run_env)
        assert r.returncode == 0, r.stderr
        import time
        time.sleep(1.1)
    out = json.loads(r.stdout)
    assert out["pulled_back_and_verified"]["ok"] and out["local_verify"]["ok"]
    assert len(list((tmp_path / "bk").glob("engine-*.sqlite"))) == 2
    assert len(list(remote.glob("engine-*.sqlite"))) == 3


def _audit_doc(ts, cash, positions):
    return {"generated_at": ts, "schema": "gold-miner-audit/1", "host": "test",
            "broker": {"ok": True, "data": {"available_cash": cash, "positions": positions, "pending_orders": [],
                                            "trade_history": [{"positionId": 1, "netProfit": 2.5, "fees": 0.5}],
                                            "trade_history_since": "2026-08-28"}}}


def test_gm_audit_importer_classifies_real_account_shapes(tmp_path):
    """INVARIANT (synthetic documents in the audit's exact shape): the importer is read-only and offline; the
    existing short is PRE_EXISTING; a later new Gold Miner position is EXTERNAL_POSITION (halt) with its cash use
    explained; an unexplained cash change is a discrepancy; account equity stays None (no unrealized P&L)."""
    from utengine.core.configuration import load_engine_config
    from utengine.core.reconciliation import reconcile
    from utengine.persistence.db import DB
    from utengine.tools.gm_audit import AuditReplayReader, load_audit
    short = {"position_id": "9100000001", "instrument_id": 3002, "symbol": "GDX", "is_buy": False, "units": 48.28,
             "amount": 780.0, "leverage": 5, "open_rate": 81.44, "total_fees": 0.0, "total_external_fees": 5.9}
    new = {**short, "position_id": "3616999999", "is_buy": True, "amount": 400.0, "units": 24.0}
    docs = [_audit_doc("2026-10-27T16:52:00+00:00", 520.0, [short]),
            _audit_doc("2026-10-27T18:12:00+00:00", 120.0, [short, new]),
            _audit_doc("2026-10-27T19:00:00+00:00", 130.0, [short, new])]
    paths = []
    for i, d in enumerate(docs):
        p = tmp_path / f"a{i}.txt"
        p.write_text('{"event": "broker_api_call"}\n' + json.dumps(d, indent=2), encoding="utf-8")
        paths.append(p)
    cfg = load_engine_config(write_engine_yaml(tmp_path / "c" / "engine.yaml", mode="shadow"))
    db = DB(tmp_path / "s" / "engine.sqlite")
    reader = AuditReplayReader([load_audit(p) for p in paths], {3002: "GDX"})
    r0 = reconcile(db, reader, cfg, "a", docs[0]["generated_at"])
    assert r0.clean and r0.account_equity is None and r0.strategy_equity == 520.0
    r1 = reconcile(db, reader, cfg, "a", docs[1]["generated_at"])
    assert {i["kind"] for i in r1.issues} == {"EXTERNAL_POSITION"}
    r2 = reconcile(db, reader, cfg, "a", docs[2]["generated_at"])
    assert "CASH_UNEXPLAINED" in {i["kind"] for i in r2.issues}
    own = {r["position_id"]: r["owner"] for r in db.q("SELECT * FROM ownership")}
    assert own == {"9100000001": "PRE_EXISTING", "3616999999": "EXTERNAL_UNACK"}
