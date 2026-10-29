"""READ-ONLY eToro adapter. It has no write methods, and its transport refuses every request that is not on the
read allowlist BEFORE any network I/O (ReadOnlyViolation). It does not subclass BrokerWriter.

Endpoints (eToro public API, https://public-api.etoro.com; auth headers x-api-key / x-user-key / x-request-id):
  GET  /api/v1/me                                          identity and scopes            VERIFIED_LIVE (GM audit)
  GET  /api/v1/trading/info/real/pnl                       credit, positions, pending     VERIFIED_LIVE (GM audit)
  GET  /api/v1/trading/info/trade/history                  closed trades                  VERIFIED_LIVE (GM audit)
  GET  /api/v2/trading/info/orders:lookup?orderId=         open-order status              VERIFIED_LIVE (GM)
  GET  /api/v1/trading/info/real/close-orders/{orderId}    close-order status, proceeds   VERIFIED_LIVE (GM)
  GET  /api/v1/market-data/instruments/rates?instrumentIds= bid/ask                       VERIFIED_LIVE (GM)
  POST /api/v2/trading/info/eligibility                    QUERY (list in body)           VERIFIED_LIVE (GM)
  GET  /api/v1/money/accounts/cash/{accountId}/transactions cash-account transactions     DOCUMENTED only: it covers
       the eToro Money cash account, and whether trading-account deposits appear there is UNVERIFIED -> this adapter
       reports cash_transactions as UNAVAILABLE and the engine halts on unexplained cash changes instead.
"VERIFIED_LIVE (GM ...)" means the live Gold Miner engine reads that endpoint on the real account every cycle
(read-only audit snapshot of 2026-10-27); THIS engine has not been run with credentials (no credential was
provided for this release), so every field stays UNVERIFIED for this engine until `trading-engine broker probe`
succeeds with owner-provisioned READ-ONLY credentials.
Credentials are read from environment variables named in engine.yaml; they are never logged or persisted.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request
import uuid

from ..core.canonical import canonical_json, sha256_text
from ..core.errors import BrokerError, BrokerUnavailable, ConfigError, KeyNotReadOnly, ReadOnlyViolation
from .base import (DERIVED, DOCUMENTED, UNAVAILABLE, VERIFIED_LIVE, AccountSnapshot, BrokerReader, MirrorHolding,
                   OrderStatus, PendingOrder, Position, Quote)

BASE = "https://public-api.etoro.com"
READ_ALLOWLIST = (
    ("GET", re.compile(r"^/api/v1/me$")),
    ("GET", re.compile(r"^/api/v1/trading/info/real/pnl$")),
    ("GET", re.compile(r"^/api/v1/trading/info/trade/history\?minDate=\d{4}-\d{2}-\d{2}&page=\d+&pageSize=\d+$")),
    ("GET", re.compile(r"^/api/v2/trading/info/orders:lookup\?orderId=\d+$")),
    ("GET", re.compile(r"^/api/v2/trading/info/orders:lookup\?referenceId=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                       r"[0-9a-f]{4}-[0-9a-f]{12}$")),
    ("GET", re.compile(r"^/api/v1/trading/info/real/close-orders/\d+$")),
    ("GET", re.compile(r"^/api/v1/market-data/instruments/rates\?instrumentIds=\d+(,\d+)*$")),
    ("POST", re.compile(r"^/api/v2/trading/info/eligibility$")),
    # instrument search by exact ticker (market data; VERIFIED_LIVE by GM's client, and by this engine 2026-10-29)
    ("GET", re.compile(r"^/api/v1/market-data/search\?internalSymbolFull=[A-Z0-9.]{1,12}$")),
    # balances and eToro-Money funding evidence (DOCUMENTED, api-portal 2026-10-29; not yet observed by this engine)
    ("GET", re.compile(r"^/api/v1/balances\?accountTypes=Trading,Cash&includeSubAccounts=false&expand=equityDetails$")),
    ("GET", re.compile(r"^/api/v1/money/accounts/cash/[0-9a-fA-F-]{36}/transactions\?pageSize=\d+(&pageToken=[A-Za-z0-9_\-=%.]+)?$")),
    ("GET", re.compile(r"^/api/v1/balances/trading/[A-Za-z0-9_\-]+/history\?fromDate=\d{4}-\d{2}-\d{2}&toDate=\d{4}-\d{2}-\d{2}$")),
)


def check_read_only(method: str, path: str) -> None:
    for m, rx in READ_ALLOWLIST:
        if method == m and rx.match(path):
            return
    raise ReadOnlyViolation(f"read-only eToro transport refused {method} {path.split('?')[0]}")


USER_AGENT = "etoro-universal-trading-engine/0.9 (read-only)"


class UrllibTransport:
    """eToro's edge rejects urllib's default "Python-urllib/x.y" signature with HTTP 403 "error code: 1010" (observed
    2026-10-29 with the engine's own read-only key), so every request carries an honest, identifying User-Agent."""

    def __init__(self, timeout: float):
        self.timeout = timeout

    def __call__(self, method, url, headers, body):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": USER_AGENT, **headers,
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class EtoroReadOnly(BrokerReader):
    name = "etoro-read-only"

    def __init__(self, cfg: dict, transport=None, env=None):
        env = os.environ if env is None else env
        b = cfg["broker"]
        if b["access"] != "live_read_only":
            raise ConfigError("EtoroReadOnly requires broker.access=live_read_only")
        self._api_key = env.get(b["credentials_env"]["api_key"])
        self._user_key = env.get(b["credentials_env"]["user_key"])
        self.configured = bool(self._api_key and self._user_key)
        self.transport = transport or UrllibTransport(b["request_timeout_seconds"])
        self.symbols = {}
        self.key_scopes_verified = False

    def verify_read_only_key(self) -> dict:
        """Prove from GET /api/v1/me that the key's GRANTED scopes contain no write scope. A key that can write
        (e.g. Gold Miner's, with trade.real:write) is refused permanently with KeyNotReadOnly; an answer without
        a scope list cannot prove read-only and is refused too. A transport failure leaves it UNVERIFIED, and every
        later read re-checks first, so no account data is ever read with an unproven key."""
        if not self.configured:
            return {"status": "NOT_CONFIGURED"}
        if self.key_scopes_verified:
            return {"status": "READ_ONLY"}
        me = self._request("GET", "/api/v1/me", _scope_check=False)
        scopes = me.get("scopes") if isinstance(me, dict) else None
        if not isinstance(scopes, list) or not scopes:
            raise KeyNotReadOnly("GET /api/v1/me returned no granted-scope list: the key cannot be proven read-only")
        write = sorted(str(s) for s in scopes if str(s).lower().endswith(":write"))
        if write:
            raise KeyNotReadOnly(f"the eToro key grants {len(write)} write scope(s) {write}; provision a separate "
                                 "key with read scopes only (never Gold Miner's key)")
        self.key_scopes_verified = True
        return {"status": "READ_ONLY", "scopes": sorted(str(s) for s in scopes)}

    def _request(self, method: str, path: str, body=None, allow_404: bool = False, _scope_check: bool = True):
        check_read_only(method, path)                 # before credentials are even touched
        if not self.configured:
            raise BrokerUnavailable("eToro read-only credentials are not provisioned (environment variables unset)")
        if _scope_check and not self.key_scopes_verified:
            self.verify_read_only_key()
        headers = {"x-api-key": self._api_key, "x-user-key": self._user_key, "x-request-id": str(uuid.uuid4())}
        try:
            status, payload = self.transport(method, BASE + path, headers, body)
        except Exception as exc:  # noqa: BLE001 -- reads: any transport failure means "could not read"
            raise BrokerUnavailable(f"{method} {path.split('?')[0]}: {type(exc).__name__}") from exc
        if status == 404 and allow_404:
            return None
        if status >= 400:
            raise BrokerError(f"{method} {path.split('?')[0]} -> HTTP {status}")
        try:
            return json.loads(payload.decode("utf-8")) if payload else {}
        except ValueError as exc:
            raise BrokerUnavailable(f"{method} {path.split('?')[0]}: malformed JSON") from exc

    # ------------------------------------------------------------------
    def capabilities(self) -> dict:
        return {"credit": VERIFIED_LIVE + " (GM audit; this engine: unverified)",
                "positions": VERIFIED_LIVE + " (GM audit; this engine: unverified)",
                "pending_orders": VERIFIED_LIVE + " (GM audit; this engine: unverified)",
                "unrealized_pnl": DOCUMENTED + " (positions[].unrealizedPnL.pnL)",
                "equity": DERIVED + " (eToro 'Calculate Equity' guide)",
                "order_status": VERIFIED_LIVE + " (GM)", "close_order_status": VERIFIED_LIVE + " (GM)",
                "trade_history": VERIFIED_LIVE + " (GM)", "quotes": VERIFIED_LIVE + " (GM)",
                "eligibility": VERIFIED_LIVE + " (GM)", "cash_transactions": UNAVAILABLE,
                "buying_power_margin": UNAVAILABLE, "financing_dividends_detail": UNAVAILABLE,
                "writes": "NOT IMPLEMENTED (no write method exists)"}

    def whoami(self) -> dict:
        me = self._request("GET", "/api/v1/me")
        return {"fields_present": sorted(me), "scopes": me.get("scopes", [])}

    def account_identity(self) -> str | None:
        """Hash of the account's numeric customer id from /api/v1/me (never stored in clear)."""
        me = self._request("GET", "/api/v1/me")
        for k in ("gcid", "realCid", "realCID", "cid", "customerId"):
            if me.get(k) not in (None, ""):
                return f"etoro:{k}:{sha256_text(str(me[k]))[:16]}"
        try:                                       # documented: GET /api/v1/balances returns the customer's gcid
            r = self._request("GET", "/api/v1/balances?accountTypes=Trading,Cash&includeSubAccounts=false"
                                     "&expand=equityDetails")
        except (BrokerUnavailable, BrokerError):
            return None
        return f"etoro:gcid:{sha256_text(str(r['gcid']))[:16]}" if r.get("gcid") not in (None, "") else None

    def inventory(self) -> AccountSnapshot:
        return self._parse(self._request("GET", "/api/v1/trading/info/real/pnl"), allow_mirrors=True)

    def account_snapshot(self) -> AccountSnapshot:
        return self._parse(self._request("GET", "/api/v1/trading/info/real/pnl"), allow_mirrors=False)

    def _parse(self, raw, allow_mirrors: bool) -> AccountSnapshot:
        port = raw.get("clientPortfolio") if isinstance(raw.get("clientPortfolio"), dict) else raw
        credit = port.get("credit", port.get("credits", raw.get("credit")))
        if not isinstance(credit, (int, float)) or isinstance(credit, bool):
            raise BrokerUnavailable("pnl response carries no numeric credit; refusing to infer cash")
        pend, pend_amt = [], 0.0
        for o in port.get("ordersForOpen") or []:
            if isinstance(o, dict) and o.get("mirrorID", 0) == 0:
                amt = float(o.get("amount") or 0.0)
                pend_amt += amt
                pend.append(PendingOrder(str(o.get("orderID")), int(o.get("instrumentID", 0)), amt,
                                         bool(o.get("isBuy", True)), "open"))
        for o in port.get("orders") or []:
            if isinstance(o, dict):
                amt = float(o.get("amount") or 0.0)
                pend_amt += amt
                pend.append(PendingOrder(str(o.get("orderID")), int(o.get("instrumentID", 0)), amt,
                                         bool(o.get("isBuy", True)), "mit"))
        positions = []
        for p in port.get("positions") or []:
            u = p.get("unrealizedPnL")
            u = u if isinstance(u, dict) else {}
            pnl, expo, cr = u.get("pnL"), u.get("exposureInAccountCurrency"), u.get("closeRate")
            num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
            positions.append(Position(
                position_id=str(p.get("positionID")), instrument_id=int(p["instrumentID"]),
                symbol=self.symbols.get(int(p["instrumentID"])), is_buy=bool(p["isBuy"]),
                units=float(p.get("units", p.get("amountInUnits", 0.0)) or 0.0), amount=float(p["amount"]),
                leverage=int(p.get("leverage", 1)), open_rate=float(p.get("openRate", 0.0)),
                settlement="UNKNOWN", unrealized_pnl=float(pnl) if num(pnl) else None,
                # overnight financing is reported in totalExternalFees (observed on a real leveraged CFD while
                # totalFees was 0.0): both are fees to date
                fees=float(p.get("totalFees", 0.0) or 0.0) + float(p.get("totalExternalFees", 0.0) or 0.0),
                opened_at=p.get("openDateTime"), exposure_usd=abs(float(expo)) if num(expo) else None,
                close_rate=float(cr) if num(cr) else None, quote_at=u.get("timestamp")))
        mirrors = []
        for m in port.get("mirrors") or []:
            if not isinstance(m, dict):
                continue
            mpos = m.get("positions") or []
            pnl = [((p.get("unrealizedPnL") or {}).get("pnL") if isinstance(p.get("unrealizedPnL"), dict) else None)
                   for p in mpos]
            inv = float(m.get("investedAmount", m.get("initialInvestment", m.get("amount", 0.0))) or 0.0)
            mirrors.append(MirrorHolding(mirror_id=str(m.get("mirrorID", m.get("mirrorId"))),
                                         parent_username=m.get("parentUsername"), invested=inv,
                                         value=None if any(x is None for x in pnl) else
                                         round(sum(float(p.get("amount", 0.0)) for p in mpos) + sum(pnl) +
                                               float(m.get("availableAmount", 0.0) or 0.0), 6),
                                         positions=len(mpos)))
        if mirrors and not allow_mirrors:
            raise BrokerUnavailable("account has copy-trading mirrors; not supported by this release")
        return AccountSnapshot(as_of=dt.datetime.now(dt.timezone.utc).isoformat(), credit=float(credit),
                               pending_open_amount=round(pend_amt, 6), positions=tuple(positions),
                               pending_orders=tuple(pend), currency="USD", source="etoro-real-pnl",
                               raw_sha256=sha256_text(canonical_json(raw)),
                               provenance=self.capabilities(), mirrors=tuple(mirrors))

    @staticmethod
    def _open_status(order_id, r) -> OrderStatus:
        st = r.get("status") or {}
        execs = r.get("positionExecutions") or r.get("positions") or []
        pids = tuple(str(p.get("positionId", p.get("positionID"))) for p in execs
                     if p.get("positionId", p.get("positionID")) is not None)
        return OrderStatus(order_id=str(r.get("orderId", order_id)), status_id=st.get("id"),
                           error_code=st.get("errorCode"), error_message=str(st.get("errorMessage") or ""),
                           position_ids=pids, filled_amount=None, filled_units=None, proceeds=None, rate=None, raw=r)

    def order_status(self, order_id, kind):
        if kind == "open":
            r = self._request("GET", f"/api/v2/trading/info/orders:lookup?orderId={int(order_id)}", allow_404=True)
            return None if r is None else self._open_status(order_id, r)
        r = self._request("GET", f"/api/v1/trading/info/real/close-orders/{int(order_id)}", allow_404=True)
        if r is None:
            return None
        rows = r.get("positions") or []
        return OrderStatus(order_id=str(order_id), status_id=r.get("statusID"), error_code=r.get("errorCode"),
                           error_message=str(r.get("errorMessage") or ""),
                           position_ids=tuple(str(p.get("positionID")) for p in rows),
                           filled_amount=sum(float(p.get("amount", 0)) for p in rows) if rows else None,
                           filled_units=sum(float(p.get("units", 0)) for p in rows) if rows else None,
                           proceeds=r.get("proceeds") if r.get("statusID") == 3 else None,
                           rate=rows[0].get("rate") if rows else None, raw=r)

    def order_status_by_client_ref(self, client_ref: str, kind: str):
        """OPEN orders only: orders:lookup?referenceId=<x-request-id> -- eToro's documented recovery handle when the
        create-order response is lost. Close orders have no referenceId (returns None: no evidence)."""
        if kind != "open":
            return None
        from .etoro_live import request_id_for
        ref = request_id_for(client_ref)
        r = self._request("GET", f"/api/v2/trading/info/orders:lookup?referenceId={ref}", allow_404=True)
        return None if r is None else self._open_status(r.get("orderId"), r)

    def closed_trades(self, min_date: str, page: int = 1, page_size: int = 50) -> list:
        """Closed-trade rows (shape verified live via the GM audit: positionId, orderId, instrumentId, isBuy,
        leverage, units, openRate, closeRate, openTimestamp, closeTimestamp, investment, initialInvestment,
        netProfit, fees, ...). Reporting only: realized P&L is NOT used to explain cash (netProfit/fees vs the
        credited amount is unverified for partial closes and financing)."""
        r = self._request("GET", f"/api/v1/trading/info/trade/history?minDate={min_date}&page={page}&pageSize={page_size}")
        return r if isinstance(r, list) else (r.get("trades") or [] if isinstance(r, dict) else [])

    def cash_transactions(self, since_iso):
        return None                                   # UNAVAILABLE for the trading account (see module doc)

    def account_balances(self) -> dict:
        """GET /api/v1/balances (expand=equityDetails): the MAIN trading account's available, frozenCash, currentPNL
        (account-level unrealized P&L) and totalUsedMargin, plus the eToro-Money cash account ids. Documented
        decomposition: trading total = available + totalUsedMargin + currentPNL (native currency)."""
        r = self._request("GET", "/api/v1/balances?accountTypes=Trading,Cash&includeSubAccounts=false"
                                 "&expand=equityDetails")
        trading, cash = None, []
        for b in r.get("balances") or []:
            if b.get("accountType") == "Trading" and b.get("subType") != "subAccount" and trading is None:
                e = b.get("equityDetails") or {}
                trading = {"account_id": str(b.get("accountId")), "currency": b.get("currency"),
                           "available": e.get("available"), "frozen_cash": e.get("frozenCash"),
                           "current_pnl": e.get("currentPNL"), "used_margin": e.get("totalUsedMargin"),
                           "balance": b.get("balance")}
            elif b.get("accountType") == "Cash" and b.get("accountId"):
                cash.append(str(b["accountId"]))
        return {"trading": trading, "cash_account_ids": cash}

    def funding_evidence(self, since_iso):
        """Settled eToro-Money internal transfers (counterparty internal_account) since `since_iso`, as signed
        trading-account amounts: a DEBIT of the cash account is a candidate DEPOSIT into trading, a CREDIT a candidate
        WITHDRAWAL from it. Evidence only: a row classifies a credit ONLY if it matches its amount and time
        (core/funding.investigate). Direct card/bank deposits into the trading account are NOT covered by any
        documented feed. Returns None when the capability is unavailable."""
        from .base import CashTransaction
        try:
            ids = self.account_balances()["cash_account_ids"]
        except (BrokerUnavailable, BrokerError):
            return None
        out = []
        for acc in ids:
            token = None
            for _page in range(20):
                path = f"/api/v1/money/accounts/cash/{acc}/transactions?pageSize=200" + (f"&pageToken={token}" if token else "")
                r = self._request("GET", path)
                stop = False
                for t in r.get("results") or []:
                    if str(t.get("postedAt")) <= since_iso:
                        stop = True
                        continue
                    cp = t.get("counterparty") or {}
                    if t.get("transactionType") != "internalTransfer" or t.get("status") != "settled" \
                            or cp.get("type") != "internal_account":
                        continue
                    amt = float(t.get("amount") or 0.0)
                    signed = amt if t.get("direction") == "debit" else -amt
                    out.append(CashTransaction(tx_id=str(t.get("id")), kind="DEPOSIT" if signed > 0 else "WITHDRAWAL",
                                               amount=signed, currency=str(t.get("currency")),
                                               posted_at=str(t.get("postedAt")),
                                               evidence=f"eToro Money internalTransfer {t.get('id')} "
                                                        f"({t.get('direction')}, counterparty {cp.get('name')})"))
                pg = r.get("pagination") or {}
                token = pg.get("nextPageToken")
                if stop or not pg.get("hasNext") or not token:
                    break
        return out

    def balance_history(self, account_id: str, from_date: str, to_date: str) -> list:
        """End-of-day trading-account snapshots (cash, investedAmount, pnl, total): corroborates the amount and the
        day of a funding change; it carries NO reason codes, so it never classifies anything by itself."""
        r = self._request("GET", f"/api/v1/balances/trading/{account_id}/history?fromDate={from_date}&toDate={to_date}")
        return [{"date": s.get("date"), **{k: a.get(k) for k in ("cash", "investedAmount", "pnl", "total")}}
                for s in r.get("snapshots") or [] for a in s.get("accountSnapshots") or []
                if str(a.get("accountId")) == str(account_id)]

    def search_instrument(self, symbol: str) -> list:
        """Instrument rows whose eToro internal symbol is exactly `symbol` (read-only market data)."""
        r = self._request("GET", f"/api/v1/market-data/search?internalSymbolFull={symbol.upper()}")
        items = r.get("items") if isinstance(r, dict) else r
        return items if isinstance(items, list) else []

    def eligibility(self, instrument_ids):
        r = self._request("POST", "/api/v2/trading/info/eligibility",
                          {"instrumentIds": [int(i) for i in instrument_ids], "currency": "USD"})
        return {"raw": r}

    def quotes(self, instrument_ids):
        ids = ",".join(str(int(i)) for i in instrument_ids)
        r = self._request("GET", f"/api/v1/market-data/instruments/rates?instrumentIds={ids}")
        out = {}
        for q in r.get("rates", []):
            out[int(q["instrumentID"])] = Quote(int(q["instrumentID"]), float(q["bid"]), float(q["ask"]),
                                                str(q.get("date")))
        return out
