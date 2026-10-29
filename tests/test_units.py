"""Unit-level invariants: state machine, audit log, planner, configuration helpers, backups."""
import datetime as dt
import os
import sqlite3
import subprocess
import sys

import pytest

from conftest import ROOT, Env
from utengine.brokers.base import Position, Quote
from utengine.core.errors import IllegalTransition
from utengine.core.order_planner import plan
from utengine.core.order_state import TRANSITIONS, transition
from utengine.core.portfolio import Allocation
from utengine.persistence.db import DB


def test_illegal_transitions_raise(tmp_path):
    """INVARIANT: the state machine refuses transitions outside its table (e.g. FILLED -> SUBMITTING)."""
    db = DB(tmp_path / "x.sqlite")
    db.x("INSERT INTO intents(intent_id,plan_id,leg_index,phase,action,symbol,instrument_id,status,mode,created_at,"
         "updated_at) VALUES('I1','P',0,2,'OPEN','TQQQ',1,'INTENT_PERSISTED','simulation','t','t')")
    transition(db, "I1", "SUBMITTING", "t")
    with pytest.raises(IllegalTransition):
        transition(db, "I1", "INTENT_PERSISTED", "t")
    transition(db, "I1", "UNKNOWN", "t")
    with pytest.raises(IllegalTransition):
        transition(db, "I1", "SUBMITTING", "t")                     # an UNKNOWN intent is never resubmitted
    assert "SUBMITTING" not in TRANSITIONS["UNKNOWN"]


def test_audit_log_is_append_only_and_chained(tmp_path):
    """INVARIANT: audit rows cannot be updated or deleted; tampering with the stored payload breaks the chain."""
    db = DB(tmp_path / "x.sqlite")
    db.audit("A", {"x": 1})
    db.audit("B", {"x": 2})
    assert db.verify_audit_chain()[0]
    for sql in ("UPDATE audit_log SET payload='{}'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.IntegrityError):
            db.con.execute(sql)
    db.con.execute("DROP TRIGGER audit_no_update")                   # an attacker with file access
    db.con.execute("UPDATE audit_log SET payload='{\"x\":9}' WHERE seq=1")
    assert db.verify_audit_chain()[0] is False


def _pos(pid, value, units):
    return Position(pid, 1, "TQQQ", True, units, value, 1, value / units, "CFD", 0.0, 0.0, None)


def test_planner_sell_then_buy_and_partial_close():
    """INVARIANT: closes are phase 1 and opens phase 2; an over-weight is reduced with full closes (largest first)
    and one partial close; changes below the minimum order are not traded."""
    alloc = Allocation(1000.0, {"TQQQ": 300.0, "SQQQ": 500.0}, {"TQQQ": 0.3, "SQQQ": 0.5}, {"TQQQ": 1, "SQQQ": 2})
    pos = [_pos("a", 600.0, 10.0), _pos("b", 200.0, 4.0)]
    q = {1: Quote(1, 60.0, 60.02, "t"), 2: Quote(2, 20.0, 20.01, "t")}
    legs, notes = plan(alloc, pos, q, {"min_order_usd": 10, "min_order_pct_nav": 0.001}, 10)
    assert [(l.phase, l.action, l.symbol) for l in legs] == [(1, "PARTIAL_CLOSE", "TQQQ"), (2, "OPEN", "SQQQ")]
    assert legs[0].position_id == "a" and abs(legs[0].units * 60.0 - 500.0) < 0.1
    tiny = Allocation(1000.0, {"TQQQ": 805.0}, {"TQQQ": 0.805}, {"TQQQ": 1})
    legs, notes = plan(tiny, pos, q, {"min_order_usd": 10, "min_order_pct_nav": 0.001}, 10)
    assert legs == [] and "below minimum order" in notes[0]


def test_planner_liquidates_non_targets_only_engine_positions():
    """INVARIANT: only engine-owned positions are passed to the planner, and every non-targeted one is closed."""
    alloc = Allocation(1000.0, {}, {}, {})
    legs, _ = plan(alloc, [_pos("a", 600.0, 10.0)], {}, {"min_order_usd": 10, "min_order_pct_nav": 0.0}, 10)
    assert [(l.action, l.position_id, l.units) for l in legs] == [("CLOSE", "a", None)]


def test_backup_and_restore_verification(tmp_path):
    """INVARIANT: an online backup restores to a temporary location with a valid audit chain and integrity."""
    env = Env(tmp_path)
    env.engine()
    env.decide_and_execute()
    cfg, home = str(env.cfg_path), str(env.home)
    r = subprocess.run([sys.executable, "-m", "utengine", "--config", cfg, "--home", home, "backup", "--to",
                        str(tmp_path / "bk")], capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert r.returncode == 0, r.stderr
    b = next((tmp_path / "bk").glob("engine-*.sqlite"))
    r = subprocess.run([sys.executable, "-m", "utengine", "--config", cfg, "--home", home, "restore-verify",
                        "--backup", str(b)], capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert r.returncode == 0 and '"integrity": "ok"' in r.stdout and "entries verified" in r.stdout, r.stdout


def test_calendar_timestamps_are_utc_and_exchange_aware():
    """INVARIANT: stored timestamps are UTC ISO strings; exchange times are derived with zoneinfo."""
    from utengine.core.calendar import Calendar
    t = Calendar().open_utc(dt.date(2026, 10, 27))
    assert t.tzinfo == dt.timezone.utc and t.hour == 13


def test_module_hash_is_line_ending_independent(tmp_path):
    """INVARIANT: the module source hash recorded at activation is the same for CRLF and LF checkouts."""
    from utengine.strategy_modules import _sha
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_bytes(b"x = 1\r\ny = 2\r\n")
    b.write_bytes(b"x = 1\ny = 2\n")
    assert _sha(a) == _sha(b)
