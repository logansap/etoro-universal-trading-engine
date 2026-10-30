"""trading-engine CLI. Every command here has real, tested behaviour; there is no live-trading command."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import sys
import tempfile
import time

from . import __version__
from .core import accounting, halts, strategy_manager
from .core.configuration import (load_all_strategies, load_engine_config, strategy_content_fingerprint,
                                 _load_yaml)
from .core.errors import EngineError
from .core.execution import operator_resolve
from .core.reconciliation import acknowledge_external, ownership_report
from .strategy_modules import REGISTRY


def _engine(a):
    from .core.engine import TradingEngine
    clock = None
    if a.now:
        fixed = dt.datetime.fromisoformat(a.now)
        if fixed.tzinfo is None:
            raise SystemExit("--now needs a timezone offset, e.g. 2026-10-27T14:00:00+00:00")
        start = time.monotonic()
        clock = lambda: fixed + dt.timedelta(seconds=time.monotonic() - start)  # noqa: E731
    return TradingEngine(a.config, a.home, clock=clock)


def _print(obj):
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_config(a):
    if a.action == "validate":
        cfg = load_engine_config(a.config)
        sdir = pathlib.Path(cfg["runtime"]["strategies_dir"])
        sdir = sdir if sdir.is_absolute() else pathlib.Path(cfg["_path"]).parent.parent / sdir
        specs = load_all_strategies(sdir, REGISTRY)
        _print({"engine": cfg["_path"], "engine_fingerprint": cfg["_fingerprint"], "mode": cfg["runtime"]["mode"],
                "strategies": {k: {"implementation": v["implementation"], "enabled": v["enabled"],
                                   "fingerprint": v["fingerprint"], "module_status": REGISTRY[v["implementation"]].status}
                               for k, v in specs.items()}, "valid": True})
        return 0
    raw = _load_yaml(pathlib.Path(a.file))
    fp = strategy_content_fingerprint(raw)
    print(fp)
    if a.write:
        p = pathlib.Path(a.file)
        text = p.read_text(encoding="utf-8")
        old = raw.get("fingerprint")
        if old is None or text.count(str(old)) != 1:
            raise SystemExit("cannot locate a unique existing fingerprint line to replace")
        p.write_text(text.replace(str(old), fp), encoding="utf-8")
        print(f"wrote {p} (an active strategy will now show CONFIG_DRIFT until re-activated)")
    return 0


def cmd_account(a):
    e = _engine(a)
    now = e.nows()
    if a.action == "reconcile":
        r = e.reconcile("manual", e.now())
        _print({"status": r.status, "issues": r.issues, "strategy_equity": r.strategy_equity,
                "account_equity": r.account_equity, "available_cash": r.snapshot.available_cash if r.snapshot else None})
        return 0 if r.clean else 1
    if a.action == "status":
        _print({"ownership": ownership_report(e.db), "performance": accounting.performance(e.db),
                "cash_flows": [dict(x) for x in e.db.q("SELECT * FROM cash_flows ORDER BY ts")],
                "discrepancies": [dict(x) for x in e.db.q("SELECT * FROM discrepancies ORDER BY ts")]})
        return 0
    if a.action == "resolve":
        fid = accounting.resolve_discrepancy(e.db, a.discrepancy, a.classify, a.evidence, a.operator, now,
                                             intent_id=a.intent)
        print(f"discrepancy {a.discrepancy} classified as {a.classify} (flow {fid}). Clear the halt with "
              "`trading-engine halt-clear` after reviewing.")
        return 0
    if a.action == "acknowledge-external":
        acknowledge_external(e.db, a.position_id, a.operator, a.note, now)
        print(f"position {a.position_id} acknowledged as external (excluded, never touched)")
        return 0
    raise SystemExit(2)


def cmd_strategy(a):
    e = _engine(a)
    if a.action == "list":
        _print(strategy_manager.list_strategies(e.db, e.specs))
        return 0
    if a.action == "preview-switch":
        _print(e.switcher.preview(a.to, e.now()))
        return 0
    if a.action == "activate":
        res = e.switcher.activate(a.to, a.plan_hash, a.approved_by, e.now())
        _print(res)
        return 0 if res["status"] == "COMPLETED" else 1
    raise SystemExit(2)


def _pilot_db(a, live_ok: bool = False):
    """Operator commands that touch only the configuration and the database: no broker, no trading credential.
    Approvals exist only in supervised modes; the kill switch works in every mode with live_controls."""
    from .core.supervision import guard_active, is_supervised
    from .persistence.db import DB
    cfg = load_engine_config(a.config)
    mode = cfg["runtime"]["mode"]
    if not (is_supervised(mode) or (live_ok and guard_active(mode, cfg))):
        raise SystemExit("this command needs runtime.mode supervised_* (approvals) or, for the kill switch, any mode "
                         "with live_controls (live, supervised_*, simulation)")
    return cfg, DB(pathlib.Path(a.home) / "engine.sqlite"), pathlib.Path(a.home)


def cmd_orders(a):
    if a.action in ("pending", "approve", "reject"):
        from .core import supervision
        cfg, db, _home = _pilot_db(a)
        now = dt.datetime.now(dt.timezone.utc)
        if a.action == "pending":
            _print(supervision.pending(db))
        elif a.action == "approve":
            _print(supervision.approve(db, cfg, a.intent, a.approved_by, now, a.ttl_minutes))
        else:
            supervision.reject(db, a.intent, a.operator, a.reason, now.isoformat())
            print(f"{a.intent} -> BLOCKED (rejected by {a.operator})")
        return 0
    e = _engine(a)
    if a.action == "resolve":
        operator_resolve(e.db, e.broker, a.intent, a.as_status, a.operator, a.evidence, e.nows())
        print(f"{a.intent} -> {a.as_status}")
        return 0
    where = "" if a.all else "WHERE status NOT IN ('RECONCILED','SHADOW_RECORDED','ABANDONED','BLOCKED')"
    _print([dict(r) for r in e.db.q(f"SELECT intent_id, plan_id, phase, action, symbol, amount, units, position_id, "
                                     f"status, broker_order_id, mode, updated_at FROM intents {where} ORDER BY created_at")])
    return 0


def cmd_pilot_approval_template(a):
    from .core.security import PILOT_APPROVAL_PHRASE
    from .core.takeover import release_id
    cfg = load_engine_config(a.config)
    if cfg["runtime"]["mode"] != "supervised_live":
        raise SystemExit("the approval binds a supervised_live configuration: install it first, then run this")
    _print({"kind": "SUPERVISED_PILOT", "phrase": PILOT_APPROVAL_PHRASE, "release_sha": release_id(),
            "config_fingerprint": cfg["_fingerprint"], "approved_by": "<owner name>",
            "expires_at": "<ISO 8601 UTC, e.g. 2026-11-06T21:00:00+00:00>"})
    return 0


def cmd_live_activation_template(a):
    from .core.security import LIVE_ACTIVATION_PHRASE
    from .core.takeover import release_id
    cfg = load_engine_config(a.config)
    if cfg["runtime"]["mode"] != "live":
        raise SystemExit("the activation binds a live configuration: install it first, then run this")
    _print({"kind": "AUTONOMOUS_LIVE", "phrase": LIVE_ACTIVATION_PHRASE, "release_sha": release_id(),
            "config_fingerprint": cfg["_fingerprint"], "approved_by": "<owner name>",
            "expires_at": None})
    return 0


def cmd_kill_switch(a):
    from .core import supervision
    cfg, db, home = _pilot_db(a, live_ok=True)
    p = supervision.engage_kill_switch(cfg, home, a.operator, a.reason, dt.datetime.now(dt.timezone.utc).isoformat(), db)
    print(f"KILL SWITCH ENGAGED: {p} (no order can be sent; remove the file and clear the halt to resume)")
    return 0


def cmd_status(a):
    e = _engine(a)
    rep = e.status()
    if a.cmd == "health" and getattr(a, "notify", False):
        if rep["heartbeat_stale"]:
            e.db.alert("CRITICAL", "HEARTBEAT_STALE", f"no engine heartbeat for {rep['heartbeat_age_s']} s",
                       ts=e.nows())
        rep["notifications_flush"] = e.flush_notifications(e.now())
    _print(rep)
    return 0 if (rep["healthy"] or a.cmd == "status") else 1


def cmd_report(a):
    from .core.accounting import account_report
    e = _engine(a)
    r = e.reconcile("report", e.now())
    rep = account_report(e.db, r)
    if e.multi:
        from .core.portfolio_engine import portfolio_report
        rep["portfolio"] = portfolio_report(e, r)
    _print(rep)
    return 0


def cmd_portfolio(a):
    from .core import sleeves
    from .core.portfolio_engine import portfolio_report
    e = _engine(a)
    if not e.multi:
        raise SystemExit("portfolio commands need portfolio.mode: multi_strategy")
    r = e.reconcile("portfolio", e.now())
    if a.action == "status":
        _print({"reconciliation": r.status, "issues": r.issues, **portfolio_report(e, r)})
        return 0 if r.clean else 1
    if not r.clean:
        raise SystemExit("reconciliation is not clean: " + "; ".join(i["kind"] for i in r.issues))
    val = e.sleeve_valuation(r)
    if a.action == "restructure-preview":
        w = {k: float(v) for k, v in (x.split("=") for x in a.weights.split(","))}
        _print(sleeves.restructure_preview(e.db, w, val, e.specs, REGISTRY, e.nows()))
        return 0
    if a.action == "restructure":
        _print(sleeves.restructure(e.db, a.preview_hash, a.approved_by, val, e.nows()))
        return 0
    if a.action == "reallocate-preview":
        w = {k: float(v) for k, v in (x.split("=") for x in a.weights.split(","))}
        _print(sleeves.reallocation_preview(e.db, w, val, e.nows()))
        return 0
    _print(sleeves.reallocate(e.db, a.preview_hash, a.approved_by, val, e.nows()))
    return 0


def cmd_exceptions(a):
    e = _engine(a)
    rows = halts.open_exceptions(e.db) if not a.all else [dict(r) for r in e.db.q(
        "SELECT * FROM exceptions ORDER BY last_seen DESC")]
    _print(rows)
    return 0


def _dash_ctx(a):
    cfg = load_engine_config(a.config)
    sdir = pathlib.Path(cfg["runtime"]["strategies_dir"])
    sdir = sdir if sdir.is_absolute() else pathlib.Path(cfg["_path"]).parent.parent / sdir
    return cfg, load_all_strategies(sdir, REGISTRY)


def cmd_dashboard(a):
    """The dashboard never constructs the trading engine: it reads the engine's database read-only."""
    from .dashboard import server
    if a.action == "set-password":
        pw = pathlib.Path(a.password_file).read_text(encoding="utf-8").strip() if a.password_file else None
        if pw is None:
            import getpass
            pw = getpass.getpass("new dashboard password (>= 12 characters): ")
        print(f"password hash written to {server.set_password(a.home, pw)} (scrypt; the password itself is not stored)")
        return 0
    if a.action == "login-link":
        up, inst = server.probe_instance(a.port)
        if not up:
            print(f"warning: no dashboard answers on 127.0.0.1:{a.port}; start it (dashboard serve --port {a.port}) "
                  f"or pass the --port it runs on", file=sys.stderr)
        elif inst != server.instance_id(a.home):
            print(f"refused: the dashboard on 127.0.0.1:{a.port} serves a different state directory than "
                  f"{a.home}; a link minted here would be rejected there", file=sys.stderr)
            return 2
        tok = server.new_login_link(a.home)
        print(f"http://127.0.0.1:{a.port}/login?otp={tok}&next=/  (single use, valid {server.OTP_TTL} s)")
        return 0
    from .dashboard.data import Data
    cfg, specs = _dash_ctx(a)
    print(f"read-only dashboard on http://{a.host}:{a.port}/ (mode {cfg['runtime']['mode']}; Ctrl+C stops)")
    clock = None
    if a.now:                                   # rehearsal / demo clock, as for the engine commands
        fixed, start = dt.datetime.fromisoformat(a.now), time.monotonic()
        clock = lambda: fixed + dt.timedelta(seconds=time.monotonic() - start)  # noqa: E731
    server.serve(Data(cfg, specs, a.home, clock=clock), a.home, host=a.host, port=a.port, allow_remote=a.allow_remote,
                 secure_cookie=a.secure_cookie)
    return 0


def cmd_retirement(a):
    from .core import retirement
    from .persistence.db import DB
    db = DB(pathlib.Path(a.home) / "engine.sqlite")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if a.action == "record":
        _print(retirement.record(db, a.evidence, a.recorded_by, now, takeover_run=a.takeover_run))
        return 0
    cfg = load_engine_config(a.config)
    _print(retirement.readiness(db, cfg["runtime"]["mode"] == "simulation"))
    return 0


def cmd_takeover(a):
    from .core import takeover as T
    e = _engine(a)
    now = e.now()
    if a.action == "preview":
        pv = T.preview(e, now)
        if a.report:
            pathlib.Path(a.report).write_text(T.render(pv), encoding="utf-8")
        _print({k: pv[k] for k in ("run_id", "plan_hash", "state", "capital", "allocation", "risks",
                                   "remaining_after_takeover")})
        return 0
    if a.action == "status":
        r = T.run(e.db, a.run)
        ev = [dict(x) for x in e.db.q("SELECT ts, from_state, to_state, detail FROM takeover_events WHERE run_id=? "
                                      "ORDER BY id", (a.run,))]
        ap = [dict(x) for x in e.db.q("SELECT approval_id, kind, approved_by, created_at, expires_at, status, "
                                      "status_reason FROM takeover_approvals WHERE run_id=?", (a.run,))]
        _print({"run_id": a.run, "state": r["state"], "plan_hash": r["plan_hash"], "events": ev, "approvals": ap})
        return 0
    if a.action == "approve":
        _print(T.approve(e, a.run, a.kind, a.approved_by, now, max_cost_usd=a.max_cost,
                         max_exposure_usd=a.max_exposure, ttl_hours=a.ttl_hours))
        return 0
    if a.action == "revoke":
        T.revoke(e, a.approval, a.operator, a.reason, now)
        return 0
    if a.action == "cancel":
        T.cancel(e, a.run, a.operator, a.reason, now)
        return 0
    if a.action == "writer-evidence":
        T.record_writer_evidence(e, a.run, a.writer, a.verdict, a.evidence, a.operator, now)
        return 0
    if a.action == "enable-live":
        T.enable_live(e, a.run, now)
    _print(T.advance(e, a.run, now, a.operator))
    return 0


def cmd_data(a):
    e = _engine(a)
    out = e.data_check()
    e.flush_notifications(e.now())
    _print(out)
    return 0 if out["status"] == "OK" else 1


def cmd_notify(a):
    e = _engine(a)
    if a.action == "test":
        e.db.alert(a.severity, "NOTIFY_TEST", a.message, ts=e.nows(), dedupe_minutes=0)
    elif a.action == "crash":
        e.db.alert("CRITICAL", "CRASH", f"systemd reported a failure of {a.unit}", ts=e.nows())
    _print(e.flush_notifications(e.now()))
    return 0


def cmd_halt(a):
    e = _engine(a)
    if a.cmd == "halt":
        hid = halts.raise_halt(e.db, "EMERGENCY_HALT", "NEW_EXPOSURE", f"operator {a.operator}: {a.reason}", e.nows())
        print(f"emergency halt {hid} active: no new exposure (nothing is liquidated)")
    else:
        halts.clear(e.db, a.id, a.operator, a.reason, e.nows())
        print(f"halt {a.id} cleared by {a.operator}")
    return 0


def cmd_run(a):
    e = _engine(a)
    print(json.dumps(e.startup(), default=str))
    if a.once:
        try:
            print(json.dumps(e.cycle(), default=str))
        except BaseException as exc:
            # an aborted cycle must not look like a clean finish in the audit log (rehearsal finding 2026-10-29)
            e.shutdown(f"run --once aborted: {type(exc).__name__}")
            raise
        e.shutdown("run --once finished")
        return 0
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = e.cfg["runtime"]["cycle_interval_seconds"]
    while not stop["flag"]:
        try:
            e.cycle()
        except EngineError as exc:
            e.db.alert("CRITICAL", "CYCLE_ERROR", str(exc))
            e.log("cycle_error", error=str(exc))
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)
    e.shutdown("SIGTERM/SIGINT")
    return 0


def cmd_broker_audit(a):
    """Reconcile the REAL account from saved read-only Gold Miner audit snapshots, in a scratch state directory."""
    from .core.accounting import account_report
    from .core.reconciliation import reconcile as _rec
    from .persistence.db import DB
    from .tools.gm_audit import AuditReplayReader, history_summary, load_audit
    cfg = load_engine_config(a.config)
    scratch = pathlib.Path(a.scratch_home)
    if (scratch / "engine.sqlite").exists():
        raise SystemExit(f"{scratch} already has a database; use an empty scratch directory")
    docs = [load_audit(p) for p in a.snapshot]
    sdir = pathlib.Path(cfg["runtime"]["strategies_dir"])
    sdir = sdir if sdir.is_absolute() else pathlib.Path(cfg["_path"]).parent.parent / sdir
    specs = load_all_strategies(sdir, REGISTRY)
    symbols = {inst["etoro_instrument_id"]: sym for s in specs.values() for sym, inst in s["instruments"].items()
               if inst["etoro_instrument_id"]}
    db = DB(scratch / "engine.sqlite")
    reader = AuditReplayReader(docs, symbols)
    runs = []
    r = None
    for d in docs:
        r = _rec(db, reader, cfg, "audit-replay", d["generated_at"])
        runs.append({"snapshot_generated_at": d["generated_at"], "status": r.status, "issues": r.issues,
                     "available_cash": r.snapshot.available_cash, "strategy_equity": r.strategy_equity})
    _print({"runs": runs, "ownership": ownership_report(db), "report": account_report(db, r),
            "trade_history": history_summary(docs[-1]), "unrealized_pnl": "UNAVAILABLE in audit snapshots"})
    return 0


def cmd_broker(a):
    if a.action == "reconcile-audit":
        return cmd_broker_audit(a)
    e = _engine(a)
    caps = e.broker.capabilities()
    out = {"broker": e.broker.name, "capabilities": caps}
    try:
        snap = e.broker.account_snapshot()
        out["read"] = {"ok": True, "credit": snap.credit, "available_cash": snap.available_cash,
                       "positions": len(snap.positions), "pending_orders": len(snap.pending_orders),
                       "equity": snap.equity}
        if hasattr(e.broker, "whoami"):
            out["whoami"] = e.broker.whoami()
    except Exception as exc:  # noqa: BLE001
        out["read"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _print(out)
    return 0 if out["read"]["ok"] else 1


def cmd_verify(a):
    e = _engine(a)
    from .strategy_modules.vendor import verify_all
    ok, msg = e.db.verify_audit_chain()
    integ = e.db.one("PRAGMA integrity_check")[0]
    _print({"audit_chain": msg, "audit_chain_ok": ok, "sqlite_integrity": integ, "vendored_sources": verify_all(),
            "engine_fingerprint": e.cfg["_fingerprint"], "config_drift": strategy_manager.drift(e.db, e.specs)})
    return 0 if ok and integ == "ok" else 1


def _offsite_push(cfg, local_file: pathlib.Path, manifest: pathlib.Path, env=None) -> dict:
    """Push a backup off the host, then pull it back into a temporary directory and run the restore drill on it."""
    import subprocess
    env = os.environ if env is None else env
    b = cfg.get("backup") or {}
    method = b.get("offsite_method", "none")
    if method == "none":
        return {"offsite": "disabled (backup.offsite_method: none)"}
    target = env.get(b.get("offsite_target_env") or "")
    if not target:
        raise SystemExit(f"offsite target env var {b.get('offsite_target_env')} is not set")
    with tempfile.TemporaryDirectory() as td:
        back = pathlib.Path(td) / local_file.name
        if method == "copy":
            dest = pathlib.Path(target)
            dest.mkdir(parents=True, exist_ok=True)
            for f in (local_file, manifest):
                shutil.copy2(f, dest / f.name)
            shutil.copy2(dest / local_file.name, back)
            shutil.copy2(dest / manifest.name, pathlib.Path(td) / manifest.name)
        else:
            for f in (local_file, manifest):
                subprocess.run(["rsync", "-a", "--checksum", str(f), target.rstrip("/") + "/"], check=True)
            for f in (local_file, manifest):
                subprocess.run(["rsync", "-a", target.rstrip("/") + "/" + f.name, str(pathlib.Path(td) / f.name)],
                               check=True)
        res = _verify_backup_file(back)
    return {"offsite": method, "target_env": b.get("offsite_target_env"), "pulled_back_and_verified": res}


def _verify_backup_file(b: pathlib.Path) -> dict:
    from .persistence.db import DB
    man = json.loads(b.with_name(b.name.replace(".sqlite", ".manifest.json")).read_text())
    sha_ok = hashlib.sha256(b.read_bytes()).hexdigest() == man["sha256"]
    with tempfile.TemporaryDirectory() as td:
        tgt = pathlib.Path(td) / "engine.sqlite"
        shutil.copy2(b, tgt)
        db = DB(tgt)
        ok, msg = db.verify_audit_chain()
        integ = db.one("PRAGMA integrity_check")[0]
        counts = {t: db.one(f"SELECT COUNT(*) c FROM {t}")["c"] for t in ("audit_log", "intents", "decisions",
                                                                           "cash_flows", "account_snapshots")}
        db.close()
    return {"sha256_ok": sha_ok, "audit_chain": msg, "audit_chain_ok": ok, "integrity": integ, "rows": counts,
            "ok": bool(sha_ok and ok and integ == "ok")}


def cmd_backup(a):
    home = pathlib.Path(a.home)
    src = home / "engine.sqlite"
    dest = pathlib.Path(a.to)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = dest / f"engine-{stamp}.sqlite"
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(out))
    with d:
        s.backup(d)
    s.close()
    d.close()
    extra = {}
    for name in ("sim_broker.json",):
        if (home / name).exists():
            shutil.copy2(home / name, dest / f"{name}.{stamp}")
            extra[name] = hashlib.sha256((home / name).read_bytes()).hexdigest()
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    man = dest / f"engine-{stamp}.manifest.json"
    man.write_text(json.dumps({"file": out.name, "sha256": sha, "extra": extra, "created": stamp}, indent=1))
    result = {"backup": str(out), "sha256": sha, "local_verify": _verify_backup_file(out)}
    cfg = load_engine_config(a.config)
    retain = (cfg.get("backup") or {}).get("retain")
    if retain:
        olds = sorted(dest.glob("engine-*.sqlite"))[:-retain]
        for f in olds:                                   # prunes only this tool's own backup files
            f.unlink()
            f.with_name(f.name.replace(".sqlite", ".manifest.json")).unlink(missing_ok=True)
        result["pruned"] = [f.name for f in olds]
    if a.offsite:
        result.update(_offsite_push(cfg, out, man))
    _print(result)
    ok = result["local_verify"]["ok"] and result.get("pulled_back_and_verified", {"ok": True})["ok"]
    if not ok:
        from .persistence.db import DB
        DB(home / "engine.sqlite").alert("CRITICAL", "BACKUP_FAILED", f"backup verification failed: {out.name}")
    return 0 if ok else 1


def cmd_restore_verify(a):
    res = _verify_backup_file(pathlib.Path(a.backup))
    _print({"restored_to_temp": True, **res})
    return 0 if res["ok"] else 1


def cmd_sim(a):
    e = _engine(a)
    if e.mode not in ("simulation", "supervised_simulation"):
        raise SystemExit("sim commands exist only in simulation modes")
    if a.action == "deposit":
        e.broker.deposit(a.amount, evidence=a.evidence or "operator test deposit (simulation)")
    elif a.action == "external-open":
        e.broker.external_open(a.instrument_id, a.amount)
    print(json.dumps({"credit": e.broker.s["credit"]}))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="trading-engine", description=f"eToro universal trading engine {__version__} "
                                "(simulation / shadow only; live order submission is not implemented)")
    p.add_argument("--config", default=os.environ.get("UTE_CONFIG", "config/engine.yaml"))
    p.add_argument("--home", default=os.environ.get("UTE_HOME", "var"))
    p.add_argument("--now", default=None, help="fixed clock start (ISO with offset) for rehearsals and tests")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("config")
    cs = c.add_subparsers(dest="action", required=True)
    cs.add_parser("validate")
    f = cs.add_parser("fingerprint")
    f.add_argument("file")
    f.add_argument("--write", action="store_true")
    c.set_defaults(fn=cmd_config)
    ac = sub.add_parser("account")
    acs = ac.add_subparsers(dest="action", required=True)
    acs.add_parser("reconcile")
    acs.add_parser("status")
    r = acs.add_parser("resolve")
    r.add_argument("--discrepancy", required=True)
    r.add_argument("--classify", required=True, choices=sorted(accounting.CLASSIFIABLE))
    r.add_argument("--evidence", required=True)
    r.add_argument("--operator", required=True)
    r.add_argument("--intent", default=None, help="TRADE_CLOSE only: the filled close intent without proceeds")
    x = acs.add_parser("acknowledge-external")
    x.add_argument("--position-id", required=True)
    x.add_argument("--operator", required=True)
    x.add_argument("--note", required=True)
    ac.set_defaults(fn=cmd_account)
    st = sub.add_parser("strategy")
    sts = st.add_subparsers(dest="action", required=True)
    sts.add_parser("list")
    pv = sts.add_parser("preview-switch")
    pv.add_argument("--to", required=True)
    av = sts.add_parser("activate")
    av.add_argument("--to", required=True)
    av.add_argument("--plan-hash", required=True)
    av.add_argument("--approved-by", required=True)
    st.set_defaults(fn=cmd_strategy)
    o = sub.add_parser("orders")
    o.add_argument("--all", action="store_true")
    os_ = o.add_subparsers(dest="action")
    rs = os_.add_parser("resolve")
    rs.add_argument("--intent", required=True)
    rs.add_argument("--as", dest="as_status", required=True, choices=["NOT_SUBMITTED", "REJECTED"])
    rs.add_argument("--operator", required=True)
    rs.add_argument("--evidence", required=True)
    os_.add_parser("pending", help="supervised pilot: intents waiting for a human approval")
    oa = os_.add_parser("approve", help="supervised pilot: approve ONE intent (bound to its exact content)")
    oa.add_argument("--intent", required=True)
    oa.add_argument("--approved-by", required=True)
    oa.add_argument("--ttl-minutes", type=float, default=None)
    orj = os_.add_parser("reject", help="supervised pilot: reject ONE intent (it becomes BLOCKED)")
    orj.add_argument("--intent", required=True)
    orj.add_argument("--operator", required=True)
    orj.add_argument("--reason", required=True)
    o.set_defaults(fn=cmd_orders)
    ks = sub.add_parser("kill-switch", help="supervised pilot: engage the kill switch (file + halt ALL)")
    ks.add_argument("--operator", required=True)
    ks.add_argument("--reason", required=True)
    ks.set_defaults(fn=cmd_kill_switch)
    pt = sub.add_parser("pilot-approval-template", help="print the owner approval file skeleton for THIS release and "
                                                         "configuration (the owner fills approved_by and expires_at)")
    pt.set_defaults(fn=cmd_pilot_approval_template)
    lt = sub.add_parser("live-activation-template", help="print the ONE-TIME autonomous-live activation skeleton for "
                                                         "THIS release and configuration (expires_at null = until "
                                                         "revoked; the owner may set an ISO date instead)")
    lt.set_defaults(fn=cmd_live_activation_template)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    hp = sub.add_parser("health")
    hp.add_argument("--notify", action="store_true", help="alert on a stale heartbeat and flush notifications")
    hp.set_defaults(fn=cmd_status)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    pf = sub.add_parser("portfolio", help="multi-strategy sleeves")
    pfs = pf.add_subparsers(dest="action", required=True)
    pfs.add_parser("status")
    rp = pfs.add_parser("reallocate-preview")
    rp.add_argument("--weights", required=True, help="e.g. QC_529_ETORO_RESTRICTED=0.6,QC_60_SQQQ=0.4")
    ra_ = pfs.add_parser("reallocate")
    ra_.add_argument("--preview-hash", required=True)
    ra_.add_argument("--approved-by", required=True)
    rsp = pfs.add_parser("restructure-preview", help="add/retire sleeves (e.g. 50/50 -> one 100%% sleeve); cash only")
    rsp.add_argument("--weights", required=True, help="e.g. QC_529_ETORO_API2=1.0")
    rs_ = pfs.add_parser("restructure")
    rs_.add_argument("--preview-hash", required=True)
    rs_.add_argument("--approved-by", required=True)
    pf.set_defaults(fn=cmd_portfolio)
    ex = sub.add_parser("exceptions", help="durable open exceptions (one per issue)")
    ex.add_argument("--all", action="store_true")
    ex.set_defaults(fn=cmd_exceptions)
    dsh = sub.add_parser("dashboard", help="authenticated, read-only web dashboard")
    dss = dsh.add_subparsers(dest="action", required=True)
    sv = dss.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8790)
    sv.add_argument("--allow-remote", action="store_true", help="bind a non-loopback address (prefer an SSH tunnel)")
    sv.add_argument("--secure-cookie", action="store_true", help="set Secure on the session cookie (behind TLS)")
    spw = dss.add_parser("set-password")
    spw.add_argument("--password-file", default=None, help="read the password from this file (else prompt)")
    ll = dss.add_parser("login-link")
    ll.add_argument("--port", type=int, default=8790)
    dsh.set_defaults(fn=cmd_dashboard)
    rt = sub.add_parser("retirement", help="Gold Miner retirement evidence ledger (the tool runs on the GM host)")
    rts = rt.add_subparsers(dest="action", required=True)
    rr = rts.add_parser("record")
    rr.add_argument("--evidence", required=True)
    rr.add_argument("--recorded-by", required=True)
    rr.add_argument("--takeover-run", default=None, help="a frozen verification also becomes this run's writer evidence")
    rts.add_parser("status")
    rt.set_defaults(fn=cmd_retirement)
    tk = sub.add_parser("takeover", help="full-account takeover: read-only preview + approval-gated steps")
    tks = tk.add_subparsers(dest="action", required=True)
    tp = tks.add_parser("preview")
    tp.add_argument("--report", default=None, help="write the human-readable report to this file")
    for name in ("status", "advance", "enable-live"):
        t_ = tks.add_parser(name)
        t_.add_argument("--run", required=True)
        t_.add_argument("--operator", default="")
    ta = tks.add_parser("approve")
    ta.add_argument("--run", required=True)
    ta.add_argument("--kind", required=True, choices=["DISABLE_OLD_WRITER", "CLOSE_POSITIONS",
                                                      "ACCEPT_LOSSES_AND_FEES", "ACTIVATE_STRATEGY", "ENABLE_LIVE"])
    ta.add_argument("--approved-by", required=True)
    ta.add_argument("--max-cost", type=float, default=None)
    ta.add_argument("--max-exposure", type=float, default=None)
    ta.add_argument("--ttl-hours", type=float, default=None)
    tr = tks.add_parser("revoke")
    tr.add_argument("--approval", required=True)
    tr.add_argument("--operator", required=True)
    tr.add_argument("--reason", required=True)
    tc = tks.add_parser("cancel")
    tc.add_argument("--run", required=True)
    tc.add_argument("--operator", required=True)
    tc.add_argument("--reason", required=True)
    tw = tks.add_parser("writer-evidence")
    tw.add_argument("--run", required=True)
    tw.add_argument("--writer", required=True)
    tw.add_argument("--verdict", required=True, choices=["DISABLED_VERIFIED", "ACTIVE"])
    tw.add_argument("--evidence", required=True)
    tw.add_argument("--operator", required=True)
    tk.set_defaults(fn=cmd_takeover)
    dp = sub.add_parser("data")
    dp.add_subparsers(dest="action", required=True).add_parser("check")
    dp.set_defaults(fn=cmd_data)
    nt = sub.add_parser("notify")
    nts = nt.add_subparsers(dest="action", required=True)
    nts.add_parser("flush")
    tt = nts.add_parser("test")
    tt.add_argument("--message", default="test notification from trading-engine")
    tt.add_argument("--severity", default="WARNING", choices=["INFO", "WARNING", "CRITICAL"])
    cr = nts.add_parser("crash")
    cr.add_argument("--unit", default="etoro-ute.service")
    nt.set_defaults(fn=cmd_notify)
    h = sub.add_parser("halt")
    h.add_argument("--reason", required=True)
    h.add_argument("--operator", required=True)
    h.set_defaults(fn=cmd_halt)
    hc = sub.add_parser("halt-clear")
    hc.add_argument("--id", required=True)
    hc.add_argument("--operator", required=True)
    hc.add_argument("--reason", required=True)
    hc.set_defaults(fn=cmd_halt)
    rn = sub.add_parser("run")
    g = rn.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true")
    g.add_argument("--service", action="store_true")
    rn.set_defaults(fn=cmd_run)
    b = sub.add_parser("broker")
    bs = b.add_subparsers(dest="action", required=True)
    bs.add_parser("probe")
    ra = bs.add_parser("reconcile-audit", help="reconcile the real account from saved read-only GM audit snapshots")
    ra.add_argument("--snapshot", action="append", required=True)
    ra.add_argument("--scratch-home", required=True)
    b.set_defaults(fn=cmd_broker)
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    bk = sub.add_parser("backup")
    bk.add_argument("--to", required=True)
    bk.add_argument("--offsite", action="store_true", help="also push off-host and verify the pulled-back copy")
    bk.set_defaults(fn=cmd_backup)
    rv = sub.add_parser("restore-verify")
    rv.add_argument("--backup", required=True)
    rv.set_defaults(fn=cmd_restore_verify)
    sm = sub.add_parser("sim")
    sms = sm.add_subparsers(dest="action", required=True)
    d = sms.add_parser("deposit")
    d.add_argument("--amount", type=float, required=True)
    d.add_argument("--evidence", default=None)
    eo = sms.add_parser("external-open")
    eo.add_argument("--instrument-id", type=int, required=True)
    eo.add_argument("--amount", type=float, required=True)
    sm.set_defaults(fn=cmd_sim)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a) or 0
    except EngineError as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
