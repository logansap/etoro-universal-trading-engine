"""Scenarios 1 (#60 parity vs recorded decisions) and 2 (TECS signal preserved, SQQQ target) + tree regression."""
import datetime as dt
import json

import pandas as pd
import pytest

from conftest import FIX, ROOT, SNAP_ALL, write_strategy
from utengine.core.calendar import Calendar
from utengine.core.configuration import load_strategy_spec
from utengine.core.market_data import CsvBarStore, build_snapshot
from utengine.core.strategy_api import MarketSnapshot
from utengine.strategy_modules import REGISTRY, create, qc60
from utengine.strategy_modules.vendor import load_frozen_ports

CAL = Calendar(["2025-01-09"])
NOW = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)


def module(path=ROOT / "strategies" / "60.yaml"):
    return create(load_strategy_spec(path, REGISTRY))


def snap_at(store_dir, session):
    m = module()
    return build_snapshot(CsvBarStore(store_dir), CAL, session, m.required_symbols(), lookback_sessions=760, now=NOW)


def truncate(full, i):
    cut = lambda a: {t: v[:i + 1] for t, v in a.items()}  # noqa: E731
    return MarketSnapshot(full.dates[i], full.dates[:i + 1], cut(full.open), cut(full.high), cut(full.low),
                          cut(full.close), full.provenance)


@pytest.fixture(scope="module")
def full():
    return snap_at(SNAP_ALL, dt.date(2026, 10, 26))


def test_s01_parity_with_recorded_paper_decisions():
    """INVARIANT: on the exact data snapshot each recorded decision used, the engine's qc60 selects the same target
    and reproduces every recorded RSI(10) within the 2-decimal rounding of the record (< 0.005) and SMA200 exactly."""
    rec = [r for r in json.loads((FIX / "recorded_decisions.json").read_text())
           if r["portfolio_id"] in ("P60-ORIG", "P60-ETORO", "X60SQQQ-ETORO")]
    assert len(rec) == 8
    m = module()
    for r in rec:
        s = snap_at(FIX / "snapshots" / r["snapshot"], dt.date.fromisoformat(r["session"]))
        d = m.decide(s, {}, NOW)
        why = r["rationale"]
        for t, v in why["rsi10"].items():
            assert abs(d.explanation["rsi10"][t] - v) < 0.005, (r["session"], t)
        assert d.explanation["spy_sma200"] == pytest.approx(why["spy_sma200"], abs=1e-9)
        if r["status"] == "TRADE":
            assert set(r["targets"]) == set(d.target_weights)
        assert d.target_weights == {"TQQQ": 1.0}


def test_s01_parity_with_frozen_research_port(full):
    """INVARIANT: the independent qc60 port and the hash-pinned frozen research port S60 select the identical
    holding on every session of the fixture window (2024-09-05 .. 2026-10-26)."""
    F = load_frozen_ports()
    E, S = F["engine"], F["strategies"]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in full.dates])
    frames = {t: pd.DataFrame({"open": full.open[t], "high": full.high[t], "low": full.low[t],
                               "close": full.close[t]}, index=idx).dropna() for t in full.close}
    s60 = S.S60(E.Panel(frames, "2026-10-26"))
    m, compared = module(), 0
    for i in range(len(full.dates)):
        o = s60.decide(i, E.Book(cash=1.0), 1.0)
        theirs = max(o, key=o.get) if o else None
        try:
            mine, _ = m.select(truncate(full, i))
        except Exception:  # noqa: BLE001 -- not ready yet
            mine = None
        assert mine == theirs, full.dates[i]
        compared += mine is not None
    assert compared >= 500


def test_s01_agreement_with_native_lean(full):
    """INVARIANT: on every session where native LEAN (QuantConnect cloud, $100k run) bought something inside the
    fixture window, the engine's target is among the symbols LEAN bought (48/48 sessions; LEAN data != Yahoo)."""
    ln = pd.read_csv(FIX / "lean_native_60_buys.csv")
    m, n = module(), 0
    pos = {d: i for i, d in enumerate(full.dates)}
    for _, r in ln.iterrows():
        d = dt.date.fromisoformat(r.signal_session)
        if d not in pos or d < dt.date(2024, 9, 5):
            continue
        mine, _ = m.select(truncate(full, pos[d]))
        assert mine in r.bought.split("|"), (d, r.bought, mine)
        n += 1
    assert n >= 40


BRANCHES = [  # (session, signal, final target, branch fragment) -- found in real data, cross-checked with S60/LEAN
    ("2024-09-05", "TQQQ", "TQQQ", "no overbought"),
    ("2026-04-17", "UVXY", "UVXY", "RSI QQQ > 81"),
    ("2025-07-03", "UVXY", "UVXY", "RSI SPY > 80"),
    ("2025-03-10", "TECL", "TECL", "RSI TQQQ < 30"),
    ("2026-03-20", "SPXL", "SPXL", "RSI SPY < 30"),
    ("2025-03-14", "BSV", "BSV", "argmax RSI(TECS, BSV)"),
    ("2025-04-11", "TECS", "SQQQ", "argmax RSI(TECS, BSV)"),
    ("2025-05-02", "TECS", "SQQQ", "RSI SQQQ < 34"),
    ("2025-04-24", "TECL", "TECL", "RSI SQQQ >= 34"),
]


@pytest.mark.parametrize("session,signal,final,frag", BRANCHES)
def test_s02_regression_branches_real_data(full, session, signal, final, frag):
    """INVARIANT (bull, overbought, oversold, bear, TECS, SQQQ substitution): each real-data session selects its
    recorded branch; when the tree selects TECS the signal stays TECS and only the final target is SQQQ, at the
    same 100% weight."""
    i = full.dates.index(dt.date.fromisoformat(session))
    d = module().decide(truncate(full, i), {}, NOW)
    assert d.signal_target == {signal: 1.0}
    assert d.target_weights == {final: 1.0}
    assert any(frag in p for p in d.explanation["branch_path"])
    if signal == "TECS":
        assert d.explanation["substitution"]["signal_target"] == "TECS"
        assert d.explanation["substitution"]["final_target"] == "SQQQ"


def test_s02_tecs_rsi_still_feeds_argmax(full, tmp_path):
    """INVARIANT: the substitution happens after selection -- the same session without substitutions selects TECS
    (TECS's own RSI decided the argmax), and with TECS: SQQQ the only difference is the final target."""
    i = full.dates.index(dt.date(2025, 4, 11))
    def no_sub(raw):
        raw["substitutions"] = {}
        raw["universe"]["tradable"] = ["TQQQ", "UVXY", "TECL", "SPXL", "TECS", "BSV"]
        raw["instruments"]["TECS"] = {"etoro_instrument_id": None, "eligibility": "VERIFIED_UNAVAILABLE",
                                      "settlement": "UNKNOWN", "evidence": "test"}
        del raw["instruments"]["SQQQ"]
    plain = module(write_strategy(tmp_path, "60.yaml", no_sub)).decide(truncate(full, i), {}, NOW)
    subbed = module().decide(truncate(full, i), {}, NOW)
    assert plain.target_weights == {"TECS": 1.0} and subbed.target_weights == {"SQQQ": 1.0}
    assert plain.explanation["rsi10"] == subbed.explanation["rsi10"]
    assert subbed.explanation["rsi10"]["TECS"] > subbed.explanation["rsi10"]["BSV"]


SYNTH = [  # UVXY > 74 branches never occur in the fixture window: indicator-level synthetic cases
    ({"UVXY": 80}, {}, "UVXY"),
    ({"UVXY": 90}, {"qqq_above": True, "SQQQ": 25}, "TECS"),
    ({"UVXY": 90}, {"qqq_above": True, "SQQQ": 40}, "TECL"),
    ({"UVXY": 90}, {"qqq_above": False, "TECS": 60, "BSV": 50}, "TECS"),
    ({"UVXY": 90}, {"qqq_above": False, "TECS": 40, "BSV": 50}, "BSV"),
]


@pytest.mark.parametrize("rsis,extra,expected", SYNTH)
def test_s02_synthetic_high_volatility_branches(full, monkeypatch, rsis, extra, expected):
    """INVARIANT: the bear-regime UVXY > 74 sub-tree follows the source exactly (indicator values injected)."""
    snap = truncate(full, full.dates.index(dt.date(2025, 3, 14)))           # a bear session
    base = {"SPY": 45, "QQQ": 45, "TQQQ": 45, "UVXY": 50, "TECL": 50, "SPXL": 50, "SQQQ": 50, "TECS": 50, "BSV": 50}
    base.update({k: v for k, v in {**rsis, **extra}.items() if k != "qqq_above"})
    by_id = {id(snap.close[t]): t for t in snap.close}
    monkeypatch.setattr(qc60, "rsi_wilder", lambda c, n: float(base[by_id[id(c)]]))
    real_sma = qc60.sma
    def fake_sma(c, n):
        t = by_id[id(c)]
        if t == "QQQ" and "qqq_above" in extra:
            return c[-1] - 1 if extra["qqq_above"] else c[-1] + 1
        return real_sma(c, n)
    monkeypatch.setattr(qc60, "sma", fake_sma)
    sig, _ = module().select(snap)
    exp_signal = expected
    assert sig == exp_signal
    d = module().decide(snap, {}, NOW)
    assert d.target_weights == {("SQQQ" if expected == "TECS" else expected): 1.0}


def test_strategy_target_vs_executable_allocation():
    """INVARIANT: the strategy target is the source's 100%; the executable allocation applies LEAN's 0.25% buffer,
    and an ineligible target (BSV: no verified eToro id) stays cash instead of being redistributed."""
    from utengine.core.portfolio import allocate
    spec = load_strategy_spec(ROOT / "strategies" / "60.yaml", REGISTRY)
    m = create(spec)
    full_ = snap_at(SNAP_ALL, dt.date(2026, 10, 26))
    bsv = m.decide(truncate(full_, full_.dates.index(dt.date(2025, 3, 14))), {}, NOW)
    a = allocate(bsv, spec, 2000.0)
    assert bsv.target_weights == {"BSV": 1.0} and a.targets_usd == {} and "BSV" in a.blocked and a.cash_usd == 2000.0
    tq = m.decide(full_, {}, NOW)
    a = allocate(tq, spec, 2000.0)
    assert a.targets_usd == {"TQQQ": 1995.0}


def test_source_hash_is_verified():
    """INVARIANT: the vendored QC_60_Original.py is byte-identical to the frozen capture (sha fac0d6db...)."""
    qc60.verify_source()
