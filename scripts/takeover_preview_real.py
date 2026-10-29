"""READ-ONLY takeover preview of the REAL eToro account from a saved Gold Miner audit snapshot.

    ssh gold-miner-audit > snap.txt                       # authorized, forced-command, GET-only broker reads
    python scripts/takeover_preview_real.py --snapshot snap.txt --out docs/takeover/REAL_ACCOUNT_PREVIEW_<date>.md

Nothing here can write: the broker is the offline AuditReplayReader (saved file), wrapped in takeover.ReadOnlyView;
the engine runs in shadow mode (no writer) in a fresh scratch state directory. The audit snapshot carries no
unrealized P&L and no broker quote, so proceeds are estimated from a PUBLIC market-data close (Yahoo chart,
labelled MARKET_CONTEXT_NOT_BROKER, never broker evidence) and every such position is marked SEPARATE_HANDLING until
a broker quote exists. The audit has no account identifier, so approvals are impossible (by design).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from utengine.core import takeover as T  # noqa: E402
from utengine.core.engine import TradingEngine  # noqa: E402
from utengine.core.market_data import YahooChartBarStore  # noqa: E402
from utengine.tools.gm_audit import AuditReplayReader, load_audit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--config", default=str(ROOT / "config" / "engine.multi.yaml"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    doc = load_audit(a.snapshot)
    gen = dt.datetime.fromisoformat(doc["generated_at"])
    clock = lambda: gen + dt.timedelta(seconds=30)  # noqa: E731 -- the preview is as of the snapshot
    home = pathlib.Path(tempfile.mkdtemp(prefix="ute-takeover-real-"))
    bars = YahooChartBarStore()
    probe = TradingEngine(a.config, home / "probe", clock=clock, broker=AuditReplayReader([doc], {}), bar_store=bars)
    reader = AuditReplayReader([doc] * 50, probe.id_to_symbol)
    e = TradingEngine(a.config, home / "state", clock=clock, broker=reader, bar_store=bars)
    assert e.mode == "shadow" and e.writer is None
    e.startup()
    ctx, ctx_note = {}, {}
    sym_of = {3002: "GDX"}
    for p in doc["broker"]["data"]["positions"]:
        iid = int(p["instrument_id"])
        sym = e.id_to_symbol.get(iid) or p.get("symbol") or sym_of.get(iid)
        rows = bars.bars(sym, gen.date() - dt.timedelta(days=7), gen.date()) if sym else []
        if rows:
            d, _o, _h, _l, c, _v = rows[-1]
            ctx[iid] = (c, c, dt.datetime.combine(d, dt.time(20, 0), dt.timezone.utc).isoformat())
            ctx_note[sym] = {"close": c, "session": d.isoformat(), "source": bars.source}
    pv = T.preview(e, clock(), context_quotes=ctx)
    md = T.render(pv)
    head = (f"> Source: Gold Miner read-only audit snapshot generated {doc['generated_at']} (host {doc['host']}, "
            f"GM git {doc['git']['data']['sha'][:7]}). Market-context closes (NOT broker evidence): "
            f"{json.dumps(ctx_note)}. Gold Miner's own ledger (context only): "
            f"{json.dumps(doc.get('residual', {}).get('data', {}).get('inputs'))}; unexplained residual "
            f"{doc.get('residual', {}).get('data', {}).get('unexplained_residual')} USD.\n"
            "> This preview made no broker call at all (offline file) and authorizes nothing.\n\n")
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md.replace("\n\n", "\n\n" + head, 1), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(pv, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"run_id": pv["run_id"], "state": pv["state"], "capital": pv["capital"],
                      "allocation": pv["allocation"], "out": str(out)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
