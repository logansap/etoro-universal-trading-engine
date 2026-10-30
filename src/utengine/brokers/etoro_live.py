"""eToro LIVE order adapter (BrokerWriter) -- implemented, but UNREACHABLE in this release.

It can only be constructed:
  * against the production API (https://public-api.etoro.com) with a LiveAuthorization, which only
    core.security.authorize_live() can create -- and that is refused while LIVE_RELEASE_ENABLED is False (code) and
    while the schema refuses runtime.mode=live; the engine (obtain_writer) never constructs this class at all; or
  * against CONTRACT_TEST_BASE (an RFC 6761 `.invalid` host that cannot resolve) with an injected transport, for
    the contract tests in tests/test_etoro_live_contract.py.

Documented interfaces only (api-portal.etoro.com, fetched 2026-10-27; both also used live by Gold Miner):
  OPEN   POST /api/v2/trading/execution/orders
         header x-request-id: client-supplied UUID ("send a unique GUID per order"); response
         {token, orderId, referenceId == x-request-id}. 200 = ACCEPTED, not executed.
         Outcome: GET /api/v2/trading/info/orders:lookup?orderId=... or ?referenceId=<x-request-id> -- the documented
         recovery handle when the 200 is lost. This adapter derives x-request-id deterministically from the intent
         id (uuid5), so a crashed engine can always ask "did intent X reach eToro?". eToro does NOT document what a
         REUSED x-request-id does, so an intent is still never resubmitted.
  CLOSE  POST /api/v1/trading/execution/market-close-orders/positions/{positionId}  body {InstrumentId, UnitsToDeduct}
         response {orderForClose: {orderID, ...}, token}. No referenceId: a lost close response can only be resolved
         from position evidence (see docs/PHASE2_READINESS.md, live blocker B3).
Error mapping: transport failure, 5xx, or a 2xx without an order id -> BrokerAmbiguousError (outcome UNKNOWN);
4xx -> BrokerError (definite: the request was refused).
"""
from __future__ import annotations

import json
import re
import uuid

from ..core.errors import BrokerAmbiguousError, BrokerError, ConfigError, LiveExecutionNotAvailable, ReadOnlyViolation
from ..core.security import LiveAuthorization
from .base import BrokerWriter, SubmitResult

PRODUCTION_BASE = "https://public-api.etoro.com"
CONTRACT_TEST_BASE = "https://etoro-contract-test.invalid"
ORDER_NAMESPACE = uuid.UUID("6f1c1a52-4c2e-4f0e-9a51-5a0e7c1b2d33")
WRITE_ALLOWLIST = (
    ("POST", re.compile(r"^/api/v2/trading/execution/orders$")),
    ("POST", re.compile(r"^/api/v1/trading/execution/market-close-orders/positions/\d+$")),
)


def request_id_for(client_ref: str) -> str:
    """Deterministic x-request-id for an engine intent (also the orders:lookup referenceId)."""
    return str(uuid.uuid5(ORDER_NAMESPACE, client_ref))


def check_write_allowed(method: str, path: str) -> None:
    for m, rx in WRITE_ALLOWLIST:
        if method == m and rx.match(path):
            return
    raise ReadOnlyViolation(f"live writer refused {method} {path.split('?')[0]} (not an order route)")


class EtoroLiveWriter(BrokerWriter):
    name = "etoro-live-writer"

    def __init__(self, *, transport, base: str = PRODUCTION_BASE, authorization=None, api_key=None, user_key=None,
                 settlement_for=None):
        if base == PRODUCTION_BASE:
            if not isinstance(authorization, LiveAuthorization):
                raise LiveExecutionNotAvailable("the production live writer requires a LiveAuthorization "
                                                "(authorize_live), which this release cannot issue")
        elif base != CONTRACT_TEST_BASE:
            raise ConfigError(f"unknown eToro base {base!r}")
        if transport is None:
            raise ConfigError("an explicit transport is required")
        self.base, self.transport, self.authorization = base, transport, authorization
        self._api_key, self._user_key = api_key, user_key
        self.settlement_for = settlement_for or (lambda instrument_id: "cfd")

    def _post(self, path: str, body: dict, client_ref: str) -> dict:
        check_write_allowed("POST", path)
        headers = {"x-request-id": request_id_for(client_ref)}
        if self._api_key and self._user_key:
            headers.update({"x-api-key": self._api_key, "x-user-key": self._user_key})
        try:
            status, payload = self.transport("POST", self.base + path, headers, body)
        except Exception as exc:  # noqa: BLE001 -- the request may or may not have reached eToro
            raise BrokerAmbiguousError(f"POST {path}: no response ({type(exc).__name__})") from exc
        if status >= 500:
            raise BrokerAmbiguousError(f"POST {path}: HTTP {status}; the order may have been accepted")
        if status >= 400:
            raise BrokerError(f"POST {path}: HTTP {status} (definite refusal)")
        try:
            return json.loads(payload.decode("utf-8")) if payload else {}
        except ValueError as exc:
            raise BrokerAmbiguousError(f"POST {path}: unreadable success response") from exc

    def submit_open(self, instrument_id, amount, *, is_buy, leverage, client_ref):
        if not is_buy or leverage != 1:
            raise BrokerError("only x1 long opens are supported")
        # exactly the body Gold Miner used successfully on this account since 2026-08-27 for x1 longs (SQQQ, SOXS,
        # QQQ): no `leverage` field at x1 (Gold Miner adds it only above 1); settlementType per instrument (all 11
        # pilot instruments are CFD-only on this account, eligibility API 2026-10-29/30)
        body = {"action": "open", "transaction": "buy", "instrumentId": int(instrument_id),
                "settlementType": self.settlement_for(int(instrument_id)), "orderType": "mkt",
                "amount": round(float(amount), 2), "orderCurrency": "usd"}
        raw = self._post("/api/v2/trading/execution/orders", body, client_ref)
        if raw.get("orderId") is None:
            raise BrokerAmbiguousError("order accepted without an orderId; look up by referenceId")
        return SubmitResult(order_id=str(raw["orderId"]), raw=raw)

    def submit_close(self, position_id, instrument_id, units, *, client_ref):
        body = {"InstrumentId": int(instrument_id), "UnitsToDeduct": None if units is None else float(units)}
        raw = self._post(f"/api/v1/trading/execution/market-close-orders/positions/{int(position_id)}", body,
                         client_ref)
        oid = (raw.get("orderForClose") or {}).get("orderID")
        if oid is None:
            raise BrokerAmbiguousError("close accepted without an orderID")
        return SubmitResult(order_id=str(oid), raw=raw)
