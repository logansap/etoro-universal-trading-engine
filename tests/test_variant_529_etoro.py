"""Candidate B (2026-10-29): #529 eToro-restricted is a distinct, versioned strategy with the IDENTICAL frozen rules.
It may differ from QC_529 only in identity and documentation -- never in rules, universe or instruments."""
import datetime as dt

import yaml

from conftest import ROOT, SNAP_ALL
from utengine.core.calendar import Calendar
from utengine.core.configuration import load_strategy_spec
from utengine.core.market_data import CsvBarStore, build_snapshot
from utengine.core.portfolio import allocate
from utengine.strategy_modules import REGISTRY, create

NOW = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)
RULE_KEYS = ("implementation", "source", "indicators", "universe", "substitutions", "instruments", "eligibility",
             "schedule", "allocation", "rebalancing", "risk", "execution", "settings")
API_VERIFIED = {"UVXY", "TECL", "SPXL", "SQQQ", "TLT", "BIL", "TQQQ", "SOXS", "QQQ", "PSQ", "QLD"}


def test_variant_b_has_identical_rules_and_its_own_identity():
    """INVARIANT: every rule-bearing key equals QC_529's; id, name and fingerprint differ."""
    a = yaml.safe_load((ROOT / "strategies/529.yaml").read_text(encoding="utf-8"))
    b = yaml.safe_load((ROOT / "strategies/529_etoro_restricted.yaml").read_text(encoding="utf-8"))
    for k in RULE_KEYS:
        assert a[k] == b[k], k
    assert a["id"] == "QC_529" and b["id"] == "QC_529_ETORO_RESTRICTED"
    assert a["fingerprint"] != b["fingerprint"]


def test_variant_b_keeps_unavailable_weight_in_cash_without_substitution():
    """INVARIANT: same decision as #529; the executable allocation contains only API-verified instruments and the
    rest is cash (never redistributed, never substituted)."""
    sa = load_strategy_spec(ROOT / "strategies/529.yaml", REGISTRY)
    sb = load_strategy_spec(ROOT / "strategies/529_etoro_restricted.yaml", REGISTRY)
    snap = build_snapshot(CsvBarStore(SNAP_ALL), Calendar(["2025-01-09"]), dt.date(2026, 10, 26),
                          create(sb).required_symbols(), create(sb).optional_symbols(), lookback_sessions=760, now=NOW)
    da, db_ = create(sa).decide(snap, {}, NOW), create(sb).decide(snap, {}, NOW)
    assert da.target_weights == db_.target_weights
    al = allocate(db_, sb, 2000.0)
    assert set(al.targets_usd) <= API_VERIFIED
    blocked_weight = sum(w for s, w in db_.target_weights.items() if s in al.blocked)
    assert abs(sum(al.targets_usd.values()) + al.cash_usd - 2000.0) < 0.02
    assert al.cash_usd >= 2000.0 * blocked_weight - 0.02
    assert "SOXL" in al.blocked
