"""The trading engine: one cycle = reconcile -> (decide) -> (plan -> risk -> execute -> reconcile) -> record.

Separation of concerns:
  strategy module   targets only (StrategyDecision)            strategy_modules/*
  portfolio         capital and exposure (Allocation)          core/portfolio.py
  risk              permissibility (RiskVerdict, halts)        core/risk.py
  planner           changes (Legs, sells before buys)          core/order_planner.py
  broker adapter    submit and read                            brokers/*
  executor          intents, state machine, recovery           core/execution.py
  reconciliation    what actually happened                     core/reconciliation.py
The engine is the only component that talks to all of them; strategies never see the broker.

portfolio.mode multi_strategy (docs/MULTI_STRATEGY_ARCHITECTURE.md): every sleeve decides on its own schedule; the
sleeves whose execution window is open form ONE portfolio plan per session (multi_planner: per-sleeve wants, internal
crossing, broker-level netting), executed through the same risk / executor / reconciliation path. Sleeve cash, units
and P&L live in core/sleeves.py; the broker remains authoritative for the account.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time

from . import accounting, halts, sleeves, strategy_manager
from .calendar import NY, Calendar
from .canonical import canonical_json, short_id
from .configuration import is_multi, load_all_strategies, load_engine_config
from .errors import BrokerError, ConfigError, DataError, EngineError, KeyNotReadOnly, StrategyError
from .execution import Executor
from .market_data import CsvBarStore, YahooChartBarStore, build_snapshot
from .order_planner import Leg, plan as make_legs
from .order_state import DONE, transition
from .portfolio import allocate
from .reconciliation import engine_position_ids, reconcile
from .risk import check_limits, evaluate
from .scheduler import Scheduler
from .security import Redactor, obtain_writer
from ..brokers.base import Position, Quote
from ..persistence.db import DB
from ..strategy_modules import REGISTRY, create

UTC = dt.timezone.utc


class JsonLog:
    def __init__(self, path: pathlib.Path, redactor):
        self.path = path
        self.redact = redactor
        path.parent.mkdir(parents=True, exist_ok=True)

        self.failures, self.last_error = 0, None

    def __call__(self, event: str, **fields):
        """Best-effort JSON log line. A log-write failure (disk full, permissions) is counted and reported by
        `health` (log_write_failures) instead of crashing a cycle: the database and audit chain stay the record.
        Audit 2026-10-28 finding F4."""
        line = canonical_json({"ts": dt.datetime.now(UTC).isoformat(), "event": event, **fields})
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(self.redact(line) + "\n")
        except OSError as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"


class TradingEngine:
    def __init__(self, config_path, home, *, clock=None, broker=None, bar_store=None, env=None, sleep=time.sleep):
        self.cfg = load_engine_config(config_path)
        self.home = pathlib.Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.mode = self.cfg["runtime"]["mode"]
        sdir = pathlib.Path(self.cfg["runtime"]["strategies_dir"])
        if not sdir.is_absolute():
            sdir = pathlib.Path(self.cfg["_path"]).parent.parent / sdir
        self.specs = load_all_strategies(sdir, REGISTRY)
        self.cal = Calendar(self.cfg["calendar"]["unscheduled_closures"])
        self.db = DB(self.home / "engine.sqlite")
        # UTE_LOG_DIR keeps logs outside the state directory (VPS: /home/ute/logs); default <home>/logs
        self.log = JsonLog(pathlib.Path(os.environ.get("UTE_LOG_DIR") or self.home / "logs") / "engine.jsonl",
                           Redactor(self.cfg["broker"]["credentials_env"].values(), env))
        self.auto_sim_prices = broker is None and self.mode in ("simulation", "supervised_simulation")
        if broker is not None:
            self.broker = broker
        elif self.mode in ("simulation", "supervised_simulation"):
            from ..brokers.simulated import SimulatedBroker
            self.broker = SimulatedBroker(self.home / "sim_broker.json", instruments=self.instrument_symbols(),
                                          clock=self.clock)
        else:
            from ..brokers.etoro import EtoroReadOnly
            self.broker = EtoroReadOnly(self.cfg, env=env)
        self.id_to_symbol = {iid: s for iid, s in self.instrument_symbols().items()}
        if hasattr(self.broker, "symbols"):
            self.broker.symbols.update(self.id_to_symbol)
        self.writer = obtain_writer(self.mode, self.broker, authorize=lambda: self._supervised_live_writer(env))
        self.bars = bar_store or self._bar_store()
        self.executor = Executor(self.db, self.broker, self.writer, self.cfg, self.mode, self.clock, sleep)
        from .supervision import is_supervised
        if is_supervised(self.mode):
            from . import supervision
            self.executor.supervisor = lambda row, snap: supervision.check(
                self.db, self.cfg, self.home, row, snap, self.broker.quotes, self.now())
        self.multi = is_multi(self.cfg)
        if self.multi:
            missing = [s["strategy"] for s in self.cfg["portfolio"]["sleeves"] if s["strategy"] not in self.specs]
            if missing:
                raise ConfigError(f"portfolio.sleeves: no strategy YAML for {missing}")
            self.executor.fill_hook = sleeves.on_fill
        self.switcher = strategy_manager.Switcher(self)
        from ..monitoring.notify import Notifier
        self.notifier = Notifier(self.db, self.cfg, self.home, env=env, redactor=self.log.redact)

    # ------------------------------------------------------------------ helpers
    def _bar_store(self):
        md = self.cfg["market_data"]
        if md["provider"] == "csv":
            if not md.get("csv_dir"):
                raise ConfigError("market_data.csv_dir is required for provider csv")
            d = pathlib.Path(md["csv_dir"])
            return CsvBarStore(d if d.is_absolute() else pathlib.Path(self.cfg["_path"]).parent.parent / d)
        return YahooChartBarStore()

    def _supervised_live_writer(self, env):
        """The eToro order writer for runtime.mode=supervised_live, constructed ONLY through every pilot gate
        (security.authorize_supervised). Reads keep using the READ-ONLY key; this writer holds the trading key."""
        import json as _json
        import uuid as _uuid
        from ..brokers.etoro import BASE, UrllibTransport
        from ..brokers.etoro_live import EtoroLiveWriter
        from .errors import LiveExecutionNotAvailable
        from .security import TRADING_KEY_ENV, authorize_supervised
        from .supervision import kill_switch_paths
        from .takeover import release_id
        env = os.environ if env is None else env
        tr = UrllibTransport(self.cfg["broker"]["request_timeout_seconds"])

        def scope_probe(api, user):                    # one GET; the trading key never reads account data
            st, body = tr("GET", BASE + "/api/v1/me", {"x-api-key": api, "x-user-key": user,
                                                       "x-request-id": str(_uuid.uuid4())}, None)
            if st >= 400:
                raise LiveExecutionNotAvailable(f"trading key check: GET /api/v1/me -> HTTP {st}")
            me = _json.loads(body.decode("utf-8")) if body else {}
            return me.get("scopes") if isinstance(me, dict) else None

        auth = authorize_supervised(release_sha=release_id(), config=self.cfg,
                                    approval_path=self.cfg["pilot"]["approval_file"],
                                    kill_switch_paths=kill_switch_paths(self.cfg, self.home), env=env,
                                    now=self.now(), scope_probe=scope_probe)
        self.db.audit("SUPERVISED_WRITER_AUTHORIZED", {"release_sha": auth.release_sha, "approved_by": auth.approved_by,
                                                       "expires_at": auth.expires_at}, ts=self.nows())
        return EtoroLiveWriter(transport=tr, authorization=auth, api_key=env[TRADING_KEY_ENV[0]],
                               user_key=env[TRADING_KEY_ENV[1]])

    def instrument_symbols(self) -> dict:
        out = {}
        for s in self.specs.values():
            for sym, inst in s["instruments"].items():
                if inst["etoro_instrument_id"]:
                    prev = out.get(inst["etoro_instrument_id"])
                    if prev and prev != sym:
                        raise ConfigError(f"instrument id {inst['etoro_instrument_id']} mapped to {prev} and {sym}")
                    out[inst["etoro_instrument_id"]] = sym
        return out

    def now(self) -> dt.datetime:
        return self.clock().astimezone(UTC)

    def nows(self, now=None) -> str:
        return (now or self.now()).astimezone(UTC).isoformat()

    def active_spec(self) -> dict | None:
        a = strategy_manager.active(self.db)
        return self.specs.get(a["strategy_id"]) if a else None

    def sleeve_ids(self) -> list:
        return sorted(sleeves.weights(self.db)) if self.multi else []

    def reconcile(self, point: str, now: dt.datetime | None = None):
        r = reconcile(self.db, self.broker, self.cfg, point, self.nows(now))
        if self.multi and r.snapshot is not None and sleeves.portfolio(self.db):
            self._reconcile_sleeves(r, self.nows(now))
        self.log("reconciliation", point=point, status=r.status, issues=[i["kind"] for i in r.issues],
                 strategy_equity=r.strategy_equity)
        return r

    def _reconcile_sleeves(self, r, nows: str) -> None:
        """Attribute new broker cash flows to sleeves, then (only after a CLEAN account reconciliation) check that
        the sleeves add up to the broker exactly. A breach halts everything (SLEEVE_LEDGER_MISMATCH)."""
        eng = engine_position_ids(self.db)
        if sleeves.portfolio(self.db).get("seed_required") and r.clean:
            own = {x["position_id"]: x["strategy_id"] for x in self.db.q("SELECT position_id, strategy_id FROM ownership")}
            sleeves.seed_from_snapshot(self.db, r.snapshot, eng, own, nows)
        issues = sleeves.sync_flows(self.db, r.snapshot, eng, nows)
        if r.clean:
            issues += sleeves.check_invariants(self.db, r.snapshot, eng)
        for i in issues:
            halts.raise_halt(self.db, i["kind"], "ALL", i["detail"], nows, subject=i.get("subject"))
        if issues:
            r.issues.extend(issues)
            r.status = "HALT"
            self.db.audit("SLEEVE_RECONCILIATION", {"status": "HALT", "issues": issues}, ts=nows)

    def sleeve_valuation(self, r) -> dict:
        return sleeves.valuation(self.db, r.snapshot, engine_position_ids(self.db), self.id_to_symbol)

    def engine_positions(self, r) -> list:
        ids = engine_position_ids(self.db)
        out = []
        for p in r.snapshot.positions:
            if p.position_id in ids:
                out.append(Position(**{**p.__dict__, "symbol": self.id_to_symbol.get(p.instrument_id, p.symbol)}))
        return out

    # ------------------------------------------------------------------ startup / restart
    def startup(self) -> dict:
        now = self.now()
        last_start = self.db.one("SELECT MAX(seq) s FROM audit_log WHERE kind='ENGINE_START'")["s"]
        last_stop = self.db.one("SELECT MAX(seq) s FROM audit_log WHERE kind='ENGINE_STOP'")["s"]
        if last_start and (not last_stop or last_stop < last_start):
            self.db.alert("CRITICAL", "CRASH", "the previous engine run ended without a clean shutdown (crash, kill "
                          "or power loss); startup recovery and reconciliation follow", ts=self.nows(now))
        self.db.audit("ENGINE_START", {"mode": self.mode, "engine_fingerprint": self.cfg["_fingerprint"],
                                       "strategies": {k: v["fingerprint"] for k, v in self.specs.items()},
                                       "modules": {k: e.source_sha256 for k, e in REGISTRY.items()},
                                       "schema_version": self.db.schema_version()}, ts=self.nows(now))
        gate = getattr(self.broker, "verify_read_only_key", None)
        if gate is not None:
            try:
                key = gate()
            except KeyNotReadOnly as exc:
                self.db.audit("BROKER_KEY_REFUSED", {"reason": str(exc)}, ts=self.nows(now))
                self.db.alert("CRITICAL", "BROKER_KEY_NOT_READ_ONLY", str(exc), ts=self.nows(now))
                self.flush_notifications(now)
                raise
            except BrokerError as exc:                 # unreachable now: every later read re-checks first
                key = {"status": "UNVERIFIED", "detail": f"{type(exc).__name__}: {exc}"}
            self.db.audit("BROKER_KEY_CHECK", key, ts=self.nows(now))
        if self.multi:
            pf = sleeves.bootstrap(self.db, self.cfg, self.specs, REGISTRY, self.nows(now))
            act = {"strategy_id": "PORTFOLIO", "sleeves": pf["sleeves"]}
        else:
            act = strategy_manager.bootstrap(self.db, self.cfg, self.specs, self.nows(now))
        for sp in self.db.q("SELECT plan_hash, from_strategy, to_strategy FROM switch_plans WHERE status='APPROVED'"):
            # Audit 2026-10-28 finding F2: an approved switch with no outcome = the engine stopped mid-migration.
            # The previous strategy stays active; nothing is re-executed automatically; an operator must review.
            with self.db.tx():
                self.db.con.execute("UPDATE switch_plans SET status='INTERRUPTED', finished_at=? WHERE plan_hash=?",
                                    (self.nows(now), sp["plan_hash"]))
                self.db.audit("SWITCH_INTERRUPTED", dict(sp), ts=self.nows(now))
            halts.raise_halt(self.db, "SWITCH_FAILED", "ALL", f"switch {sp['plan_hash']} {sp['from_strategy']} -> "
                             f"{sp['to_strategy']} was interrupted (engine stopped mid-migration); "
                             f"{act['strategy_id']} remains active; review `orders --all` and preview again",
                             self.nows(now))
        # resolve in-flight intents FIRST, from broker evidence, so a fill that landed while the engine was down
        # is attributed to its intent instead of being classified as an external position
        resolved = self.executor.resolve_all() if self.writer is not None else {}
        r = self.reconcile("after_restart", now)
        self.db.heartbeat("engine", {"phase": "startup", "mode": self.mode}, ts=self.nows(now))
        self.flush_notifications(now)
        return {"active": act["strategy_id"], "reconciliation": r.status, "resolved": resolved}

    def shutdown(self, reason: str) -> None:
        self.db.audit("ENGINE_STOP", {"reason": reason}, ts=self.nows())
        self.flush_notifications(self.now())

    def flush_notifications(self, now) -> dict:
        try:
            return self.notifier.flush(self.nows(now))
        except Exception as exc:  # noqa: BLE001 -- notification problems never stop trading logic
            self.log("notify_error", error=f"{type(exc).__name__}: {exc}")
            return {"error": str(exc)}

    def data_check(self, session: dt.date | None = None, now: dt.datetime | None = None) -> dict:
        """Market-data health for the active strategy: freshness (bar for the last completed session), gaps in the
        indicator window and suspect prices. Recorded in data_health; STALE/SUSPECT raise a DATA_STALE alert."""
        now = now or self.now()
        session = session or self.cal.last_completed_session(now)
        # before the first start there is no active strategy yet: check the configured initial one
        # (audit 2026-10-28 finding F5: this crashed with a TypeError on a fresh state directory)
        if self.multi:
            check = [self.specs[s["strategy"]] for s in self.cfg["portfolio"]["sleeves"]]
        else:
            check = [self.active_spec() or self.specs[self.cfg["runtime"]["initial_strategy"]]]
        status, detail = "OK", {"symbols": 0, "sha256": {}}
        for spec in check:
            module = create(spec)
            try:
                snap = build_snapshot(self.bars, self.cal, session, module.required_symbols(),
                                      module.optional_symbols(),
                                      lookback_sessions=self.cfg["market_data"]["lookback_sessions"], now=now)
                detail["sha256"][spec["id"]] = snap.provenance.sha256
                detail["symbols"] += len(snap.close)
            except DataError as exc:
                msg = str(exc)
                status = (msg.split(":")[0] if msg.split(":")[0] in ("STALE_DATA", "VENDOR_GAP", "SUSPECT_PRICE")
                          else "UNAVAILABLE")
                detail = {"error": msg, "strategy": spec["id"]}
                break
        if status == "OK" and not self.multi:
            detail["sha256"] = next(iter(detail["sha256"].values()))
        self.db.x("INSERT INTO data_health(ts,source,session,status,detail) VALUES(?,?,?,?,?)",
                  (self.nows(now), self.bars.source, session.isoformat(), status, canonical_json(detail)))
        if status != "OK":
            self.db.alert("WARNING", "DATA_STALE", f"{self.bars.source} {session}: {detail.get('error')}",
                          ts=self.nows(now))
        return {"session": session.isoformat(), "source": self.bars.source, "status": status, **detail}

    # ------------------------------------------------------------------ decision
    def compute_decision(self, spec: dict, session: dt.date, now: dt.datetime, persist: bool = True):
        module = create(spec)
        snap = build_snapshot(self.bars, self.cal, session, module.required_symbols(), module.optional_symbols(),
                              lookback_sessions=self.cfg["market_data"]["lookback_sessions"], now=now)
        st = self.db.one("SELECT state FROM strategy_state WHERE strategy_id=? AND session<? ORDER BY session DESC "
                         "LIMIT 1", (spec["id"], session.isoformat()))
        state = json.loads(st["state"]) if st else {}
        decision = module.decide(snap, state, now)
        if persist:
            with self.db.tx():
                self.db.con.execute("INSERT OR REPLACE INTO strategy_state VALUES(?,?,?)",
                                    (spec["id"], session.isoformat(), canonical_json(decision.strategy_state)))
        return module, decision

    def decide(self, spec: dict, session: dt.date, now: dt.datetime, late: bool, record: bool = True,
               recon=None) -> dict:
        did = "D" + short_id(spec["id"], session.isoformat(), n=23)
        if self.db.one("SELECT 1 FROM decisions WHERE strategy_id=? AND session=?", (spec["id"], session.isoformat())):
            return {"status": "ALREADY_DECIDED", "decision_id": did}
        r = recon or self.reconcile("before_decision", now)
        try:
            _m, d = self.compute_decision(spec, session, now)
            status, body, err = "DECIDED", canonical_json(d.to_dict()), None
        except (DataError, StrategyError) as exc:
            status, body, err = "NO_DECISION", None, f"{type(exc).__name__}: {exc}"
            self.db.x("INSERT INTO data_health(ts,source,session,status,detail) VALUES(?,?,?,?,?)",
                      (self.nows(now), self.bars.source, session.isoformat(), "DECISION_BLOCKED",
                       canonical_json({"error": err})))
        with self.db.tx():
            self.db.con.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?)",
                                (did, spec["id"], session.isoformat(), status, body, err, self.nows(now), int(late)))
            self.db.audit("DECISION", {"decision_id": did, "strategy_id": spec["id"], "session": session.isoformat(),
                                       "status": status, "error": err, "late": late,
                                       "targets": json.loads(body)["target_weights"] if body else None,
                                       "signal": json.loads(body)["signal_target"] if body else None},
                          ts=self.nows(now))
        if status != "DECIDED":
            self.db.alert("WARNING", "NO_DECISION", f"{spec['id']} {session}: {err}")
        self.log("decision", strategy=spec["id"], session=session.isoformat(), status=status, error=err)
        if record and r.snapshot is not None and r.strategy_equity is not None:
            exposure = {p.symbol or str(p.instrument_id): round(p.value, 2) for p in self.engine_positions(r)}
            rep = accounting.account_report(self.db, r)
            accounting.record_daily(self.db, session.isoformat(), account_equity=r.account_equity,
                                    strategy_equity=r.strategy_equity, credit=r.snapshot.credit, exposure=exposure,
                                    strategy_id=spec["id"], now=self.nows(now),
                                    foreign_value=rep["foreign_positions_value"])
        return {"status": status, "decision_id": did, "error": err}

    # ------------------------------------------------------------------ execution
    def quotes_for(self, positions: list, alloc) -> dict:
        ids = sorted({p.instrument_id for p in positions} | set(alloc.instrument_ids.values()))
        if not ids:
            return {}
        if self.auto_sim_prices:
            self._sim_prices(ids)
        return self.broker.quotes(ids)

    def _sim_prices(self, ids):
        """SIMULATION ONLY: price the simulated broker from the bar store (today's open if present, else the last
        close) with a symmetric half spread. Never used in shadow mode."""
        hs = self.cfg["market_data"]["simulation_half_spread_bp"] / 1e4
        today = self.now().astimezone(NY).date()
        for iid in ids:
            sym = self.id_to_symbol.get(iid)
            rows = self.bars.bars(sym, today - dt.timedelta(days=10), today) if sym else []
            if not rows:
                continue
            d, o, _h, _l, c, _v = rows[-1]
            px = o if d == today else c
            self.broker.set_price(iid, round(px * (1 - hs), 6), round(px * (1 + hs), 6))

    def execute_legs(self, plan_id: str, legs: list, spec: dict, body: dict, now: dt.datetime, plan_kind: str,
                     resize_fn=None, before_submit=None) -> dict:
        nows = self.nows(now)
        alloc_d = body["allocation"]
        from .portfolio import Allocation
        alloc = Allocation(**alloc_d)
        r = self.reconcile("before_execution", now)
        if not r.clean:
            return {"status": "BLOCKED_RECONCILIATION", "issues": [i["kind"] for i in r.issues]}
        positions = self.engine_positions(r)
        quotes = self.quotes_for(positions, alloc)
        exec_session = now.astimezone(NY).date()
        verdict = evaluate(self.db, self.cfg, spec, legs, alloc, quotes, decision_session=body["decision"]["session"],
                           execution_session=exec_session, calendar=self.cal, now=now,
                           available_cash=r.snapshot.available_cash + sum(l.amount or 0 for l in legs if l.phase == 1))
        with self.db.tx():
            self.db.con.execute("INSERT OR IGNORE INTO plans VALUES(?,?,?,?,?,?,?,?)",
                                (plan_id, body["decision"].get("decision_id", body["decision"]["session"]), spec["id"],
                                 body["decision"]["session"], nows, "PLANNED",
                                 canonical_json({"legs": [l.to_dict() for l in legs], "kind": plan_kind,
                                                 "blocked": [(l.to_dict(), why) for l, why in verdict.blocked],
                                                 "plan_blocks": verdict.plan_blocks, "notes": body.get("notes", []),
                                                 "allocation_blocked": alloc_d.get("blocked", {}),
                                                 "strategy_equity": alloc_d.get("strategy_equity")}),
                                 self.mode))
            self.db.audit("PLAN", {"plan_id": plan_id, "legs": [l.to_dict() for l in verdict.approved],
                                   "blocked": [(l.symbol, why) for l, why in verdict.blocked],
                                   "plan_blocks": verdict.plan_blocks}, ts=nows)
        for leg, why in verdict.blocked:
            row = self.executor.ensure_intent(plan_id, leg, spec["id"])
            if row["status"] == "INTENT_PERSISTED":
                transition(self.db, row["intent_id"], "BLOCKED", nows, {"reason": why})
        if not verdict.ok:
            self.db.x("UPDATE plans SET status='BLOCKED' WHERE plan_id=?", (plan_id,))
            return {"status": "BLOCKED_RISK", "reasons": verdict.plan_blocks}
        crossed = before_submit() if before_submit else None
        if not verdict.approved:
            self.db.x("UPDATE plans SET status='NO_LEGS' WHERE plan_id=?", (plan_id,))
            return {"status": "NO_LEGS" if not verdict.blocked else "ALL_LEGS_BLOCKED",
                    "blocked": [(l.symbol, w) for l, w in verdict.blocked], "crossed": crossed}
        res = self.executor.execute(plan_id, verdict.approved, spec["id"], lambda p: self.reconcile(p, self.now()),
                                    resize_fn=resize_fn)
        after = self.reconcile("after_execution", self.now())
        if after.clean:
            for row in self.db.q("SELECT intent_id, status FROM intents WHERE plan_id=?", (plan_id,)):
                if row["status"] in ("FILLED", "REJECTED", "CANCELED", "NOT_SUBMITTED"):
                    transition(self.db, row["intent_id"], "RECONCILED", self.nows(), {"snapshot": after.snapshot_id})
        final = res["status"] if after.clean or not res["status"].startswith("DONE") else "DONE_UNRECONCILED"
        self.db.x("UPDATE plans SET status=? WHERE plan_id=?", (final, plan_id))
        self.log("execution", plan_id=plan_id, status=final)
        return {**res, "status": final, "crossed": crossed}

    def execute(self, spec: dict, session: dt.date, now: dt.datetime) -> dict:
        drow = self.db.one("SELECT * FROM decisions WHERE strategy_id=? AND session=?", (spec["id"], session.isoformat()))
        if drow is None or drow["status"] != "DECIDED":
            return {"status": "NO_DECISION"}
        plan_id = "P" + short_id(drow["decision_id"], self.mode, n=23)
        existing = self.db.one("SELECT status FROM plans WHERE plan_id=?", (plan_id,))
        if existing and existing["status"] not in ("PLANNED", "HALTED_PHASE1", "HALTED_PHASE2", "HALTED_RECONCILIATION"):
            return {"status": "ALREADY_EXECUTED", "plan_status": existing["status"]}
        d = json.loads(drow["decision"])
        d["decision_id"] = drow["decision_id"]
        if existing:
            if any(h["scope"] == "ALL" for h in halts.active(self.db)):
                # never submit anything new while halted; only resolve what is already at the broker
                return {"status": "HALTED_RESOLVE_ONLY", "resolved": self.executor.resolve_all()}
            plan = json.loads(self.db.one("SELECT plan FROM plans WHERE plan_id=?", (plan_id,))["plan"])
            legs = [Leg(**l) for l in plan["legs"]]
            approved = [l for l in legs if l.leg_index not in {b[0]["leg_index"] for b in plan["blocked"]}]
            res = self.executor.execute(plan_id, approved, spec["id"], lambda p: self.reconcile(p, self.now()))
            after = self.reconcile("after_execution", self.now())
            if after.clean and res["status"].startswith("DONE"):
                for row in self.db.q("SELECT intent_id, status FROM intents WHERE plan_id=?", (plan_id,)):
                    if row["status"] in ("FILLED", "REJECTED", "CANCELED", "NOT_SUBMITTED"):
                        transition(self.db, row["intent_id"], "RECONCILED", self.nows(), {"snapshot": after.snapshot_id})
            self.db.x("UPDATE plans SET status=? WHERE plan_id=?", (res["status"], plan_id))
            return {**res, "resumed": True}
        if not d["rebalance"]:
            with self.db.tx():
                self.db.con.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)",
                                    (plan_id, drow["decision_id"], spec["id"], session.isoformat(), self.nows(now),
                                     "NO_REBALANCE", canonical_json({"legs": [], "blocked": [], "plan_blocks": []}),
                                     self.mode))
            return {"status": "NO_REBALANCE"}
        r = self.reconcile("before_planning", now)
        if not r.clean:
            return {"status": "BLOCKED_RECONCILIATION", "issues": [i["kind"] for i in r.issues]}
        flows = self.db.one("SELECT COALESCE(SUM(amount),0) s FROM cash_flows WHERE external=1 AND performance_ts IS NULL")["s"]
        prev = self.db.one("SELECT * FROM daily_equity ORDER BY session DESC LIMIT 1")
        if r.strategy_equity is None:
            # Audit 2026-10-28 finding F1: an engine position without broker unrealized P&L made strategy equity None
            # and the cycle crashed in allocate(). Sizing is refused instead (no estimate is ever substituted).
            halts.raise_halt(self.db, "ACCOUNT_SIZING_BLOCKED", "ALL", "strategy equity unavailable: the broker gave "
                             "no unrealized P&L for an engine position; no order can be sized", self.nows(now))
            return {"status": "BLOCKED_EQUITY_UNAVAILABLE"}
        check_limits(self.db, self.cfg, dict(prev) if prev else None, r.strategy_equity, flows, self.nows(now))
        from .strategy_api import StrategyDecision
        dec = StrategyDecision(**{k: v for k, v in d.items() if k != "decision_id"})
        alloc = allocate(dec, spec, r.strategy_equity)
        positions = self.engine_positions(r)
        quotes = self.quotes_for(positions, alloc)
        legs, notes = make_legs(alloc, positions, quotes, spec["rebalancing"], self.cfg["risk"]["min_order_usd"])
        body = {"decision": d, "allocation": alloc.to_dict(), "notes": notes}
        return self.execute_legs(plan_id, legs, spec, body, now, plan_kind="rebalance")

    # ------------------------------------------------------------------ the cycle
    def record_missed(self, spec: dict, now: dt.datetime) -> list:
        sched = Scheduler(self.cal, spec["schedule"])
        last = self.db.get("last_cycle_at")
        if not last:
            return []
        since = dt.datetime.fromisoformat(last)
        out = []
        for w in sched.windows_ended_since(since, now, "decision"):
            if not self.db.one("SELECT 1 FROM decisions WHERE strategy_id=? AND session=?", (spec["id"], w.session.isoformat())):
                out.append(("MISSED_DECISION", w))
        for w in sched.windows_ended_since(since, now, "execution"):
            d = self.db.one("SELECT decision_id, decision FROM decisions WHERE strategy_id=? AND session=? AND status='DECIDED'",
                            (spec["id"], w.session.isoformat()))
            if d and not self.db.one("SELECT 1 FROM plans WHERE decision_id=?", (d["decision_id"],)) \
                    and not self.db.get(f"sleeve_planned:{w.session.isoformat()}:{spec['id']}") \
                    and json.loads(d["decision"])["rebalance"]:
                out.append(("MISSED_EXECUTION", w))
        for kind, w in out:
            with self.db.tx():
                cur = self.db.con.execute("INSERT OR IGNORE INTO schedule_events(ts,strategy_id,kind,session,detail) "
                                          "VALUES(?,?,?,?,?)", (self.nows(now), spec["id"], kind, w.session.isoformat(),
                                                                canonical_json({"window_start": w.start.isoformat(),
                                                                                "window_end": w.end.isoformat(),
                                                                                "policy": "skip (no catch-up)"})))
                if cur.rowcount:
                    self.db.audit(kind, {"strategy_id": spec["id"], "session": w.session.isoformat(),
                                         "window_end": w.end.isoformat()}, ts=self.nows(now))
            self.db.alert("WARNING", kind, f"{spec['id']} {w.session}: window ended {w.end.isoformat()} (skipped)")
        return [(k, w.session.isoformat()) for k, w in out]

    def cycle(self, now: dt.datetime | None = None) -> dict:
        now = (now or self.now()).astimezone(UTC)
        if self.multi:
            from .portfolio_engine import cycle_portfolio
            return cycle_portfolio(self, now)
        out = {"at": now.isoformat(), "mode": self.mode}
        spec = self.active_spec()
        if spec is None:
            raise EngineError("no active strategy (run startup)")
        drift = strategy_manager.drift(self.db, self.specs)
        if drift:
            halts.raise_halt(self.db, "CONFIG_DRIFT", "ALL", "; ".join(drift), self.nows(now))
            out["drift"] = drift
        if self.writer is not None:
            out["resolved"] = self.executor.resolve_all()
        out["missed"] = self.record_missed(spec, now)
        sched = Scheduler(self.cal, spec["schedule"])
        dw = sched.current_decision_window(now)
        if dw and not drift:
            out["decision"] = self.decide(spec, dw.session, now, late=dw.is_late(now))
        ew = sched.current_execution_window(now)
        if ew and not drift:
            out["execution"] = self.execute(spec, ew.session, now)
        last_idle = self.db.get("last_idle_reconcile")
        if not dw and not ew and (not last_idle or (now - dt.datetime.fromisoformat(last_idle)).total_seconds() >= 900):
            r = self.reconcile("idle", now)
            self.db.put("last_idle_reconcile", now.isoformat())
            out["reconciliation"] = r.status
        if self.writer is not None:
            live = {r["plan_id"] for r in self.db.q("SELECT plan_id FROM plans")} if ew else set()
            if not ew:
                self.executor.abandon_stale(live)
        self.db.put("last_cycle_at", now.isoformat())
        out["notifications"] = self.flush_notifications(now)
        self.db.heartbeat("engine", {"phase": "cycle", "mode": self.mode, "strategy": spec["id"]}, ts=self.nows(now))
        self.log("cycle", **{k: v for k, v in out.items() if k != "at"})
        return out

    def status(self) -> dict:
        from ..monitoring.health import health
        return health(self)
