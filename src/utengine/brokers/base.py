"""Broker interface. Reading and writing are SEPARATE capabilities.

BrokerReader: account snapshot, order status, close-order status, trade history, cash transactions, eligibility,
quotes. BrokerWriter: open by amount, close by position. The eToro adapter in this release implements only
BrokerReader. The simulated broker implements both and is the only writer the engine can obtain
(core/security.py).

Units and semantics follow eToro: an OPEN is a USD `amount` (exposure = amount x leverage); every open creates its
own positionId; a CLOSE is per position (all units, or `units` for a partial close, which eToro executes by
splitting the position). Fractional units are allowed. No share-count assumptions are made for CFDs.
Order status ids follow eToro: 1 Received, 2 Placed, 3 Filled, 4 Rejected, 5 PartiallyFilled.
"""
from __future__ import annotations

from dataclasses import dataclass, field

STATUS_RECEIVED, STATUS_PLACED, STATUS_FILLED, STATUS_REJECTED, STATUS_PARTIAL = 1, 2, 3, 4, 5
STATUS_NAMES = {1: "Received", 2: "Placed", 3: "Filled", 4: "Rejected", 5: "PartiallyFilled"}

# capability levels, reported per field in AccountSnapshot.provenance and in docs/ETORO_CAPABILITIES.md
VERIFIED_LIVE = "VERIFIED_LIVE"            # observed against the real account (by this engine or the GM audit)
DOCUMENTED = "DOCUMENTED"                  # in eToro's official API reference, not observed by us
DERIVED = "DERIVED"                        # computed by documented formula from verified fields
UNAVAILABLE = "UNAVAILABLE"                # no interface known; never claimed
SIMULATED = "SIMULATED"


@dataclass(frozen=True)
class Position:
    position_id: str
    instrument_id: int
    symbol: str | None
    is_buy: bool
    units: float
    amount: float                   # invested USD (margin) of this position
    leverage: int
    open_rate: float
    settlement: str                 # CFD | REAL | UNKNOWN
    unrealized_pnl: float | None
    fees: float                     # fees to date incl. overnight financing (eToro: totalFees + totalExternalFees)
    opened_at: str | None
    exposure_usd: float | None = None   # broker-reported current exposure (absolute USD), when the broker gives it
    close_rate: float | None = None     # the broker's rate behind unrealized_pnl
    quote_at: str | None = None         # timestamp of that rate

    @property
    def value(self) -> float:
        return self.amount + (self.unrealized_pnl or 0.0)


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    instrument_id: int
    amount: float
    is_buy: bool
    kind: str                       # open | mit | close


@dataclass(frozen=True)
class AccountSnapshot:
    as_of: str
    credit: float                   # uninvested cash (eToro `credit`)
    pending_open_amount: float      # cash reserved by pending opens
    positions: tuple
    pending_orders: tuple
    currency: str
    source: str
    raw_sha256: str
    provenance: dict = field(default_factory=dict)
    mirrors: tuple = ()             # copy-trading mirrors (MirrorHolding); the engine refuses to trade beside them

    @property
    def available_cash(self) -> float:
        """eToro guide 'Calculate Available Cash': credit - pending manual opens - pending MIT orders."""
        return self.credit - self.pending_open_amount

    @property
    def total_invested(self) -> float:
        return sum(p.amount for p in self.positions) + self.pending_open_amount

    @property
    def unrealized_pnl(self) -> float | None:
        vals = [p.unrealized_pnl for p in self.positions]
        return None if any(v is None for v in vals) else sum(vals)

    @property
    def equity(self) -> float | None:
        """eToro guide 'Calculate Equity': available cash + total invested + unrealized P&L (USD, no mirrors)."""
        u = self.unrealized_pnl
        return None if u is None else self.available_cash + self.total_invested + u


@dataclass(frozen=True)
class MirrorHolding:
    mirror_id: str
    parent_username: str | None
    invested: float
    value: float | None
    positions: int


@dataclass(frozen=True)
class OrderStatus:
    order_id: str
    status_id: int | None
    error_code: int | None
    error_message: str
    position_ids: tuple             # positions created (open) or split ids (close)
    filled_amount: float | None     # open: USD invested so far
    filled_units: float | None
    proceeds: float | None          # close: cash returned
    rate: float | None
    raw: dict = field(default_factory=dict)

    @property
    def final(self) -> bool:
        return self.status_id in (STATUS_FILLED, STATUS_REJECTED)


@dataclass(frozen=True)
class CashTransaction:
    tx_id: str
    kind: str                       # DEPOSIT | WITHDRAWAL | DIVIDEND | FEE | ADJUSTMENT | OTHER
    amount: float                   # signed: + into the trading account, - out of it
    currency: str
    posted_at: str
    evidence: str


@dataclass(frozen=True)
class Quote:
    instrument_id: int
    bid: float
    ask: float
    quote_time: str


@dataclass(frozen=True)
class SubmitResult:
    order_id: str
    raw: dict = field(default_factory=dict)


class BrokerReader:
    name = "abstract"

    def capabilities(self) -> dict:
        raise NotImplementedError

    def account_snapshot(self) -> AccountSnapshot:
        raise NotImplementedError

    def order_status(self, order_id: str, kind: str) -> OrderStatus | None:
        """kind: 'open' or 'close'. None = the broker does not know the order (yet)."""
        raise NotImplementedError

    def cash_transactions(self, since_iso: str):
        """List[CashTransaction], or None when the capability is unavailable (never an empty guess)."""
        raise NotImplementedError

    def eligibility(self, instrument_ids: list) -> dict:
        raise NotImplementedError

    def quotes(self, instrument_ids: list) -> dict:
        raise NotImplementedError

    def inventory(self) -> AccountSnapshot:
        """Full read-only account inventory for a takeover preview, INCLUDING copy-trading mirrors (the normal
        account_snapshot refuses an account with mirrors)."""
        return self.account_snapshot()

    def account_identity(self) -> str | None:
        """A stable, non-secret identifier of the broker account (approvals are bound to it); None if unknown."""
        return None

    def closed_position_proceeds(self, position_id: str):
        """{'proceeds': float, ...} for a position closed without a known close-order id, or None when the broker
        offers no verified way to attribute it (then the cash change stays unexplained and the engine halts)."""
        return None


class BrokerWriter:
    def submit_open(self, instrument_id: int, amount: float, *, is_buy: bool, leverage: int,
                    client_ref: str) -> SubmitResult:
        raise NotImplementedError

    def submit_close(self, position_id: str, instrument_id: int, units: float | None, *,
                     client_ref: str) -> SubmitResult:
        raise NotImplementedError
