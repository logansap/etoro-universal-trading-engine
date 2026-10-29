"""Simulated broker with eToro semantics and fault injection. The ONLY writer the engine can obtain.

State is persisted to a JSON file after every mutation (atomic replace), so a test or a rehearsal can kill the
engine process and restart it against the same broker state. Faults are queued and consumed by the next matching
operation:
  network_before        raise BrokerAmbiguousError, request NOT processed
  http_reject           raise BrokerError (definite rejection, not processed)
  reject:<code>         accept, then the order ends Rejected with errorCode <code>
  timeout_after_accept  process the request, then raise BrokerAmbiguousError (caller never sees the order id)
  crash_after_submit    process the request, then raise SimulatedCrash (emulates the engine dying mid-call)
  pending               accept; the order stays Placed until advance()
  partial:<f>           fill fraction f now (PartiallyFilled); the rest fills on advance()
  read_unavailable      the next account_snapshot raises BrokerUnavailable
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import tempfile

from ..core.canonical import canonical_json, sha256_text
from ..core.errors import BrokerAmbiguousError, BrokerError, BrokerUnavailable
from .base import (SIMULATED, STATUS_FILLED, STATUS_PARTIAL, STATUS_PLACED, STATUS_REJECTED, AccountSnapshot,
                   BrokerReader, BrokerWriter, CashTransaction, MirrorHolding, OrderStatus, PendingOrder, Position,
                   Quote, SubmitResult)

MIN_POSITION_USD = 10.0
MIN_PARTIAL_CLOSE_USD = 9.9


class SimulatedCrash(BaseException):
    """Emulates the engine process dying in the middle of a broker call (not an Exception: nothing catches it)."""


class SimulatedBroker(BrokerReader, BrokerWriter):
    name = "simulated"

    def __init__(self, path: str | pathlib.Path | None = None, *, instruments: dict | None = None,
                 cash: float = 0.0, cash_tx_capability: bool = True, clock=None):
        self.path = pathlib.Path(path) if path else None
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        if self.path and self.path.exists():
            self.s = json.loads(self.path.read_text(encoding="utf-8"))
            self.s.setdefault("mirrors", [])
            self.s.setdefault("funding", [])
            self.s.setdefault("account_id", "SIM-0001")
        else:
            self.s = {"credit": 0.0, "positions": {}, "orders": {}, "cash_tx": [], "faults": [],
                      "instruments": {str(k): v for k, v in (instruments or {}).items()}, "prices": {},
                      "seq_order": 1000, "seq_pos": 5000, "seq_tx": 1, "cash_tx_capability": cash_tx_capability,
                      "submissions": [], "mirrors": [], "account_id": "SIM-0001", "funding": []}
            if cash:
                self.deposit(cash, evidence="initial funding")
            self._save()

    # ------------------------------------------------------------------ persistence / controls
    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sim-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.s, sort_keys=True, indent=1))
        os.replace(tmp, self.path)

    def reload(self):
        if self.path:
            self.s = json.loads(self.path.read_text(encoding="utf-8"))

    def _now(self):
        return self.clock().astimezone(dt.timezone.utc).isoformat()

    def inject(self, *faults: str):
        self.s["faults"].extend(faults)
        self._save()

    def _take_fault(self, allowed: tuple) -> str | None:
        for i, f in enumerate(self.s["faults"]):
            if f.split(":")[0] in allowed:
                return self.s["faults"].pop(i)
        return None

    def set_price(self, instrument_id: int, bid: float, ask: float | None = None):
        self.s["prices"][str(instrument_id)] = [float(bid), float(ask if ask is not None else bid)]
        self._save()

    def deposit(self, amount: float, evidence: str = "simulated deposit", record: bool = True):
        self.s["credit"] = round(self.s["credit"] + amount, 6)
        if record:
            self._tx("DEPOSIT", amount, evidence)
        self._save()

    def withdraw(self, amount: float, evidence: str = "simulated withdrawal", record: bool = True):
        self.s["credit"] = round(self.s["credit"] - amount, 6)
        if record:
            self._tx("WITHDRAWAL", -amount, evidence)
        self._save()

    def adjust_cash(self, amount: float, kind: str = "ADJUSTMENT", record: bool = True, evidence: str = "sim"):
        """Dividend, fee or unexplained balance change; `record=False` leaves no cash-transaction evidence."""
        self.s["credit"] = round(self.s["credit"] + amount, 6)
        if record:
            self._tx(kind, amount, evidence)
        self._save()

    def _tx(self, kind, amount, evidence, posted_at=None):
        tid = f"T{self.s['seq_tx']}"
        self.s["cash_tx"].append({"tx_id": tid, "kind": kind, "amount": round(amount, 6),
                                  "currency": "USD", "posted_at": posted_at or self._now(), "evidence": evidence})
        self.s["seq_tx"] += 1
        return tid

    def add_tx_record(self, kind, amount, evidence="delayed statement", posted_at=None, duplicate_of=None):
        """A cash-transaction record that arrives LATER than its cash (or a duplicate of an existing record)."""
        if duplicate_of:
            self.s["cash_tx"].append(dict(next(t for t in self.s["cash_tx"] if t["tx_id"] == duplicate_of)))
            self._save()
            return duplicate_of
        tid = self._tx(kind, amount, evidence, posted_at)
        self._save()
        return tid

    def add_funding_evidence(self, amount, ref, posted_at=None, evidence="internal transfer (simulated eToro Money)",
                             currency="USD"):
        """Funding evidence from a SEPARATE source (eToro: the eToro-Money cash account's internalTransfer rows, which
        are in the cash account's currency, e.g. EUR, while the trading account is USD)."""
        self.s["funding"].append({"tx_id": ref, "kind": "DEPOSIT" if amount > 0 else "WITHDRAWAL",
                                  "amount": round(amount, 6), "currency": currency, "posted_at": posted_at or self._now(),
                                  "evidence": evidence})
        self._save()

    def funding_evidence(self, since_iso):
        return [CashTransaction(**t) for t in self.s.get("funding", []) if t["posted_at"] > since_iso]

    def external_open(self, instrument_id: int, amount: float) -> str:
        """A manual trade made outside the engine (e.g. in the eToro app)."""
        oid = self._open(instrument_id, amount, True, 1, client_ref="EXTERNAL", status=STATUS_FILLED)[1]
        self._save()
        return oid

    def external_open_position(self, instrument_id: int, amount: float, *, is_buy: bool = True, leverage: int = 1,
                               open_rate: float | None = None) -> str:
        """A position opened by ANOTHER writer (e.g. Gold Miner's leveraged short), bypassing the x1-long rule of
        submit_open: test setup for takeover previews. Cash is reduced by the margin amount."""
        bid, ask = self._price(instrument_id)
        rate = open_rate if open_rate is not None else (ask if is_buy else bid)
        pid = str(self.s["seq_pos"])
        self.s["seq_pos"] += 1
        self.s["positions"][pid] = {"instrument_id": int(instrument_id), "units": round(amount * leverage / rate, 6),
                                    "amount": round(amount, 6), "open_rate": rate, "opened_at": self._now(),
                                    "is_buy": bool(is_buy), "leverage": int(leverage)}
        self.s["credit"] = round(self.s["credit"] - amount, 6)
        self._save()
        return pid

    def add_mirror(self, mirror_id: str, parent: str, invested: float, value: float, positions: int = 3):
        self.s["mirrors"].append({"mirror_id": mirror_id, "parent_username": parent, "invested": invested,
                                  "value": value, "positions": positions})
        self._save()

    def account_identity(self) -> str:
        return f"sim:{self.s['account_id']}"

    def inventory(self) -> AccountSnapshot:
        return self.account_snapshot()

    def advance(self):
        """Complete every Placed / PartiallyFilled order."""
        for oid, o in self.s["orders"].items():
            if o["status_id"] in (STATUS_PLACED, STATUS_PARTIAL) and o["kind"] == "open":
                rest = round(o["amount"] - o["filled_amount"], 6)
                if rest > 0:
                    self._fill_open(o, rest)
                o["status_id"] = STATUS_FILLED
        self._save()

    # ------------------------------------------------------------------ reader
    def capabilities(self) -> dict:
        caps = {k: SIMULATED for k in ("credit", "positions", "pending_orders", "unrealized_pnl", "order_status",
                                       "close_order_status", "quotes", "eligibility")}
        caps["cash_transactions"] = SIMULATED if self.s["cash_tx_capability"] else "UNAVAILABLE"
        caps["writes"] = SIMULATED
        return caps

    def _price(self, iid) -> tuple:
        p = self.s["prices"].get(str(iid))
        if not p:
            raise BrokerError(f"no simulated price for instrument {iid}")
        return p

    def account_snapshot(self) -> AccountSnapshot:
        if self._take_fault(("read_unavailable",)):
            self._save()
            raise BrokerUnavailable("simulated read failure")
        positions = []
        for pid, p in sorted(self.s["positions"].items()):
            bid, ask = self._price(p["instrument_id"])
            buy = p.get("is_buy", True)
            pnl = round(p["units"] * ((bid - p["open_rate"]) if buy else (p["open_rate"] - ask)), 6)
            positions.append(Position(position_id=pid, instrument_id=p["instrument_id"],
                                      symbol=self.s["instruments"].get(str(p["instrument_id"])), is_buy=buy,
                                      units=p["units"], amount=p["amount"], leverage=p.get("leverage", 1),
                                      open_rate=p["open_rate"], settlement="CFD", unrealized_pnl=pnl, fees=0.0,
                                      opened_at=p["opened_at"]))
        pending = [PendingOrder(order_id=oid, instrument_id=o["instrument_id"],
                                amount=round(o["amount"] - o["filled_amount"], 6), is_buy=True, kind="open")
                   for oid, o in sorted(self.s["orders"].items())
                   if o["kind"] == "open" and o["status_id"] in (STATUS_PLACED, STATUS_PARTIAL)]
        raw = {"credit": self.s["credit"], "positions": self.s["positions"], "pending": [p.__dict__ for p in pending]}
        return AccountSnapshot(as_of=self._now(), credit=self.s["credit"],
                               pending_open_amount=round(sum(p.amount for p in pending), 6),
                               positions=tuple(positions), pending_orders=tuple(pending), currency="USD",
                               source="simulated", raw_sha256=sha256_text(canonical_json(raw)),
                               provenance={k: SIMULATED for k in ("credit", "positions", "pending_orders")},
                               mirrors=tuple(MirrorHolding(**m) for m in self.s["mirrors"]))

    def order_status(self, order_id, kind):
        o = self.s["orders"].get(str(order_id))
        if not o:
            return None
        return OrderStatus(order_id=str(order_id), status_id=o["status_id"], error_code=o.get("error_code"),
                           error_message=o.get("error_message", ""), position_ids=tuple(o.get("position_ids", [])),
                           filled_amount=o.get("filled_amount"), filled_units=o.get("filled_units"),
                           proceeds=o.get("proceeds"), rate=o.get("rate"), raw=dict(o))

    def cash_transactions(self, since_iso):
        if not self.s["cash_tx_capability"]:
            return None
        return [CashTransaction(**t) for t in self.s["cash_tx"] if t["posted_at"] > since_iso]

    def eligibility(self, instrument_ids):
        return {int(i): {"tradable": str(i) in self.s["instruments"], "settlement": "CFD", "leverages": [1],
                         "min_position_usd": MIN_POSITION_USD} for i in instrument_ids}

    def quotes(self, instrument_ids):
        out = {}
        for i in instrument_ids:
            bid, ask = self._price(i)
            out[int(i)] = Quote(instrument_id=int(i), bid=bid, ask=ask, quote_time=self._now())
        return out

    def order_status_by_client_ref(self, client_ref, kind):
        """Mirrors eToro: OPEN orders can be found by the client reference (x-request-id); closes cannot."""
        if kind != "open":
            return None
        for oid, o in self.s["orders"].items():
            if o["kind"] == "open" and o.get("client_ref") == client_ref:
                return self.order_status(oid, "open")
        return None

    def closed_position_proceeds(self, position_id):
        for oid, o in self.s["orders"].items():
            if o["kind"] == "close" and o.get("closed_position_id") == str(position_id) and o["status_id"] == STATUS_FILLED:
                return {"proceeds": o["proceeds"], "units": o["filled_units"], "close_order_id": oid}
        return None

    # ------------------------------------------------------------------ writer
    def _new_order(self, kind, instrument_id, amount, client_ref):
        oid = str(self.s["seq_order"])
        self.s["seq_order"] += 1
        o = {"kind": kind, "instrument_id": int(instrument_id), "amount": round(float(amount), 6),
             "filled_amount": 0.0, "filled_units": 0.0, "status_id": STATUS_PLACED, "client_ref": client_ref,
             "position_ids": [], "created_at": self._now(), "proceeds": None, "rate": None}
        self.s["orders"][oid] = o
        self.s["submissions"].append({"order_id": oid, "kind": kind, "client_ref": client_ref})
        return oid, o

    def _fill_open(self, o, amount):
        _bid, ask = self._price(o["instrument_id"])
        units = round(amount / ask, 6)
        pid = str(self.s["seq_pos"])
        self.s["seq_pos"] += 1
        self.s["positions"][pid] = {"instrument_id": o["instrument_id"], "units": units, "amount": round(amount, 6),
                                    "open_rate": ask, "opened_at": self._now()}
        self.s["credit"] = round(self.s["credit"] - amount, 6)
        o["filled_amount"] = round(o["filled_amount"] + amount, 6)
        o["filled_units"] = round(o["filled_units"] + units, 6)
        o["position_ids"].append(pid)
        o["rate"] = ask

    def _open(self, instrument_id, amount, is_buy, leverage, client_ref, status=None):
        oid, o = self._new_order("open", instrument_id, amount, client_ref)
        available = self.s["credit"] - sum(x["amount"] - x["filled_amount"] for x in self.s["orders"].values()
                                           if x["kind"] == "open" and x["status_id"] in (STATUS_PLACED, STATUS_PARTIAL)
                                           and x is not o)
        reason = None
        if str(instrument_id) not in self.s["instruments"]:
            reason = (604, "instrument not tradable")
        elif not is_buy or leverage != 1:
            reason = (2011, "only x1 long is enabled in the simulation")
        elif amount < MIN_POSITION_USD:
            reason = (720, f"amount below minimum {MIN_POSITION_USD}")
        elif amount > available + 1e-9:
            reason = (746, "insufficient funds")
        if reason:
            o["status_id"], o["error_code"], o["error_message"] = STATUS_REJECTED, reason[0], reason[1]
        elif status == STATUS_PLACED:
            pass
        elif status and status != STATUS_FILLED:
            frac = float(status.split(":")[1])
            self._fill_open(o, round(amount * frac, 6))
            o["status_id"] = STATUS_PARTIAL
        else:
            self._fill_open(o, amount)
            o["status_id"] = STATUS_FILLED
        return o, oid

    def submit_open(self, instrument_id, amount, *, is_buy, leverage, client_ref):
        fault = self._take_fault(("network_before", "http_reject", "reject", "timeout_after_accept",
                                  "crash_after_submit", "pending", "partial"))
        if fault == "network_before":
            self._save()
            raise BrokerAmbiguousError("simulated connection reset before the request reached the broker")
        if fault == "http_reject":
            self._save()
            raise BrokerError("simulated HTTP 400 (definite rejection, nothing created)")
        status = STATUS_PLACED if fault == "pending" else (fault if fault and fault.startswith("partial") else None)
        o, oid = self._open(instrument_id, amount, is_buy, leverage, client_ref, status=status)
        if fault and fault.startswith("reject"):
            if o["status_id"] == STATUS_FILLED:   # undo the fill: the broker rejected it
                for pid in o["position_ids"]:
                    p = self.s["positions"].pop(pid)
                    self.s["credit"] = round(self.s["credit"] + p["amount"], 6)
                o["position_ids"], o["filled_amount"], o["filled_units"] = [], 0.0, 0.0
            o["status_id"], o["error_code"] = STATUS_REJECTED, int(fault.split(":")[1]) if ":" in fault else 999
            o["error_message"] = "simulated rejection"
        self._save()
        if fault == "timeout_after_accept":
            raise BrokerAmbiguousError("simulated read timeout after the broker accepted the request")
        if fault == "crash_after_submit":
            raise SimulatedCrash("engine killed after the broker accepted the request")
        return SubmitResult(order_id=oid, raw={"orderId": oid})

    def submit_close(self, position_id, instrument_id, units, *, client_ref):
        fault = self._take_fault(("network_before", "http_reject", "reject", "timeout_after_accept",
                                  "crash_after_submit"))
        if fault == "network_before":
            self._save()
            raise BrokerAmbiguousError("simulated connection reset before the request reached the broker")
        if fault == "http_reject":
            self._save()
            raise BrokerError("simulated HTTP 400 (definite rejection, nothing closed)")
        oid, o = self._new_order("close", instrument_id, 0.0, client_ref)
        p = self.s["positions"].get(str(position_id))
        if p is None or p["instrument_id"] != int(instrument_id):
            o["status_id"], o["error_code"], o["error_message"] = STATUS_REJECTED, 404, "position not found"
        elif fault and fault.startswith("reject"):
            o["status_id"], o["error_code"], o["error_message"] = STATUS_REJECTED, 999, "simulated rejection"
        else:
            bid, ask = self._price(p["instrument_id"])
            u = p["units"] if units is None or units >= p["units"] - 1e-9 else round(float(units), 6)
            frac = u / p["units"]
            released = round(p["amount"] * frac, 6)
            buy = p.get("is_buy", True)
            proceeds = round(released + u * ((bid - p["open_rate"]) if buy else (p["open_rate"] - ask)), 6)
            if u < p["units"] and (proceeds < MIN_PARTIAL_CLOSE_USD or p["amount"] - released < MIN_POSITION_USD):
                o["status_id"], o["error_code"] = STATUS_REJECTED, 776
                o["error_message"] = "deducted or remaining equity under the allowed minimum"
            else:
                if u >= p["units"] - 1e-9:
                    del self.s["positions"][str(position_id)]
                    split_id = str(position_id)
                else:
                    split_id = str(self.s["seq_pos"])
                    self.s["seq_pos"] += 1
                    p["units"] = round(p["units"] - u, 6)
                    p["amount"] = round(p["amount"] - released, 6)
                self.s["credit"] = round(self.s["credit"] + proceeds, 6)
                o.update(status_id=STATUS_FILLED, proceeds=proceeds, rate=bid if buy else ask, filled_units=u,
                         filled_amount=released, position_ids=[split_id], closed_position_id=str(position_id))
        self._save()
        if fault == "timeout_after_accept":
            raise BrokerAmbiguousError("simulated read timeout after the broker accepted the close")
        if fault == "crash_after_submit":
            raise SimulatedCrash("engine killed after the broker accepted the close")
        return SubmitResult(order_id=oid, raw={"orderId": oid})
