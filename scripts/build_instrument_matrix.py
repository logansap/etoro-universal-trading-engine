"""Build the #529 / #60 eToro instrument-eligibility matrix from RECORDED, READ-ONLY evidence.

    python scripts/build_instrument_matrix.py
Outputs docs/data/etoro_instrument_matrix_2026-10-29.json (machine-readable). No broker call is made: on
2026-10-28/29 no authorized live eToro read path existed for this engine (no read-only key; the eToro connector is
invalidated; the Gold Miner audit command returns positions/cash/history only). Evidence used, all read-only:
  A. Gold Miner account instrument search, REAL account, 2026-08-26 14:26-14:32Z
     (D:/AI-Gold-Miner-Trading-Engine/reports/execution_coverage.json `live_tradability`): instrumentId,
     isBuyEnabled, isCurrentlyTradable, isDelisted, bid/ask, rate_date; "no non-crypto exact/US match" = absent.
  B. Gold Miner eligibility call, REAL account, 2026-08-27 (POST /api/v2/trading/info/eligibility), documented in
     src/gold_miner_engine/execution/settlement_types.py and risk/fundability.py for all 26 of its instruments:
     long x1 -> real only if offered (no ETF offers it) else cfd; long x2/x5 cfd; short x1/x2/x5 cfd;
     minPositionExposure = 10.0 on every one. The raw per-instrument payload was retained only for GDX.
  C. Owner observation of the logged-in account UI, 2026-10-23 (paper_trading/policy.py ELIG-2): trade page shown
     for SOXL, SVXY, LABU (LABU.US, CFD label), BSV (CFD label); SVIX and UVIX unavailable. NOT an API check.
  D. Financing: x1 long overnight fee observed 0 on SOXS, SQQQ, QQQ in this account; 10.05%/yr observed only on
     a leveraged x5 long GDX CFD (paper_trading/policy.py).
  E. Market context only (NOT eToro): IEX top-of-book quotes from the paper championship DB snapshot.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[1]
GM = pathlib.Path("D:/AI-Gold-Miner-Trading-Engine")
PAPER = pathlib.Path("D:/TSR-evidence/paper_audit_2026-10-28/championship_snapshot.sqlite")
UNIVERSE_529 = ["UVXY", "TECL", "SOXL", "SPXL", "LABU", "SVIX", "SVXY", "SQQQ", "TLT", "BIL", "BTAL", "TQQQ",
                "TECS", "SOXS", "QQQ", "PSQ", "QLD", "BSV", "UVIX"]
TARGETS_60 = ["TQQQ", "UVXY", "TECL", "SPXL", "SQQQ", "BSV"]           # after TECS -> SQQQ
OWNER = {"SOXL": "trade page shown", "SVXY": "trade page shown", "LABU": "trade page shown (LABU.US, CFD label)",
         "BSV": "trade page shown (CFD label)", "SVIX": "unavailable", "UVIX": "unavailable"}
FIN_X1_ZERO_OBSERVED = {"SOXS", "SQQQ", "QQQ"}


def main():
    cov = json.loads((GM / "reports/execution_coverage.json").read_text(encoding="utf-8"))
    search, search_at = cov["live_tradability"], cov["generated_at_utc"]
    gm_scope = set(json.loads((GM / "docs/leaf_tradability_raw.json").read_text(encoding="utf-8")))
    con = sqlite3.connect(f"file:{PAPER}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = []
    for t in sorted(set(UNIVERSE_529) | set(TARGETS_60)):
        s = search.get(t)
        r = {"symbol": t, "used_by": [x for x, u in (("529", UNIVERSE_529), ("60-SQQQ", TARGETS_60)) if t in u]}
        if isinstance(s, dict) and s.get("instrumentId"):
            m = (s["bid"] + s["ask"]) / 2
            r.update(instrument_id=s["instrumentId"],
                     api_status="VERIFIED (account search 2026-08-26; eligibility pattern 2026-08-27; >60 days old)",
                     available=bool(s["isBuyEnabled"] and s["isCurrentlyTradable"] and not s["isDelisted"]),
                     long=True, short="CFD x1/x2/x5 (eligibility pattern, Gold Miner 2026-08-27)",
                     execution="CFD (no ETF offered `real` settlement on this EEA account)",
                     leverage_long="x1 (cfd), x2/x5 (cfd)", margin="amount = exposure / leverage (x1: 100%)",
                     min_order_usd=10.0, etoro_spread_bp=round((s["ask"] - s["bid"]) / m * 1e4, 2),
                     etoro_quote_at=s["rate_date"], etoro_quote_age_s=round((dt.datetime.fromisoformat(search_at)
                                                                             - dt.datetime.fromisoformat(s["rate_date"][:26] + "+00:00")).total_seconds(), 1),
                     financing=("x1 long overnight fee observed 0 in this account" if t in FIN_X1_ZERO_OBSERVED else
                                "x1 long: 0 per eToro's published CFD fee page; not observed on this instrument"),
                     evidence=["A: account search " + s["rate_date"], "B: eligibility pattern 2026-08-27 (Gold Miner)"],
                     verified_at=search_at)
        elif isinstance(s, dict) and s.get("error"):
            r.update(instrument_id=None, api_status="UNAVAILABLE (account search: no match, 2026-08-26)",
                     available=False, long=False, short=None, execution=None, leverage_long=None, margin=None,
                     min_order_usd=None, etoro_spread_bp=None, etoro_quote_at=None, etoro_quote_age_s=None,
                     financing=None, evidence=[f"A: {s['error']}"], verified_at=search_at)
        else:
            r.update(instrument_id=None, api_status="UNVERIFIED (no API evidence for this account)",
                     available=None, long=None, short=None, execution=None, leverage_long=None, margin=None,
                     min_order_usd=None, etoro_spread_bp=None, etoro_quote_at=None, etoro_quote_age_s=None,
                     financing=None, evidence=["C: owner UI 2026-10-23: " + OWNER.get(t, "not observed")],
                     verified_at=None)
        r["owner_ui_2026_10_23"] = OWNER.get(t)
        r["in_gold_miner_eligibility_scope"] = t in gm_scope or bool(isinstance(s, dict) and s.get("instrumentId"))
        q = con.execute("SELECT spread_bp FROM quotes WHERE ticker=? AND quality='OK' AND collected_at>=? AND "
                        "collected_at<=?", (t, "2026-10-27T13:45:00", "2026-10-28T19:05:00")).fetchall()
        last = con.execute("SELECT collected_at FROM quotes WHERE ticker=? AND quality='OK' ORDER BY id DESC LIMIT 1",
                           (t,)).fetchone()
        r["iex_context"] = {"median_spread_bp_regular_hours": round(statistics.median(x["spread_bp"] for x in q), 2)
                            if q else None, "samples": len(q), "latest_ok_quote": last["collected_at"] if last else None,
                            "note": "IEX single-venue quote via Alpaca: market context, NOT an eToro quote"}
        r["engine_yaml_status"] = None
        rows.append(r)
    import yaml
    y529 = yaml.safe_load((ROOT / "strategies/529.yaml").read_text(encoding="utf-8"))["instruments"]
    y60 = yaml.safe_load((ROOT / "strategies/60.yaml").read_text(encoding="utf-8"))["instruments"]
    for r in rows:
        i = y529.get(r["symbol"]) or y60.get(r["symbol"])
        if i:
            r["engine_yaml_status"] = {"etoro_instrument_id": i["etoro_instrument_id"], "eligibility": i["eligibility"]}
            if r["instrument_id"] and i["etoro_instrument_id"] != r["instrument_id"]:
                raise SystemExit(f"instrument id mismatch {r['symbol']}: yaml {i['etoro_instrument_id']} vs evidence {r['instrument_id']}")
    out = {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "account": "eToro REAL (EEA retail)",
           "live_api_available_to_engine": False,
           "why": "no read-only key provisioned; eToro MCP connector invalidated; GM audit command has no eligibility",
           "summary": {s: sorted(r["symbol"] for r in rows if r["api_status"].startswith(s))
                       for s in ("VERIFIED", "UNAVAILABLE", "UNVERIFIED")},
           "instruments": rows}
    p = ROOT / "docs/data/etoro_instrument_matrix_2026-10-29.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out["summary"], indent=1))


if __name__ == "__main__":
    main()
