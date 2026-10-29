"""Scenario 25: reboot recovery -- the engine PROCESS is killed (hard) and restarted against the same state."""
import json
import os
import subprocess
import sys
import time

from conftest import DECIDE_AT, EXECUTE_AT, IDS, ROOT, write_engine_yaml
from utengine.brokers.simulated import SimulatedBroker
from utengine.persistence.db import DB


def _start(cfg, home, now, *args):
    return subprocess.Popen([sys.executable, "-m", "utengine", "--config", str(cfg), "--home", str(home), "--now",
                             now.isoformat(), "run", *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})


def _wait_cycles(home, n, timeout=60):
    log = home / "logs" / "engine.jsonl"
    t0 = time.time()
    while time.time() - t0 < timeout:
        if log.exists() and sum('"event":"cycle"' in ln for ln in log.read_text(encoding="utf-8").splitlines()) >= n:
            return
        time.sleep(0.2)
    raise AssertionError("engine did not cycle")


def test_s25_hard_kill_and_restart(tmp_path):
    """INVARIANT: after hard kills (no shutdown hook) during the decision and execution windows, restarts
    resume from durable state: one decision, one plan, one broker submission, a valid audit chain and a clean,
    healthy engine."""
    cfg = write_engine_yaml(tmp_path / "config" / "engine.yaml")
    home = tmp_path / "home"
    SimulatedBroker(home / "sim_broker.json", instruments={v: k for k, v in IDS.items()}, cash=2000.0)
    p = _start(cfg, home, DECIDE_AT, "--service")
    _wait_cycles(home, 1)
    p.kill()                                                      # power loss
    p.wait(10)
    p = _start(cfg, home, EXECUTE_AT, "--service")
    _wait_cycles(home, 2)
    p.kill()
    p.wait(10)
    r = subprocess.run([sys.executable, "-m", "utengine", "--config", str(cfg), "--home", str(home), "--now",
                        EXECUTE_AT.replace(minute=40).isoformat(), "run", "--once"], cwd=ROOT, capture_output=True,
                       text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert r.returncode == 0, r.stderr
    db = DB(home / "engine.sqlite")
    assert db.one("SELECT COUNT(*) c FROM decisions")["c"] == 1
    assert db.one("SELECT COUNT(*) c FROM plans")["c"] == 1
    sim = json.loads((home / "sim_broker.json").read_text())
    assert len(sim["submissions"]) == 1 and len(sim["positions"]) == 1
    assert db.verify_audit_chain()[0]
    assert db.q("SELECT * FROM halts WHERE status='ACTIVE'") == []
    starts = db.one("SELECT COUNT(*) c FROM audit_log WHERE kind='ENGINE_START'")["c"]
    assert starts == 3
