"""Gold Miner retirement ledger (docs/GOLD_MINER_RETIREMENT_PLAN.md).

The retirement itself runs ON THE GOLD MINER HOST, by the owner, with deployment/gm_retirement/gm_retire.sh. This
engine never touches that host: it only RECORDS the evidence files the tool writes (inventory, preflight, freeze
verification, backup + restore drill, rollback, decommission, purge), durably and hash-chained (audit log), and computes
the readiness checklist the dashboard shows. Recording is idempotent by the evidence file's SHA-256, so a step can be
re-run and re-recorded after any failure (resumable).

Two-writer rule: the new engine may only be activated with a write capability after a recorded freeze verification
with `frozen: true`; Gold Miner may only be resumed (rollback) while this engine reports no write capability (checked
by the tool on the host). Neither side can be live at the same time as the other.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from .canonical import canonical_json
from .errors import EngineError

SCHEMAS = {"gm-retirement-inventory/1": "inventory", "gm-retirement-preflight/1": "preflight",
           "gm-retirement-freeze/1": "freeze", "gm-retirement-backup/1": "backup",
           "gm-retirement-restore-drill/1": "restore_drill", "gm-retirement-rollback/1": "rollback",
           "gm-retirement-decommission/1": "decommission", "gm-retirement-purge/1": "purge"}
STEPS = ("inventory", "preflight", "freeze", "backup", "restore_drill", "positions_planned", "positions_closed",
         "exclusive_ownership", "activation_gates", "decommission", "purge")


def records(db) -> list:
    return db.get("gm_retirement", [])


def record(db, path, recorded_by: str, now: str, takeover_run: str | None = None) -> dict:
    if not recorded_by.strip():
        raise EngineError("--recorded-by is required")
    raw = pathlib.Path(path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    step = SCHEMAS.get(doc.get("schema")) or ("restore_drill" if "files_verified" in doc else None)
    if step is None:
        raise EngineError(f"{path}: not a gm_retire.sh evidence file (schema {doc.get('schema')})")
    if "values" in json.dumps(doc) and step == "inventory" and doc.get("credentials") and \
            doc["credentials"].get("values") != "NEVER READ OR COPIED":
        raise EngineError("refused: an inventory must never contain credential values")
    sha = hashlib.sha256(raw).hexdigest()
    rows = records(db)
    if any(r["sha256"] == sha for r in rows):
        return next(r for r in rows if r["sha256"] == sha)          # idempotent: re-recording is a no-op
    rec = {"step": step, "sha256": sha, "file": str(path), "recorded_by": recorded_by, "recorded_at": now,
           "at": doc.get("at"), "summary": _summary(step, doc)}
    with db.tx():
        db.put("gm_retirement", rows + [rec])
        db.audit("GM_RETIREMENT_EVIDENCE", {**rec, "document": doc}, ts=now)
    if step == "freeze" and doc.get("frozen") and takeover_run:
        from . import takeover as T
        T.record_writer_evidence(_EngineShim(db), takeover_run, "gold-miner", "DISABLED_VERIFIED",
                                 f"gm_retire.sh freeze verification sha256 {sha[:16]} ({path}): "
                                 + ", ".join(k for k, v in doc["checks"].items() if v), recorded_by,
                                 __import__("datetime").datetime.fromisoformat(now))
    return rec


class _EngineShim:
    def __init__(self, db):
        self.db = db


def _summary(step, d) -> dict:
    if step == "inventory":
        return {"units": [(u["unit"], u.get("ActiveState"), u.get("UnitFileState")) for u in d["units"]],
                "armed": d["armed"], "kill_switch": d["kill_switch"], "real_kill_switch": d["real_kill_switch"],
                "state_files": len(d["state_files"]), "credential_variables": (d.get("credentials") or {}).get(
                    "variable_names"), "processes": len(d["processes"]), "cycle_lock_free": d["cycle_lock_free"]}
    if step == "preflight":
        return {"ok": d["ok"], "blockers": d["blockers"], "notes": d["notes"]}
    if step == "freeze":
        return {"frozen": d["frozen"], "checks": d["checks"]}
    if step == "backup":
        return {"archive": d["archive"], "archive_sha256": d["archive_sha256"], "files": len(d["repo_files"])}
    if step == "restore_drill":
        return {"ok": d["ok"], "files_verified": d["files_verified"], "problems": d["problems"]}
    return {k: v for k, v in d.items() if k != "schema"}


def readiness(db, writer_capable: bool) -> dict:
    """Checklist for the dashboard / runbook. Each item is DONE, PENDING or BLOCKED with the evidence or reason."""
    rows = records(db)
    last = {}
    for r in rows:
        last[r["step"]] = r
    tk = db.one("SELECT run_id, state, plan_hash FROM takeover_runs ORDER BY created_at DESC LIMIT 1")
    own = db.get("account_takeover")

    def item(done, evidence=None, reason=None):
        return {"status": "DONE" if done else ("BLOCKED" if reason else "PENDING"), "evidence": evidence,
                "reason": reason}
    fr = last.get("freeze")
    rd = last.get("restore_drill")
    out = {
        "inventory": item("inventory" in last, last.get("inventory", {}).get("sha256")),
        "preflight": item(bool(last.get("preflight", {}).get("summary", {}).get("ok")),
                          last.get("preflight", {}).get("sha256"),
                          "; ".join(last["preflight"]["summary"]["blockers"]) if "preflight" in last
                          and last["preflight"]["summary"]["blockers"] else None),
        "freeze": item(bool(fr and fr["summary"]["frozen"]), fr and fr["sha256"],
                       None if not fr or fr["summary"]["frozen"] else "freeze verification not frozen: " + ", ".join(
                           k for k, v in fr["summary"]["checks"].items() if not v)),
        "backup": item("backup" in last, last.get("backup", {}).get("sha256")),
        "restore_drill": item(bool(rd and rd["summary"]["ok"]), rd and rd["sha256"],
                              None if not rd or rd["summary"]["ok"] else "; ".join(rd["summary"]["problems"])),
        "positions_planned": item(bool(tk), tk and f"takeover {tk['run_id']} ({tk['state']})"),
        "positions_closed": item(bool(tk and tk["state"] in ("RECONCILE", "ALLOCATE", "ACTIVATE", "MONITOR")),
                                 tk and tk["state"]),
        "exclusive_ownership": item(bool(own), own and own.get("exclusive_verified_at")),
        "activation_gates": item(False, None, "live release gates closed (docs/PRODUCTION_READINESS.md: NO-GO)"),
        "decommission": item("decommission" in last, last.get("decommission", {}).get("sha256")),
        "purge": item("purge" in last, last.get("purge", {}).get("sha256")),
    }
    two_writers = bool(writer_capable and not (fr and fr["summary"]["frozen"]))
    return {"steps": out, "records": len(rows), "engine_write_capability": writer_capable,
            "two_writer_risk": two_writers,
            "two_writer_rule": "the engine may hold a write capability only after a recorded frozen verification; "
                               "Gold Miner may be resumed only while the engine has none",
            "next": next((k for k in STEPS if out[k]["status"] != "DONE"), None)}


def canonical(rec) -> str:
    return canonical_json(rec)
