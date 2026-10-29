"""QuantConnect #60 "Conditional Sector Rotation" -- an independent port of the frozen source.

Source: vendor/QC_60_Original.py (byte-identical capture, SHA-256 fac0d6db...; verified at import).
Parity: tests/test_qc60_parity.py checks this port against (a) the frozen research port S60 on the same data,
(b) the championship's and the #60-SQQQ experiment's recorded decisions, (c) native LEAN target sequences.

Frozen tree (every number is the source's own; YAML cannot change them -- validate_spec enforces equality):
  RSI(10, Wilder) on all nine signal symbols; SMA200(SPY), SMA20(QQQ), SMA20(TQQQ); prices = last close.
  SPY > SMA200:  RSI QQQ > 81 -> UVXY; RSI SPY > 80 -> UVXY; else TQQQ
  otherwise:     RSI TQQQ < 30 -> TECL; RSI SPY < 30 -> SPXL;
                 RSI UVXY > 74 -> (RSI UVXY > 84 -> (QQQ > SMA20 -> (RSI SQQQ < 31 -> TECS : TECL)
                                                      : argmax RSI(TECS, BSV))
                                  : UVXY)
                 else TQQQ > SMA20 -> (RSI SQQQ < 34 -> TECS : TECL) : argmax RSI(TECS, BSV)
  SetHoldings(target, 1.0, liquidateExistingHoldings=True).
Substitutions (YAML `substitutions`, e.g. TECS: SQQQ) apply ONLY to the final target: TECS's own RSI still feeds
argmax RSI(TECS, BSV), and the substitute receives the same dollar weight. No partial-weight, volatility- or
rebound-filtered variants exist here.
LEAN's FreePortfolioValuePercentage (0.25%) and minimum order (0.1% of NAV) are execution semantics: they are
carried by the YAML (allocation.cash_buffer_pct, rebalancing.min_order_pct_nav) and applied by the portfolio
manager and planner, so the strategy target stays the source's 100%.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import pathlib

from .. import STRATEGY_INTERFACE_VERSION
from ..core.errors import ConfigError, DataError
from ..core.strategy_api import MarketSnapshot, StrategyDecision

MODULE_VERSION = "1.0.0"
SOURCE_FILE = pathlib.Path(__file__).resolve().parent / "vendor" / "QC_60_Original.py"
SOURCE_SHA256 = "fac0d6dbe1ddf831a90a7ed74dc251600239c5403a4c886a466b83aec390ea50"
SIGNAL = ("SPY", "QQQ", "TQQQ", "UVXY", "TECL", "SPXL", "SQQQ", "TECS", "BSV")
TREE_TARGETS = ("UVXY", "TQQQ", "TECL", "SPXL", "TECS", "BSV")
FROZEN_INDICATORS = {"rsi_period": 10, "rsi_type": "wilder", "spy_sma_period": 200, "qqq_sma_period": 20,
                     "tqqq_sma_period": 20}
FROZEN_SETTINGS = {"bull_qqq_rsi_uvxy": 81, "bull_spy_rsi_uvxy": 80, "bear_tqqq_rsi_tecl": 30,
                   "bear_spy_rsi_spxl": 30, "uvxy_rsi_high": 74, "uvxy_rsi_extreme": 84,
                   "sqqq_rsi_tecs_extreme": 31, "sqqq_rsi_tecs_otherwise": 34}
ALLOWED_SUBSTITUTIONS = ({}, {"TECS": "SQQQ"})


def verify_source() -> None:
    actual = hashlib.sha256(SOURCE_FILE.read_bytes()).hexdigest()
    if actual != SOURCE_SHA256:
        raise ConfigError(f"QC_60_Original.py hash {actual} != frozen {SOURCE_SHA256}")


def validate_spec(spec: dict) -> None:
    verify_source()
    if spec["source"]["sha256"] != SOURCE_SHA256:
        raise ConfigError("qc60: source.sha256 must be the frozen QC_60_Original.py hash")
    if spec.get("indicators") != FROZEN_INDICATORS:
        raise ConfigError(f"qc60: indicators are frozen to {FROZEN_INDICATORS}; variants are not supported")
    if spec.get("settings") != FROZEN_SETTINGS:
        raise ConfigError(f"qc60: settings (tree thresholds) are frozen to {FROZEN_SETTINGS}")
    if tuple(spec["universe"]["signal"]) != SIGNAL:
        raise ConfigError(f"qc60: universe.signal must be exactly {list(SIGNAL)}")
    subs = spec["substitutions"]
    if subs not in ALLOWED_SUBSTITUTIONS:
        raise ConfigError(f"qc60: substitutions must be one of {ALLOWED_SUBSTITUTIONS}")
    expected = sorted({subs.get(t, t) for t in TREE_TARGETS})
    if sorted(spec["universe"]["tradable"]) != expected:
        raise ConfigError(f"qc60: universe.tradable must be {expected} for these substitutions")
    if spec["allocation"]["cash_buffer_pct"] != 0.0025 or spec["rebalancing"]["min_order_pct_nav"] != 0.001:
        raise ConfigError("qc60: LEAN SetHoldings semantics are frozen (cash_buffer_pct 0.0025, min_order_pct_nav 0.001)")


# ----------------------------------------------------------------------------- indicators (LEAN semantics)
def rsi_wilder(closes, n: int):
    """LEAN RelativeStrengthIndex(n, Wilders): average gain/loss seeded with the simple mean of the first n
    changes (ready after n+1 prices), then Wilder smoothing. Starts at the first finite price; any later gap
    makes the value unavailable (None) rather than guessed."""
    xs = list(closes)
    s = next((i for i, x in enumerate(xs) if math.isfinite(x)), None)
    if s is None:
        return None
    y = xs[s:]
    if any(not math.isfinite(v) for v in y) or len(y) < n + 1:
        return None
    gains = [max(y[k] - y[k - 1], 0.0) for k in range(1, len(y))]
    losses = [max(y[k - 1] - y[k], 0.0) for k in range(1, len(y))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for k in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[k]) / n
        al = (al * (n - 1) + losses[k]) / n
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


def sma(closes, n: int):
    tail = list(closes)[-n:]
    if len(tail) < n or any(not math.isfinite(v) for v in tail):
        return None
    return sum(tail) / n


class QC60:
    IMPLEMENTATION = "qc60"
    MODULE_VERSION = MODULE_VERSION

    def __init__(self, spec: dict, module_sha256: str):
        validate_spec(spec)
        self.spec = spec
        self.module_sha256 = module_sha256
        self.subs = dict(spec["substitutions"])

    def required_symbols(self):
        return list(SIGNAL)

    def optional_symbols(self):
        return []

    def select(self, snap: MarketSnapshot):
        c = snap.close
        rsi = {t: rsi_wilder(c[t], 10) for t in SIGNAL}
        px = {t: c[t][-1] for t in ("SPY", "QQQ", "TQQQ")}
        sma_spy, sma_qqq, sma_tqqq = sma(c["SPY"], 200), sma(c["QQQ"], 20), sma(c["TQQQ"], 20)
        need = [rsi[t] for t in ("SPY", "QQQ", "TQQQ", "SQQQ", "UVXY", "TECS", "BSV")] + [sma_spy, sma_qqq, sma_tqqq]
        if any(v is None for v in need):
            raise DataError("qc60: indicators not ready (insufficient or gapped history)")

        def argmax_rsi(cands):                      # source: strict '>' from -1 -> first maximum wins
            best, hi = None, -1.0
            for t in cands:
                if rsi[t] > hi:
                    best, hi = t, rsi[t]
            return best

        path = []
        if px["SPY"] > sma_spy:
            path.append("bull: SPY > SMA200")
            if rsi["QQQ"] > 81:
                tgt = "UVXY"; path.append("RSI QQQ > 81")  # noqa: E702
            elif rsi["SPY"] > 80:
                tgt = "UVXY"; path.append("RSI SPY > 80")  # noqa: E702
            else:
                tgt = "TQQQ"; path.append("no overbought")  # noqa: E702
        else:
            path.append("bear: SPY <= SMA200")
            if rsi["TQQQ"] < 30:
                tgt = "TECL"; path.append("RSI TQQQ < 30")  # noqa: E702
            elif rsi["SPY"] < 30:
                tgt = "SPXL"; path.append("RSI SPY < 30")  # noqa: E702
            elif rsi["UVXY"] > 74:
                path.append("RSI UVXY > 74")
                if rsi["UVXY"] > 84:
                    path.append("RSI UVXY > 84")
                    if px["QQQ"] > sma_qqq:
                        path.append("QQQ > SMA20")
                        tgt = "TECS" if rsi["SQQQ"] < 31 else "TECL"
                        path.append("RSI SQQQ < 31" if tgt == "TECS" else "RSI SQQQ >= 31")
                    else:
                        tgt = argmax_rsi(["TECS", "BSV"]); path.append("QQQ <= SMA20: argmax RSI(TECS, BSV)")  # noqa: E702
                else:
                    tgt = "UVXY"; path.append("RSI UVXY <= 84")  # noqa: E702
            else:
                path.append("RSI UVXY <= 74")
                if px["TQQQ"] > sma_tqqq:
                    path.append("TQQQ > SMA20")
                    tgt = "TECS" if rsi["SQQQ"] < 34 else "TECL"
                    path.append("RSI SQQQ < 34" if tgt == "TECS" else "RSI SQQQ >= 34")
                else:
                    tgt = argmax_rsi(["TECS", "BSV"]); path.append("TQQQ <= SMA20: argmax RSI(TECS, BSV)")  # noqa: E702
        info = {"branch_path": path, "rsi10": {t: round(v, 4) for t, v in rsi.items()},
                "spy_close": px["SPY"], "spy_sma200": sma_spy, "qqq_close": px["QQQ"], "qqq_sma20": sma_qqq,
                "tqqq_close": px["TQQQ"], "tqqq_sma20": sma_tqqq}
        return tgt, info

    def decide(self, snap: MarketSnapshot, state: dict, now: dt.datetime) -> StrategyDecision:
        signal, info = self.select(snap)
        final = self.subs.get(signal, signal)
        if final != signal:
            info["substitution"] = {"signal_target": signal, "final_target": final, "rule": "same dollar weight",
                                    "tecs_close": snap.close[signal][-1] if signal in snap.close else None,
                                    "substitute_close": snap.close[final][-1] if final in snap.close else None}
        ids = {final: self.spec["instruments"][final]["etoro_instrument_id"]}
        return StrategyDecision(
            interface_version=STRATEGY_INTERFACE_VERSION, strategy_id=self.spec["id"],
            implementation=self.IMPLEMENTATION, module_version=MODULE_VERSION,
            strategy_version=self.spec["version"], config_fingerprint=self.spec["fingerprint"],
            session=snap.session.isoformat(), decided_at=now.astimezone(dt.timezone.utc).isoformat(),
            signal_target={signal: 1.0}, target_weights={final: 1.0}, cash_weight=0.0, instrument_ids=ids,
            rebalance=True, explanation=info,
            data_provenance={**snap.provenance.__dict__, "module_sha256": self.module_sha256,
                             "source_sha256": SOURCE_SHA256},
            strategy_state={"last_signal_target": signal, "last_final_target": final},
        ).validate()
