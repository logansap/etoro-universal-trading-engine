"""Actionable notifications.

Every alert (db.alert) is stored with the operator ACTION that resolves it. Notifier.flush() delivers undelivered,
non-suppressed alerts at or above notifications.min_severity:
  * always to the local outbox file  <UTE_HOME>/alerts.jsonl  (journald also receives every line via the service);
  * to a webhook when the environment variable named by notifications.webhook_url_env is set (JSON POST with a
    Slack/Teams/ntfy-compatible "text" field). The URL is a secret: it lives only in the environment and is
    redacted from logs.
Delivery failures are retried on later cycles (attempts, last_error) up to max_attempts; a failed delivery never
stops a trading cycle. Duplicate alerts inside notifications.dedupe_minutes are recorded but not re-sent.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import urllib.request

SEVERITY = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
ACTIONS = {
    "CASH_DISCREPANCY": "Check the eToro statement, then `trading-engine account resolve --discrepancy <id> --classify "
                        "<KIND> --evidence <ref> --operator <you>`, then `halt-clear`.",
    "EXTERNAL_POSITION": "Identify who traded. If legitimate: `trading-engine account acknowledge-external --position-id "
                         "<id> --operator <you> --note <why>`, then `halt-clear`. Never close it from the engine.",
    "EXTERNAL_PENDING_ORDER": "A pending order not created by the engine exists. Verify it in eToro before clearing.",
    "EXTERNAL_CHANGE": "A foreign position changed or closed. Verify, then classify any cash effect and clear.",
    "POSITION_MISMATCH": "An engine position changed outside the engine. Investigate before `halt-clear`.",
    "POSITION_MISSING": "An engine position vanished (manual close, broker stop?). Investigate before `halt-clear`.",
    "UNKNOWN_ORDER": "Order outcome unknown. Check eToro for the intent's instrument and amount; the engine re-queries "
                     "every cycle. If nothing exists: `trading-engine orders resolve --as NOT_SUBMITTED`.",
    "UNRESOLVED_ORDER": "An order is pending or partial at the broker; the engine keeps polling it. `halt-clear` after "
                        "`trading-engine orders` shows it final.",
    "CONFIG_DRIFT": "A strategy YAML or module changed. Revert it, or re-activate via `strategy preview-switch`.",
    "BROKER_UNAVAILABLE": "Broker reads fail. Check credentials (`trading-engine broker probe`) and network; clears itself.",
    "SWITCH_FAILED": "A strategy switch partly executed. Review `trading-engine orders --all`, then preview again.",
    "DAILY_LOSS_LIMIT": "Daily loss limit hit: no new exposure. Review, then `halt-clear` with the owner's decision.",
    "DRAWDOWN_LIMIT": "Drawdown limit hit: no new exposure. Owner decision required before `halt-clear`.",
    "EMERGENCY_HALT": "Operator emergency halt is active. `halt-clear` when the reason is resolved.",
    "MISSED_DECISION": "A decision window passed without a decision (engine down or data missing). Not caught up; "
                       "check `trading-engine status` and the service journal.",
    "MISSED_EXECUTION": "An execution window passed without execution. Not caught up; check the service journal.",
    "NO_DECISION": "No decision: market data stale or gapped (`trading-engine data check`). Nothing was traded.",
    "DATA_STALE": "Market data for the last session is missing or suspect. Run `trading-engine data check`.",
    "CRASH": "The engine stopped without a clean shutdown. `systemctl status etoro-ute`, `journalctl -u etoro-ute`; "
             "startup reconciliation has run -- check `trading-engine status`.",
    "HEARTBEAT_STALE": "No engine heartbeat. `systemctl status etoro-ute`; restart if needed.",
    "ORDER_REJECTED": "The broker rejected an order (not retried). See `trading-engine orders --all`.",
    "CYCLE_ERROR": "A cycle raised an error. See the journal; the next cycle retries.",
    "ACCOUNT_SIZING_BLOCKED": "The broker gave no unrealized P&L for an engine position, so strategy equity is "
                              "unknown and nothing can be sized. Check `trading-engine broker probe` / `report`.",
    "BACKUP_FAILED": "Backup or restore verification failed. Run `trading-engine backup --offsite` by hand and inspect.",
}


def action_for(kind: str) -> str:
    return ACTIONS.get(kind, "See docs/OPERATIONS_RUNBOOK.md.")


class Notifier:
    def __init__(self, db, cfg: dict, home, env=None, transport=None, redactor=None):
        self.db, self.cfg = db, cfg.get("notifications") or {}
        self.home = pathlib.Path(home)
        env = os.environ if env is None else env
        name = self.cfg.get("webhook_url_env")
        self.url = env.get(name) if name else None
        self.transport = transport or self._post
        self.redact = redactor or (lambda s: s)
        self.instance = cfg["engine"]["instance_id"]

    def _post(self, url, payload: bytes, timeout):
        req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status

    def flush(self, now: str | None = None) -> dict:
        now = now or dt.datetime.now(dt.timezone.utc).isoformat()
        floor = SEVERITY.get(self.cfg.get("min_severity", "WARNING"), 1)
        max_attempts = self.cfg.get("max_attempts", 10)
        rows = self.db.q("SELECT * FROM alerts WHERE delivered_at IS NULL AND suppressed=0 AND attempts<? ORDER BY id",
                         (max_attempts,))
        sent = failed = 0
        outbox = self.home / "alerts.jsonl"
        for r in rows:
            if SEVERITY.get(r["severity"], 0) < floor:
                self.db.x("UPDATE alerts SET delivered_at=?, last_error='below min_severity' WHERE id=?", (now, r["id"]))
                continue
            msg = {"instance": self.instance, "ts": r["ts"], "severity": r["severity"], "kind": r["kind"],
                   "message": r["message"], "action": r["action"] or action_for(r["kind"])}
            msg["text"] = f"[{r['severity']}] {self.instance} {r['kind']}: {r['message']}\nACTION: {msg['action']}"
            with outbox.open("a", encoding="utf-8") as f:
                f.write(self.redact(json.dumps(msg, sort_keys=True)) + "\n")
            if not self.url:
                self.db.x("UPDATE alerts SET delivered_at=?, last_error=NULL WHERE id=?", (now, r["id"]))
                sent += 1
                continue
            try:
                status = self.transport(self.url, json.dumps(msg).encode(), self.cfg.get("timeout_seconds", 10))
                if not (200 <= int(status) < 300):
                    raise RuntimeError(f"HTTP {status}")
                self.db.x("UPDATE alerts SET delivered_at=?, attempts=attempts+1, last_error=NULL WHERE id=?",
                          (now, r["id"]))
                sent += 1
            except Exception as exc:  # noqa: BLE001 -- delivery must never break a cycle
                self.db.x("UPDATE alerts SET attempts=attempts+1, last_error=? WHERE id=?",
                          (self.redact(f"{type(exc).__name__}: {exc}")[:300], r["id"]))
                failed += 1
        return {"sent": sent, "failed": failed, "webhook": bool(self.url)}
