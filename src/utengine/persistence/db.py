"""Durable state: SQLite (WAL, synchronous=FULL) with ordered migrations and a hash-chained, append-only audit log.

Every state change that matters is written BEFORE the action it describes (an intent is persisted before the
broker is called) and inside a transaction. The audit log cannot be updated or deleted (SQL triggers abort), and
`verify_audit_chain()` recomputes every hash.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
from contextlib import contextmanager

from ..core.canonical import canonical_json, sha256_text

MIGRATIONS = [
    (1, """
CREATE TABLE engine_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE audit_log(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TABLE account_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, point TEXT NOT NULL,
  source TEXT NOT NULL, credit REAL NOT NULL, pending_open REAL NOT NULL, equity REAL, positions TEXT NOT NULL,
  pending_orders TEXT NOT NULL, raw_sha256 TEXT NOT NULL, provenance TEXT NOT NULL);
CREATE TABLE ownership(position_id TEXT PRIMARY KEY, instrument_id INTEGER NOT NULL, symbol TEXT,
  owner TEXT NOT NULL CHECK(owner IN ('ENGINE','PRE_EXISTING','EXTERNAL_UNACK','EXTERNAL_ACK')),
  strategy_id TEXT, intent_id TEXT, units REAL NOT NULL, amount REAL NOT NULL, first_seen TEXT NOT NULL,
  closed_at TEXT, close_intent_id TEXT, note TEXT);
CREATE TABLE cash_flows(flow_id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, amount REAL NOT NULL,
  external INTEGER NOT NULL, source TEXT NOT NULL, evidence TEXT NOT NULL, ref TEXT,
  recon_snapshot_id INTEGER, performance_ts TEXT);
CREATE TABLE discrepancies(disc_id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, amount REAL,
  detail TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED')), resolution TEXT);
CREATE TABLE halts(halt_id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
  scope TEXT NOT NULL CHECK(scope IN ('ALL','NEW_EXPOSURE')), reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','CLEARED')), cleared_at TEXT, cleared_by TEXT);
CREATE TABLE decisions(decision_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, session TEXT NOT NULL,
  status TEXT NOT NULL, decision TEXT, error TEXT, created_at TEXT NOT NULL, late INTEGER NOT NULL DEFAULT 0,
  UNIQUE(strategy_id, session));
CREATE TABLE plans(plan_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
  session TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, plan TEXT NOT NULL, mode TEXT NOT NULL);
CREATE TABLE intents(intent_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, leg_index INTEGER NOT NULL,
  phase INTEGER NOT NULL, action TEXT NOT NULL, symbol TEXT NOT NULL, instrument_id INTEGER NOT NULL,
  amount REAL, units REAL, position_id TEXT, status TEXT NOT NULL, broker_order_id TEXT, mode TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, pre_submit TEXT, result TEXT);
CREATE TABLE order_events(id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT NOT NULL, ts TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE daily_equity(session TEXT PRIMARY KEY, ts TEXT NOT NULL, account_equity REAL, strategy_equity REAL,
  credit REAL, external_flow REAL NOT NULL, pnl REAL, twr_index REAL, hwm REAL, drawdown REAL,
  exposure TEXT NOT NULL, strategy_id TEXT);
CREATE TABLE heartbeat(component TEXT PRIMARY KEY, ts TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, severity TEXT NOT NULL,
  kind TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE schedule_events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, strategy_id TEXT NOT NULL,
  kind TEXT NOT NULL, session TEXT NOT NULL, detail TEXT NOT NULL, UNIQUE(strategy_id, kind, session));
CREATE TABLE switch_plans(plan_hash TEXT PRIMARY KEY, from_strategy TEXT NOT NULL, to_strategy TEXT NOT NULL,
  created_at TEXT NOT NULL, status TEXT NOT NULL, plan TEXT NOT NULL, approved_by TEXT, finished_at TEXT);
CREATE TABLE strategy_state(strategy_id TEXT NOT NULL, session TEXT NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY(strategy_id, session));
"""),
    (2, """
ALTER TABLE alerts ADD COLUMN dedupe_key TEXT;
ALTER TABLE alerts ADD COLUMN action TEXT;
ALTER TABLE alerts ADD COLUMN delivered_at TEXT;
ALTER TABLE alerts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alerts ADD COLUMN last_error TEXT;
ALTER TABLE alerts ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE daily_equity ADD COLUMN foreign_value REAL;
CREATE TABLE data_health(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, source TEXT NOT NULL,
  session TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
"""),
    (3, """
ALTER TABLE halts ADD COLUMN subject TEXT;
ALTER TABLE halts ADD COLUMN sleeve TEXT;
CREATE TABLE exceptions(exception_id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, sleeve TEXT,
  severity TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED')), first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL, message TEXT NOT NULL, action TEXT, notified_at TEXT,
  resolved_at TEXT, resolved_by TEXT);
CREATE TABLE sleeve_ledger(seq INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL,
  sleeve TEXT NOT NULL, kind TEXT NOT NULL, cash REAL NOT NULL, instrument_id INTEGER, symbol TEXT,
  units REAL NOT NULL, external INTEGER NOT NULL, flow_id TEXT, intent_id TEXT, plan_id TEXT, detail TEXT NOT NULL);
CREATE TRIGGER sleeve_ledger_no_update BEFORE UPDATE ON sleeve_ledger BEGIN SELECT RAISE(ABORT, 'sleeve_ledger is append-only'); END;
CREATE TRIGGER sleeve_ledger_no_delete BEFORE DELETE ON sleeve_ledger BEGIN SELECT RAISE(ABORT, 'sleeve_ledger is append-only'); END;
CREATE TABLE sleeve_flow_posts(flow_id TEXT PRIMARY KEY, ts TEXT NOT NULL, policy TEXT NOT NULL);
CREATE TABLE sleeve_daily(session TEXT NOT NULL, sleeve TEXT NOT NULL, ts TEXT NOT NULL, equity REAL NOT NULL,
  cash REAL NOT NULL, holdings TEXT NOT NULL, external_flow REAL NOT NULL, pnl REAL NOT NULL, twr_index REAL NOT NULL,
  hwm REAL NOT NULL, drawdown REAL NOT NULL, PRIMARY KEY(session, sleeve));
CREATE TABLE takeover_runs(run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  state TEXT NOT NULL, account_id TEXT, release_id TEXT NOT NULL, plan_hash TEXT, plan TEXT, note TEXT);
CREATE TABLE takeover_events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE takeover_approvals(approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
  plan_hash TEXT NOT NULL, binding TEXT NOT NULL, approved_by TEXT NOT NULL, created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('VALID','REVOKED','INVALIDATED','CONSUMED')), status_reason TEXT);
CREATE TABLE takeover_writer_evidence(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  writer TEXT NOT NULL, ts TEXT NOT NULL, verdict TEXT NOT NULL, evidence TEXT NOT NULL, recorded_by TEXT NOT NULL);
"""),
]
GENESIS = "0" * 64


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class DB:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self._depth = 0
        self.migrate()

    def close(self):
        self.con.close()

    # ------------------------------------------------------------------ transactions / migrations
    @contextmanager
    def tx(self):
        if self._depth:
            self._depth += 1
            try:
                yield self.con
            finally:
                self._depth -= 1
            return
        self.con.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.con
            self.con.execute("COMMIT")
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    def migrate(self):
        self.con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, "
                         "applied_at TEXT NOT NULL)")
        done = {r[0] for r in self.con.execute("SELECT version FROM schema_migrations")}
        for v, sql in MIGRATIONS:
            if v in done:
                continue
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for stmt in [s.strip() for s in sql.split(";\n") if s.strip()]:
                    self.con.execute(stmt)
                self.con.execute("INSERT INTO schema_migrations VALUES(?,?)", (v, utcnow()))
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise

    def schema_version(self) -> int:
        r = self.con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(r[0] or 0)

    # ------------------------------------------------------------------ key/value state
    def get(self, key, default=None):
        r = self.con.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key, value):
        with self.tx():
            self.con.execute("INSERT INTO engine_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
                             "value=excluded.value, updated_at=excluded.updated_at",
                             (key, canonical_json(value), utcnow()))

    # ------------------------------------------------------------------ audit chain
    def audit(self, kind: str, payload: dict, ts: str | None = None) -> str:
        with self.tx():
            r = self.con.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev = r[0] if r else GENESIS
            ts = ts or utcnow()
            body = canonical_json(payload)
            h = sha256_text(f"{prev}|{ts}|{kind}|{body}")
            self.con.execute("INSERT INTO audit_log(ts,kind,payload,prev_hash,hash) VALUES(?,?,?,?,?)",
                             (ts, kind, body, prev, h))
            return h

    def verify_audit_chain(self) -> tuple:
        prev, n = GENESIS, 0
        for row in self.con.execute("SELECT * FROM audit_log ORDER BY seq"):
            if row["prev_hash"] != prev:
                return False, f"chain break at seq {row['seq']}"
            h = sha256_text(f"{prev}|{row['ts']}|{row['kind']}|{row['payload']}")
            if h != row["hash"]:
                return False, f"hash mismatch at seq {row['seq']}"
            prev, n = h, n + 1
        return True, f"{n} entries verified"

    # ------------------------------------------------------------------ helpers
    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        with self.tx():
            return self.con.execute(sql, args)

    def heartbeat(self, component: str, detail: dict, ts: str | None = None):
        self.x("INSERT INTO heartbeat VALUES(?,?,?) ON CONFLICT(component) DO UPDATE SET ts=excluded.ts, "
               "detail=excluded.detail", (component, ts or utcnow(), canonical_json(detail)))

    def alert(self, severity: str, kind: str, message: str, ts: str | None = None, dedupe_minutes: int = 60):
        """Record an alert for delivery (monitoring/notify.py). The same kind+message within `dedupe_minutes` is
        stored as suppressed (kept for the record, not re-sent)."""
        ts = ts or utcnow()
        key = sha256_text(f"{kind}|{message}")[:16]
        since = (dt.datetime.fromisoformat(ts) - dt.timedelta(minutes=dedupe_minutes)).isoformat()
        dup = self.one("SELECT 1 FROM alerts WHERE dedupe_key=? AND suppressed=0 AND ts>=?", (key, since))
        from ..monitoring.notify import action_for
        self.x("INSERT INTO alerts(ts,severity,kind,message,dedupe_key,action,suppressed) VALUES(?,?,?,?,?,?,?)",
               (ts, severity, kind, message, key, action_for(kind), 1 if dup else 0))
