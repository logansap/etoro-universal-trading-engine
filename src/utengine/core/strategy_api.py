"""Versioned strategy interface (STRATEGY_INTERFACE_VERSION).

    decide(market_snapshot, strategy_state) -> StrategyDecision

A strategy only states TARGETS. It never sees the broker, cash, orders or positions; it cannot submit anything.
The portfolio manager turns targets into capital, the risk engine decides what is permissible, the planner
computes changes, the broker adapter submits and reads, reconciliation establishes what actually happened.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field

from .. import STRATEGY_INTERFACE_VERSION
from .errors import StrategyError


@dataclass(frozen=True)
class DataProvenance:
    source: str
    fetched_at: str
    sha256: str
    adjusted: bool
    first_date: str
    last_date: str


@dataclass(frozen=True)
class MarketSnapshot:
    """Daily bars for every requested symbol, aligned on one session calendar (the exchange calendar).
    `session` is the session whose close is the latest bar. Arrays are tuples of floats (NaN = no bar)."""
    session: dt.date
    dates: tuple
    open: dict
    high: dict
    low: dict
    close: dict
    provenance: DataProvenance

    def symbols(self):
        return sorted(self.close)


@dataclass(frozen=True)
class StrategyDecision:
    interface_version: str
    strategy_id: str
    implementation: str
    module_version: str
    strategy_version: str
    config_fingerprint: str
    session: str                     # decision session (ISO date): decided after its close
    decided_at: str                  # UTC ISO timestamp
    signal_target: dict              # the strategy's own target before substitutions
    target_weights: dict             # final strategy target, after substitutions (symbol -> weight of equity)
    cash_weight: float               # explicit cash
    instrument_ids: dict             # symbol -> eToro instrument id (or None if unverified)
    rebalance: bool                  # the strategy wants its targets re-applied this session
    explanation: dict
    data_provenance: dict
    strategy_state: dict = field(default_factory=dict)

    def validate(self) -> "StrategyDecision":
        if self.interface_version != STRATEGY_INTERFACE_VERSION:
            raise StrategyError(f"interface version {self.interface_version} != {STRATEGY_INTERFACE_VERSION}")
        tot = self.cash_weight
        for s, w in self.target_weights.items():
            if not isinstance(w, float) or not math.isfinite(w) or w < 0:
                raise StrategyError(f"invalid weight {s}={w!r} (long-only, finite)")
            tot += w
        if not math.isfinite(self.cash_weight) or self.cash_weight < -1e-9:
            raise StrategyError(f"invalid cash weight {self.cash_weight}")
        if abs(tot - 1.0) > 1e-9:
            raise StrategyError(f"weights + cash must sum to 1, got {tot}")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def utc_now_iso(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).isoformat()
