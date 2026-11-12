"""
Records watched markets' order books and trades (plan 2.6).

Recording starts in Phase 2, before any strategy exists, because new-market
books are "the most valuable input for tuning later" (spec) -- and because
the market maker spent two weeks unable to answer its gating question for
want of a raw tape it had never persisted.

Snapshots: when a book has changed, at most once per min_interval; and at
least once per heartbeat even if it hasn't, so a quiet book is still
provably quiet rather than missing. Stale books are never recorded -- a book
with a sequence hole is not data. Writes are batched into one transaction
per pass.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
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
    paused_low_disk: bool = False


class Recorder:
    def __init__(self, books: OrderBookStore, repo: Repository, clock: Clock, *,
                 tickers: Callable[[], list[str]], min_interval_sec: float, heartbeat_sec: float,
                 depth_levels: int, disk_path: Path | None = None, min_free_gb: float = 0.0,
                 on_low_disk: Callable[[float], None] | None = None) -> None:
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
        # Disk guard. The bot shares a droplet with the market maker, and a
        # full disk would kill the market maker's frozen test along with this bot.
        # Below min_free_gb, snapshots pause (trades, far smaller, continue);
        # they resume with 0.5 GB of hysteresis.
        self._disk_path = disk_path
        self._min_free = min_free_gb
        self._on_low_disk = on_low_disk
        self._disk_checked = float("-inf")

    def _disk_ok(self, now: float) -> bool:
        if self._disk_path is None or self._min_free <= 0:
            return True
        if now - self._disk_checked >= 30:
            self._disk_checked = now
            free_gb = shutil.disk_usage(self._disk_path).free / 1e9
            threshold = self._min_free + (0.5 if self.stats.paused_low_disk else 0.0)
            paused = free_gb < threshold
            if paused and not self.stats.paused_low_disk:
                log.error("recording_paused_low_disk", free_gb=round(free_gb, 2), min_free_gb=self._min_free)
                if self._on_low_disk is not None:
                    self._on_low_disk(free_gb)
            elif not paused and self.stats.paused_low_disk:
                log.warning("recording_resumed", free_gb=round(free_gb, 2))
            self.stats.paused_low_disk = paused
        return not self.stats.paused_low_disk

    def record_due(self) -> int:
        now = self._clock.now()
        if not self._disk_ok(now):
            return 0
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
