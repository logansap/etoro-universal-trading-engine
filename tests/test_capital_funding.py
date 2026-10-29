"""Full-account capital ownership and automatic, evidence-backed funding discovery (core/funding.py).

Simulated broker. `cash_tx_capability=False` emulates eToro (no trading-account deposit feed); evidence then comes
only from `add_funding_evidence` (the eToro-Money internal-transfer source) or a later `add_tx_record`.
Every test states the invariant it protects."""
import datetime as dt
import json

import pytest

from conftest import (DECIDE_AT, EXECUTE_AT, FIFTY_FIFTY, FULL_CAPITAL, IDS, NEXT_DECIDE, NEXT_EXECUTE,
                      extended_store, multi_env, write_engine_yaml)
from utengine.brokers.simulated import SimulatedBroker, SimulatedCrash
from utengine.core import accounting, funding, halts, sleeves
from utengine.core.errors import ConfigError
from utengine.core.reconciliation import engine_position_ids

A, B = "QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"
D2 = dt.date(2026, 10, 27)


def env_(tmp_path, feed=False, day2=False, **kw):
    return multi_env(tmp_path, capital=FULL_CAPITAL, broker_kwargs={"cash_tx_capability": feed},
                     bar_store=extended_store({D2: 1.0}) if day2 else None, **kw)


def rec(e):
    r = e.reconcile("t", e.now())
    return r, e.sleeve_valuation(r)


def cash(e):
    return {s: b["cash"] for s, b in sleeves.balances(e.db).items() if s in (A, B)}


def check_capital(e, r):
    v = e.sleeve_valuation(r)
    rep = funding.capital_report(e.db, r, v)
    assert rep["status"] == "VERIFIED", rep
    assert not sleeves.check_invariants(e.db, r.snapshot, engine_position_ids(e.db))
    return rep


def ticks(env, e, n=1, **adv):
    out = None
    for _ in range(n):
        env.clock.advance(**(adv or {"minutes": 1}))
        out = e.reconcile("t", env.clock())
    return out


def kinds(e):
    return {h["kind"] for h in halts.active(e.db) + halts.active_sleeve(e.db)}


# ------------------------------------------------------------------------------------------------ configuration
def test_manual_capital_amount_is_refused(tmp_path):
    """INVARIANT: capital is discovered from the broker; a typed budget is refused by the schema."""
    from utengine.core.configuration import load_engine_config
    bad = dict(FULL_CAPITAL, manual_capital_amount=1500)
    with pytest.raises(ConfigError, match="must be null"):
        load_engine_config(write_engine_yaml(tmp_path / "e.yaml", portfolio=FIFTY_FIFTY, capital=bad))


# ------------------------------------------------------------------------------------------------ takeover start
def test_initial_takeover_of_a_funded_account_reconciles_to_broker_equity(tmp_path):
    """INVARIANT: after taking over a funded account (cash + a foreign GDX short), all of it is discovered and
    verified: sum of sleeve equity == broker account equity, nothing unattributed."""
    from test_takeover import GM, approve_all_to_close
    from utengine.core import takeover as T
    env = multi_env(tmp_path, takeover=GM, capital=FULL_CAPITAL, clock_at=EXECUTE_AT)
    env.broker.external_open_position(IDS["GDX"], 740.0, is_buy=False, leverage=5, open_rate=81.43)
    e = env.engine()
    pv = T.preview(e, env.clock())
    approve_all_to_close(env, e, pv)
    T.advance(e, pv["run_id"], env.clock(), "owner")
    T.advance(e, pv["run_id"], env.clock(), "owner")
    T.approve(e, pv["run_id"], "ACTIVATE_STRATEGY", "owner", env.clock())
    T.advance(e, pv["run_id"], env.clock(), "owner")
    r, v = rec(e)
    rep = check_capital(e, r)
    assert rep["foreign_positions_value"] == 0 and abs(rep["account_equity"] - env.broker.s["credit"]) < 1e-6
    assert v["sleeves"][A]["equity"] == pytest.approx(v["sleeves"][B]["equity"], abs=0.02)


def test_zero_cash_account_with_open_positions(tmp_path):
    """INVARIANT: an account with no cash but open foreign positions bootstraps with INITIAL_CAPITAL 0; nothing is
    sized from cash that does not exist; after an approved takeover the proceeds become the sleeves' capital."""
    from test_takeover import GM, approve_all_to_close
    from utengine.core import takeover as T
    env = multi_env(tmp_path, cash=740.0, takeover=GM, capital=FULL_CAPITAL, clock_at=EXECUTE_AT)
    env.broker.external_open_position(IDS["GDX"], 740.0, is_buy=False, leverage=5, open_rate=81.43)
    e = env.engine()
    assert accounting.funding_summary(e.db)["INITIAL_CAPITAL"] == 0.0
    r, v = rec(e)
    assert all(x["equity"] == 0.0 for x in v["sleeves"].values())
    pv = T.preview(e, env.clock())
    approve_all_to_close(env, e, pv)
    for _ in range(2):
        T.advance(e, pv["run_id"], env.clock(), "owner")
    T.approve(e, pv["run_id"], "ACTIVATE_STRATEGY", "owner", env.clock())
    T.advance(e, pv["run_id"], env.clock(), "owner")
    r, v = rec(e)
    check_capital(e, r)
    assert sum(cash(e).values()) == pytest.approx(env.broker.s["credit"], abs=1e-6) and env.broker.s["credit"] > 700


# ------------------------------------------------------------------------------------------------ deposits
def test_deposit_when_all_funds_are_invested(tmp_path):
    """INVARIANT: a deposit while (almost) everything is invested is split 50/50 into sleeve cash, excluded from P&L,
    and #60 invests its half at its next rebalance; nothing else is redistributed."""
    env = env_(tmp_path, day2=True)
    e = env.engine()
    env.decide_and_execute()
    c0 = cash(e)
    env.broker.deposit(1000.0, record=False)
    env.broker.add_funding_evidence(1000.0, "TR-1")
    ticks(env, e)
    assert {s: round(cash(e)[s] - c0[s], 6) for s in (A, B)} == {A: 500.0, B: 500.0}
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    env.clock.set(NEXT_EXECUTE)
    e.cycle()
    r, v = rec(e)
    assert v["sleeves"][B]["cash"] < 10
    check_capital(e, r)


def test_multiple_deposits_on_one_day_are_matched_individually(tmp_path):
    """INVARIANT: two deposits between two reconciliations (one combined unexplained credit) are classified from two
    evidence records (subset match), each with its own reference; nothing is double counted."""
    env = env_(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(300.0, record=False)
    env.broker.deposit(200.0, record=False)
    ticks(env, e)
    assert funding.suspense_total(e.db) == 500.0
    env.broker.add_funding_evidence(300.0, "TR-A")
    env.broker.add_funding_evidence(200.0, "TR-B")
    r = ticks(env, e)
    assert funding.suspense_total(e.db) == 0.0
    refs = sorted(x["ref"] for x in e.db.q("SELECT ref FROM cash_flows WHERE kind='DEPOSIT'"))
    assert refs == ["fund:funding_evidence:TR-A", "fund:funding_evidence:TR-B"]
    assert r.clean and check_capital(e, r)


def test_deposit_during_market_hours_and_during_an_open_order(tmp_path):
    """INVARIANT: a deposit arriving while an open order is in flight is classified and allocated without touching
    the order's reservation; after the fill every sleeve and the account still add up."""
    env = env_(tmp_path)
    e = env.engine()
    env.clock.set(DECIDE_AT)
    e.cycle()
    env.broker.inject("pending")
    env.clock.set(EXECUTE_AT)
    e.cycle()
    assert sum(sleeves.reserved(e.db).values()) > 0
    env.broker.deposit(400.0, record=False)
    env.broker.add_funding_evidence(400.0, "TR-OPEN")
    ticks(env, e)
    assert funding.suspense_total(e.db) == 0.0
    env.broker.advance()
    e.executor.resolve_all()
    r, _ = rec(e)
    for h in halts.active(e.db):
        halts.clear(e.db, h["halt_id"], "t", "order filled", e.nows())
    r, _ = rec(e)
    check_capital(e, r)


def test_deposit_detected_before_its_statement_then_explained(tmp_path):
    """INVARIANT: cash that arrives BEFORE its statement row is held in suspense (not allocated, not spendable, one
    warning); when the statement row appears it explains that suspense item -- it is not counted a second time."""
    env = env_(tmp_path, feed=True)
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(250.0, record=False)
    r, v = ticks(env, e), None
    assert funding.suspense_total(e.db) == 250.0 and r.clean
    assert sum(cash(e).values()) == pytest.approx(env.broker.s["credit"] - 250.0, abs=1e-6)
    assert r.strategy_equity == pytest.approx(sum(x["equity"] for x in e.sleeve_valuation(r)["sleeves"].values()), abs=1e-4)
    env.broker.add_tx_record("DEPOSIT", 250.0, evidence="statement row, one day late")
    r = ticks(env, e)
    assert funding.suspense_total(e.db) == 0.0 and r.clean
    assert e.db.one("SELECT COUNT(*) n FROM cash_flows WHERE kind='DEPOSIT'")["n"] == 1
    assert not e.db.one("SELECT 1 FROM discrepancies WHERE status='OPEN'")
    check_capital(e, r)


def test_delayed_funding_confirmation_does_not_rewrite_returns(tmp_path):
    """INVARIANT: an unexplained credit stays in suspense across a daily record (no external flow, no P&L), and is
    allocated only when evidence arrives two days later; the earlier TWR is never rewritten."""
    env = env_(tmp_path, day2=True)
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(600.0, record=False)
    ticks(env, e)
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    d1 = {r["sleeve"]: dict(r) for r in e.db.q("SELECT * FROM sleeve_daily WHERE session=?", (D2.isoformat(),))}
    assert all(abs(d1[s]["external_flow"]) < 1e-9 and abs(d1[s]["pnl"]) < 2.0 for s in (A, B))
    env.broker.add_funding_evidence(600.0, "TR-LATE", posted_at=(NEXT_DECIDE - dt.timedelta(hours=3)).isoformat())
    env.clock.advance(days=2)
    ticks(env, e)
    assert funding.suspense_total(e.db) == 0.0
    d1b = {r["sleeve"]: dict(r) for r in e.db.q("SELECT * FROM sleeve_daily WHERE session=?", (D2.isoformat(),))}
    assert d1b == d1                                             # history untouched
    assert cash(e)[A] - d1[A]["cash"] == pytest.approx(300.0, abs=1e-6)


def test_withdrawal_larger_than_a_sleeves_cash_never_uses_the_other_sleeve(tmp_path):
    """INVARIANT: a verified withdrawal is split by weight even when one sleeve has almost no cash: that sleeve goes
    negative and its OWN next rebalance sells to restore it; the other sleeve's cash and units are not touched."""
    env = env_(tmp_path, day2=True)
    e = env.engine()
    env.decide_and_execute()
    c0 = cash(e)
    env.broker.withdraw(300.0, record=False)
    env.broker.add_funding_evidence(-300.0, "WD-1")
    ticks(env, e)
    c1 = cash(e)
    assert c1[B] == pytest.approx(c0[B] - 150.0, abs=1e-6) and c1[B] < 0
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    env.clock.set(NEXT_EXECUTE)
    e.cycle()
    r, v = rec(e)
    assert v["sleeves"][B]["cash"] > -1.0                         # #60 sold its own TQQQ to cover
    check_capital(e, r)


def test_unexplained_cash_decrease_still_halts(tmp_path):
    """INVARIANT: money LEAVING the account without evidence is never put in suspense: it halts (the sleeves could
    otherwise spend cash that is gone)."""
    env = env_(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    env.broker.withdraw(100.0, record=False)
    r = ticks(env, e)
    assert not r.clean and "CASH_DISCREPANCY" in kinds(e) and funding.suspense_total(e.db) == 0.0


def test_broker_financing_deduction_is_trading_pnl(tmp_path):
    """INVARIANT: an evidenced financing/overnight fee is split by sleeve equity and counted in P&L, never as a
    withdrawal and never put in suspense."""
    env = env_(tmp_path, feed=True)
    e = env.engine()
    env.decide_and_execute()
    c0 = cash(e)
    env.broker.adjust_cash(-1.50, kind="FEE", evidence="overnight fee")
    r = ticks(env, e)
    assert r.clean and funding.suspense_total(e.db) == 0.0
    assert sum(c0[s] - cash(e)[s] for s in (A, B)) == pytest.approx(1.50, abs=1e-6)
    assert accounting.funding_summary(e.db)["FEE"] == -1.5 and accounting.funding_summary(e.db)["WITHDRAWAL"] == 0


def test_partial_close_followed_by_deposit(tmp_path):
    """INVARIANT: a partial close's proceeds go to the selling sleeve (TRADE_CLOSE); a deposit afterwards is split by
    weight; sale proceeds are never classified as funding."""
    from test_multi_strategy import day2
    env = env_(tmp_path, day2=True)
    e = env.engine()
    env.decide_and_execute()
    day2(env, forced={A: {}})
    assert e.db.one("SELECT 1 FROM intents WHERE action='PARTIAL_CLOSE' AND status='RECONCILED'")
    env.broker.deposit(200.0, record=False)
    env.broker.add_funding_evidence(200.0, "TR-PC")
    r = ticks(env, e)
    assert r.clean and accounting.funding_summary(e.db)["DEPOSIT"] == 200.0
    assert e.db.one("SELECT COUNT(*) n FROM cash_flows WHERE kind='DEPOSIT'")["n"] == 1
    check_capital(e, r)


def test_restart_during_deposit_reconciliation_never_duplicates(tmp_path, monkeypatch):
    """INVARIANT: the engine dies after the deposit flow was recorded but before it reached the sleeves; at restart it
    is posted exactly once."""
    env = env_(tmp_path, feed=True)
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(500.0, evidence="bank T-9")
    real = sleeves.sync_flows

    def dying(*a, **k):
        raise SimulatedCrash("killed between the deposit flow and the sleeve posting")
    monkeypatch.setattr(sleeves, "sync_flows", dying)
    env.clock.advance(minutes=1)
    with pytest.raises(SimulatedCrash):
        e.reconcile("t", env.clock())
    monkeypatch.setattr(sleeves, "sync_flows", real)
    e2 = env.engine()
    r = ticks(env, e2, 3)
    assert e2.db.one("SELECT COUNT(*) n FROM sleeve_ledger WHERE kind='DEPOSIT'")["n"] == 2       # one per sleeve
    assert e2.db.one("SELECT COUNT(*) n FROM cash_flows WHERE kind='DEPOSIT'")["n"] == 1
    check_capital(e2, r)


def test_duplicate_funding_records_are_booked_once(tmp_path):
    """INVARIANT: a broker statement row returned twice, or a transfer reported twice, books one flow."""
    env = env_(tmp_path, feed=True)
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(150.0, evidence="T-dup")
    tid = env.broker.s["cash_tx"][-1]["tx_id"]
    env.broker.add_tx_record("DEPOSIT", 150.0, duplicate_of=tid)
    r = ticks(env, e, 3)
    assert e.db.one("SELECT COUNT(*) n FROM cash_flows WHERE kind='DEPOSIT'")["n"] == 1 and r.clean
    env.broker.deposit(80.0, record=False)
    env.broker.add_funding_evidence(80.0, "TR-D")
    env.broker.add_funding_evidence(80.0, "TR-D")
    ticks(env, e, 3)
    assert e.db.one("SELECT COUNT(*) n FROM cash_flows WHERE kind='DEPOSIT'")["n"] == 2
    check_capital(e, e.reconcile("t", env.clock()))


def test_deposit_reversal_is_reversed_proportionally(tmp_path):
    """INVARIANT: an evidenced reversal of a deposit removes the same capital from the sleeves (external, not a
    trading loss); net contributions return to their earlier value."""
    env = env_(tmp_path, feed=True)
    e = env.engine()
    env.decide_and_execute()
    n0 = accounting.funding_summary(e.db)["net_contributions"]
    c0 = cash(e)
    env.broker.deposit(400.0, evidence="card deposit C-1")
    ticks(env, e)
    env.broker.adjust_cash(-400.0, kind="DEPOSIT_REVERSAL", evidence="card deposit C-1 reversed")
    r = ticks(env, e)
    assert r.clean and accounting.funding_summary(e.db)["net_contributions"] == pytest.approx(n0, abs=1e-6)
    assert all(cash(e)[s] == pytest.approx(c0[s], abs=1e-6) for s in (A, B))


# ------------------------------------------------------------------------------------------------ prices and P&L
def test_price_moves_change_equity_without_any_funding_event(tmp_path):
    """INVARIANT: equity moving with prices creates no flow, no suspense, no alert; capital still verifies."""
    env = env_(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    n = e.db.one("SELECT COUNT(*) n FROM cash_flows")["n"]
    env.price_all(1.08)
    r = ticks(env, e, 3)
    assert r.clean and e.db.one("SELECT COUNT(*) n FROM cash_flows")["n"] == n
    assert funding.suspense_total(e.db) == 0.0 and halts.open_exceptions(e.db) == []
    check_capital(e, r)


def test_one_sleeve_gains_while_the_other_loses(tmp_path):
    """INVARIANT: #529-only holdings falling and the shared TQQQ rising give opposite sleeve P&L that adds up to the
    account's change."""
    env = env_(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    r0, v0 = rec(e)
    for sym, f in (("TLT", 0.5), ("SQQQ", 0.5), ("TECL", 0.5), ("TQQQ", 1.02)):
        bid, ask = env.broker.s["prices"][str(IDS[sym])]
        env.broker.set_price(IDS[sym], bid * f, ask * f)
    r1, v1 = rec(e)
    da = v1["sleeves"][A]["equity"] - v0["sleeves"][A]["equity"]
    db_ = v1["sleeves"][B]["equity"] - v0["sleeves"][B]["equity"]
    assert da < 0 < db_
    assert da + db_ == pytest.approx(r1.strategy_equity - r0.strategy_equity, abs=1e-4)


# ------------------------------------------------------------------------------------------------ persistence
def test_capital_survives_reboot_without_reset(tmp_path):
    """INVARIANT: a restart (VPS reboot) re-reads the same capital: no second INITIAL_CAPITAL, no re-seeding, the same
    sleeve balances, and capital verifies."""
    env = env_(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    b0 = sleeves.balances(e.db)
    e2 = env.engine()
    r, _ = rec(e2)
    assert sleeves.balances(e2.db) == b0
    assert e2.db.one("SELECT COUNT(*) n FROM cash_flows WHERE kind='INITIAL_CAPITAL'")["n"] == 1
    check_capital(e2, r)


def test_funds_arriving_months_later_follow_the_current_weights(tmp_path):
    """INVARIANT: a deposit 4 months after activation is split 50/50 of the NEW money only; each sleeve keeps its own
    accumulated equity (profits are never redistributed)."""
    env = env_(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    env.price_all(1.3)
    env.clock.advance(days=120)
    r0, v0 = rec(e)
    env.broker.deposit(900.0, record=False)
    env.broker.add_funding_evidence(900.0, "TR-LATER")
    env.clock.advance(minutes=1)
    r1, v1 = rec(e)
    for s in (A, B):
        assert v1["sleeves"][s]["equity"] - v0["sleeves"][s]["equity"] == pytest.approx(450.0, abs=1e-4)
    assert v1["sleeves"][A]["equity"] != pytest.approx(v1["sleeves"][B]["equity"], abs=1.0)
    check_capital(e, r1)


# ------------------------------------------------------------------------------------------------ safety
def test_sale_proceeds_are_never_classified_as_funding(tmp_path, monkeypatch):
    """INVARIANT: a lost close's proceeds (B3) look like an unexplained credit; even a coincidental funding record
    of the same amount is NOT used to classify it while a close's proceeds are unknown."""
    from test_multi_strategy import day2
    env = env_(tmp_path, day2=True)
    e = env.engine()
    env.decide_and_execute()
    monkeypatch.setattr(SimulatedBroker, "closed_position_proceeds", lambda self, pid: None)
    env.broker.inject("timeout_after_accept")
    day2(env, forced={A: {}, B: {"SQQQ": 1.0}})
    assert "CLOSE_PROCEEDS_UNKNOWN" in kinds(e)
    items = funding.open_suspense(e.db)
    assert items, "the lost proceeds must be visible as an unclassified credit"
    env.broker.add_funding_evidence(items[0]["amount"], "TR-COINCIDENCE")
    ticks(env, e, 2)
    assert not e.db.one("SELECT 1 FROM cash_flows WHERE kind='DEPOSIT'")


def test_suspense_is_not_spendable(tmp_path):
    """INVARIANT: funds in suspense are excluded from strategy equity and from every sleeve; the next plan cannot
    size orders with them."""
    env = env_(tmp_path, day2=True)
    e = env.engine()
    env.decide_and_execute()
    env.broker.deposit(5000.0, record=False)
    r = ticks(env, e)
    assert funding.suspense_total(e.db) == 5000.0
    assert r.strategy_equity < 2100
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    env.clock.set(NEXT_EXECUTE)
    e.cycle()
    spent = -sum(x["amount"] for x in e.db.q("SELECT amount FROM cash_flows WHERE kind='TRADE_OPEN'"))
    assert spent < 2100


def test_reallocation_is_not_a_daily_loss(tmp_path):
    """REGRESSION (rehearsal 2026-10-29): after a 50/50 -> 30/70 reallocation the donor sleeve's equity drop is an
    external transfer, not a -40% daily loss: no DAILY_LOSS_LIMIT sleeve halt at the next execution."""
    env = env_(tmp_path, day2=True)
    e = env.engine()
    env.decide_and_execute()
    r, v = rec(e)
    pv = sleeves.reallocation_preview(e.db, {A: 0.3, B: 0.7}, v, e.nows())
    sleeves.reallocate(e.db, pv["preview_hash"], "owner", v, e.nows())
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    env.clock.set(NEXT_EXECUTE)
    e.cycle()
    assert "DAILY_LOSS_LIMIT" not in kinds(e)


def test_opens_are_capped_by_broker_cash_when_a_sleeve_is_negative(tmp_path):
    """REGRESSION (rehearsal 2026-10-29): when one sleeve is negative after a reallocation, the other sleeve's buys
    are scaled to what the broker can fund instead of being blocked; no order exceeds available cash."""
    env = env_(tmp_path, day2=True)
    e = env.engine()
    env.decide_and_execute()
    r, v = rec(e)
    pv = sleeves.reallocation_preview(e.db, {A: 0.3, B: 0.7}, v, e.nows())
    sleeves.reallocate(e.db, pv["preview_hash"], "owner", v, e.nows())
    avail = env.broker.s["credit"]
    env.clock.set(NEXT_DECIDE)
    e.cycle()
    env.clock.set(NEXT_EXECUTE)
    e.cycle()
    plan = json.loads(e.db.one("SELECT plan FROM plans WHERE strategy_id='PORTFOLIO' ORDER BY created_at DESC")["plan"])
    assert not any("unaffordable" in why for _l, why in plan["blocked"])
    r, _ = rec(e)
    assert r.clean and env.broker.s["credit"] >= -1e-6 and avail > 0


# ------------------------------------------------------------------------------------------------ eToro contract
def test_etoro_funding_evidence_contract_and_read_only_allowlist(tmp_path):
    """CONTRACT (documented shapes, api-portal 2026-10-29; not yet observed by this engine): balances give the main
    trading account (available, currentPNL, usedMargin) and the eToro-Money cash account; its settled internal
    transfers become signed trading-account funding evidence; card payments are ignored; every path passes the
    read-only allowlist and no write path exists."""
    import json as _j
    from utengine.brokers.etoro import EtoroReadOnly, check_read_only
    from utengine.core.configuration import load_engine_config
    from utengine.core.errors import ReadOnlyViolation
    cash_id = "3f2b6a1e-1111-4a2b-9c3d-123456789abc"
    calls = []

    def transport(method, url, headers, body):
        path = url.split("public-api.etoro.com")[1]
        calls.append((method, path))
        if path == "/api/v1/me":
            return 200, _j.dumps({"gcid": 1, "scopes": ["etoro-public:money.balance:read",
                                                        "etoro-public:money.cash-transactions:read"]}).encode()
        if path.startswith("/api/v1/balances?"):
            return 200, _j.dumps({"gcid": 1, "balances": [
                {"accountId": "T-1", "accountType": "Trading", "subType": "main", "balance": 1185.0, "currency": "USD",
                 "equityDetails": {"available": 560.0, "frozenCash": 0, "currentPNL": -115.0, "totalUsedMargin": 740.0}},
                {"accountId": "T-SUB", "accountType": "Trading", "subType": "subAccount", "balance": 50},
                {"accountId": cash_id, "accountType": "Cash", "balance": 20}]}).encode()
        if "/transactions" in path:
            return 200, _j.dumps({"results": [
                {"id": "tx-1", "accountId": cash_id, "transactionType": "internalTransfer", "transactionSubtype": "transfer",
                 "direction": "debit", "status": "settled", "amount": "500.00", "currency": "USD",
                 "postedAt": "2026-10-29T09:00:00+00:00", "counterparty": {"name": "eToro Trading", "type": "internal_account"}},
                {"id": "tx-2", "accountId": cash_id, "transactionType": "card", "transactionSubtype": "cardPayment",
                 "direction": "debit", "status": "settled", "amount": "12.00", "currency": "USD",
                 "postedAt": "2026-10-29T08:00:00+00:00", "counterparty": {"name": "shop", "type": "merchant"}},
                {"id": "tx-3", "accountId": cash_id, "transactionType": "internalTransfer", "transactionSubtype": "transfer",
                 "direction": "credit", "status": "settled", "amount": "75.00", "currency": "USD",
                 "postedAt": "2026-10-28T09:00:00+00:00", "counterparty": {"name": "eToro Trading", "type": "internal_account"}}],
                "pagination": {"pageSize": 200, "nextPageToken": None, "hasNext": False}}).encode()
        return 404, b""
    cfg = load_engine_config(write_engine_yaml(tmp_path / "e.yaml", mode="shadow"))
    r = EtoroReadOnly(cfg, transport=transport, env={"UTE_ETORO_READONLY_API_KEY": "k" * 20,
                                                   "UTE_ETORO_READONLY_USER_KEY": "u" * 20})
    b = r.account_balances()
    assert b["trading"]["account_id"] == "T-1" and b["trading"]["current_pnl"] == -115.0
    assert b["cash_account_ids"] == [cash_id]
    ev = r.funding_evidence("2026-10-20T00:00:00+00:00")
    assert sorted((x.tx_id, x.kind, x.amount) for x in ev) == [("tx-1", "DEPOSIT", 500.0), ("tx-3", "WITHDRAWAL", -75.0)]
    assert all(m == "GET" for m, _p in calls)
    for m, p in calls:
        check_read_only(m, p)
    for bad in ("/api/v1/money/accounts/cash/%s/transfers" % cash_id, "/api/v1/transfers"):
        with pytest.raises(ReadOnlyViolation):
            check_read_only("POST", bad)


def test_cross_currency_funding_evidence_is_cited_never_matched(tmp_path):
    """As on the real account: EUR 1,000.00 left eToro Money and USD 1,150.00 arrived in trading; the API gives no
    rate. INVARIANT: evidence in another currency never classifies a credit, even when the numbers coincide; it is
    cited on the suspense item with the rate it would imply, and a USD record still classifies it later."""
    env = env_(tmp_path)
    e = env.engine()
    env.decide_and_execute()
    env.broker.add_funding_evidence(1000.0, "EUR-TR", currency="EUR")
    env.broker.add_funding_evidence(250.0, "EUR-SAME-NUMBER", currency="EUR")
    env.broker.deposit(1150.0, record=False)
    ticks(env, e)
    assert funding.suspense_total(e.db) == 1150.0
    env.broker.deposit(250.0, record=False)                  # same number as a EUR row: still not matched
    ticks(env, e)
    assert funding.suspense_total(e.db) == 1400.0
    items = funding.open_suspense(e.db)
    cited = json.loads(items[0]["detail"])["cross_currency_evidence"]
    assert {c["tx_id"] for c in cited} == {"EUR-TR", "EUR-SAME-NUMBER"}
    assert next(c for c in cited if c["tx_id"] == "EUR-TR")["implied_rate_if_this_alone"] == round(1150.0 / 1000, 6)
    assert not e.db.q("SELECT 1 FROM cash_flows WHERE kind='DEPOSIT'")
    env.broker.add_funding_evidence(1150.0, "USD-TR")
    ticks(env, e)
    assert funding.suspense_total(e.db) == 250.0
