"""rc2 execution reliability: the failures of the first live session (2026-11-02) and the production lessons of Gold
Miner, against the real eToro adapter and a contract fake that reproduces the observed broker behaviour.

2026-11-02: three opens filled; eToro's order lookup named each new position (positionExecutions[]) before the
portfolio listed two of them. rc1 recorded ownership only for listed positions and still marked the orders FILLED;
reconciliation then classified the engine's own TLT and SQQQ as foreign (EXTERNAL_POSITION), took their cost out of
strategy equity (a phantom -378.06), the daily loss stop blocked TECL, the 0.15% opening fees were an unexplained cash
change, and the engine halted ALL. Fixture: tests/fixtures/etoro_order_lookup_2026-11-02.json (sanitized).
"""
import datetime as dt
import json

import pytest

from conftest import ROOT
from test_autonomous_live import controls
from test_etoro_live_contract import GDX_SHORT, PRICES, SQQQ, TLT, TQQQ, Rig, open_leg
from utengine.brokers.etoro import EtoroReadOnly
from utengine.core import attribution, halts, supervision
from utengine.core.accounting import record_flow, resolve_discrepancy
from utengine.core.errors import EngineError
from utengine.core.order_planner import Leg

TECL = 10855
FIX = json.loads((ROOT / "tests" / "fixtures" / "etoro_order_lookup_2026-11-02.json").read_text(encoding="utf-8"))
# the 11-02 plan, rescaled to a USD 2,000 account (12.5 / 12.5 / 20.83 / 33.33 %)
NOV02 = [(0, TLT, "TLT", 250.0), (1, SQQQ, "SQQQ", 250.0), (2, TQQQ, "TQQQ", 416.67), (3, TECL, "TECL", 666.67)]
HIDDEN = 3          # portfolio reads during which a new position is not listed (> the executor's re-read budget)
# 2026-11-02 as observed: after the TLT and SQQQ opens the portfolio kept returning the PRE-open state (cash and
# positions) through the fill check and the next leg's pre-submit read; after TQQQ it was current
STALE_1102 = (HIDDEN + 1, HIDDEN + 1, 0, 0)


def rig_nov02(tmp_path, lag=STALE_1102, fee_bp=15.0, daily_stop=300.0, hide=()):
    rig = Rig(tmp_path)
    rig.fake.prices[TECL] = (167.90, 168.05)
    rig.reader.symbols[TECL] = "TECL"
    rig.fake.portfolio_lag = list(lag)
    rig.fake.visibility_lag = list(hide)
    rig.fake.open_fee_bp = fee_bp
    cfg = {**rig.cfg, "live_controls": controls(tmp_path, daily_loss_stop_usd=daily_stop, max_gross_usd=2000.0),
           "risk": {**rig.cfg["risk"], "max_quote_age_seconds": 10 ** 9, "max_spread_bp": None}}
    rig.guard_cfg = cfg
    rig.ex.supervisor = lambda row, snap: supervision.check(rig.db, cfg, tmp_path / "home", row, snap,
                                                            rig.reader.quotes, rig.clock())
    return rig


def legs(spec=NOV02):
    return [open_leg(i, iid, sym, amt) for i, iid, sym, amt in spec]


def owners(rig):
    return {r["position_id"]: r["owner"] for r in rig.db.q("SELECT position_id, owner FROM ownership")}


def engine_ids(rig):
    return {r["position_id"] for r in rig.db.q("SELECT position_id FROM ownership WHERE owner='ENGINE' AND "
                                                "closed_at IS NULL")}


def statuses(rig):
    return [r["status"] for r in rig.db.q("SELECT status FROM intents ORDER BY leg_index")]


# ------------------------------------------------------------------------------------------------ adapter / fixture
@pytest.mark.parametrize("sym", ["TLT", "SQQQ", "TQQQ"])
def test_real_order_lookup_shape_is_parsed(sym):
    """INVARIANT (R9: the FULL real lookup, sanitized): the position id, filled margin, units, average price and the
    opening fee come from positionExecutions[] / openingData; the three fee fields of the real payload agree."""
    body = FIX["lookups"][sym]
    st = EtoroReadOnly._open_status(body["orderId"], body)
    exp = FIX["expected"][sym]
    assert st.status_id == 3 and list(st.position_ids) == exp["position_ids"]
    assert st.filled_amount == pytest.approx(exp["filled_amount"]) and st.fee == pytest.approx(exp["fee"])
    assert st.filled_units == pytest.approx(exp["units"]) and st.rate == pytest.approx(exp["rate"])
    assert st.executions[0]["position_id"] == exp["position_ids"][0]
    e = body["positionExecutions"][0]
    assert body["totalCosts"] == e["openingData"]["fees"] == pytest.approx(body["frozenAmount"] - e["marginAccountCurrency"])


def _strip_fee_fields(body):
    b = json.loads(json.dumps(body))
    b.pop("totalCosts", None)
    for e in b["positionExecutions"]:
        e.get("openingData", {}).pop("fees", None)
    return b


def test_fee_fallbacks_and_implausible_values_are_never_booked():
    """INVARIANT: without openingData.fees the order's totalCosts is used, then a small non-negative frozenAmount -
    margin; a large or negative difference, or an implausible fee field, is never booked as a cost."""
    base = FIX["lookups"]["TLT"]
    exp = FIX["expected"]["TLT"]["fee"]
    no_exec_fee = json.loads(json.dumps(base))
    no_exec_fee["positionExecutions"][0]["openingData"].pop("fees")
    assert EtoroReadOnly._open_status(1, no_exec_fee).fee == pytest.approx(exp)          # totalCosts
    bare = _strip_fee_fields(base)
    assert EtoroReadOnly._open_status(1, bare).fee == pytest.approx(exp)                 # frozenAmount - margin
    for frozen in (240.0, 400.0):
        assert EtoroReadOnly._open_status(1, {**bare, "frozenAmount": frozen}).fee is None
    assert EtoroReadOnly._open_status(1, {k: v for k, v in bare.items() if k != "frozenAmount"}).fee is None
    huge = json.loads(json.dumps(base))
    huge["positionExecutions"][0]["openingData"]["fees"] = 100.0
    assert EtoroReadOnly._open_status(1, huge).fee is None


def test_portfolio_order_id_is_parsed(tmp_path):
    """INVARIANT: the portfolio's positions[].orderID (observed on the real account 2026-11-03) is parsed: it names the
    order that opened each position (used by late attribution and by the repair)."""
    rig = rig_nov02(tmp_path, lag=(), fee_bp=0.0)
    rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))
    p = next(x for x in rig.reader.account_snapshot().positions if x.instrument_id == TLT)
    oid = rig.db.one("SELECT broker_order_id FROM intents")["broker_order_id"]
    assert p.order_id == str(oid)


# ------------------------------------------------------------------------------------------------ the 11-02 replay
def test_nov02_replay_delayed_visibility_is_attributed_fees_booked_and_no_phantom_loss(tmp_path):
    """REGRESSION (2026-11-02): TLT and SQQQ are not listed by the portfolio when their lookups say Filled.
    INVARIANT: all four legs are sent and FILLED; every new position is ENGINE-owned (none EXTERNAL); the daily loss
    stop does not fire (no phantom loss); the opening fees are booked so the cash reconciliation is CLEAN; strategy
    equity = cash + all four positions."""
    rig = rig_nov02(tmp_path)
    for _ in range(12):                                  # engine cycles inside the execution window
        out = rig.run("P1102", legs())
        if out["status"] not in ("HELD", "HALTED_RECONCILIATION"):
            break
        rig.clock.advance(seconds=60)
    assert out["phase2"] == ["FILLED"] * 4, (out, statuses(rig))
    assert len(rig.fake.posts("orders")) == 4
    new = [str(p) for p in rig.fake.positions if p != GDX_SHORT["positionID"]]
    assert all(owners(rig)[p] == "ENGINE" for p in new) and len(new) == 4
    assert not rig.db.q("SELECT 1 FROM intents WHERE status='BLOCKED'")
    r = rig.recon()
    assert r.clean, r.issues
    assert r.unexplained_cash == pytest.approx(0.0, abs=0.01)
    fees = rig.db.one("SELECT COALESCE(SUM(amount),0) s FROM cash_flows WHERE kind='FEE'")["s"]
    assert fees == pytest.approx(-sum(round(a * 15e-4, 2) for *_, a in NOV02), abs=0.011)
    assert r.strategy_equity == pytest.approx(r.snapshot.available_cash + sum(p.value for p in r.engine_positions))
    assert {p.position_id for p in r.engine_positions} == set(new)
    assert not attribution.markers(rig.db, attribution.AWAIT_VISIBLE)
    # no open was sent on an account read that predates an earlier fill (its cash and equity could not be judged)
    sent = rig.db.q("SELECT i.intent_id, i.pre_submit, e.ts FROM intents i JOIN order_events e USING(intent_id) "
                    "WHERE e.to_status='SUBMITTING' ORDER BY e.id")
    for k, row in enumerate(sent):
        listed = set(json.loads(row["pre_submit"])["positions"])
        earlier = {r["position_id"] for r in rig.db.q(
            "SELECT o.position_id FROM ownership o JOIN intents i ON o.intent_id=i.intent_id WHERE i.intent_id IN "
            f"({','.join('?' * k)})", tuple(x["intent_id"] for x in sent[:k]))} if k else set()
        assert earlier <= listed, (row["intent_id"], earlier - listed)
    assert rig.db.q("SELECT 1 FROM audit_log WHERE kind='ORDER_HELD' AND payload LIKE '%waiting for the broker%'")


def test_rc1_behaviour_is_what_failed(tmp_path, monkeypatch):
    """The replay above, with rc1's fill booking (ownership only for listed positions, FILLED regardless) and no late
    attribution, reproduces production: TECL BLOCKED by the daily loss stop, two EXTERNAL_POSITION halts and a cash
    discrepancy. Guards the fixture: if this stops failing the old way, the replay no longer reproduces 11-02."""
    rig = rig_nov02(tmp_path, fee_bp=15.0)

    def rc1_apply_fill(row, st):
        snap = rig.reader.account_snapshot()
        live = {p.position_id: p for p in snap.positions}
        for pid in st.position_ids:
            p = live.get(pid)
            if p is not None:
                attribution.record_open(rig.db, row, pid, instrument_id=p.instrument_id, amount=p.amount,
                                        units=p.units, fee=None, visible=True, now=rig.ex.now(), evidence="rc1")
        return True
    monkeypatch.setattr(rig.ex, "apply_fill", rc1_apply_fill)
    monkeypatch.setattr(attribution, "late_attribute", lambda *a, **k: [])
    monkeypatch.setattr(supervision, "unattributed", lambda *a, **k: [])
    out = rig.run("P1102", legs())
    assert out["phase2"][:3] == ["FILLED"] * 3 and out["phase2"][3] == "BLOCKED"
    blocked = rig.db.one("SELECT detail FROM order_events WHERE to_status='BLOCKED'")["detail"]
    assert "daily loss stop" in blocked
    r = rig.recon()
    kinds = sorted(i["kind"] for i in r.issues)
    assert kinds == ["CASH_UNEXPLAINED", "EXTERNAL_POSITION", "EXTERNAL_POSITION"], kinds


def test_filled_order_without_position_ids_is_not_filled_until_attributable(tmp_path):
    """INVARIANT: a lookup that says Filled but names no position (the shape was unverified before 11-02) never makes
    the intent FILLED without ownership: it stays PENDING (halt UNRESOLVED_ORDER) and resolves to FILLED from the
    single matching new position once the portfolio lists it -- never resubmitted."""
    rig = rig_nov02(tmp_path, lag=(), hide=(20,), fee_bp=0.0)
    rig.fake.omit_execution_ids = True
    out = rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))
    assert out["status"] == "HALTED_PHASE2" and statuses(rig) == ["PENDING"]
    assert not engine_ids(rig)
    rig.fake.hidden.clear()
    rig.restart()
    assert rig.ex.resolve_all() and statuses(rig) == ["FILLED"]
    assert len(rig.fake.posts("orders")) == 1 and len(engine_ids(rig)) == 1


def test_position_never_listed_counts_at_cost_then_becomes_missing_after_the_grace(tmp_path):
    """INVARIANT: recorded from its order but not listed: within the grace it is no reconciliation issue and counts
    at cost in strategy equity and exposure; after the grace its absence is POSITION_MISSING (fail closed)."""
    rig = rig_nov02(tmp_path, lag=(), hide=(10 ** 6,), fee_bp=0.0)
    rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))
    assert statuses(rig) == ["FILLED"]
    r = rig.recon()
    assert [i["kind"] for i in r.issues] == ["AWAITING_VISIBILITY"] and not halts.active(rig.db)
    assert r.strategy_equity == pytest.approx(r.snapshot.available_cash + 250.0)
    snap = rig.reader.account_snapshot()
    assert supervision._strategy_equity(rig.db, snap) == pytest.approx(snap.credit + 250.0)
    assert supervision._engine_exposure(rig.db, snap) == pytest.approx(250.0)
    rig.clock.advance(seconds=attribution.VISIBILITY_GRACE_S + 1)
    assert "POSITION_MISSING" in {i["kind"] for i in rig.recon().issues}


def test_late_attribution_from_order_evidence_and_a_true_foreign_position_stays_foreign(tmp_path, monkeypatch):
    """INVARIANT: a listed position with no ownership row is the engine's ONLY if an engine order's lookup lists it;
    a position opened outside the engine is still EXTERNAL_POSITION (no attribution by instrument or amount)."""
    rig = rig_nov02(tmp_path, lag=(), fee_bp=0.0)
    rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))
    pid = next(iter(engine_ids(rig)))
    with rig.db.tx():                                     # the state a crash between fill and booking leaves
        rig.db.con.execute("DELETE FROM ownership WHERE position_id=?", (pid,))
        rig.db.con.execute("DELETE FROM cash_flows WHERE ref=?", (f"open:{pid}",))
    ext = rig.fake.seq_pos
    rig.fake.seq_pos += 1
    rig.fake.positions[ext] = {"positionID": ext, "instrumentID": TLT, "isBuy": True, "units": 3.0, "amount": 245.0,
                               "leverage": 1, "openRate": 81.7, "totalFees": 0.0, "openDateTime": "x"}
    rig.fake.credit -= 245.0
    r = rig.recon()
    assert owners(rig)[pid] == "ENGINE"
    assert owners(rig)[str(ext)] == "EXTERNAL_UNACK"
    assert [i["subject"] for i in r.issues if i["kind"] == "EXTERNAL_POSITION"] == [str(ext)]


def test_open_waits_while_an_unattributed_position_is_listed_and_closes_are_not_blocked(tmp_path):
    """INVARIANT (Gold Miner's rule: block risk-INCREASING actions while exposure is unproven): when a position of
    unknown owner is listed at send time an open is HELD (retried in this window, not BLOCKED for the day), and the
    gross-exposure figure includes that position; a close is not held for it."""
    rig = rig_nov02(tmp_path, lag=(), fee_bp=0.0)
    rig.run("P1", legs([(0, TQQQ, "TQQQ", 300.0)]))
    pid = next(iter(engine_ids(rig)))
    ext = rig.fake.seq_pos
    rig.fake.seq_pos += 1
    rig.fake.positions[ext] = {"positionID": ext, "instrumentID": TLT, "isBuy": True, "units": 3.0, "amount": 245.0,
                               "leverage": 1, "openRate": 81.7, "totalFees": 0.0, "openDateTime": "x"}
    snap = rig.reader.account_snapshot()
    now = rig.clock()
    home = tmp_path / "home"
    kind, why = supervision.check(rig.db, rig.guard_cfg, home, {"intent_id": "Iopen", "action": "OPEN",
                                  "amount": 100.0, "instrument_id": SQQQ}, snap, rig.reader.quotes, now)
    assert kind == supervision.HOLD and str(ext) in why
    assert supervision._engine_exposure(rig.db, snap) >= 245.0 + 290.0
    kind, why = supervision.check(rig.db, rig.guard_cfg, home, {"intent_id": "Iclose", "action": "CLOSE",
                                  "amount": 300.0, "instrument_id": TQQQ, "position_id": pid}, snap,
                                  rig.reader.quotes, now)
    assert kind is None, why


# ------------------------------------------------------------------------------------------------ closes
def test_filled_close_with_a_stale_portfolio_closes_the_ownership(tmp_path):
    """REGRESSION (same race, close side; Gold Miner confirmed closes by re-reading until the position was gone):
    INVARIANT: a filled full close the portfolio still lists is closed in the engine's books (proceeds booked, no
    POSITION_MISSING later); if the broker still lists it after the grace it is POSITION_MISMATCH."""
    rig = rig_nov02(tmp_path, lag=(), fee_bp=0.0)
    rig.run("P1", legs([(0, TQQQ, "TQQQ", 300.0)]))
    pid = next(iter(engine_ids(rig)))
    rig.fake.close_lag = 6
    out = rig.run("P2", [Leg(0, 1, "CLOSE", "TQQQ", TQQQ, 300.0, None, pid, "test")])
    assert out["phase1"] == ["FILLED"]
    row = rig.db.one("SELECT closed_at FROM ownership WHERE position_id=?", (pid,))
    assert row["closed_at"] is not None and pid not in engine_ids(rig)
    assert rig.db.one("SELECT COUNT(*) c FROM cash_flows WHERE kind='TRADE_CLOSE'")["c"] == 1
    assert rig.recon().clean                              # still listed, inside the grace
    rig.fake.close_lag = 0
    rig.fake.ghosts.clear()
    r = rig.recon()
    assert r.clean, r.issues
    assert not attribution.markers(rig.db, attribution.AWAIT_ABSENT)
    # a close that never disappears
    rig.run("P3", legs([(0, TQQQ, "TQQQ", 300.0)]))
    pid2 = next(iter(engine_ids(rig)))
    rig.fake.close_lag = 10 ** 6
    rig.run("P4", [Leg(0, 1, "CLOSE", "TQQQ", TQQQ, 300.0, None, pid2, "test")])
    rig.clock.advance(seconds=attribution.VISIBILITY_GRACE_S + 1)
    assert "POSITION_MISMATCH" in {i["kind"] for i in rig.recon().issues}


def test_restart_between_fill_and_booking_books_once(tmp_path, monkeypatch):
    """INVARIANT: a crash after the fill is booked but before FILLED is recorded resolves on restart from the order id
    without a second order, a second ownership row or a second cash flow."""
    rig = rig_nov02(tmp_path, lag=(), hide=(HIDDEN,), fee_bp=15.0)
    real = rig.ex.apply_fill

    def crash_after(row, st):
        real(row, st)
        raise KeyboardInterrupt("process killed")
    monkeypatch.setattr(rig.ex, "apply_fill", crash_after)
    with pytest.raises(KeyboardInterrupt):
        rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))
    rig.restart()
    assert rig.ex.resolve_all() and statuses(rig) == ["FILLED"]
    assert len(rig.fake.posts("orders")) == 1
    assert rig.db.one("SELECT COUNT(*) c FROM ownership WHERE owner='ENGINE' AND position_id<>?",
                      (str(GDX_SHORT["positionID"]),))["c"] == 1
    assert rig.db.one("SELECT COUNT(*) c FROM cash_flows WHERE kind IN ('TRADE_OPEN','FEE')")["c"] == 2
    assert rig.recon().clean


# ------------------------------------------------------------------------------------------------ repair tooling
def production_state(tmp_path, monkeypatch):
    """The production state after 11-02: rc1 booking + rc1 reconciliation (no late attribution)."""
    rig = rig_nov02(tmp_path)

    def rc1_apply_fill(row, st):
        live = {p.position_id: p for p in rig.reader.account_snapshot().positions}
        for pid in st.position_ids:
            if pid in live:
                p = live[pid]
                attribution.record_open(rig.db, row, pid, instrument_id=p.instrument_id, amount=p.amount,
                                        units=p.units, fee=None, visible=True, now=rig.ex.now(), evidence="rc1")
        return True
    with monkeypatch.context() as m:
        m.setattr(rig.ex, "apply_fill", rc1_apply_fill)
        m.setattr(attribution, "late_attribute", lambda *a, **k: [])
        m.setattr(supervision, "unattributed", lambda *a, **k: [])
        rig.run("P1102", legs())
        rig.recon()
    ext = [pid for pid, o in owners(rig).items() if o == "EXTERNAL_UNACK"]
    assert len(ext) == 2
    return rig, ext


def intent_for(rig, pid):
    inst = rig.db.one("SELECT instrument_id FROM ownership WHERE position_id=?", (pid,))["instrument_id"]
    return rig.db.one("SELECT intent_id FROM intents WHERE instrument_id=?", (inst,))["intent_id"]


def test_repair_reattributes_both_positions_and_reconciliation_becomes_clean(tmp_path, monkeypatch):
    """INVARIANT: attribute-fill turns each misattributed position back into ENGINE from the broker's order evidence;
    the EXTERNAL_TRADE booking is reversed against its original snapshot (no new cash change); after the fee
    discrepancy is classified FEE and the halts are cleared, reconciliation is CLEAN and strategy equity includes all
    three positions. Nothing is sent to the broker."""
    rig, ext = production_state(tmp_path, monkeypatch)
    posts = len(rig.fake.posts())
    for pid in ext:
        plan = attribution.repair_plan(rig.db, rig.reader, pid, intent_for(rig, pid))
        attribution.repair_apply(rig.db, plan, "owner", "11-02 delayed-visibility fill", rig.clock().isoformat())
    assert all(owners(rig)[p] == "ENGINE" for p in ext)
    disc = rig.db.one("SELECT disc_id FROM discrepancies WHERE status='OPEN'")["disc_id"]
    resolve_discrepancy(rig.db, disc, "FEE", "eToro opening fees (frozenAmount - margin per order)", "owner",
                        rig.clock().isoformat())
    for h in halts.active(rig.db):
        halts.clear(rig.db, h["halt_id"], "owner", "repaired", rig.clock().isoformat())
    r = rig.recon()
    assert r.clean, r.issues
    assert len(r.engine_positions) == 3
    assert r.strategy_equity == pytest.approx(r.snapshot.available_cash + sum(p.value for p in r.engine_positions))
    assert len(rig.fake.posts()) == posts


@pytest.mark.parametrize("case", ["wrong_intent", "acknowledged", "twice", "lookup_does_not_list",
                                  "portfolio_names_another_order"])
def test_repair_refusals(tmp_path, monkeypatch, case):
    """INVARIANT: the repair needs broker order evidence for THAT position and an unacknowledged row; it refuses a
    mismatched intent, an acknowledged position, a second application, and a lookup that does not list it."""
    rig, ext = production_state(tmp_path, monkeypatch)
    pid = ext[0]
    iid = intent_for(rig, pid)
    if case == "wrong_intent":
        other = intent_for(rig, ext[1])                   # the OTHER misattributed fill's intent
        with pytest.raises(EngineError, match="instrument"):
            attribution.repair_plan(rig.db, rig.reader, pid, other)
    elif case == "acknowledged":
        from utengine.core.reconciliation import acknowledge_external
        acknowledge_external(rig.db, pid, "owner", "test", rig.clock().isoformat())
        with pytest.raises(EngineError, match="EXTERNAL_ACK"):
            attribution.repair_plan(rig.db, rig.reader, pid, iid)
    elif case == "twice":
        plan = attribution.repair_plan(rig.db, rig.reader, pid, iid)
        attribution.repair_apply(rig.db, plan, "owner", "once", rig.clock().isoformat())
        with pytest.raises(EngineError, match="ENGINE"):
            attribution.repair_plan(rig.db, rig.reader, pid, iid)
        with pytest.raises(EngineError, match="changed"):
            attribution.repair_apply(rig.db, plan, "owner", "twice", rig.clock().isoformat())
    elif case == "portfolio_names_another_order":
        rig.fake.positions[int(pid)]["orderID"] = 1
        with pytest.raises(EngineError, match="opened by order 1"):
            attribution.repair_plan(rig.db, rig.reader, pid, iid)
    else:
        oid = int(rig.db.one("SELECT broker_order_id FROM intents WHERE intent_id=?", (iid,))["broker_order_id"])
        rig.fake.orders[oid]["execs"] = []
        with pytest.raises(EngineError, match="does not list"):
            attribution.repair_plan(rig.db, rig.reader, pid, iid)


def test_repair_keeps_the_sleeve_ledger_consistent_in_portfolio_mode(tmp_path, monkeypatch):
    """Production runs the multi-sleeve portfolio. The engine reproduces rc1's outcome for its largest open (booking
    suppressed, no late attribution -> EXTERNAL_UNACK, EXTERNAL_TRADE posted to the sleeve). INVARIANT: after the
    repair the sleeve owns the broker's cash and the units of every engine position again (including the repaired
    one), the sleeve invariants hold, no capital catch-up is requested, and after halt-clear reconciliation is CLEAN."""
    from test_autonomous_live import A, auto_env
    from utengine.core import sleeves
    from utengine.core.reconciliation import engine_position_ids
    good = auto_env(tmp_path / "good")
    good.decide_and_execute()                             # only to learn which symbol the plan buys most of
    target = good.e.db.one("SELECT symbol FROM ownership WHERE owner='ENGINE' AND intent_id IS NOT NULL "
                           "ORDER BY amount DESC LIMIT 1")["symbol"]
    bad = auto_env(tmp_path / "bad")
    real = attribution.record_open
    with monkeypatch.context() as m:
        m.setattr(attribution, "record_open", lambda db, row, pid, **k: False if row["symbol"] == target
                  else real(db, row, pid, **k))
        m.setattr(attribution, "late_attribute", lambda *a, **k: [])
        m.setattr(supervision, "unattributed", lambda *a, **k: [])
        bad.decide_and_execute()
    e, db = bad.e, bad.e.db
    pid = db.one("SELECT position_id FROM ownership WHERE owner='EXTERNAL_UNACK'")["position_id"]
    iid = db.one("SELECT intent_id FROM intents WHERE symbol=? AND action='OPEN'", (target,))["intent_id"]
    assert db.q("SELECT 1 FROM sleeve_ledger WHERE kind='EXTERNAL_TRADE'")
    from utengine.cli import main as cli
    args = ["--config", str(bad.cfg_path), "--home", str(bad.home), "--now", e.nows(), "account", "attribute-fill",
            "--position-id", pid, "--intent", iid, "--operator", "owner", "--note", "11-02 delayed visibility"]
    assert cli(args) == 0                                  # dry run: nothing changes
    assert db.one("SELECT owner FROM ownership WHERE position_id=?", (pid,))["owner"] == "EXTERNAL_UNACK"
    assert cli(args + ["--apply"]) == 0
    assert db.one("SELECT owner FROM ownership WHERE position_id=?", (pid,))["owner"] == "ENGINE"
    assert cli(args + ["--apply"]) == 1                    # a second application is refused
    plan = json.loads(db.one("SELECT payload FROM audit_log WHERE kind='ATTRIBUTION_REPAIRED'")["payload"])
    snap = e.broker.account_snapshot()
    got = sleeves.balances(db)[A]
    assert got["cash"] == pytest.approx(snap.credit, abs=1e-6)          # one sleeve: it owns all the cash again
    assert got["units"] == pytest.approx({p.instrument_id: p.units for p in snap.positions
                                          if p.position_id in engine_position_ids(db)})
    assert int(plan["instrument_id"]) in got["units"]
    assert sleeves.check_invariants(db, e.broker.account_snapshot(), engine_position_ids(db)) == []
    assert sleeves.catch_up_pending(db, A) is None
    for h in halts.active(db):
        halts.clear(db, h["halt_id"], "owner", "repaired", e.nows())
    r = e.reconcile("check", e.now())
    assert r.clean, r.issues


# ------------------------------------------------------------------------------------------------ R5 / R6
def test_r5_rate_limited_order_is_retried_in_the_window_without_a_duplicate(tmp_path):
    """INVARIANT (R5): an HTTP 429 on an order creates no order; the intent stays retryable (RATE_LIMITED, the plan is
    retried next cycle) instead of REJECTED for the session, and is then sent exactly once more."""
    rig = rig_nov02(tmp_path, lag=(), fee_bp=0.0)
    rig.fake.inject("http429@open")
    out = rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))
    assert out["status"] == "RATE_LIMITED" and statuses(rig) == ["RATE_LIMITED"]
    assert not rig.fake.orders and not halts.active(rig.db)
    rig.clock.advance(seconds=60)
    out = rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))
    assert out["phase2"] == ["FILLED"] and len(rig.fake.orders) == 1 and len(engine_ids(rig)) == 1


def test_r5_rate_limit_budget_then_rejected_and_window_end_abandons(tmp_path):
    """INVARIANT (R5): the retry budget is bounded (then REJECTED, nothing at the broker); a RATE_LIMITED intent whose
    window has passed is ABANDONED, never sent later; a close is retried the same way and is never duplicated."""
    from utengine.core.execution import RATE_LIMIT_BUDGET
    rig = rig_nov02(tmp_path, lag=(), fee_bp=0.0)
    rig.fake.inject(*["http429@open"] * RATE_LIMIT_BUDGET)
    for _ in range(RATE_LIMIT_BUDGET):
        rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))
    assert statuses(rig) == ["REJECTED"] and not rig.fake.orders
    rig.fake.inject("http429@open")
    rig.run("P2", legs([(0, SQQQ, "SQQQ", 200.0)]))
    assert rig.db.one("SELECT status FROM intents WHERE plan_id='P2'")["status"] == "RATE_LIMITED"
    assert rig.ex.abandon_stale(set()) == 1
    assert rig.db.one("SELECT status FROM intents WHERE plan_id='P2'")["status"] == "ABANDONED"
    rig.run("P3", legs([(0, TQQQ, "TQQQ", 300.0)]))
    pid = next(iter(engine_ids(rig)))
    rig.fake.inject("http429@close")
    close = [Leg(0, 1, "CLOSE", "TQQQ", TQQQ, 300.0, None, pid, "test")]
    assert rig.run("P4", close)["status"] == "RATE_LIMITED"
    assert rig.run("P4", close)["phase1"] == ["FILLED"]
    assert len(rig.fake.posts("market-close-orders")) == 2 and len(rig.fake.close_orders) == 1


def _check(rig, tmp_path, row):
    return supervision.check(rig.db, rig.guard_cfg, tmp_path / "home", row, rig.reader.account_snapshot(),
                             rig.reader.quotes, rig.clock())


def test_r6_daily_loss_baseline_never_self_anchors_mid_session(tmp_path):
    """INVARIANT (R6, Gold Miner's rule): with no recorded close, an engine that already holds positions has no
    daily-loss baseline and holds new opens (it does not anchor at a read that may contain the day's loss); with a
    recorded close the day starts at that close and external flows after it are not counted as a loss."""
    rig = rig_nov02(tmp_path, lag=(), fee_bp=0.0)
    rig.run("P1", legs([(0, TLT, "TLT", 250.0)]))          # flat start: anchored before anything was sent
    assert rig.db.get(f"pilot:day_start:{rig.clock().date().isoformat()}")["basis"].startswith("flat start")
    rig.clock.advance(days=1)                               # next session, no close recorded, holding TLT
    kind, why = _check(rig, tmp_path, {"intent_id": "I1", "action": "OPEN", "amount": 100.0, "instrument_id": SQQQ})
    assert kind == supervision.HOLD and "no daily-loss baseline" in why
    rig.clock.advance(days=1)
    session = rig.clock().date().isoformat()
    prev = (rig.clock() - dt.timedelta(days=1)).date().isoformat()
    eq = supervision._strategy_equity(rig.db, rig.reader.account_snapshot())
    rig.db.x("INSERT INTO daily_equity(session, ts, account_equity, strategy_equity, credit, external_flow, pnl, "
             "twr_index, hwm, drawdown, exposure, strategy_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
             (prev, (rig.clock() - dt.timedelta(hours=6)).isoformat(), eq, eq + 500.0, 0, 0, 0, 1, 1, 0, "{}", None))
    kind, why = _check(rig, tmp_path, {"intent_id": "I2", "action": "OPEN", "amount": 100.0, "instrument_id": SQQQ})
    assert kind == "block" and "daily loss stop" in why    # -500 against the recorded close
    day = rig.db.get(f"pilot:day_start:{session}")
    assert day["basis"] == f"close {prev}"
    rig.db.put(f"pilot:day_start:{session}", None)
    record_flow(rig.db, "WITHDRAWAL", -500.0, source="test", evidence="owner withdrawal after the close",
                ref="w1", now=(rig.clock() - dt.timedelta(hours=1)).isoformat())
    rig.db.x("DELETE FROM engine_state WHERE key=?", (f"pilot:day_start:{session}",))
    kind, why = _check(rig, tmp_path, {"intent_id": "I3", "action": "OPEN", "amount": 100.0, "instrument_id": SQQQ})
    assert kind is None, why                               # the withdrawal after the close is not a loss
