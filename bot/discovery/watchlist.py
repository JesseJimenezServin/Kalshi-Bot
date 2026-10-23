"""
Strategy step 1.4: watch a new market for watch_duration_min, or until a
position is opened in it.

The watchlist decides what is watched; the subscription callbacks decide what
is streamed. Phase 4 adds a second reason to keep streaming a market (an open
position needs its book to exit), so removal from the watchlist calls
`on_unwatch`, and whoever owns subscriptions decides whether to drop the feed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.clock import Clock
from bot.discovery.scanner import NewMarket
from bot.monitoring.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class WatchEntry:
    market: NewMarket
    added_at: datetime
    expires_at: datetime


class Watchlist:
    def __init__(self, clock: Clock, duration_min: float, *,
                 on_watch: Callable[[list[str]], Awaitable[None]],
                 on_unwatch: Callable[[list[str]], Awaitable[None]]) -> None:
        self._clock = clock
        self._duration = timedelta(minutes=duration_min)
        self._on_watch = on_watch
        self._on_unwatch = on_unwatch
        self._entries: dict[str, WatchEntry] = {}

    async def add(self, market: NewMarket, added_at: datetime | None = None) -> bool:
        """`added_at` lets a restart resume a watch with its ORIGINAL expiry."""
        if market.ticker in self._entries:
            return False
        start = added_at or self._clock.utcnow()
        if start + self._duration <= self._clock.utcnow():
            return False  # its watch would already have ended
        self._entries[market.ticker] = WatchEntry(market, start, start + self._duration)
        await self._on_watch([market.ticker])
        return True

    async def remove(self, ticker: str, reason: str) -> None:
        if self._entries.pop(ticker, None) is not None:
            log.info("unwatched", ticker=ticker, reason=reason)
            await self._on_unwatch([ticker])

    async def expire(self) -> list[str]:
        now = self._clock.utcnow()
        gone = [t for t, e in self._entries.items() if e.expires_at <= now]
        for t in gone:
            del self._entries[t]
        if gone:
            log.info("watch_expired", tickers=len(gone))
            await self._on_unwatch(gone)
        return gone

    def active(self) -> list[NewMarket]:
        return [e.market for e in self._entries.values()]

    def __contains__(self, ticker: object) -> bool:
        return ticker in self._entries

    def __len__(self) -> int:
        return len(self._entries)
