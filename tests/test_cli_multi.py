"""CLI surface of the multi-strategy and takeover features (simulation config, fresh state directory)."""
import json

from conftest import FIFTY_FIFTY, write_engine_yaml
from utengine.cli import main


def test_portfolio_exceptions_and_takeover_cli(tmp_path, capsys):
    """INVARIANT: the operator commands exist and are read-only by default: `portfolio status`, `exceptions`,
    `takeover preview --report` and `takeover status` run on a fresh simulated account without submitting orders;
    `takeover advance` without approvals is refused with a non-zero exit code."""
    cfg = write_engine_yaml(tmp_path / "engine.yaml", portfolio=FIFTY_FIFTY)
    base = ["--config", str(cfg), "--home", str(tmp_path / "home"), "--now", "2026-10-27T13:40:00+00:00"]
    assert main(base + ["run", "--once"]) == 0
    capsys.readouterr()
    assert main(base + ["portfolio", "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert set(st["weights"]) == {"QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"} and st["reconciliation"] == "CLEAN"
    assert main(base + ["exceptions"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    rep = tmp_path / "preview.md"
    assert main(base + ["takeover", "preview", "--report", str(rep)]) == 0
    pv = json.loads(capsys.readouterr().out)
    assert pv["state"] == "APPROVAL_REQUIRED" and rep.read_text(encoding="utf-8").startswith("# Account takeover")
    assert main(base + ["takeover", "status", "--run", pv["run_id"]]) == 0
    capsys.readouterr()
    assert main(base + ["takeover", "advance", "--run", pv["run_id"], "--operator", "owner"]) == 2
    sim = json.loads((tmp_path / "home" / "sim_broker.json").read_text(encoding="utf-8"))
    assert sim["submissions"] == []


def test_aborted_run_once_is_audited_as_aborted(tmp_path, monkeypatch):
    """REGRESSION (rehearsal 2026-10-29): a `run --once` whose cycle raises records ENGINE_STOP 'aborted: <error>',
    never 'finished', and the error propagates (non-zero exit)."""
    import sqlite3
    from utengine.core.engine import TradingEngine

    def boom(self, now=None):
        raise RuntimeError("simulated failure inside the cycle")
    cfg = write_engine_yaml(tmp_path / "engine.yaml", portfolio=FIFTY_FIFTY)
    monkeypatch.setattr(TradingEngine, "cycle", boom)
    import pytest
    with pytest.raises(RuntimeError):
        main(["--config", str(cfg), "--home", str(tmp_path / "home"), "--now", "2026-10-27T13:40:00+00:00",
              "run", "--once"])
    con = sqlite3.connect(str(tmp_path / "home" / "engine.sqlite"))
    stops = [r[0] for r in con.execute("SELECT payload FROM audit_log WHERE kind='ENGINE_STOP'")]
    assert stops and "aborted: RuntimeError" in stops[-1] and "finished" not in stops[-1]


def test_portfolio_restructure_cli_to_single_api2_sleeve(tmp_path, capsys):
    """INVARIANT: `portfolio restructure-preview` / `restructure` move the 50/50 portfolio to 100% QC_529_ETORO_API2
    by cash only, with a preview hash and an approver; no order is submitted."""
    cfg = write_engine_yaml(tmp_path / "engine.yaml", portfolio=FIFTY_FIFTY)
    base = ["--config", str(cfg), "--home", str(tmp_path / "home"), "--now", "2026-10-26T15:00:00+00:00"]
    assert main(base + ["run", "--once"]) == 0
    capsys.readouterr()
    assert main(base + ["portfolio", "restructure-preview", "--weights", "QC_529_ETORO_API2=1.0"]) == 0
    pv = json.loads(capsys.readouterr().out)
    assert pv["retired"] == ["QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"] and pv["added"] == ["QC_529_ETORO_API2"]
    assert main(base + ["portfolio", "restructure", "--preview-hash", pv["preview_hash"], "--approved-by", "owner"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "APPLIED"
    assert main(base + ["portfolio", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["weights"] == {"QC_529_ETORO_API2": 1.0}
    sim = json.loads((tmp_path / "home" / "sim_broker.json").read_text(encoding="utf-8"))
    assert sim["submissions"] == []
