"""Read-only forward-trading review of one completed paper session (championship, #60-SQQQ experiment and, when it
exists, the #529 API-strict experiment X529STRICT-ETORO).

    python scripts/forward_review.py --session 2026-10-29
Writes docs/forward_review/FORWARD_TRADE_REVIEW_<session>.md (and docs/OCT29_FORWARD_TRADE_REVIEW.md for
2026-10-29). Exit code 2 and NO output file when the session's closing snapshots do not exist yet -- nothing is
waited for or fabricated. The live paper databases are opened read-only and copied with SQLite's online backup API
into D:/TSR-evidence/forward_review_<session>/ (checksums recorded); every number below is computed from those
copies. NAV is recomputed independently (cash + units x stored decision close) and compared with the persisted one.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCES = {"championship": "D:/TSR-paper-data/paper.sqlite", "x60sqqq": "D:/TSR-paper-data-x60-sqqq/paper.sqlite",
           "x529strict": "D:/TSR-paper-data-x529-strict/paper.sqlite"}
OPTIONAL = {"x529strict"}            # activated 2026-10-29 (first decision: the 10-28 close)
FOCUS = ["P529-ORIG", "P529-ETORO", "X529STRICT-ETORO", "P504-ORIG", "P504-ETORO", "P60-ORIG", "P60-ETORO",
         "X60SQQQ-ETORO", "P755-ORIG", "P755-ETORO", "P785-ORIG", "P785-ETORO"]


def snapshot_sources(session: str) -> dict:
    ev = pathlib.Path(f"D:/TSR-evidence/forward_review_{session}")
    ev.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, src in SOURCES.items():
        if name in OPTIONAL and not pathlib.Path(src).exists():
            continue
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        dst = ev / f"{name}.sqlite"
        d = sqlite3.connect(str(dst))
        s.backup(d)
        d.close()
        s.close()
        out[name] = {"source": src, "copy": str(dst), "sha256": hashlib.sha256(dst.read_bytes()).hexdigest()}
    (ev / "CHECKSUMS.json").write_text(json.dumps({**out, "taken_utc": dt.datetime.now(dt.timezone.utc).isoformat()},
                                                  indent=1))
    return out


def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def review(session: str) -> str:
    src = snapshot_sources(session)
    A, B = ro(src["championship"]["copy"]), ro(src["x60sqqq"]["copy"])
    X = ro(src["x529strict"]["copy"]) if "x529strict" in src else None
    if A.execute("SELECT COUNT(*) n FROM snapshots WHERE session=?", (session,)).fetchone()["n"] < 10 or \
            B.execute("SELECT COUNT(*) n FROM snapshots WHERE session=?", (session,)).fetchone()["n"] < 1:
        print(f"session {session}: closing snapshots not available yet -- nothing written")
        sys.exit(2)
    closes = {(r["ticker"]): r["close"] for r in A.execute(
        "SELECT ticker, close FROM bars WHERE source='yahoo' AND adjusted=1 AND session=?", (session,))}
    prev_s = A.execute("SELECT MAX(session) s FROM snapshots WHERE session<?", (session,)).fetchone()["s"]
    prev_closes = {r["ticker"]: r["close"] for r in A.execute(
        "SELECT ticker, close FROM bars WHERE source='yahoo' AND adjusted=1 AND session=?", (prev_s,))}
    L = [f"# Forward paper review: session {session}", "",
         f"Read-only copies: championship `{src['championship']['sha256'][:16]}`, #60-SQQQ "
         f"`{src['x60sqqq']['sha256'][:16]}`"
         + (f", #529 API-strict `{src['x529strict']['sha256'][:16]}`" if X else "")
         + f" (D:\\TSR-evidence\\forward_review_{session}\\CHECKSUMS.json). "
         "Virtual money only; no broker.", ""]
    bench = {b: (closes[b] / prev_closes[b] - 1) for b in ("SPY", "QQQ") if b in closes and b in prev_closes}
    L += ["## Benchmarks (close to close)", "", " | ".join(f"{b} {v * 100:+.2f}%" for b, v in bench.items()), ""]
    L += ["## Closing NAV", "", "| Portfolio | NAV (persisted) | Recomputed diff | Day | Since start | Cash | "
          "Realized P&L (session fills) | Unrealized P&L | Costs (session) |", "|---|---|---|---|---|---|---|---|---|"]
    pos_by = {}
    for pid in FOCUS:
        c = B if pid.startswith("X60") else (X if pid.startswith("X529") else A)
        if c is None:
            continue
        sn = c.execute("SELECT * FROM snapshots WHERE portfolio_id=? AND session=?", (pid, session)).fetchone()
        if not sn:
            continue
        rows = json.loads(sn["positions"])
        nav_re = sn["cash"] + sum(r["units"] * closes[r["ticker"]] for r in rows)
        fills = c.execute("SELECT * FROM fills WHERE portfolio_id=? AND session=?", (pid, session)).fetchall()
        realized = sum((f["realized_pnl"] or 0) for f in fills)
        costs = sum(f["commission"] + f["spread_cost"] for f in fills)
        unreal = sum(r["unrealized"] for r in rows)
        start = c.execute("SELECT start_cash FROM portfolios WHERE portfolio_id=?", (pid,)).fetchone()["start_cash"]
        L.append(f"| {pid} | {sn['nav']:.2f} | {nav_re - sn['nav']:+.2f} | {sn['day_return'] * 100:+.2f}% | "
                 f"{(sn['nav'] / start - 1) * 100:+.2f}% | {sn['cash']:.2f} | {realized:+.2f} | {unreal:+.2f} | {costs:.2f} |")
        pos_by[pid] = ({r["ticker"]: round(r["units"], 9) for r in rows}, rows, fills, sn)
    L += ["", "## Positions at the close and the session's opening fills", ""]
    for pid, (_u, rows, fills, sn) in pos_by.items():
        L.append(f"**{pid}** (gross {sn['gross_exposure']:.2f}, beta {sn['beta_exposure']:.2f}): " + ", ".join(
            f"{r['ticker']} {r['units']:.4f} u = {r['value']:.2f} ({r['weight'] * 100:.1f}%)" for r in rows))
        if fills:
            L.append("  fills: " + "; ".join(f"{'BUY' if f['units'] > 0 else 'SELL'} {f['ticker']} {abs(f['units']):.4f} "
                                             f"@ {f['open_price']:.4f} ({f['price_source']})" for f in fills))
        L.append("")
    g = [json.loads(r["rationale"] or "{}") for r in A.execute(
        "SELECT rationale FROM decisions WHERE portfolio_id='P529-ORIG' AND session=?", (session,))]
    L += ["## #529 crash guard and #504 equivalence", ""]
    if g:
        L.append(f"- Decision after the {session} close: crash_guard_active = **{g[0].get('crash_guard_active')}**, "
                 f"QQQ 10-day return = {g[0].get('qqq_10d_return'):+.4f} (entry below -0.10, release above -0.04).")
    for tr in ("ORIG", "ETORO"):
        a, b = pos_by.get(f"P504-{tr}"), pos_by.get(f"P529-{tr}")
        if a and b:
            L.append(f"- {tr}: P529 positions {'IDENTICAL to' if a[0] == b[0] else 'DIFFER from'} P504; NAV "
                     f"{b[3]['nav']:.2f} vs {a[3]['nav']:.2f}.")
    st, el = pos_by.get("X529STRICT-ETORO"), pos_by.get("P529-ETORO")
    L += ["", "## #529 API-strict (11 API-verified instruments) vs the ELIG-2 eToro track", ""]
    if st and el:
        api = {"UVXY", "TECL", "SPXL", "SQQQ", "TLT", "BIL", "TQQQ", "SOXS", "QQQ", "PSQ", "QLD"}
        only = sorted(t for t in el[0] if t not in api)
        L.append(f"- X529STRICT-ETORO NAV {st[3]['nav']:.2f} vs P529-ETORO {el[3]['nav']:.2f} (both started at 2,000; "
                 "the strict track started at the 2026-10-28 close, so cumulative returns cover different sessions).")
        L.append(f"- Held by the ELIG-2 track but NOT allowed in the strict track: {only or 'none'}; strict cash "
                 f"{st[3]['cash']:.2f} ({st[3]['cash'] / st[3]['nav'] * 100:.1f}% of NAV)"
                 + (" -- not yet invested: this is its first decision close; its first fills are at the next open."
                    if abs(st[3]["cash"] - st[3]["nav"]) < 0.01 else "."))
    elif X is None:
        L.append("- The strict experiment's database does not exist yet.")
    else:
        L.append("- No strict-track snapshot for this session (its first decision is the 2026-10-28 close).")
    L += ["", "## Data integrity", ""]
    for name, c in (("championship", A), ("#60-SQQQ", B), ("#529 strict", X)):
        if c is None:
            continue
        ev = c.execute("SELECT source, kind, COUNT(*) n FROM data_events WHERE session=? GROUP BY source, kind",
                       (session,)).fetchall()
        missed = c.execute("SELECT COUNT(*) n FROM decisions WHERE session=? AND status='MISSED'",
                           (session,)).fetchone()["n"]
        fb = c.execute("SELECT COUNT(*) n FROM fills WHERE session=? AND LOWER(price_source) NOT LIKE '%sip%'",
                       (session,)).fetchone()["n"]
        L.append(f"- {name}: data events {[(r['source'], r['kind'], r['n']) for r in ev] or 'none'}; MISSED decisions "
                 f"{missed}; fills at a fallback (non-SIP) open {fb}.")
    bad = [(pid, round(x[3]["cash"] + sum(r["units"] * closes[r["ticker"]] for r in x[1]) - x[3]["nav"], 4))
           for pid, x in pos_by.items()]
    worst = max((abs(d) for _p, d in bad), default=0.0)
    L.append(f"- Independent NAV recomputation: largest difference {worst:.4f} USD "
             + ("(all within 0.01)." if worst <= 0.01 else f"-- CHECK: {[b for b in bad if abs(b[1]) > 0.01]}"))
    n = B.execute("SELECT COUNT(*) n FROM substitutions").fetchone()["n"]
    ns = B.execute("SELECT COUNT(*) n FROM substitutions WHERE session=?", (session,)).fetchone()["n"]
    L += ["", "## #60-SQQQ substitution", "", f"- Substitutions recorded in total: **{n}** ({ns} on {session}). "
          + ("No forward evidence for the TECS -> SQQQ rule yet." if n == 0 else "See the substitutions table."), ""]
    dec = [*A.execute("SELECT portfolio_id, status, targets, skipped FROM decisions WHERE session=? ORDER BY "
                      "portfolio_id", (session,)).fetchall(),
           *B.execute("SELECT portfolio_id, status, targets, skipped FROM decisions WHERE session=?", (session,)).fetchall(),
           *(X.execute("SELECT portfolio_id, status, targets, skipped FROM decisions WHERE session=?",
                       (session,)).fetchall() if X else [])]
    L += ["## Decisions taken after this close (orders for the next open)", "",
          "| Portfolio | Status | Target units | Skipped |", "|---|---|---|---|"]
    for d in dec:
        if d["portfolio_id"] in FOCUS:
            tg = json.loads(d["targets"] or "{}")
            L.append(f"| {d['portfolio_id']} | {d['status']} | " + ", ".join(f"{k} {v:.4f}" for k, v in tg.items())
                     + f" | {d['skipped'] or ''} |")
    L += ["", "**Caveat:** one session is not evidence of an edge. These paper numbers are not live-execution "
          "evidence and do not authorize anything.", ""]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    a = ap.parse_args()
    md = review(a.session)
    out = ROOT / "docs" / "forward_review" / f"FORWARD_TRADE_REVIEW_{a.session}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    if a.session == "2026-10-29":
        (ROOT / "docs" / "OCT29_FORWARD_TRADE_REVIEW.md").write_text(md, encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
