"""Health and status: one structured report used by `status`, `health` (exit code) and the systemd watchdog."""
from __future__ import annotations

import datetime as dt
import json

from ..core import accounting, halts, sleeves, strategy_manager
from ..core.order_state import UNRESOLVED
from ..core.security import LIVE_RELEASE_ENABLED
from ..strategy_modules import REGISTRY
from ..strategy_modules.vendor import verify_all


def health(engine) -> dict:
    db, now = engine.db, engine.now()
    hb = db.one("SELECT * FROM heartbeat WHERE component='engine'")
    hb_age = (now - dt.datetime.fromisoformat(hb["ts"])).total_seconds() if hb else None
    act = strategy_manager.active(db)
    last_rec = db.one("SELECT payload FROM audit_log WHERE kind='RECONCILIATION' ORDER BY seq DESC LIMIT 1")
    rec = json.loads(last_rec["payload"]) if last_rec else None
    unresolved = [dict(r) for r in db.q(
        f"SELECT intent_id, status, symbol, action FROM intents WHERE status IN ({','.join('?' * len(UNRESOLVED))})",
        tuple(UNRESOLVED))]
    discs = [dict(r) for r in db.q("SELECT disc_id, kind, amount, ts FROM discrepancies WHERE status='OPEN'")]
    chain_ok, chain_msg = db.verify_audit_chain()
    try:
        vendored, vendored_ok = verify_all(), True
    except Exception as exc:  # noqa: BLE001
        vendored, vendored_ok = str(exc), False
    last_dec = db.one("SELECT strategy_id, session, status, error, created_at FROM decisions ORDER BY created_at DESC LIMIT 1")
    missed = [dict(r) for r in db.q("SELECT kind, session, ts FROM schedule_events ORDER BY id DESC LIMIT 10")]
    act_halts = halts.active(db) + halts.active_sleeve(db)
    dh = db.one("SELECT ts, source, session, status, detail FROM data_health ORDER BY id DESC LIMIT 1")
    backlog = db.one("SELECT COUNT(*) c FROM alerts WHERE delivered_at IS NULL AND suppressed=0")["c"]
    failing = db.one("SELECT COUNT(*) c FROM alerts WHERE delivered_at IS NULL AND attempts>0")["c"]
    stale = hb_age is None or hb_age > engine.cfg["monitoring"]["heartbeat_stale_seconds"]
    initial = engine.cfg["runtime"]["initial_strategy"]
    rep = {
        "at": now.isoformat(), "mode": engine.mode, "live_execution": "DISABLED: live writer implemented but the release gate is closed "
        "(LIVE_RELEASE_ENABLED=False) and mode=live is refused by the schema",
        "live_release_enabled": LIVE_RELEASE_ENABLED, "write_capability": engine.writer is not None,
        "broker": engine.broker.name, "broker_capabilities": engine.broker.capabilities(),
        "active_strategy": act if not engine.multi else {"strategy_id": "PORTFOLIO",
                                                          "sleeves": sleeves.weights(db)},
        "config_drift": (strategy_manager.drift(db, engine.specs) if not engine.multi else
                         [w for v in sleeves.drift(db, engine.specs, REGISTRY).values() for w in v]),
        "initial_strategy_note": None if engine.multi or not act or act["strategy_id"] == initial else
        f"engine.yaml initial_strategy={initial} is ignored after first start; use `strategy preview-switch`",
        "heartbeat_age_s": hb_age, "heartbeat_stale": stale,
        "last_reconciliation": rec and {k: rec.get(k) for k in ("point", "status", "strategy_equity",
                                                                 "available_cash", "unexplained_cash", "issues")},
        "halts": act_halts, "open_discrepancies": discs, "unresolved_intents": unresolved,
        "last_decision": dict(last_dec) if last_dec else None, "schedule_events": missed,
        "performance": accounting.performance(db), "audit_chain": chain_msg, "audit_chain_ok": chain_ok,
        "sleeve_performance": sleeves.performance(db) if engine.multi else None,
        "open_exceptions": [{k: x[k] for k in ("exception_id", "kind", "subject", "sleeve", "severity", "first_seen",
                                               "last_seen", "occurrences", "message")}
                            for x in halts.open_exceptions(db)],
        "market_data": dict(dh) if dh else None,
        "notifications": {"undelivered": backlog, "failing_delivery": failing,
                          "webhook_configured": bool(engine.notifier.url)},
        "vendored_sources_ok": vendored_ok, "schema_version": db.schema_version(),
        "log_write_failures": engine.log.failures, "log_last_error": engine.log.last_error,
    }
    rep["healthy"] = bool(chain_ok and vendored_ok and not act_halts and not unresolved and not discs and not stale
                          and not rep["config_drift"] and (not dh or dh["status"] == "OK") and not failing
                          and not engine.log.failures)
    return rep
