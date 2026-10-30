"""Phase 2: the eToro live-order adapter -- authorization boundary and contract tests against a fake eToro API.

The executor, the read-only adapter, the live writer (contract-test host only) and reconciliation run together.
The real account's external GDX CFD short (Gold Miner) is present in the fake and must stay PRE_EXISTING and
untouched in every scenario.
"""
import datetime as dt

import pytest

from conftest import Clock, write_engine_yaml
from fake_etoro import GDX_SHORT, FakeEtoro
from utengine.brokers.etoro import EtoroReadOnly, check_read_only
from utengine.brokers.etoro_live import (CONTRACT_TEST_BASE, PRODUCTION_BASE, EtoroLiveWriter, check_write_allowed,
                                         request_id_for)
from utengine.brokers.simulated import SimulatedCrash
from utengine.core import halts, security
from utengine.core.configuration import load_engine_config
from utengine.core.errors import LiveExecutionNotAvailable, ReadOnlyViolation
from utengine.core.execution import Executor
from utengine.core.order_planner import Leg
from utengine.core.reconciliation import reconcile
from utengine.persistence.db import DB

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 27, 13, 35, tzinfo=UTC)
ENV = {"UTE_ETORO_READONLY_API_KEY": "contract-read-key-1", "UTE_ETORO_READONLY_USER_KEY": "contract-read-user-1"}
TQQQ, SQQQ, TLT, GDX = 4465, 4459, 3020, 3002
PRICES = {TQQQ: (68.40, 68.42), SQQQ: (38.37, 38.39), TLT: (81.70, 81.72)}


class Rig:
    def __init__(self, tmp_path, credit=2000.0):
        self.clock = Clock(NOW)
        self.cfg = load_engine_config(write_engine_yaml(tmp_path / "config" / "engine.yaml", mode="shadow"))
        self.fake = FakeEtoro(credit=credit, prices=PRICES, clock=self.clock)
        self.db = DB(tmp_path / "engine.sqlite")
        self.reader = EtoroReadOnly(self.cfg, transport=self.fake, env=ENV)
        self.reader.symbols.update({TQQQ: "TQQQ", SQQQ: "SQQQ", TLT: "TLT", GDX: "GDX"})
        self.restart()
        assert self.recon("bootstrap").status in ("CLEAN", "HALT")

    def restart(self):
        writer = EtoroLiveWriter(transport=self.fake, base=CONTRACT_TEST_BASE)
        self.ex = Executor(self.db, self.reader, writer, self.cfg, "contract-test", self.clock, sleep=lambda s: None)

    def recon(self, point="check"):
        return reconcile(self.db, self.reader, self.cfg, point, self.clock().isoformat())

    def run(self, plan_id, legs):
        return self.ex.execute(plan_id, legs, "QC_60_SQQQ", lambda p: self.recon(p))

    def owner(self, pid):
        r = self.db.one("SELECT owner FROM ownership WHERE position_id=?", (str(pid),))
        return r["owner"] if r else None

    def kinds(self):
        return {h["kind"] for h in halts.active(self.db)}


def open_leg(i, iid, sym, amount):
    return Leg(i, 2, "OPEN", sym, iid, amount, None, None, "contract")


def assert_gm_untouched(rig):
    assert rig.owner(GDX_SHORT["positionID"]) == "PRE_EXISTING"
    assert rig.fake.positions[GDX_SHORT["positionID"]]["units"] == GDX_SHORT["units"]
    assert not any(str(GDX_SHORT["positionID"]) in r[1] for r in rig.fake.posts())


# ----------------------------------------------------------------------------------------------- authorization
def test_live_writer_cannot_target_production_without_authorization():
    """INVARIANT: the production writer requires a LiveAuthorization, which only authorize_live / authorize_supervised
    can create; a non-live configuration is refused before anything else is looked at."""
    with pytest.raises(LiveExecutionNotAvailable):
        EtoroLiveWriter(transport=lambda *a: (200, b"{}"), base=PRODUCTION_BASE)
    with pytest.raises(LiveExecutionNotAvailable):
        security.LiveAuthorization("x", "y", "z", "w")
    with pytest.raises(LiveExecutionNotAvailable, match="not a live configuration"):
        security.authorize_live(release_sha="x", config={"runtime": {"mode": "shadow"}, "security": {}},
                                activation_path="nope", kill_switch_paths=[], env={}, now=NOW,
                                scope_probe=lambda a, u: [])


def test_opening_the_code_gate_alone_is_not_enough(tmp_path):
    """INVARIANT (autonomous-live release): the code gate is open, yet a shadow configuration, a missing activation
    file and a missing trading key are each refused (the full gate matrix is in tests/test_autonomous_live.py)."""
    cfg = load_engine_config(write_engine_yaml(tmp_path / "e.yaml", mode="shadow"))
    with pytest.raises(LiveExecutionNotAvailable, match="not a live configuration"):
        security.authorize_live(release_sha="abc", config=cfg, activation_path=tmp_path / "a.json",
                                kill_switch_paths=[tmp_path / "KILL"], env={}, now=NOW, scope_probe=lambda a, u: [])
    live = {**cfg, "runtime": {**cfg["runtime"], "mode": "live"}, "security": {**cfg["security"],
            "live_execution_enabled": True}, "live_controls": {"forbidden_hashes_file": str(tmp_path / "fb")}}
    with pytest.raises(LiveExecutionNotAvailable, match="no owner approval file"):
        security.authorize_live(release_sha="abc", config=live, activation_path=tmp_path / "a.json",
                                kill_switch_paths=[tmp_path / "KILL"], env={}, now=NOW, scope_probe=lambda a, u: [])


def test_writer_allowlist_and_engine_never_gets_it():
    """INVARIANT: the writer can only POST the two order routes; the engine's obtain_writer never returns it."""
    with pytest.raises(ReadOnlyViolation):
        check_write_allowed("POST", "/api/v1/money/transfers")
    with pytest.raises(ReadOnlyViolation):
        check_write_allowed("DELETE", "/api/v2/trading/execution/orders/1")
    with pytest.raises(ReadOnlyViolation):
        check_read_only("POST", "/api/v2/trading/execution/orders")
    assert security.obtain_writer("shadow", object()) is None
    with pytest.raises(LiveExecutionNotAvailable):
        security.obtain_writer("live", object())


# ----------------------------------------------------------------------------------------------- execution
def test_c01_success_documented_request_and_reconciliation(tmp_path):
    """INVARIANT: an open sends exactly the documented body with x-request-id = uuid5(intent); the fill is attributed
    from positionExecutions; reconciliation is CLEAN; the GM short stays PRE_EXISTING and untouched."""
    rig = Rig(tmp_path)
    out = rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert out["status"] == "DONE" and out["phase2"] == ["FILLED"]
    (_m, path, rid, body), = rig.fake.posts()
    iid = rig.db.one("SELECT intent_id FROM intents")["intent_id"]
    assert rid == request_id_for(iid)
    assert body == {"action": "open", "transaction": "buy", "instrumentId": TQQQ, "settlementType": "cfd",
                    "orderType": "mkt", "amount": 1000.0, "orderCurrency": "usd"}
    r = rig.recon()
    assert r.clean, r.issues
    assert r.strategy_equity == pytest.approx(2000.0 - 1000.0 + sum(p.value for p in r.engine_positions))
    assert_gm_untouched(rig)


@pytest.mark.parametrize("fault,expected", [("reject:746@open", "REJECTED"), ("http400@open", "REJECTED")])
def test_c02_rejection(tmp_path, fault, expected):
    """INVARIANT: an accepted-then-rejected order and a definite HTTP 4xx both end REJECTED, move no cash, are never
    retried."""
    rig = Rig(tmp_path)
    rig.fake.inject(fault)
    out = rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert out["phase2"] == [expected] and rig.fake.credit == 2000.0
    rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert len(rig.fake.posts()) == 1 and rig.recon().clean


def test_c03_partial_fill_then_complete(tmp_path):
    """INVARIANT: PartiallyFilled (5) is tracked; the rest is attributed when the broker completes; one POST."""
    rig = Rig(tmp_path)
    rig.fake.inject("partial@open")
    out = rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert out["status"] == "HALTED_PHASE2" and "UNRESOLVED_ORDER" in rig.kinds()
    rig.fake.advance()
    assert rig.ex.resolve_all() == {rig.db.one("SELECT intent_id FROM intents")["intent_id"]: "FILLED"}
    engine_pos = [p for p in rig.fake.positions.values() if p["instrumentID"] == TQQQ]
    assert len(engine_pos) == 2 and abs(sum(p["amount"] for p in engine_pos) - 1000.0) < 0.02
    assert len(rig.fake.posts()) == 1
    r = rig.recon()
    assert not [i for i in r.issues if i["kind"] != "UNRESOLVED_ORDER"]


def test_c03b_canceled_partially_filled_is_final(tmp_path):
    """INVARIANT: CanceledPartiallyFilled (9) is final: the executed part is owned and booked, the intent CANCELED."""
    rig = Rig(tmp_path)
    rig.fake.inject("partial_cancel@open")
    out = rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert out["phase2"] == ["CANCELED"]
    assert rig.recon().clean
    assert rig.db.one("SELECT COUNT(*) c FROM ownership WHERE owner='ENGINE'")["c"] == 1


@pytest.mark.parametrize("fault", ["lose_response@open", "http500_after@open"])
def test_c04_timeout_after_acceptance_resolved_by_reference(tmp_path, fault):
    """INVARIANT: a lost or 5xx response after eToro accepted the order is resolved through orders:lookup?referenceId
    (the deterministic x-request-id) -- FILLED, exactly one POST, reconciliation CLEAN."""
    rig = Rig(tmp_path)
    rig.fake.inject(fault)
    out = rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert out["phase2"] == ["FILLED"] and len(rig.fake.posts()) == 1
    lookups = [r for r in rig.fake.requests if r[1] == "/api/v2/trading/info/orders:lookup"]
    assert lookups
    assert rig.db.one("SELECT COUNT(*) c FROM audit_log WHERE kind='ORDER_FOUND_BY_REFERENCE'")["c"] == 1
    assert rig.recon().clean
    assert_gm_untouched(rig)


def test_c05_unknown_outcome_halts_and_is_never_resubmitted(tmp_path):
    """INVARIANT: a request lost before reaching eToro (reference lookup 404, no new position) stays UNKNOWN and halts;
    re-resolution only queries; no second POST is ever made. (Proving 'never received' is impossible -> operator.)"""
    rig = Rig(tmp_path)
    rig.fake.inject("drop@open")
    out = rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert out["phase2"] == ["UNKNOWN"] and "UNKNOWN_ORDER" in rig.kinds()
    for _ in range(3):
        rig.restart()
        rig.ex.resolve_all()
        rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert len(rig.fake.posts()) == 1 and rig.fake.orders == {}


def test_c06_restart_during_execution(tmp_path):
    """INVARIANT: the engine dying right after eToro accepted the order leaves SUBMITTING; after a restart the
    intent is found by reference and FILLED -- no second POST."""
    rig = Rig(tmp_path)
    rig.fake.inject("crash@open")
    with pytest.raises(SimulatedCrash):
        rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert rig.db.one("SELECT status FROM intents")["status"] == "SUBMITTING"
    rig.restart()
    assert list(rig.ex.resolve_all().values()) == ["FILLED"]
    rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 1000.0)])
    assert len(rig.fake.posts()) == 1 and rig.recon().clean


def test_c07_duplicate_submission_prevention(tmp_path):
    """INVARIANT: re-running the same plan (duplicate cycles, restarts) never re-POSTs; x-request-ids are unique per
    intent and stable across runs."""
    rig = Rig(tmp_path)
    legs = [open_leg(0, TQQQ, "TQQQ", 600.0), open_leg(1, SQQQ, "SQQQ", 300.0)]
    for _ in range(3):
        rig.run("P1", legs)
        rig.restart()
    rids = [r[2] for r in rig.fake.posts()]
    assert len(rids) == 2 and len(set(rids)) == 2
    assert set(rids) == {request_id_for(r["intent_id"]) for r in rig.db.q("SELECT intent_id FROM intents")}


def test_c08_multi_instrument_transition_and_reconciliation(tmp_path):
    """INVARIANT: CLOSE TQQQ (phase 1) completes and reconciles (proceeds from close-orders/{id}) before the SQQQ and
    TLT opens (phase 2); the final broker state matches ownership; the GM short is untouched; cash is fully
    explained (CLEAN)."""
    rig = Rig(tmp_path)
    rig.run("P0", [open_leg(0, TQQQ, "TQQQ", 1500.0)])
    pid = next(p["positionID"] for p in rig.fake.positions.values() if p["instrumentID"] == TQQQ)
    rig.fake.prices[TQQQ] = (70.00, 70.02)                       # a gain to realize
    legs = [Leg(0, 1, "CLOSE", "TQQQ", TQQQ, 1530.0, None, str(pid), "switch"),
            open_leg(1, SQQQ, "SQQQ", 900.0), open_leg(2, TLT, "TLT", 600.0)]
    out = rig.run("P1", legs)
    assert out["status"] == "DONE" and out["phase1"] == ["FILLED"] and out["phase2"] == ["FILLED", "FILLED"]
    posts = [r[1] for r in rig.fake.posts()]
    assert posts.index(f"/api/v1/trading/execution/market-close-orders/positions/{pid}") < len(posts) - 2
    r = rig.recon()
    assert r.clean, r.issues
    held = {p.instrument_id for p in r.engine_positions}
    assert held == {SQQQ, TLT}
    flows = {x["kind"] for x in rig.db.q("SELECT kind FROM cash_flows")}
    assert {"TRADE_OPEN", "TRADE_CLOSE", "INITIAL_CAPITAL"} <= flows
    assert_gm_untouched(rig)


def test_c09_partial_close(tmp_path):
    """INVARIANT: a PARTIAL_CLOSE sends UnitsToDeduct, eToro splits the position, the parent keeps the remaining
    units in ownership, and cash reconciles."""
    rig = Rig(tmp_path)
    rig.run("P0", [open_leg(0, TQQQ, "TQQQ", 1500.0)])
    pid = next(p["positionID"] for p in rig.fake.positions.values() if p["instrumentID"] == TQQQ)
    units = round(rig.fake.positions[pid]["units"] / 3, 6)
    out = rig.run("P1", [Leg(0, 1, "PARTIAL_CLOSE", "TQQQ", TQQQ, 500.0, units, str(pid), "reduce")])
    assert out["phase1"] == ["FILLED"]
    assert rig.fake.posts()[-1][3] == {"InstrumentId": TQQQ, "UnitsToDeduct": units}
    own = rig.db.one("SELECT units FROM ownership WHERE position_id=?", (str(pid),))["units"]
    assert own == pytest.approx(rig.fake.positions[pid]["units"])
    assert rig.recon().clean


def test_c10_lost_close_response_is_a_live_blocker(tmp_path):
    """INVARIANT (documented live blocker B3): a close whose response is lost has no referenceId lookup. The engine
    proves the close from the position's disappearance but cannot see the proceeds through any verified read, so
    the cash change stays unexplained -> CASH_DISCREPANCY halt. It is never re-sent."""
    rig = Rig(tmp_path)
    rig.run("P0", [open_leg(0, TQQQ, "TQQQ", 1500.0)])
    pid = next(p["positionID"] for p in rig.fake.positions.values() if p["instrumentID"] == TQQQ)
    rig.fake.inject("lose_response@close")
    out = rig.run("P1", [Leg(0, 1, "CLOSE", "TQQQ", TQQQ, 1500.0, None, str(pid), "liquidate")])
    assert out["phase1"] == ["FILLED"]
    r = rig.recon()
    assert not r.clean and "CASH_DISCREPANCY" in rig.kinds()
    assert len(rig.fake.posts("market-close-orders")) == 1


def test_c11_autonomous_guard_through_the_real_writer(tmp_path):
    """INVARIANT (autonomous release): with the live guard installed on the real EtoroLiveWriter / EtoroReadOnly
    (fake eToro HTTP), an in-cap open is sent with the body Gold Miner proved (no leverage field) and filled; an
    over-cap open never produces an HTTP request; a kill switch blocks the next one."""
    from utengine.core import supervision
    rig = Rig(tmp_path)
    cfg = {**rig.cfg, "runtime": {**rig.cfg["runtime"], "mode": "live"},
           "live_controls": {"max_order_usd": 500.0, "max_gross_usd": 900.0, "max_orders_per_session": 10,
                             "daily_loss_stop_usd": 500.0, "cumulative_loss_stop_usd": 900.0,
                             "activation_file": str(tmp_path / "a.json"), "forbidden_hashes_file": str(tmp_path / "f")}}
    rig.ex.supervisor = lambda row, snap: supervision.check(rig.db, cfg, tmp_path, row, snap, rig.reader.quotes,
                                                            rig.clock())
    out = rig.run("P1", [open_leg(0, TQQQ, "TQQQ", 400.0), open_leg(1, SQQQ, "SQQQ", 600.0)])
    posts = rig.fake.posts()
    assert len(posts) == 1 and posts[0][3] == {"action": "open", "transaction": "buy", "instrumentId": TQQQ,
                                               "settlementType": "cfd", "orderType": "mkt", "amount": 400.0,
                                               "orderCurrency": "usd"}
    st = {r["symbol"]: r["status"] for r in rig.db.q("SELECT symbol, status FROM intents")}
    assert st["TQQQ"] == "FILLED" and st["SQQQ"] == "BLOCKED"
    (tmp_path / "KILL_SWITCH").write_text("drill")
    rig.run("P2", [open_leg(0, TLT, "TLT", 100.0)])
    assert len(rig.fake.posts()) == 1
    assert_gm_untouched(rig)
