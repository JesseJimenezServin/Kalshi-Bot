"""
Shared fair-value types (plan 3.1).

Every model implements  estimate(market, context) -> FairValue | None  and
is pure: no I/O, no clock. Prices here are Decimal CENTS (0-100) -- the
unit the strategy reasons in -- converted once from Kalshi's dollar strings
at the edge (Quote.from_*).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Quote:
    """Best YES bid/ask in cents and their sizes. Either side may be missing."""

    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal = Decimal(0)
    ask_size: Decimal = Decimal(0)

    @classmethod
    def from_dollars(cls, bid: Decimal | None, ask: Decimal | None, bid_size: Decimal | None,
                     ask_size: Decimal | None) -> Quote:
        # Kalshi reports an absent side as price 0 (bid) / 1 (ask) with size 0.
        b = bid * HUNDRED if bid is not None and bid > 0 and (bid_size or 0) > 0 else None
        a = ask * HUNDRED if ask is not None and ask < 1 and (ask_size or 0) > 0 else None
        return cls(b, a, (bid_size or Decimal(0)) if b is not None else Decimal(0),
                   (ask_size or Decimal(0)) if a is not None else Decimal(0))

    @property
    def two_sided(self) -> bool:
        return self.bid is not None and self.ask is not None

    def mid_and_halfwidth(self) -> tuple[Decimal, Decimal] | None:
        """Best price estimate and how uncertain it is, in cents.

        Two-sided: the midpoint, +/- half the spread. One-sided, the only
        honest statement is a bound: no bid means worth somewhere in
        [0, ask]; no ask means somewhere in [bid, 100]."""
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2, (self.ask - self.bid) / 2
        if self.ask is not None:
            return self.ask / 2, self.ask / 2
        if self.bid is not None:
            return (HUNDRED + self.bid) / 2, (HUNDRED - self.bid) / 2
        return None

    @property
    def depth(self) -> Decimal:
        """Contracts available on the thinner side; 0 if one-sided."""
        return min(self.bid_size, self.ask_size) if self.two_sided else Decimal(0)


@dataclass(frozen=True)
class MarketInfo:
    ticker: str
    event_ticker: str
    series_ticker: str
    strike_type: str | None
    floor_strike: float | None
    cap_strike: float | None
    close_time: datetime | None = None

    @property
    def order_key(self) -> float:
        """Position on the strike axis: bottom tail first."""
        if self.floor_strike is not None:
            return self.floor_strike
        if self.cap_strike is not None:
            return self.cap_strike - 1e6  # 'less' tail: below everything
        return float("-inf")


@dataclass(frozen=True)
class EventContext:
    event_ticker: str
    series_ticker: str
    mutually_exclusive: bool
    markets: tuple[MarketInfo, ...]
    quotes: Mapping[str, Quote | None]
    now: datetime

    def sorted_markets(self) -> list[MarketInfo]:
        return sorted(self.markets, key=lambda m: m.order_key)


@dataclass(frozen=True)
class FairValue:
    cents: Decimal
    confidence: float
    model: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence {self.confidence} outside [0, 1]")


class FairValueModel(Protocol):
    name: str

    def estimate(self, market: MarketInfo, ctx: EventContext) -> FairValue | None: ...


def clip_cents(x: Decimal) -> Decimal:
    """Clip to 1-99 and round to 4 places (1/100 of a cent), so every model's
    output fits the database's exact 6-place storage, which refuses to round."""
    return min(Decimal(99), max(Decimal(1), x)).quantize(Decimal("0.0001"))
