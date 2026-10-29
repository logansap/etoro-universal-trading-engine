"""SIMULATION demonstration of the multi-strategy engine and the takeover workflow (no real broker, no network).

    python scripts/multi_strategy_demo.py  ->  docs/data/multi_strategy_demo.json (numbers quoted in
                                               docs/MULTI_STRATEGY_TEST_EVIDENCE.md)

Ten demonstrations, each on a fresh simulated account built from the test fixtures (USD 2,000; prices = the
2026-10-26 fixture closes; decision after the 10-26 close, execution at the 10-27 / 10-28 opens).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from conftest import EXECUTE_AT, IDS, extended_store, multi_env  # noqa: E402
from test_multi_strategy import D2, day2, force_decision  # noqa: E402,F401
from test_takeover import GM  # noqa: E402
from utengine.brokers.simulated import SimulatedBroker, SimulatedCrash  # noqa: E402
from utengine.core import accounting, halts, sleeves, takeover as T  # noqa: E402
from utengine.core.reconciliation import engine_position_ids  # noqa: E402

A, B = "QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"
OUT = {}


def tmp():
    return pathlib.Path(tempfile.mkdtemp(prefix="ute-demo-"))


def sv(e):
    r = e.reconcile("demo", e.now())
    v = e.sleeve_valuation(r)
    return r, {s: {"cash": round(x["cash"], 2), "equity": round(x["equity"], 2),
                   "holdings": {e.id_to_symbol.get(i, i): {"units": round(h["units"], 6), "value": round(h["value"], 2)}
                                for i, h in x["holdings"].items()}} for s, x in v["sleeves"].items()}


def demo_main():
    env = multi_env(tmp(), bar_store=extended_store({D2: 1.0}))
    e = env.engine()
    d, _x = env.decide_and_execute()
    r, s1 = sv(e)
    broker = {}
    for p in env.broker.s["positions"].values():
        k = e.id_to_symbol[p["instrument_id"]]
        broker[k] = round(broker.get(k, 0.0) + p["units"], 6)
    tq_orders = sum(1 for o in env.broker.s["orders"].values() if o["instrument_id"] == IDS["TQQQ"])
    OUT["1_two_strategies"] = {"decisions": {k: v["status"] for k, v in d["decisions"]["2026-10-26"].items()},
                               "plans": e.db.one("SELECT COUNT(*) c FROM plans")["c"], "sleeves_after_day1": s1}
    OUT["2_shared_tqqq"] = {"broker_tqqq_units": broker["TQQQ"], "broker_tqqq_orders": tq_orders,
                            "sleeve_tqqq_units": {s: s1[s]["holdings"]["TQQQ"]["units"] for s in (A, B)},
                            "sum_equals_broker": abs(sum(s1[s]["holdings"]["TQQQ"]["units"] for s in (A, B))
                                                     - broker["TQQQ"]) < 1e-6}
    OUT["5_combined_exposure"] = {"broker_units": broker, "account_strategy_equity": round(r.strategy_equity, 2),
                                  "sum_sleeve_equity": round(sum(x["equity"] for x in s1.values()), 2)}
    env.broker.deposit(1000.0, evidence="bank transfer ref DEMO-1")
    env.clock.advance(minutes=1)
    _r, s2 = sv(e)
    OUT["3_verified_deposit"] = {"deposit": 1000.0,
                                 "cash_change": {s: round(s2[s]["cash"] - s1[s]["cash"], 2) for s in (A, B)},
                                 "flow": dict(e.db.one("SELECT kind, amount, source, ref FROM cash_flows WHERE "
                                                       "kind='DEPOSIT'"))}
    bid, ask = env.broker.s["prices"][str(IDS["TQQQ"])]
    env.broker.set_price(IDS["TQQQ"], bid * 1.05, ask * 1.05)
    _r, s3 = sv(e)
    OUT["4_independent_pnl"] = {"tqqq_move": "+5%", "equity_change": {s: round(s3[s]["equity"] - s2[s]["equity"], 2)
                                                                       for s in (A, B)}}
    env.broker.set_price(IDS["TQQQ"], bid, ask)
    subs0 = len(env.broker.s["submissions"])
    out = day2(env, forced={A: {}})
    plan = next(v for k, v in out["execution"][D2.isoformat()].items() if k.startswith("P"))
    _r, s4 = sv(e)
    legs = [dict(x) for x in e.db.q("SELECT action, symbol, units, status FROM intents WHERE created_at>=? ORDER BY "
                                    "leg_index", ((EXECUTE_AT + dt.timedelta(hours=20)).isoformat(),))]
    OUT["6_safe_rebalance"] = {"forced": "#529 -> 100% cash; #60 decides normally (holds TQQQ)",
                               "plan_status": plan["status"], "broker_legs": legs,
                               "orders_sent": len(env.broker.s["submissions"]) - subs0,
                               "529_after": s4[A], "60_tqqq_units_before_after": [s2[B]["holdings"]["TQQQ"]["units"],
                                                                                 s4[B]["holdings"]["TQQQ"]["units"]]}
    r, _ = sv(e)
    OUT["invariants_clean"] = not sleeves.check_invariants(e.db, r.snapshot, engine_position_ids(e.db))
    OUT["audit_chain"] = e.db.verify_audit_chain()[1]


def demo_restart():
    env = multi_env(tmp())
    e = env.engine()
    from conftest import DECIDE_AT
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.broker.inject("crash_after_submit")
    env.clock.set(EXECUTE_AT)
    try:
        e.cycle()
    except SimulatedCrash:
        pass
    sent_at_crash = len(env.broker.s["submissions"])
    e2 = env.engine()
    for _ in range(3):
        env.clock.advance(minutes=1)
        e2.cycle()
    refs = [s["client_ref"] for s in env.broker.s["submissions"]]
    r, _s = sv(e2)
    OUT["7_restart_no_double_orders"] = {"submissions_at_crash": sent_at_crash, "submissions_after": len(refs),
                                         "unique_client_refs": len(set(refs)) == len(refs), "reconciliation": r.status,
                                         "plans": e2.db.one("SELECT COUNT(*) c FROM plans")["c"]}


def demo_unknown_close():
    env = multi_env(tmp(), bar_store=extended_store({D2: 1.0}))
    e = env.engine()
    env.decide_and_execute()
    SimulatedBroker.closed_position_proceeds = lambda self, pid: None        # as on eToro: no close lookup
    env.broker.inject("timeout_after_accept")
    out = day2(env, forced={A: {}, B: {"SQQQ": 1.0}})
    plan = next(v for k, v in out["execution"][D2.isoformat()].items() if k.startswith("P"))
    subs = len(env.broker.s["submissions"])
    env.clock.advance(minutes=5)
    e.cycle()
    OUT["8_unknown_close_outcome"] = {"plan_status": plan["status"],
                                      "halts": sorted({h["kind"] for h in halts.active(e.db)}),
                                      "opens_started": e.db.one("SELECT COUNT(*) c FROM intents WHERE phase=2 AND "
                                                                "status NOT IN ('INTENT_PERSISTED','BLOCKED',"
                                                                "'ABANDONED') AND created_at>=?",
                                                                ((EXECUTE_AT + dt.timedelta(hours=20)).isoformat(),))["c"],
                                      "orders_sent_in_next_cycle": len(env.broker.s["submissions"]) - subs,
                                      "open_exceptions": [(x["kind"], x["occurrences"]) for x in
                                                          halts.open_exceptions(e.db)]}


def demo_takeover():
    env = multi_env(tmp(), takeover=GM, clock_at=EXECUTE_AT)
    gdx = env.broker.external_open_position(IDS["GDX"], 740.0, is_buy=False, leverage=5, open_rate=81.43)
    e = env.engine()
    h0 = hashlib.sha256(env.broker_path.read_bytes()).hexdigest()
    pv = T.preview(e, env.clock())
    h1 = hashlib.sha256(env.broker_path.read_bytes()).hexdigest()
    g = next(r for r in pv["positions"] if r["position_id"] == gdx)
    OUT["9_takeover_preview"] = {"broker_state_unchanged": h0 == h1, "broker_mutations": pv["broker_mutations"],
                                 "gdx": {k: g[k] for k in ("direction", "leverage", "units", "exposure_usd", "owner",
                                                           "est_close_proceeds", "proceeds_basis", "est_realized_pnl",
                                                           "financing", "action")},
                                 "capital": pv["capital"], "allocation": pv["allocation"],
                                 "aggregate_targets": pv["proposed_holdings"]["aggregate_targets_usd"]}
    attempts = []
    for _ in range(2):
        try:
            T.advance(e, pv["run_id"], env.clock(), "owner")
        except T.TakeoverError as exc:
            attempts.append(str(exc)[:120])
    OUT["10_takeover_stops_at_approval"] = {"state": T.run(e.db, pv["run_id"])["state"], "advance_refusals": attempts,
                                            "broker_state_unchanged":
                                                hashlib.sha256(env.broker_path.read_bytes()).hexdigest() == h0}
    (ROOT / "docs" / "takeover").mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / "takeover" / "SIMULATED_TAKEOVER_PREVIEW.md").write_text(T.render(pv), encoding="utf-8")


def main():
    demo_main()
    demo_restart()
    demo_unknown_close()
    demo_takeover()
    out = ROOT / "docs" / "data" / "multi_strategy_demo.json"
    out.write_text(json.dumps(OUT, indent=1, default=str), encoding="utf-8")
    print(json.dumps(OUT, indent=1, default=str))


if __name__ == "__main__":
    main()
