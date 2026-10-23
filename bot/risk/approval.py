"""
The order gate. The REST client's write methods accept only an Approved, and
only the risk manager can construct one.

Python cannot make this tamper-proof, but it makes "placed an order without
asking the risk manager" impossible to do by accident: there is no code path
from an OrderRequest to the exchange that does not go through approve().

Approval is also single-use and short-lived -- a decision made against one
book state must not be replayed later against another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

# Only bot.risk.manager imports this. Anything else holding it is a bug.
_RISK_MANAGER_TOKEN = object()

APPROVAL_TTL_SEC = 5.0

Intent = Literal["buy_yes", "sell_yes", "buy_no", "sell_no"]
TimeInForce = Literal["fill_or_kill", "good_till_canceled", "immediate_or_cancel"]

# V2 orders quote the YES leg only (docs/API_NOTES.md section 6).
_BOOK_SIDE: dict[str, Literal["bid", "ask"]] = {
    "buy_yes": "bid",   # buy YES
    "sell_no": "bid",   # selling NO  == buying YES at 1 - price
    "sell_yes": "ask",  # sell YES
    "buy_no": "ask",    # buying NO   == selling YES at 1 - price
}


@dataclass(frozen=True)
class OrderRequest:
    """What the strategy wants. `yes_price` is always the YES-leg price."""

    ticker: str
    intent: Intent
    count: Decimal
    yes_price: Decimal
    client_order_id: str
    time_in_force: TimeInForce = "good_till_canceled"
    expiration_time: int | None = None  # unix seconds; GTC + expiry = exchange-enforced timeout
    reduce_only: bool = False
    post_only: bool = False
    exchange_index: int | None = None
    event_ticker: str | None = None

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("count must be positive")
        if not Decimal("0") < self.yes_price < Decimal("1"):
            raise ValueError("yes_price must be strictly between 0 and 1 dollars")
        if self.reduce_only and self.time_in_force != "immediate_or_cancel":
            raise ValueError("Kalshi rejects reduce_only unless time_in_force is immediate_or_cancel")
        if self.time_in_force == "immediate_or_cancel" and self.expiration_time is not None:
            raise ValueError("immediate_or_cancel cannot carry an expiration_time")

    @property
    def book_side(self) -> Literal["bid", "ask"]:
        return _BOOK_SIDE[self.intent]

    def to_wire(self) -> dict[str, object]:
        body: dict[str, object] = {
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "side": self.book_side,
            "count": f"{self.count:.2f}",
            "price": f"{self.yes_price:.4f}",
            "time_in_force": self.time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": self.post_only,
            "reduce_only": self.reduce_only,
        }
        if self.expiration_time is not None:
            body["expiration_time"] = self.expiration_time
        if self.exchange_index is not None:
            body["exchange_index"] = self.exchange_index
        return body


@dataclass(frozen=True)
class CancelRequest:
    order_id: str
    ticker: str  # needed so the exchange can route to the right shard
    exchange_index: int | None = None


@dataclass
class Approved:
    """Proof that the risk manager passed this exact request, once, just now."""

    request: OrderRequest | CancelRequest
    issued_at: float
    _token: object = field(repr=False, default=None)
    _spent: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self._token is not _RISK_MANAGER_TOKEN:
            raise PermissionError("Approved can only be issued by the risk manager")

    def spend(self, now: float) -> OrderRequest | CancelRequest:
        """Consume the approval. Raises if reused or stale."""
        if self._spent:
            raise PermissionError("approval already used")
        if now - self.issued_at > APPROVAL_TTL_SEC:
            raise PermissionError(f"approval is stale ({now - self.issued_at:.1f}s old)")
        self._spent = True
        return self.request


@dataclass(frozen=True)
class Rejected:
    request: OrderRequest | CancelRequest
    reason: str


def _issue(request: OrderRequest | CancelRequest, now: float) -> Approved:
    """For bot.risk.manager only."""
    return Approved(request=request, issued_at=now, _token=_RISK_MANAGER_TOKEN)
