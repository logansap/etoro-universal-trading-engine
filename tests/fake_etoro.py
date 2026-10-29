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
        self.positions[pid] = {"positionID": pid, "instrumentID": o["iid"], "isBuy": True,
                               "units": round(amount / ask, 6), "amount": round(amount, 2), "leverage": 1,
                               "openRate": ask, "totalFees": 0.0,
                               "openDateTime": self.clock().isoformat()}
        self.credit = round(self.credit - round(amount, 2), 6)
        o["filled"] = round(o["filled"] + amount, 6)
        o["execs"].append(pid)

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
            pos = []
            for p in self.positions.values():
                bid, ask = self.prices[p["instrumentID"]]
                pnl = p["units"] * ((bid - p["openRate"]) if p["isBuy"] else (p["openRate"] - ask))
                pos.append({**p, "unrealizedPnL": {"pnL": round(pnl, 6)}})
            pend = [{"orderID": oid, "instrumentID": o["iid"], "amount": round(o["amount"] - o["filled"], 6),
                     "isBuy": True, "mirrorID": 0} for oid, o in self.orders.items() if o["status"] in (1, 2, 5)]
            return self._json(200, {"clientPortfolio": {"credit": self.credit, "positions": pos,
                                                        "ordersForOpen": pend, "orders": [], "mirrors": []}})
        if method == "GET" and path == "/api/v2/trading/info/orders:lookup":
            o = None
            if "orderId" in q:
                o = self.orders.get(int(q["orderId"]))
            elif "referenceId" in q:
                o = next((x for x in self.orders.values() if x["ref"] == q["referenceId"]), None)
            if o is None:
                return self._json(404, {"title": "Not Found"})
            names = {1: "Received", 2: "Placed", 3: "Filled", 4: "Rejected", 5: "PartiallyFilled", 9: "CanceledPartiallyFilled"}
            return self._json(200, {"orderId": o["id"], "status": {"id": o["status"], "name": names.get(o["status"]),
                                                                  "errorCode": o["err"], "errorMessage": o["msg"]},
                                    "positionExecutions": [{"positionId": pid, "state": "open"} for pid in o["execs"]],
                                    "requestedAmount": o["amount"]})
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
                del self.positions[pid]
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
