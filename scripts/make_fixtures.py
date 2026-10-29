"""Build the parity fixtures in tests/fixtures/ from READ-ONLY sources (run once; outputs are committed).

  python scripts/make_fixtures.py

Sources (never modified):
  D:/TSR-paper-data/snapshots/*.parquet            Yahoo adjusted daily bars used by the championship decisions
  D:/TSR-paper-data-x60-sqqq/snapshots/*.parquet   ... used by the #60-SQQQ experiment decisions
  D:/TSR-paper-data/paper.sqlite, D:/TSR-paper-data-x60-sqqq/paper.sqlite   recorded decisions (opened mode=ro)
  D:/TSR-evidence/strategy_60/tables/LONG_100000/orders.csv   native LEAN #60 orders -> ONLY (session, symbol,
      side) is kept: no prices or quantities leave the evidence store (QuantConnect data stays quarantined).
The bar fixtures are Yahoo-derived and are private test data; do not redistribute.
"""
from __future__ import annotations

import gzip
import json
import pathlib
import sqlite3

import pandas as pd

OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"
S60 = ["SPY", "QQQ", "TQQQ", "UVXY", "TECL", "SPXL", "SQQQ", "TECS", "BSV"]
SNAPS = {
    "92a8f8be7497b1e9": ("D:/TSR-paper-data/snapshots", "all"),      # championship 2026-10-22
    "a5c93f2b9a4e0b7c": ("D:/TSR-paper-data/snapshots", "all"),      # championship 2026-10-23
    "930795e32531bedc": ("D:/TSR-paper-data/snapshots", "all"),      # championship 2026-10-26
    "53f150d71cc1e074": ("D:/TSR-paper-data-x60-sqqq/snapshots", "60"),  # experiment 2026-10-23
    "45fe197a87f7adb2": ("D:/TSR-paper-data-x60-sqqq/snapshots", "60"),  # experiment 2026-10-26
}


def write_snap(sha, src, which):
    df = pd.read_parquet(f"{src}/{sha}.parquet")
    d = OUT / "snapshots" / sha
    d.mkdir(parents=True, exist_ok=True)
    for t, g in df.groupby("ticker"):
        if which == "60" and t not in S60:
            continue
        g = g.sort_values("date")
        rows = ["date,open,high,low,close,volume"] + [
            f"{pd.Timestamp(r.date).date()},{r.open!r},{r.high!r},{r.low!r},{r.close!r},{r.volume!r}"
            for r in g.itertuples()]
        with gzip.open(d / f"{t}.csv.gz", "wt", encoding="utf-8", newline="") as f:
            f.write("\n".join(rows) + "\n")


def decisions():
    out = []
    for db, pids in (("D:/TSR-paper-data/paper.sqlite", ("P60-ORIG", "P60-ETORO", "P529-ORIG")),
                     ("D:/TSR-paper-data-x60-sqqq/paper.sqlite", ("X60SQQQ-ETORO",))):
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        q = (f"SELECT portfolio_id, session, status, data_snapshot, nav_at_close, rationale, targets FROM decisions "
             f"WHERE portfolio_id IN ({','.join('?' * len(pids))}) ORDER BY session, portfolio_id")
        for pid, s, st, snap, nav, why, tg in c.execute(q, pids):
            out.append({"portfolio_id": pid, "session": s, "status": st, "snapshot": json.loads(snap)["sha16"],
                        "nav_at_close": nav, "rationale": json.loads(why), "targets": json.loads(tg)})
        c.close()
    (OUT / "recorded_decisions.json").write_text(json.dumps(out, indent=1, sort_keys=True))


def lean_targets():
    o = pd.read_csv("D:/TSR-evidence/strategy_60/tables/LONG_100000/orders.csv")
    o = o[(o.quantity > 0) & (o.tag.fillna("") != "Liquidated")]
    seq = o.groupby("signal_session").symbol.agg(lambda s: "|".join(sorted(set(s))))
    seq.rename("bought").to_csv(OUT / "lean_native_60_buys.csv")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for sha, (src, which) in SNAPS.items():
        write_snap(sha, src, which)
    decisions()
    lean_targets()
    print("fixtures written to", OUT)
