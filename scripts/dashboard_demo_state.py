"""Build a SIMULATED 50/50 engine state for the dashboard demonstration (no broker, no network).

    python scripts/dashboard_demo_state.py <out dir>
Writes <out dir>/config/engine.yaml (simulation, 50/50, capital, takeover) and <out dir>/home (engine state):
  session 1 decision + execution (shared TQQQ), a broker-evidenced deposit, a deposit-sized credit WITHOUT evidence
  (held in suspense), a TQQQ +3% move, session 2 with #529 reduced to cash (partial close + internal crossing), a
  foreign Gold Miner-like GDX short x5, a takeover preview stopped at approval, and Gold Miner retirement evidence
  produced by deployment/gm_retirement/gm_retire.sh in the container rehearsal (if present).
Everything is labelled SIMULATION by the dashboard.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from conftest import FULL_CAPITAL, IDS, Env, extended_store, write_engine_yaml, FIFTY_FIFTY  # noqa: E402
from test_multi_strategy import D2, day2  # noqa: E402
from test_takeover import GM  # noqa: E402
from utengine.core import takeover as T  # noqa: E402


def main(out: str):
    out = pathlib.Path(out)
    cfg = write_engine_yaml(out / "config" / "engine.yaml", portfolio=FIFTY_FIFTY, takeover=GM, capital=FULL_CAPITAL)
    env = Env(out, cfg_path=cfg, bar_store=extended_store({D2: 1.0}), broker_kwargs={"cash_tx_capability": False})
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(600.0, record=False)
    env.broker.add_funding_evidence(600.0, "TR-DEMO-1")
    env.clock.advance(minutes=2)
    e.reconcile("demo", env.clock())
    env.broker.deposit(125.0, record=False)                     # no evidence -> suspense
    env.clock.advance(minutes=2)
    e.reconcile("demo", env.clock())
    bid, ask = env.broker.s["prices"][str(IDS["TQQQ"])]
    env.broker.set_price(IDS["TQQQ"], bid * 1.03, ask * 1.03)
    day2(env, forced={"QC_529_ETORO_RESTRICTED": {"TQQQ": 0.2}})
    env.broker.external_open_position(IDS["GDX"], 300.0, is_buy=False, leverage=5, open_rate=81.43)
    env.clock.advance(minutes=3)
    r = e.reconcile("demo", env.clock())
    from utengine.core import halts
    from utengine.core.reconciliation import acknowledge_external
    pid = [k for k, p in env.broker.s["positions"].items() if p["instrument_id"] == IDS["GDX"]][0]
    acknowledge_external(e.db, pid, "owner", "Gold Miner's GDX short (shared account)", e.nows())
    for h in halts.active(e.db):
        halts.clear(e.db, h["halt_id"], "owner", "foreign position acknowledged", e.nows())
    env.clock.advance(minutes=1)
    e.reconcile("demo", env.clock())
    T.preview(e, env.clock())
    ev = pathlib.Path("D:/TSR-evidence/multi_rehearsal_2026-10-29/multi_rehearsal_artifacts/gm_retirement")
    if ev.exists():
        from utengine.core import retirement
        for f in sorted(ev.glob("*.json")):
            try:
                retirement.record(e.db, f, "rehearsal", e.nows())
            except Exception:  # noqa: BLE001 -- demo only
                pass
    print(json.dumps({"config": str(cfg), "home": str(env.home), "now": env.clock().isoformat(),
                      "reconciliation": r.status}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
