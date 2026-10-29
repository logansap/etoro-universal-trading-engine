"""Offline, read-only reconciliation of the REAL eToro account from Gold Miner audit snapshots.

Input: the output of the authorized read-only audit command (`ssh gold-miner-audit`, a forced command on the
Gold Miner VPS that performs GET-only broker reads with Gold Miner's own key and prints a JSON snapshot). This
module never connects to anything, never sees a credential and never touches Gold Miner: it parses saved files.

What a snapshot can and cannot provide (verified 2026-10-27):
  available cash        broker.available_cash  (credit - pending; pending orders are listed separately)
  positions             position_id, instrument_id, is_buy, units, amount, leverage, open_rate, fees
  pending orders        broker.pending_orders
  closed trades         broker.trade_history (eToro rows: positionId, orderId, netProfit, fees, investment, ...)
  unrealized P&L        NOT included -> account equity cannot be computed from a snapshot (reported as such)
  deposits/withdrawals  NOT included (no eToro trading-account feed exists; see docs/ETORO_CAPABILITIES.md)
"""
from __future__ import annotations

import json
import pathlib

from ..brokers.base import UNAVAILABLE, VERIFIED_LIVE, AccountSnapshot, BrokerReader, PendingOrder, Position
from ..core.canonical import canonical_json, sha256_text
from ..core.errors import BrokerUnavailable


def load_audit(path) -> dict:
    """Parse the audit command output: JSON log lines followed by one pretty-printed JSON document."""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    i = text.find("{\n")
    if i < 0:
        raise BrokerUnavailable(f"{path}: no audit JSON document found")
    doc = json.loads(text[i:])
    if doc.get("schema") != "gold-miner-audit/1" or not doc.get("broker", {}).get("ok"):
        raise BrokerUnavailable(f"{path}: not a successful gold-miner-audit/1 snapshot")
    return doc


def to_snapshot(doc: dict, symbols: dict) -> AccountSnapshot:
    b = doc["broker"]["data"]
    pend = [PendingOrder(str(o.get("orderID", o.get("order_id"))), int(o.get("instrumentID", o.get("instrument_id", 0))),
                         float(o.get("amount", 0.0)), bool(o.get("isBuy", True)), "open")
            for o in b.get("pending_orders") or []]
    pend_amt = round(sum(o.amount for o in pend), 6)
    positions = tuple(Position(position_id=str(p["position_id"]), instrument_id=int(p["instrument_id"]),
                               symbol=symbols.get(int(p["instrument_id"]), p.get("symbol")), is_buy=bool(p["is_buy"]),
                               units=float(p["units"]), amount=float(p["amount"]), leverage=int(p["leverage"]),
                               open_rate=float(p["open_rate"]), settlement="CFD", unrealized_pnl=None,
                               fees=float(p.get("total_fees") or 0.0) + float(p.get("total_external_fees") or 0.0),
                               opened_at=None)
                      for p in b["positions"])
    raw = {"available_cash": b["available_cash"], "positions": b["positions"], "pending": b.get("pending_orders")}
    return AccountSnapshot(as_of=doc["generated_at"], credit=round(float(b["available_cash"]) + pend_amt, 6),
                           pending_open_amount=pend_amt, positions=positions, pending_orders=tuple(pend),
                           currency="USD", source="gold-miner-audit (read-only, offline)",
                           raw_sha256=sha256_text(canonical_json(raw)),
                           provenance={"credit": VERIFIED_LIVE + " (audit available_cash + pending)",
                                       "positions": VERIFIED_LIVE + " (audit)", "unrealized_pnl": UNAVAILABLE})


class AuditReplayReader(BrokerReader):
    """Serves saved audit snapshots in order (one per account_snapshot call). Read-only by construction."""
    name = "gm-audit-replay"

    def __init__(self, docs: list, symbols: dict):
        self.docs, self.symbols, self.i = docs, symbols, 0

    def capabilities(self):
        return {"credit": VERIFIED_LIVE, "positions": VERIFIED_LIVE, "pending_orders": VERIFIED_LIVE,
                "unrealized_pnl": UNAVAILABLE, "cash_transactions": UNAVAILABLE, "writes": "NONE"}

    def account_snapshot(self):
        doc = self.docs[min(self.i, len(self.docs) - 1)]
        self.i += 1
        return to_snapshot(doc, self.symbols)

    def cash_transactions(self, since_iso):
        return None

    def order_status(self, order_id, kind):
        return None

    def eligibility(self, instrument_ids):
        return {}

    def quotes(self, instrument_ids):
        return {}


def history_summary(doc: dict) -> dict:
    rows = doc["broker"]["data"].get("trade_history") or []
    return {"closed_trades": len(rows), "since": doc["broker"]["data"].get("trade_history_since"),
            "net_profit_sum": round(sum(float(r.get("netProfit") or 0) for r in rows), 2),
            "fees_sum": round(sum(float(r.get("fees") or 0) for r in rows), 2),
            "fields": sorted(rows[0]) if rows else []}
