"""Scenario 27 (config tampering, unsupported modules) and the live-permission boundary."""
import pathlib

import pytest
import yaml

from conftest import ROOT, Env, write_engine_yaml, write_strategy
from utengine.core import halts, strategy_manager
from utengine.core.configuration import load_engine_config, load_strategy_spec
from utengine.core.errors import ConfigError, LiveExecutionNotAvailable
from utengine.core.security import LIVE_RELEASE_ENABLED, obtain_writer
from utengine.strategy_modules import REGISTRY


def _engine_yaml(tmp_path, mutate):
    cfg = yaml.safe_load((ROOT / "config" / "engine.yaml").read_text(encoding="utf-8"))
    mutate(cfg)
    p = tmp_path / "engine.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_repository_configs_validate():
    """INVARIANT: the shipped engine.yaml (shadow) and all strategy YAMLs validate; 687 is disabled/unavailable."""
    cfg = load_engine_config(ROOT / "config" / "engine.yaml")
    assert cfg["runtime"]["mode"] == "shadow" and cfg["security"]["live_execution_enabled"] is False
    specs = {p.name: load_strategy_spec(p, REGISTRY) for p in (ROOT / "strategies").glob("*.yaml")}
    assert specs["687.yaml"]["enabled"] is False and REGISTRY["goldminer687"].status == "UNAVAILABLE"


@pytest.mark.parametrize("mutate,err", [
    (lambda c: c["runtime"].update(mode="live"), LiveExecutionNotAvailable),
    (lambda c: c["security"].update(live_execution_enabled=True), LiveExecutionNotAvailable),
    (lambda c: c["broker"].update(access="live_trading"), ConfigError),
    (lambda c: c["broker"]["credentials_env"].update(api_key="sk-live-" + "0123456789abcdef"), ConfigError),
    (lambda c: c.update(extra_key=1), ConfigError),
    (lambda c: c["risk"].update(max_leverage=5), ConfigError),
    (lambda c: c["risk"].update(allow_short=True), ConfigError),
    (lambda c: c["broker"].update(api_base="https://evil.example.com"), ConfigError),
    (lambda c: c["runtime"].update(mode="simulation"), ConfigError),          # simulation needs simulated broker
])
def test_s27_engine_yaml_cannot_enable_live_or_unsafe(tmp_path, mutate, err):
    """INVARIANT: no edit of engine.yaml enables live trading, leverage, shorts, inline secrets or another host."""
    with pytest.raises(err):
        load_engine_config(_engine_yaml(tmp_path, mutate))


def test_s27_strategy_tampering_detected(tmp_path):
    """INVARIANT: editing a strategy YAML without re-fingerprinting is CONFIG_TAMPERED."""
    p = tmp_path / "60.yaml"
    text = (ROOT / "strategies" / "60.yaml").read_text(encoding="utf-8").replace("cash_buffer_pct: 0.0025",
                                                                                 "cash_buffer_pct: 0.0030")
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="CONFIG_TAMPERED"):
        load_strategy_spec(p, REGISTRY)


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r.update(implementation="os.system"), "not in the approved strategy registry"),
    (lambda r: r["settings"].update(bull_qqq_rsi_uvxy=75), "frozen"),
    (lambda r: r["indicators"].update(rsi_period=14), "frozen"),
    (lambda r: r["allocation"].update(capital_fraction=0.5), "not allowed"),
    (lambda r: r["substitutions"].update(TECS="SH"), "substitution"),
    (lambda r: r["execution"].update(mode="live"), "live"),
    (lambda r: r["eligibility"].update(max_leverage=3), "not allowed"),
    (lambda r: r.update(api_token="abcdef123456"), "credential|unknown"),
])
def test_s27_unsupported_modules_and_variants_refused(tmp_path, mutate, match):
    """INVARIANT: re-fingerprinted YAML still cannot select an unregistered module, change the frozen tree,
    create a 50%/75% variant, substitute arbitrarily, request live or leverage, or carry a credential."""
    p = write_strategy(tmp_path, "60.yaml", mutate)
    with pytest.raises((ConfigError, LiveExecutionNotAvailable), match=match):
        load_strategy_spec(p, REGISTRY)


def test_s27_687_cannot_be_enabled(tmp_path):
    """INVARIANT: the Gold Miner reference YAML cannot be enabled or made live-eligible."""
    p = write_strategy(tmp_path, "687.yaml", lambda r: r.update(enabled=True))
    with pytest.raises(ConfigError, match="reference only"):
        load_strategy_spec(p, REGISTRY)


def test_s27_active_strategy_drift_halts(tmp_path):
    """INVARIANT: a deliberately re-fingerprinted change to the ACTIVE strategy's YAML does not take effect: the
    engine raises CONFIG_DRIFT (halt ALL) and makes no decision until re-activated through the workflow."""
    sdir = tmp_path / "strategies"
    for n in ("60.yaml", "529.yaml", "687.yaml"):
        write_strategy(sdir, n, lambda r: None)
    env = Env(tmp_path, cfg_path=write_engine_yaml(tmp_path / "config" / "engine.yaml", strategies_dir=sdir))
    env.engine()
    write_strategy(sdir, "60.yaml", lambda r: r["logging"].update(level="DEBUG"))
    e = env.engine()
    out = e.cycle()
    assert out["drift"] and "decision" not in out
    assert any(h["kind"] == "CONFIG_DRIFT" for h in halts.active(e.db))


def test_s27_editing_initial_strategy_does_not_switch(tmp_path):
    """INVARIANT: changing engine.yaml runtime.initial_strategy after the first start changes nothing; status says
    to use the switch workflow."""
    env = Env(tmp_path)
    env.engine()
    env.cfg_path = write_engine_yaml(tmp_path / "config" / "engine.yaml", initial="QC_529")
    e = env.engine()
    assert strategy_manager.active(e.db)["strategy_id"] == "QC_60_SQQQ"
    assert "ignored after first start" in e.status()["initial_strategy_note"]


def test_live_writer_cannot_be_obtained():
    """INVARIANT: no code path yields a live writer; the release gate constant is False and 'live' raises."""
    assert LIVE_RELEASE_ENABLED is False
    with pytest.raises(LiveExecutionNotAvailable):
        obtain_writer("live", object())
    assert obtain_writer("shadow", object()) is None


def test_live_order_code_is_isolated_and_never_constructed():
    """INVARIANT: eToro order routes appear ONLY in brokers/etoro_live.py; the only BrokerWriters are the simulated
    broker and that gated adapter; nothing outside etoro_live.py (and its contract tests) constructs it."""
    src = ROOT / "src" / "utengine"
    offenders = [p.name for p in src.rglob("*.py") if "trading/execution" in p.read_text(encoding="utf-8")]
    assert offenders == ["etoro_live.py"], offenders
    writers = sorted(p.name for p in src.rglob("*.py") if "(BrokerReader, BrokerWriter)" in p.read_text(encoding="utf-8")
                     or "(BrokerWriter)" in p.read_text(encoding="utf-8"))
    assert writers == ["etoro_live.py", "simulated.py"]
    users = sorted(p.name for p in src.rglob("*.py") if "EtoroLiveWriter" in p.read_text(encoding="utf-8"))
    # PILOT RELEASE: exactly one constructor call site, engine._supervised_live_writer, and it constructs the
    # writer only after authorize_supervised() returned (the authorization object is passed in)
    assert users == ["engine.py", "etoro_live.py"], users
    eng = (src / "core" / "engine.py").read_text(encoding="utf-8")
    assert eng.count("EtoroLiveWriter(") == 1
    body = eng[eng.index("def _supervised_live_writer"):eng.index("def instrument_symbols")]
    assert "EtoroLiveWriter(" in body and body.index("authorize_supervised(release_sha") < body.index(
        "EtoroLiveWriter(") and "authorization=auth" in body
    assert eng.count("_supervised_live_writer(") == 2        # its definition and the single obtain_writer closure


def test_no_secrets_in_repository():
    """INVARIANT: no tracked text file contains something that looks like a credential value."""
    import re
    pat = re.compile(r"(x-api-key|x-user-key|api[_-]?key|secret|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]", re.I)
    bad = []
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".yaml", ".md", ".toml", ".sh", ".service", ".txt", ".json"} \
                and ".git" not in p.parts and "var" not in p.parts:
            if pat.search(p.read_text(encoding="utf-8", errors="ignore")):
                bad.append(str(p))
    assert bad == []
