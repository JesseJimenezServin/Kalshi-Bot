"""
Records watched markets' order books and trades (plan 2.6).

Recording starts in Phase 2, before any strategy exists, because new-market
books are "the most valuable input for tuning later" (spec) -- and because
the liquidity lab spent two weeks unable to answer its gating question for
want of a raw tape it had never persisted.

Snapshots: when a book has changed, at most once per min_interval; and at
least once per heartbeat even if it hasn't, so a quiet book is still
provably quiet rather than missing. Stale books are never recorded -- a book
with a sequence hole is not data. Writes are batched into one transaction
per pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from bot.clock import Clock
from bot.data.orderbook import OrderBookStore
from bot.monitoring.log import get_logger
from bot.storage.repo import Repository

log = get_logger(__name__)


@dataclass
class RecorderStats:
    snapshots: int = 0
    trades: int = 0
    skipped_stale: int = 0


class Recorder:
    def __init__(self, books: OrderBookStore, repo: Repository, clock: Clock, *,
                 tickers: Callable[[], list[str]], min_interval_sec: float, heartbeat_sec: float,
                 depth_levels: int) -> None:
        self._books = books
        self._repo = repo
        self._clock = clock
        self._tickers = tickers
        self._min_interval = min_interval_sec
        self._heartbeat = heartbeat_sec
        self._depth = depth_levels
        self._last: dict[str, tuple[float, int]] = {}  # ticker -> (recorded_at, book version)
        self._trade_buffer: list[dict[str, Any]] = []
        self.stats = RecorderStats()

    def record_due(self) -> int:
        now = self._clock.now()
        rows: list[dict[str, Any]] = []
        for ticker in self._tickers():
            book = self._books.get(ticker)
            if book is None:
                continue
            if book.stale:
                self.stats.skipped_stale += 1
                continue
            version = book.snapshots_applied + book.deltas_applied
            last = self._last.get(ticker)
            if last is not None:
                elapsed = now - last[0]
                changed = version != last[1]
                if not ((changed and elapsed >= self._min_interval) or elapsed >= self._heartbeat):
                    continue
            bids, asks = book.snapshot_levels(self._depth)
            rows.append({"ticker": ticker, "ts": datetime.fromtimestamp(now, UTC), "yes_bids": bids,
                         "yes_asks": asks})
            self._last[ticker] = (now, version)
        self._repo.add_snapshots(rows)
        self.stats.snapshots += len(rows)
        return len(rows)

    def forget(self, tickers: list[str]) -> None:
        for t in tickers:
            self._last.pop(t, None)

    def on_trade(self, msg: dict[str, Any]) -> None:
        try:
            ts_ms = msg.get("ts_ms")
            ts = (datetime.fromtimestamp(int(ts_ms) / 1000, UTC) if ts_ms
                  else datetime.fromtimestamp(int(msg["ts"]), UTC))
            self._trade_buffer.append({
                "trade_id": str(msg["trade_id"]), "ticker": msg["market_ticker"], "ts": ts,
                "yes_price": Decimal(msg["yes_price_dollars"]), "count": Decimal(msg["count_fp"]),
                "taker_side": msg.get("taker_outcome_side") or msg.get("taker_side"),
                "taker_book_side": msg.get("taker_book_side"),
            })
        except (KeyError, ValueError, TypeError) as e:
            log.warning("trade_unparseable", error=repr(e), msg=msg)

    def flush_trades(self) -> int:
        rows, self._trade_buffer = self._trade_buffer, []
        n = self._repo.add_trades(rows)
        self.stats.trades += n
        return n
