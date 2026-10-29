"""Rehearsal helper (runs INSIDE the container with the release's venv python). Never used on a real host.

  sim_multi_setup.py config <release dir> <out yaml> <csv dir> <home>   simulation copy of config/engine.multi.yaml
  sim_multi_setup.py fixtures <release dir> <out dir>                   fixture bars + ONE synthetic session 2026-10-27
                                                                        (close = the 10-26 close; labelled synthetic)
  sim_multi_setup.py fault <sim_broker.json> <fault>                     queue a simulated-broker fault
  sim_multi_setup.py check <home>                                        sleeve invariants + crossing/duplicate facts
"""
import csv
import gzip
import json
import pathlib
import sqlite3
import sys

import yaml


def config(rel, out, csv_dir, home):
    cfg = yaml.safe_load((pathlib.Path(rel) / "config" / "engine.multi.yaml").read_text(encoding="utf-8"))
    cfg["engine"]["instance_id"] = "ute-multi-sim-rehearsal"
    cfg["runtime"]["mode"] = "simulation"
    cfg["runtime"]["strategies_dir"] = str(pathlib.Path(rel) / "strategies")
    cfg["broker"]["name"], cfg["broker"]["access"] = "simulated", "simulated"
    cfg["market_data"]["provider"], cfg["market_data"]["csv_dir"] = "csv", csv_dir
    cfg["reconciliation"]["order_poll_seconds"] = 0.0
    cfg["reconciliation"]["order_poll_attempts"] = 2
    pathlib.Path(out).write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def fixtures(rel, out):
    src = pathlib.Path(rel) / "tests" / "fixtures" / "snapshots" / "930795e32531bedc"
    dst = pathlib.Path(out)
    dst.mkdir(parents=True, exist_ok=True)
    for f in sorted(src.glob("*.csv.gz")):
        rows = list(csv.reader(gzip.open(f, "rt", encoding="utf-8")))
        last = rows[-1]
        assert last[0] == "2026-10-26", (f.name, last[0])
        c = last[4]
        rows.append(["2026-10-27", c, c, c, c, last[5]])          # SYNTHETIC rehearsal session (flat prices)
        with open(dst / f.name.replace(".gz", ""), "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
    (dst / "README.txt").write_text("rehearsal only: fixture bars through 2026-10-26 + one SYNTHETIC flat session "
                                    "2026-10-27 (close = 10-26 close). Not market data.\n", encoding="utf-8")


def fault(path, f):
    p = pathlib.Path(path)
    s = json.loads(p.read_text(encoding="utf-8"))
    s["faults"].append(f)
    p.write_text(json.dumps(s, sort_keys=True, indent=1), encoding="utf-8")


def force_decision(home, strategy, session, weights_json):
    """REHEARSAL ONLY (as in tests/test_multi_strategy.py): replace one persisted decision's targets so that a
    deterministic crossing can be exercised. Never used on a real host; recorded in the report."""
    import sqlite3 as _s
    con = _s.connect(str(pathlib.Path(home) / "engine.sqlite"))
    d = json.loads(con.execute("SELECT decision FROM decisions WHERE strategy_id=? AND session=?",
                               (strategy, session)).fetchone()[0])
    w = {k: float(v) for k, v in json.loads(weights_json).items()}
    d.update(target_weights=w, signal_target=dict(w), cash_weight=1.0 - sum(w.values()), rebalance=True)
    con.execute("UPDATE decisions SET decision=? WHERE strategy_id=? AND session=?", (json.dumps(d), strategy, session))
    con.commit()


def check(home):
    home = pathlib.Path(home)
    con = sqlite3.connect(f"file:{home / 'engine.sqlite'}?mode=ro", uri=True)
    sim = json.loads((home / "sim_broker.json").read_text(encoding="utf-8"))
    units = {}
    for sl, iid, u in con.execute("SELECT sleeve, instrument_id, SUM(units) FROM sleeve_ledger WHERE instrument_id IS "
                                  "NOT NULL GROUP BY sleeve, instrument_id"):
        units.setdefault(iid, {})[sl] = u
    eng = {r[0] for r in con.execute("SELECT position_id FROM ownership WHERE owner='ENGINE' AND closed_at IS NULL")}
    broker = {}                      # the engine's own positions only (foreign positions are outside the sleeves)
    for pid, p in sim["positions"].items():
        if pid in eng:
            broker[p["instrument_id"]] = broker.get(p["instrument_id"], 0.0) + p["units"]
    led_cash = con.execute("SELECT SUM(cash) FROM sleeve_ledger").fetchone()[0]
    refs = [x["client_ref"] for x in sim["submissions"] if x["client_ref"] not in ("EXTERNAL",)]
    out = {
        "sleeves": sorted({r[0] for r in con.execute("SELECT DISTINCT sleeve FROM sleeve_ledger")}),
        "units_match": all(abs(sum(v.values()) - broker.get(i, 0.0)) < 1e-6 for i, v in units.items())
        and all(i in units for i in broker),
        "shared_instruments": sorted(i for i, v in units.items() if sum(1 for x in v.values() if x > 1e-9) > 1),
        "cash_matches_broker": abs(led_cash - sim["credit"]) < 0.06,
        "crossings": con.execute("SELECT COUNT(*) FROM sleeve_ledger WHERE kind LIKE 'CROSS_%'").fetchone()[0],
        "submissions": len(refs), "duplicate_submissions": len(refs) - len(set(refs)),
        "decisions": con.execute("SELECT COUNT(*) FROM decisions WHERE status='DECIDED'").fetchone()[0],
        "portfolio_plans": con.execute("SELECT COUNT(*) FROM plans WHERE strategy_id='PORTFOLIO'").fetchone()[0],
        "alerts_external_position": con.execute("SELECT COUNT(*) FROM alerts WHERE kind='EXTERNAL_POSITION' AND "
                                                "suppressed=0").fetchone()[0],
        "exception_occurrences": dict(con.execute("SELECT kind, occurrences FROM exceptions").fetchall()),
        "unresolved_intents": con.execute("SELECT COUNT(*) FROM intents WHERE status IN ('SUBMITTING','ACKNOWLEDGED',"
                                          "'PENDING','PARTIALLY_FILLED','UNKNOWN')").fetchone()[0],
        "schema_version": con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
    }
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"config": config, "fixtures": fixtures, "fault": fault, "check": check,
     "force-decision": force_decision}[cmd](*args)
