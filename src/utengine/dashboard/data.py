"""Read-only financial data for the dashboard (docs/DASHBOARD_ARCHITECTURE.md).

Rules enforced here:
  * the engine database is opened READ-ONLY (SQLite URI mode=ro + PRAGMA query_only); nothing is ever written;
  * the dashboard never calls the broker and holds no broker credential: every broker number comes from the engine's
    last recorded reconciliation snapshot, shown with its age and a status label;
  * labels: VERIFIED (broker-reported, fresh, reconciliation clean), STALE (older than the staleness limit),
    UNRECONCILED (latest reconciliation not clean), UNAVAILABLE (the broker did not supply it: never estimated),
    SIMULATED (simulation mode: a simulated broker, not eToro), SHADOW (read-only broker data, nothing traded);
  * a metric that cannot be computed from recorded evidence is None with a reason, never a guess.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import types

from ..brokers.base import AccountSnapshot, MirrorHolding, PendingOrder, Position
from ..core import funding, halts, retirement, sleeves
from ..core.calendar import NY, Calendar
from ..core.order_state import UNRESOLVED
from ..core.scheduler import Scheduler

UTC = dt.timezone.utc
STALE_SECONDS = 900


class _RO:
    """Minimal read-only stand-in for persistence.db.DB (the core modules only call q/one/get on it here)."""

    def __init__(self, path: pathlib.Path):
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA query_only=ON")

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()

    def get(self, key, default=None):
        r = self.con.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def close(self):
        self.con.close()


class Data:
    def __init__(self, cfg: dict, specs: dict, home, clock=None):
        self.cfg, self.specs = cfg, specs
        self.path = pathlib.Path(home) / "engine.sqlite"
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.mode = cfg["runtime"]["mode"]
        self.cal = Calendar(cfg["calendar"]["unscheduled_closures"])
        self.symbols = {i["etoro_instrument_id"]: s for sp in specs.values() for s, i in sp["instruments"].items()
                        if i["etoro_instrument_id"]}

    def db(self):
        if not self.path.exists():
            raise FileNotFoundError("engine database not found (the engine has not started yet)")
        return _RO(self.path)

    # ---------------------------------------------------------------- snapshot rebuilt from the DB
    def _snapshot(self, db):
        r = db.one("SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1")
        if not r:
            return None, None
        pos = tuple(Position(**p) for p in json.loads(r["positions"]))
        pend = tuple(PendingOrder(**o) for o in json.loads(r["pending_orders"]))
        snap = AccountSnapshot(as_of=r["ts"], credit=r["credit"], pending_open_amount=r["pending_open"], positions=pos,
                               pending_orders=pend, currency="USD", source=r["source"], raw_sha256=r["raw_sha256"],
                               provenance=json.loads(r["provenance"]))
        return snap, dict(r)

    def _last_recon(self, db):
        r = db.one("SELECT ts, payload FROM audit_log WHERE kind='RECONCILIATION' ORDER BY seq DESC LIMIT 1")
        return (r["ts"], json.loads(r["payload"])) if r else (None, None)

    def _label(self, snap, recon):
        if snap is None:
            return "UNAVAILABLE"
        age = (self.clock() - dt.datetime.fromisoformat(snap.as_of)).total_seconds()
        if recon and recon.get("status") == "UNAVAILABLE":
            return "STALE"
        if age > STALE_SECONDS:
            return "STALE"
        if recon and recon.get("status") != "CLEAN":
            return "UNRECONCILED"
        return {"simulation": "SIMULATED", "shadow": "SHADOW"}.get(self.mode, "VERIFIED")

    def _context(self, db):
        snap, row = self._snapshot(db)
        ts, recon = self._last_recon(db)
        from ..core.reconciliation import engine_position_ids
        eng = engine_position_ids(db)
        val = sleeves.valuation(db, snap, eng, self.symbols) if snap is not None and sleeves.portfolio(db) else None
        eq = None
        if snap is not None:
            ep = [p for p in snap.positions if p.position_id in eng]
            if all(p.unrealized_pnl is not None for p in ep):
                eq = round(snap.available_cash + sum(p.value for p in ep) - (funding.suspense_total(db)
                                                                             if funding.automatic(self.cfg) else 0), 6)
        rr = types.SimpleNamespace(snapshot=snap, status=(recon or {}).get("status", "UNAVAILABLE"), strategy_equity=eq)
        return snap, recon, ts, eng, val, rr

    # ---------------------------------------------------------------- sections
    def meta(self) -> dict:
        return {"mode": self.mode.upper(), "instance": self.cfg["engine"]["instance_id"],
                "live_trading": "DISABLED (release gate closed; mode=live refused)", "read_only": True,
                "now": self.clock().isoformat()}

    def overview(self) -> dict:
        db = self.db()
        try:
            snap, recon, ts, eng, val, rr = self._context(db)
            label = self._label(snap, recon)
            cap = funding.capital_report(db, rr, val) if snap is not None else {"status": "UNAVAILABLE"}
            daily = [dict(r) for r in db.q("SELECT session, strategy_equity, account_equity, pnl, external_flow, "
                                           "twr_index, drawdown FROM daily_equity ORDER BY session")]
            w = sleeves.weights(db)
            sl = {}
            for s, v in ((val or {}).get("sleeves") or {}).items():
                sl[s] = {"weight": w.get(s), "equity": v["equity"], "cash": v["cash"], "reserved": v["reserved"],
                         "invested_value": v["holdings_value"],
                         "assigned_vs_invested": None if not v["equity"] else round(v["holdings_value"] / v["equity"], 6)}
            flows = {k: v for k, v in __import__("utengine.core.accounting", fromlist=["x"]).funding_summary(db).items()}
            return {**self.meta(), "data_label": label, "snapshot_as_of": snap.as_of if snap else None,
                    "snapshot_age_s": None if snap is None else round((self.clock() - dt.datetime.fromisoformat(
                        snap.as_of)).total_seconds()),
                    "last_reconciliation": {"at": ts, "status": (recon or {}).get("status"),
                                            "issues": [i["kind"] for i in (recon or {}).get("issues", [])]},
                    "account": cap, "sleeves": sl, "unallocated_pool": (sleeves.balances(db).get(sleeves.UNALLOCATED)
                                                                         or {}).get("cash", 0.0),
                    "pnl": self._pnl(daily), "curve": [{"session": d["session"], "equity": d["strategy_equity"],
                                                        "twr": d["twr_index"], "drawdown": d["drawdown"]} for d in daily],
                    "funding": flows, "open_exceptions": len(halts.open_exceptions(db))}
        finally:
            db.close()

    @staticmethod
    def _pnl(daily: list) -> dict:
        """P&L excluding external flows (deposits never count as income), from the recorded daily closes."""
        if not daily:
            return {"daily": None, "weekly": None, "monthly": None, "lifetime": None,
                    "note": "no daily close recorded yet"}

        def since(days):
            last = dt.date.fromisoformat(daily[-1]["session"])
            rows = [d for d in daily if dt.date.fromisoformat(d["session"]) > last - dt.timedelta(days=days)]
            return round(sum(d["pnl"] or 0.0 for d in rows), 2)
        return {"daily": round(daily[-1]["pnl"] or 0.0, 2), "weekly": since(7), "monthly": since(31),
                "lifetime": round(sum(d["pnl"] or 0.0 for d in daily), 2),
                "twr_lifetime": round(daily[-1]["twr_index"] - 1.0, 6), "max_drawdown": min(d["drawdown"] for d in daily),
                "basis": "recorded daily closes; P&L = equity change minus external flows"}

    def strategies(self) -> dict:
        db = self.db()
        try:
            snap, recon, ts, eng, val, rr = self._context(db)
            perf = sleeves.performance(db)
            out = {}
            vs = (val or {}).get("sleeves") or {}
            for sid in sorted(sleeves.weights(db)):
                d = db.one("SELECT session, status, decision, error, created_at FROM decisions WHERE strategy_id=? "
                           "ORDER BY session DESC LIMIT 1", (sid,))
                dec = json.loads(d["decision"]) if d and d["decision"] else None
                exp = (dec or {}).get("explanation", {})
                spec = self.specs.get(sid)
                blocked = {}
                if dec and spec:
                    from ..core.portfolio import eligibility_reason
                    for sym, x in dec["target_weights"].items():
                        why = eligibility_reason(spec, sym)
                        if why:
                            blocked[sym] = {"weight": x, "reason": why}
                v = vs.get(sid, {})
                hold = {h["symbol"] or str(i): {"units": h["units"], "value": h["value"],
                                                "weight": None if not v.get("equity") or h["value"] is None
                                                else round(h["value"] / v["equity"], 6)}
                        for i, h in (v.get("holdings") or {}).items()}
                status = {}
                if spec and spec["implementation"] == "qc529":
                    status = {"crash_guard_active": exp.get("crash_guard_active"),
                              "qqq_10d_return": exp.get("qqq_10d_return"), "sleeves_ready": exp.get("sleeves_ready"),
                              "rule": "guard on when QQQ 10-day return <= -10%, off when >= -4% (frozen settings)"}
                if spec and spec["implementation"] == "qc60":
                    bull = exp.get("spy_close") is not None and exp.get("spy_sma200") is not None and \
                        exp["spy_close"] > exp["spy_sma200"]
                    status = {"regime": ("BULL (SPY above its 200-day SMA)" if bull else "BEAR (SPY at/below SMA200)")
                              if exp.get("spy_sma200") else None, "branch_path": exp.get("branch_path"),
                              "substitution": exp.get("substitution")}
                out[sid] = {"equity": v.get("equity"), "cash": v.get("cash"), "holdings": hold,
                            "performance": perf.get(sid), "decision": None if not d else {
                                "session": d["session"], "status": d["status"], "error": d["error"],
                                "decided_at": d["created_at"], "targets": (dec or {}).get("target_weights"),
                                "signal_target": (dec or {}).get("signal_target"), "rebalance": (dec or {}).get("rebalance"),
                                "explanation": exp},
                            "cash_from_unavailable_instruments": blocked, "status": status,
                            "realized_unrealized": self._sleeve_pnl(db, sid, v)}
            overlap = {}
            for s, v in vs.items():
                for i, h in (v.get("holdings") or {}).items():
                    if h["units"] > 1e-9:
                        overlap.setdefault(h["symbol"] or str(i), {})[s] = h["value"]
            return {**self.meta(), "data_label": self._label(snap, recon), "sleeves": out,
                    "overlap": {k: v for k, v in overlap.items() if len(v) > 1},
                    "exposure_by_instrument": {k: round(sum(x or 0 for x in v.values()), 2) for k, v in overlap.items()}}
        finally:
            db.close()

    def _sleeve_pnl(self, db, sid, v) -> dict:
        """Average-cost realized P&L per sleeve from its own ledger (fills and crossings), unrealized = broker value of
        its units - remaining cost. Fees inside fills are part of the cost."""
        cost, units, realized = {}, {}, 0.0
        for r in db.q("SELECT kind, cash, units, instrument_id FROM sleeve_ledger WHERE sleeve=? AND instrument_id IS "
                      "NOT NULL ORDER BY seq", (sid,)):
            i, u, c = r["instrument_id"], r["units"], r["cash"]
            if u > 0:
                cost[i] = cost.get(i, 0.0) - c
                units[i] = units.get(i, 0.0) + u
            elif u < 0 and units.get(i, 0) > 0:
                avg = cost[i] / units[i]
                realized += c - avg * (-u)
                cost[i] -= avg * (-u)
                units[i] += u
        hv = v.get("holdings_value")
        rem = sum(cost.values())
        return {"realized": round(realized, 2), "unrealized": None if hv is None else round(hv - rem, 2),
                "method": "average cost per sleeve and instrument"}

    def trades(self) -> dict:
        db = self.db()
        try:
            intents = [dict(r) for r in db.q("SELECT intent_id, plan_id, phase, action, symbol, amount, units, "
                                             "position_id, status, broker_order_id, mode, created_at, updated_at, result "
                                             "FROM intents ORDER BY created_at DESC LIMIT 200")]
            for x in intents:
                res = json.loads(x.pop("result") or "{}")
                x["sleeves"] = res.get("sleeves")
                x["strategy_id"] = res.get("strategy_id")
            fills = [dict(r) for r in db.q("SELECT ts, sleeve, kind, symbol, cash, units, intent_id, plan_id FROM "
                                           "sleeve_ledger WHERE kind IN ('OPEN_FILL','CLOSE_FILL','CROSS_IN','CROSS_OUT',"
                                           "'CLOSE_PROCEEDS') ORDER BY seq DESC LIMIT 300")]
            fees = [dict(r) for r in db.q("SELECT ts, kind, amount, source, evidence FROM cash_flows WHERE kind IN "
                                          "('FEE','DIVIDEND','ADJUSTMENT') ORDER BY ts DESC")]
            partial = [r for r in intents if r["status"] == "PARTIALLY_FILLED"]
            return {**self.meta(), "orders": intents, "fills_by_sleeve": fills, "fees_and_financing": fees,
                    "fees_total": round(sum(f["amount"] for f in fees if f["kind"] == "FEE"), 2),
                    "partial_fills": partial,
                    "rejected": [r for r in intents if r["status"] in ("REJECTED", "CANCELED")],
                    "pending_or_unresolved": [r for r in intents if r["status"] in UNRESOLVED],
                    "blocked": [r for r in intents if r["status"] == "BLOCKED"]}
        finally:
            db.close()

    def operations(self) -> dict:
        db = self.db()
        try:
            snap, recon, ts, eng, val, rr = self._context(db)
            hb = db.one("SELECT ts, detail FROM heartbeat WHERE component='engine'")
            dh = db.one("SELECT ts, source, session, status, detail FROM data_health ORDER BY id DESC LIMIT 1")
            last_dec = db.one("SELECT strategy_id, session, status, created_at FROM decisions ORDER BY created_at DESC "
                              "LIMIT 1")
            nxt = {}
            now = self.clock()
            for sid in (sleeves.weights(db) or {}):
                sch = Scheduler(self.cal, self.specs[sid]["schedule"])
                nxt[sid] = {"next_decision": _next(sch, now, "decision"), "next_execution": _next(sch, now, "execution")}
            ok, msg = _verify_chain(db)
            bdir = pathlib.Path(os.environ.get("UTE_BACKUP_DIR") or self.path.parent / "backups")
            backups = sorted(p.name for p in bdir.glob("engine-*.manifest.json")) if bdir.exists() else []
            return {**self.meta(), "heartbeat": dict(hb) if hb else None,
                    "heartbeat_age_s": None if not hb else round((now - dt.datetime.fromisoformat(hb["ts"])).total_seconds()),
                    "broker": {"last_snapshot_at": snap.as_of if snap else None, "source": snap.source if snap else None,
                               "label": self._label(snap, recon), "connectivity": (recon or {}).get("status")},
                    "market_data": dict(dh) if dh else None,
                    "reconciliation": {"at": ts, **({k: recon.get(k) for k in ("point", "status", "issues",
                                                                               "unexplained_cash")} if recon else {})},
                    "halts": halts.active(db) + halts.active_sleeve(db),
                    "exceptions_open": halts.open_exceptions(db),
                    "alert_history": [dict(r) for r in db.q("SELECT ts, severity, kind, message, suppressed, delivered_at "
                                                            "FROM alerts ORDER BY id DESC LIMIT 100")],
                    "audit_chain": msg, "audit_chain_ok": ok,
                    "schema_version": db.one("SELECT MAX(version) v FROM schema_migrations")["v"],
                    "backups": backups[-10:], "last_decision": dict(last_dec) if last_dec else None,
                    "schedule": nxt}
        finally:
            db.close()

    def migration(self) -> dict:
        db = self.db()
        try:
            snap, recon, ts, eng, val, rr = self._context(db)
            foreign = [p.__dict__ for p in (snap.positions if snap else []) if p.position_id not in eng]
            own = {r["position_id"]: r["owner"] for r in db.q("SELECT position_id, owner FROM ownership")}
            for f in foreign:
                f["owner"] = own.get(f["position_id"], "UNCLASSIFIED")
            tk = db.one("SELECT run_id, state, plan_hash, created_at, updated_at, plan FROM takeover_runs ORDER BY "
                        "created_at DESC LIMIT 1")
            plan = json.loads(tk["plan"]) if tk and tk["plan"] else None
            appr = [dict(r) for r in db.q("SELECT approval_id, kind, approved_by, created_at, expires_at, status, "
                                          "status_reason FROM takeover_approvals WHERE run_id=?", (tk["run_id"],))] if tk else []
            ev = [dict(r) for r in db.q("SELECT ts, from_state, to_state, detail FROM takeover_events WHERE run_id=? "
                                        "ORDER BY id", (tk["run_id"],))] if tk else []
            trail = [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "payload": json.loads(r["payload"])}
                     for r in db.q("SELECT seq, ts, kind, payload FROM audit_log WHERE kind LIKE 'TAKEOVER%' OR kind LIKE "
                                   "'GM_RETIREMENT%' OR kind IN ('HALT_RAISED','HALT_CLEARED','POOL_ALLOCATED',"
                                   "'REALLOCATED','FUNDING_CLASSIFIED','FUNDS_IN_SUSPENSE') ORDER BY seq DESC LIMIT 200")]
            kc = db.one("SELECT ts, payload FROM audit_log WHERE kind IN ('BROKER_KEY_CHECK','BROKER_KEY_REFUSED') "
                        "ORDER BY seq DESC LIMIT 1")
            key = {"at": kc["ts"], **json.loads(kc["payload"])} if kc else None
            if key and "reason" in key:
                key["status"] = "REFUSED"
            gm = retirement.readiness(db, self.mode == "simulation")
            exclusive = db.get("account_takeover")
            blockers = []
            if self.mode not in ("simulation", "supervised_simulation") and not (key and key.get("status") == "READ_ONLY"):
                blockers.append("read-only eToro key for this engine not verified in this state directory (last "
                                f"check: {(key or {}).get('status', 'none recorded')})")
            if gm["steps"]["freeze"]["status"] != "DONE":
                blockers.append("Gold Miner is the live writer on the same account (no recorded frozen verification)")
            if foreign:
                blockers.append(f"{len(foreign)} position(s) on the account not owned by the engine (Gold Miner's): "
                                "the takeover must resolve them before the engine has exclusive ownership")
            if not exclusive:
                blockers.append("exclusive account ownership not verified (takeover not completed)")
            blockers += ["lost-close-response recovery (B3)", "live writer not exercised on real eToro",
                         "live release gates closed (docs/PRODUCTION_READINESS.md: NO-GO)"]
            needed = ["DISABLE_OLD_WRITER", "CLOSE_POSITIONS", "ACCEPT_LOSSES_AND_FEES", "ACTIVATE_STRATEGY",
                      "ENABLE_LIVE (always refused in this release)"]
            return {**self.meta(), "gold_miner_retirement": gm, "broker_key": key,
                    "pre_takeover": not exclusive,
                    "foreign_positions": foreign,
                    "pending_orders": [o.__dict__ for o in (snap.pending_orders if snap else [])],
                    "takeover": None if not tk else {
                        "run_id": tk["run_id"], "state": tk["state"], "plan_hash": tk["plan_hash"],
                        "created_at": tk["created_at"], "capital": plan and plan["capital"],
                        "allocation": plan and plan["allocation"], "risks": plan and plan["risks"],
                        "positions": plan and [{k: x[k] for k in ("position_id", "symbol", "direction", "leverage",
                                                                  "units", "exposure_usd", "est_close_proceeds",
                                                                  "proceeds_basis", "action")} for x in plan["positions"]],
                        "approvals": appr, "events": ev},
                    "required_approvals": needed,
                    "blockers": blockers,
                    "audit_trail": trail}
        finally:
            db.close()


def _next(sch, now, kind):
    """The current or next window of `kind` (decision after a close; execution at the next open)."""
    d = (now - dt.timedelta(days=4)).astimezone(NY).date()
    for _ in range(14):
        if sch.cal.is_trading_day(d):
            w = sch.decision_window(d) if kind == "decision" else sch.execution_window(d)
            if w.end > now:
                return {"for_session": d.isoformat(), "start": w.start.isoformat(), "end": w.end.isoformat(),
                        "open_now": w.start <= now < w.end}
        d += dt.timedelta(days=1)
    return None


def _verify_chain(db) -> tuple:
    from ..persistence.db import GENESIS
    from ..core.canonical import sha256_text
    prev, n = GENESIS, 0
    for row in db.q("SELECT * FROM audit_log ORDER BY seq"):
        if row["prev_hash"] != prev:
            return False, f"chain break at seq {row['seq']}"
        h = sha256_text(f"{prev}|{row['ts']}|{row['kind']}|{row['payload']}")
        if h != row["hash"]:
            return False, f"hash mismatch at seq {row['seq']}"
        prev, n = h, n + 1
    return True, f"{n} entries verified"
