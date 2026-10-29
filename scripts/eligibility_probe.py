"""Read-only eToro instrument eligibility + quote snapshot for the #529 / #60 universes, with the engine's READ-ONLY
key loaded from a protected env file (never printed, never in argv).

    python scripts/eligibility_probe.py <credentials.env> <out.json>
    python scripts/eligibility_probe.py --matrix <snapshot.json> <matrix.json>   (no broker call)

Every request goes through EtoroReadOnly (route allowlist + read-only scope gate): the exact-ticker instrument search,
POST /api/v2/trading/info/eligibility (a read despite the verb) and the rates endpoint. No account data is read or
stored. The output holds instrument facts only; the script asserts no credential value appears in it.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from run_with_credentials import load  # noqa: E402
from utengine.brokers.etoro import EtoroReadOnly  # noqa: E402
from utengine.core.configuration import load_engine_config  # noqa: E402

UNIVERSE_529 = ["UVXY", "TECL", "SOXL", "SPXL", "LABU", "SVIX", "SVXY", "SQQQ", "TLT", "BIL", "BTAL", "TQQQ",
                "TECS", "SOXS", "QQQ", "PSQ", "QLD", "BSV", "UVIX"]
TARGETS_60 = ["TQQQ", "UVXY", "TECL", "SPXL", "SQQQ", "BSV"]
ALIASES = {"LABU": ["LABU", "LABU.US"]}          # eToro lists LABU as LABU.US (owner UI 2026-10-23; API 2026-10-29)
SEARCH_FIELDS = ("internalInstrumentId", "internalSymbolFull", "internalInstrumentDisplayName",
                 "internalAssetClassName", "internalExchangeName", "isDelisted", "isCurrentlyTradable",
                 "isExchangeOpen", "isHiddenFromClient")


def utc():
    return dt.datetime.now(dt.timezone.utc)


def matrix(snap: dict, max_quote_age_s: float) -> dict:
    """The eligibility matrix from one snapshot. API status: VERIFIED (found, tradable, not delisted, open allowed),
    UNAVAILABLE (exact-ticker search found nothing) or NOT_TRADABLE. A quote older than the engine's freshness limit
    is reported, never used as a current price."""
    at = dt.datetime.fromisoformat(snap["quotes_received_at"])
    rows = []
    for sym, r in sorted(snap["instruments"].items()):
        row = {"symbol": sym, "used_by": r["used_by"]}
        if "instrument_id" not in r:
            rows.append({**row, "api_status": "UNAVAILABLE", "reason": "exact-ticker search: no match"})
            rows[-1]["searched_at"] = r["search_at"]
            continue
        h, e, q = r["search"][0], r["eligibility"] or {}, r["quote"] or {}
        ok = h["isCurrentlyTradable"] and not h["isDelisted"] and e.get("allowOpenPosition")
        lc = e.get("leverage_configs") or []
        age = None
        if q.get("date"):
            age = round((at - dt.datetime.fromisoformat(q["date"][:26].rstrip("Z") + "+00:00")).total_seconds())
        mid = (q["bid"] + q["ask"]) / 2 if q.get("bid") else None
        rows.append({**row, "instrument_id": r["instrument_id"], "etoro_symbol": h["internalSymbolFull"],
                     "api_status": "VERIFIED" if ok else "NOT_TRADABLE",
                     "settlement": sorted({c["settlement"] for c in lc}),
                     "long_leverage": sorted({v for c in lc if c["direction"] == "long" for v in c["leverage"]}),
                     "short_leverage": sorted({v for c in lc if c["direction"] == "short" for v in c["leverage"]}),
                     "min_position_usd": e.get("minPositionExposure"), "units": e.get("unitsQuantityType"),
                     "requires_w8ben": e.get("requiresW8Ben"), "bid": q.get("bid"), "ask": q.get("ask"),
                     "spread_bp": None if not mid else round((q["ask"] - q["bid"]) / mid * 1e4, 2),
                     "quote_at": q.get("date"), "quote_age_s": age,
                     "quote_fresh": age is not None and age <= max_quote_age_s})
    return {"generated_from": snap["generated_at"], "key_check": snap["key_check"]["status"],
            "max_quote_age_s": max_quote_age_s, "rows": rows,
            "summary": {k: [x["symbol"] for x in rows if x["api_status"] == k]
                        for k in ("VERIFIED", "UNAVAILABLE", "NOT_TRADABLE")} |
                       {"STALE_QUOTE": [x["symbol"] for x in rows if x.get("api_status") == "VERIFIED"
                                        and not x["quote_fresh"]]}}


def main(argv):
    if len(argv) == 3 and argv[0] == "--matrix":
        cfg = load_engine_config(ROOT / "config" / "engine.multi.yaml")
        m = matrix(json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8")),
                   cfg["risk"]["max_quote_age_seconds"])
        pathlib.Path(argv[2]).write_text(json.dumps(m, indent=1), encoding="utf-8")
        print(json.dumps(m["summary"]))
        return 0
    if len(argv) != 2:
        raise SystemExit(__doc__)
    creds = load(pathlib.Path(argv[0]))
    cfg = load_engine_config(ROOT / "config" / "engine.multi.yaml")
    r = EtoroReadOnly(cfg, env=creds)
    out = {"generated_at": utc().isoformat(), "key_check": r.verify_read_only_key(), "source": "eToro public API, "
           "engine read-only key (EtoroReadOnly)", "instruments": {}}
    ids = {}
    for sym in sorted(set(UNIVERSE_529) | set(TARGETS_60)):
        hits = []
        for q in ALIASES.get(sym, [sym]):
            hits += [x for x in r.search_instrument(q) if str(x.get("internalSymbolFull", "")).upper() == q]
        row = {"used_by": [k for k, u in (("529", UNIVERSE_529), ("60-SQQQ", TARGETS_60)) if sym in u],
               "search_at": utc().isoformat(), "search": [{k: h.get(k) for k in SEARCH_FIELDS} for h in hits]}
        if hits:
            ids[sym] = int(hits[0]["internalInstrumentId"])
        out["instruments"][sym] = row
    el = r.eligibility(sorted(ids.values()))["raw"] if ids else {}
    out["eligibility_at"] = utc().isoformat()
    by_id = {int(e["instrumentId"]): e for e in (el.get("eligibilities") or [])}
    quotes = r._request("GET", "/api/v1/market-data/instruments/rates?instrumentIds=" +
                        ",".join(map(str, sorted(ids.values())))) if ids else {}
    out["quotes_received_at"] = utc().isoformat()
    qby = {int(q["instrumentID"]): q for q in (quotes.get("rates") or [])}
    for sym, iid in ids.items():
        e, q = by_id.get(iid), qby.get(iid)
        row = out["instruments"][sym]
        row["instrument_id"] = iid
        row["eligibility"] = None if e is None else {
            k: e.get(k) for k in ("minPositionExposure", "allowOpenPosition", "allowClosePosition",
                                  "allowPartialClosePosition", "requiresW8Ben", "unitsQuantityType",
                                  "orderFillBehaviorType", "tradeUnitType", "maxUnitsPerOrder")}
        if e is not None:
            row["eligibility"]["leverage_configs"] = [
                {"settlement": c.get("settlementType"), "direction": c.get("direction"),
                 "leverage": c.get("leverageValues"), "min_amount": c.get("minPositionAmount"),
                 "potential": c.get("isPotential")} for c in e.get("leverageConfigs") or []]
        row["quote"] = None if q is None else {"bid": q.get("bid"), "ask": q.get("ask"), "date": q.get("date")}
    s = json.dumps(out, indent=1, default=str)
    for v in creds.values():
        assert v not in s
    pathlib.Path(argv[1]).write_text(s, encoding="utf-8")
    print(argv[1], len(ids), "instruments found;", sorted(set(out["instruments"]) - set(ids)), "not found")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
