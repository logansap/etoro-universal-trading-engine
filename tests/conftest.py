"""Shared harness: a simulation-mode engine on fixture data, a controllable clock and a persistent simulated broker.

Session used by most scenario tests: decision after the 2026-10-26 close (fixture 930795e32531bedc ends there),
execution at the 2026-10-27 open. New York is on EDT (UTC-4) on both days.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from utengine.brokers.simulated import SimulatedBroker  # noqa: E402
from utengine.core.configuration import strategy_content_fingerprint  # noqa: E402
from utengine.core.engine import TradingEngine  # noqa: E402
from utengine.core.market_data import CsvBarStore, DictBarStore  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
SNAP_ALL = FIX / "snapshots" / "930795e32531bedc"
UTC = dt.timezone.utc
DECIDE_AT = dt.datetime(2026, 10, 26, 20, 55, tzinfo=UTC)     # 16:55 New York
EXECUTE_AT = dt.datetime(2026, 10, 27, 13, 35, tzinfo=UTC)    # 09:35 New York
IDS = {"TQQQ": 4465, "UVXY": 3246, "TECL": 10855, "SPXL": 3230, "SQQQ": 4459, "TLT": 3020, "BIL": 4407,
       "SOXS": 3227, "QQQ": 3006, "PSQ": 2192, "QLD": 15727, "GDX": 3002}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = t

    def advance(self, **kw):
        self.t = self.t + dt.timedelta(**kw)


def write_engine_yaml(path: pathlib.Path, *, mode="simulation", initial="QC_60_SQQQ", strategies_dir=None,
                      portfolio=None, takeover=None, capital=None, **risk_overrides) -> pathlib.Path:
    cfg = yaml.safe_load((ROOT / "config" / "engine.yaml").read_text(encoding="utf-8"))
    if portfolio is not None:
        cfg["portfolio"] = portfolio
    if takeover is not None:
        cfg["takeover"] = takeover
    if capital is not None:
        cfg["capital"] = capital
    cfg["runtime"]["mode"] = mode
    cfg["runtime"]["initial_strategy"] = initial
    cfg["runtime"]["strategies_dir"] = str(strategies_dir or ROOT / "strategies")
    if mode == "simulation":
        cfg["broker"]["name"], cfg["broker"]["access"] = "simulated", "simulated"
    cfg["market_data"]["provider"] = "csv"
    cfg["market_data"]["csv_dir"] = str(SNAP_ALL)
    cfg["reconciliation"]["order_poll_seconds"] = 0.0
    cfg["reconciliation"]["order_poll_attempts"] = 2
    cfg["risk"].update(risk_overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def write_strategy(dirpath: pathlib.Path, src_name: str, mutate) -> pathlib.Path:
    raw = yaml.safe_load((ROOT / "strategies" / src_name).read_text(encoding="utf-8"))
    mutate(raw)
    raw["fingerprint"] = strategy_content_fingerprint(raw)
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / src_name
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return p


def last_close(sym: str) -> float:
    rows = CsvBarStore(SNAP_ALL).bars(sym, dt.date(2026, 10, 20), dt.date(2026, 10, 26))
    return rows[-1][4]


class Env:
    """A restartable engine: `engine()` builds a NEW TradingEngine on the same home, DB and broker file."""

    def __init__(self, tmp: pathlib.Path, *, cash=2000.0, cfg_path=None, clock_at=DECIDE_AT, broker_kwargs=None,
                 bar_store=None, **risk):
        self.tmp = tmp
        self.home = tmp / "home"
        self.cfg_path = cfg_path or write_engine_yaml(tmp / "config" / "engine.yaml", **risk)
        self.clock = Clock(clock_at)
        self.broker_path = self.home / "sim_broker.json"
        self.broker = SimulatedBroker(self.broker_path, instruments={v: k for k, v in IDS.items()}, cash=cash,
                                      clock=self.clock, **(broker_kwargs or {}))
        self.bar_store = bar_store or CsvBarStore(SNAP_ALL)
        self.price_all()
        self.e = None

    def price_all(self, factor=1.0):
        for sym, iid in IDS.items():
            if sym == "GDX":
                self.broker.set_price(iid, 80.0, 80.02)
                continue
            px = last_close(sym) * factor
            self.broker.set_price(iid, round(px * 0.9998, 6), round(px * 1.0002, 6))

    def engine(self, restart=True):
        if restart:
            self.broker = SimulatedBroker(self.broker_path, clock=self.clock)
        self.e = TradingEngine(self.cfg_path, self.home, clock=self.clock, broker=self.broker,
                               bar_store=self.bar_store)
        self.e.startup()
        return self.e

    def decide_and_execute(self):
        e = self.e or self.engine()
        self.clock.set(DECIDE_AT)
        d = e.cycle()
        self.clock.set(EXECUTE_AT)
        x = e.cycle()
        return d, x


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def extended_store(extra: dict) -> DictBarStore:
    """Fixture bars plus synthetic later sessions: {date: price factor vs the 2026-10-26 close} (simulation only)."""
    base = CsvBarStore(SNAP_ALL)
    data = {}
    for f in SNAP_ALL.glob("*.csv.gz"):
        sym = f.name.split(".")[0]
        rows = base.bars(sym, dt.date(2000, 1, 1), dt.date(2026, 10, 26))
        last = rows[-1]
        for d, k in sorted(extra.items()):
            c = last[4] * k
            rows = rows + [(d, c, c, c, c, last[5])]
        data[sym] = rows
    return DictBarStore(data, source="fixture+synthetic")


FIFTY_FIFTY = {"mode": "multi_strategy", "funding_policy": "proportional", "rebalance_policy": "strategy_independent",
               "aggregate_risk": "enforced",
               "sleeves": [{"strategy": "QC_529_ETORO_RESTRICTED", "weight": 0.5},
                           {"strategy": "QC_60_SQQQ", "weight": 0.5}]}


FULL_CAPITAL = {"ownership": "full_designated_account", "sizing_source": "verified_broker_equity",
                "manual_capital_amount": None, "funding_policy": "automatic_evidence_backed",
                "external_flow_allocation": "proportional", "preserve_strategy_cash": True}


def multi_env(tmp: pathlib.Path, *, cash=2000.0, portfolio=None, takeover=None, capital=None, **kw) -> "Env":
    cfg = write_engine_yaml(tmp / "config" / "engine.yaml", portfolio=portfolio or FIFTY_FIFTY, takeover=takeover,
                            capital=capital)
    return Env(tmp, cash=cash, cfg_path=cfg, **kw)


NEXT_DECIDE = dt.datetime(2026, 10, 27, 20, 55, tzinfo=UTC)
NEXT_EXECUTE = dt.datetime(2026, 10, 28, 13, 35, tzinfo=UTC)
