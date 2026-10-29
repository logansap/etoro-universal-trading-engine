"""Gold Miner #687 -- READ-ONLY REFERENCE. There is no adapter in this release (registry status UNAVAILABLE).

The LIVE engine is D:\\AI-Gold-Miner-Trading-Engine (VPS /home/engine/AI-Gold-Miner-Trading-Engine,
gold-miner-engine.service). This engine never imports, calls, stops or configures it. A future migration must
provide, through the normal strategy interface, a module that:
  * returns target weights per tree leaf with GDXU/GDXD expressed as the strategy intends, while the leverage
    translation (GDX CFD at x5 with a broker stop) becomes an EXECUTION capability of the eToro adapter, gated by
    the risk engine (this release allows only x1 long);
  * reproduces the frozen tree version (63e407d4c2b1 at the time of writing) with recorded-decision parity;
  * owns no positions until an approved transition plan transfers the live #687 book (see
    docs/STRATEGY_SWITCHING.md, "Adopting positions").
validate_spec refuses any 687 YAML that is enabled or claims live eligibility.
"""
from __future__ import annotations

from ..core.errors import ConfigError


def validate_spec(spec: dict) -> None:
    if spec["enabled"] or spec["live_eligible"]:
        raise ConfigError("goldminer687: reference only -- must be enabled: false and live_eligible: false")
