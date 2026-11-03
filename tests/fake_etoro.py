"""A contract-level fake of the documented eToro Public API (HTTP transport), for the live-writer contract tests.

Shapes follow api-portal.etoro.com (2026-10-27) and the live Gold Miner observations: pnl -> clientPortfolio
{credit, positions[], ordersForOpen[], orders[]}; create order -> {token, orderId, referenceId}; orders:lookup by
orderId or referenceId -> {orderId, status{id,name,errorCode,errorMessage}, positionExecutions[]}; close ->
{orderForClose{orderID,...}, token}; close-orders/{id} -> {orderID, statusID, errorCode, proceeds, positions[]}.
A reused x-request-id is treated as a NEW order (eToro documents no de-duplication), so any engine resubmission
would be visible as a duplicate here. Faults are consumed by the next POST of the matching kind.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse

from utengine.brokers.simulated import SimulatedCrash

GDX_SHORT = {"positionID": 9100000001, "instrumentID": 3002, "isBuy": False, "units": 48.0, "amount": 780.0,
             "leverage": 5, "openRate": 81.44, "totalFees": 0.0, "openDateTime": "2026-10-27T13:30:30Z"}


class FakeEtoro:
    def __init__(self, credit=2000.0, prices=None, clock=None, with_gm_short=True):
        self.credit = credit
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.prices = dict(prices or {})                      # iid -> (bid, ask)
        self.positions = {}
        if with_gm_short:
            self.positions[GDX_SHORT["positionID"]] = dict(GDX_SHORT)
            self.prices.setdefault(3002, (81.30, 81.33))
        self.orders, self.close_orders = {}, {}
        self.faults = []
        self.requests = []
        self.seq_order, self.seq_pos = 1621000000, 3617000000
        # production behaviours observed 2026-11-02 (all off by default):
        self.portfolio_lag = []         # per open, in order: portfolio reads that still return the pre-open state
                                        # (cash AND positions stale together, as on 2026-11-02)
        self.visibility_lag = []        # per new position, in order: reads during which it alone is not listed
        self.stale, self.cache = 0, None
        self.close_lag = 0              # portfolio reads during which a fully closed position is still listed
        self.open_fee_bp = 0.0          # opening fee charged with each open (observed 14.9-15.0 bp on x1 CFDs)
        self.omit_execution_ids = False # positionExecutions[] without position ids
        self.hidden, self.ghosts = {}, {}

    # ------------------------------------------------------------------ helpers
    def inject(self, *f):
        self.faults.extend(f)

    def _fault(self, kind):
        """Faults are 'name[:arg][@open|@close]'; without a scope they apply to the next POST of either kind."""
        for i, f in enumerate(self.faults):
            scope = f.split("@")[1] if "@" in f else "any"
            if scope in ("any", kind):
                return self.faults.pop(i).split("@")[0]
        return None

    def _json(self, code, obj):
        return code, json.dumps(obj).encode()

    def _pending_amount(self):
        return sum(o["amount"] - o["filled"] for o in self.orders.values() if o["status"] in (1, 2, 5))

    def _execute_open(self, o, amount):
        bid, ask = self.prices[o["iid"]]
        pid = self.seq_pos
        self.seq_pos += 1
        fee = round(amount * self.open_fee_bp / 1e4, 2)
        # observed 2026-11-03: the opening fee is in totalExternalFees; totalFees (overnight financing) stays 0
        self.positions[pid] = {"positionID": pid, "orderID": o["id"], "instrumentID": o["iid"], "isBuy": True,
                               "units": round(amount / ask, 6), "amount": round(amount, 2), "leverage": 1,
                               "openRate": ask, "totalFees": 0.0, "totalExternalFees": fee,
                               "openDateTime": self.clock().isoformat()}
        self.credit = round(self.credit - round(amount, 2) - fee, 6)
        o["filled"] = round(o["filled"] + amount, 6)
        o["fee"] = round(o.get("fee", 0.0) + fee, 6)
        o["execs"].append(pid)
        if self.visibility_lag:
            self.hidden[pid] = self.visibility_lag.pop(0)
        if self.portfolio_lag:
            self.stale = self.portfolio_lag.pop(0)

    def advance(self):
        for o in self.orders.values():
            if o["status"] in (2, 5):
                rest = round(o["amount"] - o["filled"], 6)
                if rest > 0:
                    self._execute_open(o, rest)
                o["status"] = 3

    def posts(self, kind=None):
        return [r for r in self.requests if r[0] == "POST" and (kind is None or kind in r[1])]

    # ------------------------------------------------------------------ transport
    def __call__(self, method, url, headers, body):
        u = urllib.parse.urlsplit(url)
        path, q = u.path, dict(urllib.parse.parse_qsl(u.query))
        self.requests.append((method, path, headers.get("x-request-id"), body))
        if method == "GET" and path == "/api/v1/me":                 # the READ-ONLY key the reader holds
            return self._json(200, {"gcid": 1, "scopes": ["etoro-public:trade.real:read",
                                                          "etoro-public:market-data:read"]})
        if method == "GET" and path == "/api/v1/trading/info/real/pnl":
            if self.stale > 0 and self.cache is not None:
                self.stale -= 1
                return self._json(200, self.cache)
            pos = []
            listed = [p for p in self.positions.values() if self.hidden.get(p["positionID"], 0) <= 0]
            for pid in [k for k, v in self.hidden.items() if v > 0]:
                self.hidden[pid] -= 1
            listed += [g for g in self.ghosts.values()]
            for pid in list(self.ghosts):
                self.ghosts[pid]["_reads"] -= 1
                if self.ghosts[pid]["_reads"] <= 0:
                    del self.ghosts[pid]
            for p in listed:
                p = {k: v for k, v in p.items() if k != "_reads"}
                bid, ask = self.prices[p["instrumentID"]]
                pnl = p["units"] * ((bid - p["openRate"]) if p["isBuy"] else (p["openRate"] - ask))
                pos.append({**p, "unrealizedPnL": {"pnL": round(pnl, 6)}})
            pend = [{"orderID": oid, "instrumentID": o["iid"], "amount": round(o["amount"] - o["filled"], 6),
                     "isBuy": True, "mirrorID": 0} for oid, o in self.orders.items() if o["status"] in (1, 2, 5)]
            self.cache = {"clientPortfolio": {"credit": self.credit, "positions": pos,
                                              "ordersForOpen": pend, "orders": [], "mirrors": []}}
            return self._json(200, self.cache)
        if method == "GET" and path == "/api/v2/trading/info/orders:lookup":
            o = None
            if "orderId" in q:
                o = self.orders.get(int(q["orderId"]))
            elif "referenceId" in q:
                o = next((x for x in self.orders.values() if x["ref"] == q["referenceId"]), None)
            if o is None:
                return self._json(404, {"title": "Not Found"})
            names = {1: "Received", 2: "Placed", 3: "Filled", 4: "Rejected", 5: "PartiallyFilled", 9: "CanceledPartiallyFilled"}
            execs = []
            for pid in o["execs"]:
                pos = self.positions.get(pid) or self.ghosts.get(pid) or {}
                amt = pos.get("amount", o["filled"] / max(len(o["execs"]), 1))
                # the full lookup shape read from the real account 2026-11-03 (openingData carries units/price/fee)
                e = {"addedFunds": 0.0, "initialExposureAccountCurrency": amt, "initialExposureAssetCurrency": amt,
                     "investedAmountCurrency": 1, "marginAccountCurrency": amt, "marginAssetCurrency": amt,
                     "remainingUnits": pos.get("units"), "state": "open",
                     "openingData": {"orderId": o["id"], "units": pos.get("units"), "avgPrice": pos.get("openRate"),
                                     "fees": pos.get("totalExternalFees", 0.0), "taxes": 0.0, "markup": 0.0}}
                if not self.omit_execution_ids:
                    e["positionId"] = pid
                execs.append(e)
            body = {"orderId": o["id"], "status": {"id": o["status"], "name": names.get(o["status"]),
                                                   "errorCode": o["err"], "errorMessage": o["msg"]},
                    "positionExecutions": execs, "requestedAmount": o["amount"]}
            if self.open_fee_bp:
                body["frozenAmount"] = round(o["filled"] + o.get("fee", 0.0), 2)
                body["totalCosts"] = round(o.get("fee", 0.0), 2)
            return self._json(200, body)
        if method == "GET" and path.startswith("/api/v1/trading/info/real/close-orders/"):
            c = self.close_orders.get(int(path.rsplit("/", 1)[1]))
            return self._json(200, c) if c else self._json(404, {})
        if method == "GET" and path == "/api/v1/market-data/instruments/rates":
            ids = [int(i) for i in q["instrumentIds"].split(",")]
            return self._json(200, {"rates": [{"instrumentID": i, "bid": self.prices[i][0], "ask": self.prices[i][1],
                                               "date": self.clock().isoformat()} for i in ids]})
        if method == "POST" and path == "/api/v2/trading/execution/orders":
            return self._post_open(headers, body)
        if method == "POST" and path.startswith("/api/v1/trading/execution/market-close-orders/positions/"):
            return self._post_close(int(path.rsplit("/", 1)[1]), headers, body)
        return self._json(404, {"title": "RouteNotFound"})

    def _post_open(self, headers, body):
        f = self._fault("open")
        if f == "drop":
            raise ConnectionError("connection reset before the request reached eToro")
        if f == "http400":
            return self._json(400, {"title": "Bad Request"})
        if f == "http429":                                       # the rate limiter answers before any order exists
            return self._json(429, {"title": "Too Many Requests"})
        oid = self.seq_order
        self.seq_order += 1
        o = {"id": oid, "ref": headers["x-request-id"], "iid": body["instrumentId"], "amount": body["amount"],
             "filled": 0.0, "status": 2, "err": 0, "msg": None, "execs": []}
        self.orders[oid] = o
        if body["amount"] > self.credit - self._pending_amount() + o["amount"] - 1e-9 or body["amount"] < 10:
            o.update(status=4, err=746, msg="insufficient funds or below minimum")
        elif f and f.startswith("reject"):
            o.update(status=4, err=int(f.split(":")[1]) if ":" in f else 999, msg="rejected")
        elif f == "pending":
            pass
        elif f == "partial":
            self._execute_open(o, round(o["amount"] / 2, 2))
            o["status"] = 5
        elif f == "partial_cancel":
            self._execute_open(o, round(o["amount"] / 2, 2))
            o["status"] = 9
        else:
            self._execute_open(o, o["amount"])
            o["status"] = 3
        resp = {"token": "tok-" + str(oid), "orderId": oid, "referenceId": headers["x-request-id"]}
        if f == "lose_response":
            raise TimeoutError("read timeout after eToro accepted the order")
        if f == "crash":
            raise SimulatedCrash("engine killed after eToro accepted the order")
        if f == "http500_after":
            return self._json(500, {})
        return self._json(200, resp)

    def _post_close(self, pid, headers, body):
        f = self._fault("close")
        if f == "drop":
            raise ConnectionError("connection reset")
        if f == "http400":
            return self._json(400, {})
        if f == "http429":
            return self._json(429, {})
        p = self.positions.get(pid)
        oid = self.seq_order
        self.seq_order += 1
        if p is None or p["instrumentID"] != body["InstrumentId"] or (f or "").startswith("reject"):
            self.close_orders[oid] = {"orderID": oid, "statusID": 4, "errorCode": 404 if p is None else 999,
                                      "errorMessage": "rejected", "proceeds": 0.0, "positions": []}
        else:
            bid, _ = self.prices[p["instrumentID"]]
            u = p["units"] if body.get("UnitsToDeduct") is None else min(float(body["UnitsToDeduct"]), p["units"])
            frac = u / p["units"]
            released = round(p["amount"] * frac, 2)
            proceeds = round(released + u * (bid - p["openRate"]), 2)
            if frac >= 1 - 1e-9:
                gone = self.positions.pop(pid)
                if self.close_lag:
                    self.ghosts[pid] = {**gone, "_reads": self.close_lag}
                split = pid
            else:
                split = self.seq_pos
                self.seq_pos += 1
                p["units"] = round(p["units"] - u, 6)
                p["amount"] = round(p["amount"] - released, 2)
            self.credit = round(self.credit + proceeds, 6)
            self.close_orders[oid] = {"orderID": oid, "statusID": 3, "errorCode": 0, "errorMessage": "",
                                      "proceeds": proceeds, "positions": [{"positionID": split, "amount": released,
                                                                           "units": u, "rate": bid}]}
        if f == "lose_response":
            raise TimeoutError("read timeout after eToro accepted the close")
        return self._json(200, {"orderForClose": {"positionID": pid, "instrumentID": body["InstrumentId"],
                                                  "orderID": oid, "statusID": 1}, "token": "tok"})
