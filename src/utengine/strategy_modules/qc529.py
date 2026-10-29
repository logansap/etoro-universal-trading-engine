"""QuantConnect #529 (QuadEnsemble + velocity crash guard), multi-instrument.

Implementation: the hash-pinned frozen research port `ports.S529` (vendor/tsr_frozen), which subclasses the #504
sleeve port that research/qc_four_strategies parity-checked against #504's published LEAN backtest. Nothing is
re-derived here; this adapter only (1) builds the port's Panel from the snapshot, (2) restores the source's two
pieces of state (`_in_crash`, the last sleeve label), (3) calls the port's own decide(), and (4) converts its
target units at NAV 1 back into weights. Source semantics kept:
  * four 25% sleeves T10 / T11 / S2 / S3, weight-summed; the crash guard (QQQ 10-day return < -10% -> gross
    0.50, released above -4%) multiplies every weight; the rest is explicit cash;
  * trades ONLY when the sleeve label (including the guard state) changes -> `rebalance=False` otherwise;
  * symbols without a bar on the session are dropped by the port (their weight stays cash, never redistributed).
State starts fresh (guard off, no label) on the first decision, as the source's first post-warm-up rebalance.
"""
from __future__ import annotations

import datetime as dt
import math

from .. import STRATEGY_INTERFACE_VERSION
from ..core.errors import ConfigError, DataError
from ..core.strategy_api import MarketSnapshot, StrategyDecision
from .vendor import ORIGINALS, load_frozen_ports

MODULE_VERSION = "1.0.0"
SOURCE_SHA256 = ORIGINALS["QC_529_Original.py"]
SIGNAL = ("TQQQ", "TECL", "SOXL", "SQQQ", "UVXY", "SVIX", "SVXY", "XLK", "KMLM", "TLT", "QQQE", "VTV", "VOX",
          "VOOG", "VOOV", "XLP", "XLY", "FAS", "SPXL", "LABU", "SPY", "IOO", "XLF", "TECS", "SOXS", "QQQ", "PSQ",
          "QLD", "BTAL", "BIL", "AGG", "SH", "BND", "IEF", "BSV", "SMH", "UVIX")
TARGETS = ("UVXY", "TECL", "SOXL", "SPXL", "LABU", "SVIX", "SVXY", "SQQQ", "TLT", "BIL", "BTAL", "TQQQ", "TECS",
           "SOXS", "QQQ", "PSQ", "QLD", "BSV", "UVIX")
# symbols that list late in the source's own history and are therefore optional in a snapshot
LATE = ("KMLM", "LABU", "QQQE", "VOOG", "VOOV", "SVIX", "UVIX")
FROZEN_SETTINGS = {"crash_entry": -0.10, "crash_exit": -0.04, "crash_lookback": 10, "crash_gross": 0.5,
                   "sleeve_weight": 0.25}


def validate_spec(spec: dict) -> None:
    if spec["source"]["sha256"] != SOURCE_SHA256:
        raise ConfigError("qc529: source.sha256 must be the frozen QC_529_Original.py hash")
    if spec.get("settings") != FROZEN_SETTINGS:
        raise ConfigError(f"qc529: settings are frozen to {FROZEN_SETTINGS}")
    if spec.get("indicators") not in (None, {}):
        raise ConfigError("qc529: indicators are defined by the frozen sleeve port; leave `indicators` empty")
    if tuple(spec["universe"]["signal"]) != SIGNAL:
        raise ConfigError("qc529: universe.signal must be the source's deduplicated universe, in source order")
    if sorted(spec["universe"]["tradable"]) != sorted(TARGETS):
        raise ConfigError(f"qc529: universe.tradable must be {sorted(TARGETS)}")
    if spec["substitutions"]:
        raise ConfigError("qc529: substitutions are not part of #529 and are not supported")
    if spec["allocation"]["cash_buffer_pct"] != 0.0 or spec["rebalancing"]["min_order_pct_nav"] != 0.0:
        raise ConfigError("qc529: the source sets no cash buffer and minimum_order_margin_portfolio_percentage 0")


def _tuplify(x):
    return tuple(_tuplify(v) for v in x) if isinstance(x, list) else x


class _EmptyBook:
    def __init__(self):
        self.pos = {}


class QC529:
    IMPLEMENTATION = "qc529"
    MODULE_VERSION = MODULE_VERSION

    def __init__(self, spec: dict, module_sha256: str):
        validate_spec(spec)
        self.spec = spec
        self.module_sha256 = module_sha256
        self.frozen = load_frozen_ports()

    def required_symbols(self):
        return [s for s in SIGNAL if s not in LATE]

    def optional_symbols(self):
        return list(LATE)

    def _panel(self, snap: MarketSnapshot):
        import pandas as pd
        E = self.frozen["engine"]
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d in snap.dates])
        frames = {}
        for t in snap.close:
            df = pd.DataFrame({"open": snap.open[t], "high": snap.high[t], "low": snap.low[t],
                               "close": snap.close[t]}, index=idx)
            frames[t] = df.dropna(subset=["close"])
        return E.Panel(frames, snap.session.isoformat())

    def decide(self, snap: MarketSnapshot, state: dict, now: dt.datetime) -> StrategyDecision:
        panel = self._panel(snap)
        if panel.dates[-1].date() != snap.session:
            raise DataError("qc529: SPY calendar does not end on the decision session")
        i = panel.n - 1
        s = self.frozen["ports"].S529(panel)
        s._in_crash = bool(state.get("in_crash", False))
        s.last = _tuplify(state["last_label"]) if state.get("last_label") is not None else None
        prev_weights = dict(state.get("weights", {}))
        out = s.decide(i, _EmptyBook(), 1.0)
        ready = s.ready(i)
        sleeves = {"T10": s.t10(i) if ready[0] else {}, "T11": s.t11(i) if ready[1] else {},
                   "S2": s.s2(i) if ready[2] else {}, "S3": s.s3(i) if ready[3] else {}}
        if out is None:
            weights, rebalance = prev_weights, False
        else:
            weights = {}
            for t, units in out.items():
                w = round(float(units) * float(panel.c[t][i]), 12)
                if w > 0 and math.isfinite(w):
                    weights[t] = w
            rebalance = True
        cash = round(1.0 - sum(weights.values()), 12)
        ids = {t: (self.spec["instruments"].get(t) or {}).get("etoro_instrument_id") for t in weights}
        q = panel.c["QQQ"]
        info = {"sleeves": {k: {t: round(w, 6) for t, w in v.items()} for k, v in sleeves.items()},
                "sleeves_ready": dict(zip(("T10", "T11", "S2", "S3"), map(bool, ready))),
                "crash_guard_active": bool(s._in_crash),
                "qqq_10d_return": float(q[i] / q[i - 10] - 1) if i >= 10 else None,
                "label_changed": out is not None}
        new_state = {"in_crash": bool(s._in_crash), "last_label": s.last, "weights": weights}
        return StrategyDecision(
            interface_version=STRATEGY_INTERFACE_VERSION, strategy_id=self.spec["id"],
            implementation=self.IMPLEMENTATION, module_version=MODULE_VERSION,
            strategy_version=self.spec["version"], config_fingerprint=self.spec["fingerprint"],
            session=snap.session.isoformat(), decided_at=now.astimezone(dt.timezone.utc).isoformat(),
            signal_target=dict(weights), target_weights=dict(weights), cash_weight=cash, instrument_ids=ids,
            rebalance=rebalance, explanation=info,
            data_provenance={**snap.provenance.__dict__, "module_sha256": self.module_sha256,
                             "source_sha256": SOURCE_SHA256},
            strategy_state=new_state,
        ).validate()
