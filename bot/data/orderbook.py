"""
In-memory order books fed by the WebSocket (docs/API_NOTES.md section 4).

Kalshi sends BIDS ONLY, for YES and for NO. Asks are derived:
    best YES ask = 1 - best NO bid        best NO ask = 1 - best YES bid
because a NO bid at p is an offer to sell YES at 1 - p.

All arithmetic is Decimal. The liquidity lab's books held float sizes, so a
level cancelled to nothing landed on residue like 5.55e-17, was never removed,
and became the "best" price -- 31.6% of its logged levels were phantoms, and
they manufactured its entire reported profit (lab AUTOPSY.md section 4.1).
Decimal deltas cancel exactly, and a level is dropped the moment it reaches
<= 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Contract = Literal["yes", "no"]
ONE = Decimal(1)
ZERO = Decimal(0)


@dataclass
class OrderBook:
    ticker: str
    yes_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    no_bids: dict[Decimal, Decimal] = field(default_factory=dict)
    # A book is stale from creation until its first snapshot, and again after
    # a sequence gap until a fresh snapshot lands. Nothing may trade on it.
    stale: bool = True
    clock: int | None = None  # exchange clock of the last applied message
    updated_at: float | None = None  # our clock, unix seconds
    snapshots_applied: int = 0
    deltas_applied: int = 0

    # ----------------------------------------------------------- updates

    def apply_snapshot(self, yes: list[tuple[Decimal, Decimal]], no: list[tuple[Decimal, Decimal]],
                       clock: int | None, now: float) -> None:
        self.yes_bids = {p: s for p, s in yes if s > ZERO}
        self.no_bids = {p: s for p, s in no if s > ZERO}
        self.clock = clock
        self.updated_at = now
        self.stale = False
        self.snapshots_applied += 1

    def apply_delta(self, side: Contract, price: Decimal, delta: Decimal, clock: int | None, now: float) -> None:
        levels = self.yes_bids if side == "yes" else self.no_bids
        size = levels.get(price, ZERO) + delta
        if size > ZERO:
            levels[price] = size
        else:
            levels.pop(price, None)
        self.clock = clock
        self.updated_at = now
        self.deltas_applied += 1

    def mark_stale(self) -> None:
        self.stale = True

    # ---------------------------------------------------------- queries

    def _best(self, levels: dict[Decimal, Decimal]) -> tuple[Decimal, Decimal] | None:
        if not levels:
            return None
        p = max(levels)
        return p, levels[p]

    def best_yes_bid(self) -> tuple[Decimal, Decimal] | None:
        return self._best(self.yes_bids)

    def best_no_bid(self) -> tuple[Decimal, Decimal] | None:
        return self._best(self.no_bids)

    def best_yes_ask(self) -> tuple[Decimal, Decimal] | None:
        """Lowest price to BUY YES: the best NO bid, seen from the YES side."""
        b = self.best_no_bid()
        return None if b is None else (ONE - b[0], b[1])

    def best_no_ask(self) -> tuple[Decimal, Decimal] | None:
        b = self.best_yes_bid()
        return None if b is None else (ONE - b[0], b[1])

    def best_bid(self, contract: Contract) -> tuple[Decimal, Decimal] | None:
        return self.best_yes_bid() if contract == "yes" else self.best_no_bid()

    def best_ask(self, contract: Contract) -> tuple[Decimal, Decimal] | None:
        return self.best_yes_ask() if contract == "yes" else self.best_no_ask()

    def mid_yes(self) -> Decimal | None:
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        if bid is None or ask is None:
            return None
        return (bid[0] + ask[0]) / 2

    def depth_at_or_better(self, contract: Contract, price: Decimal) -> Decimal:
        """Contracts of `contract` available to BUY at `price` or cheaper.

        Buying YES at <= p takes NO bids at >= 1 - p (and vice versa)."""
        opposite = self.no_bids if contract == "yes" else self.yes_bids
        floor = ONE - price
        return sum((s for p, s in opposite.items() if p >= floor), ZERO)

    def bid_depth_at_or_better(self, contract: Contract, price: Decimal) -> Decimal:
        """Contracts of `contract` we could SELL at `price` or higher (exits)."""
        own = self.yes_bids if contract == "yes" else self.no_bids
        return sum((s for p, s in own.items() if p >= price), ZERO)

    def is_crossed(self) -> bool:
        """YES bid + NO bid > 1 should never persist; if it does, the book is wrong."""
        y, n = self.best_yes_bid(), self.best_no_bid()
        return y is not None and n is not None and y[0] + n[0] > ONE

    # -------------------------------------------------------- recording

    def snapshot_levels(self, depth: int) -> tuple[list[list[str]], list[list[str]]]:
        """(yes_bids best-first, yes_asks best-first) as strings, top `depth` each.
        The spec's orderbook_snapshots schema stores YES bids and YES asks."""
        bids = sorted(self.yes_bids.items(), key=lambda kv: kv[0], reverse=True)[:depth]
        asks = sorted(((ONE - p, s) for p, s in self.no_bids.items()), key=lambda kv: kv[0])[:depth]
        return [[str(p), str(s)] for p, s in bids], [[str(p), str(s)] for p, s in asks]


class OrderBookStore:
    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def get(self, ticker: str) -> OrderBook | None:
        return self._books.get(ticker)

    def ensure(self, ticker: str) -> OrderBook:
        book = self._books.get(ticker)
        if book is None:
            book = self._books[ticker] = OrderBook(ticker)
        return book

    def drop(self, ticker: str) -> None:
        self._books.pop(ticker, None)

    def tickers(self) -> list[str]:
        return list(self._books)

    def mark_stale(self, tickers: list[str] | None = None) -> None:
        for t in tickers if tickers is not None else list(self._books):
            if (b := self._books.get(t)) is not None:
                b.mark_stale()
