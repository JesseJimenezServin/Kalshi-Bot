"""
The running bot (plan 2.8). Phase 2 scope: discovery + recording, no trading.

Concurrent tasks, so a slow REST call never blocks the feed:
  ws          WebSocket feed: lifecycle, books, trades
  scan        every scan_interval: emit just-opened markets, expire the watchlist
  sweep       every sweep_interval (and on a lifecycle gap): REST backstop
  record      every 0.5 s: due snapshots, buffered trades
  results     every status_refresh_min: results for closed markets
  heartbeat   every 60 s: stats to the log; every 10 min to bot_events

Each loop runs under a supervisor: an exception is logged, recorded,
alerted, and the loop restarts with backoff, so one bug cannot silently stop
recording. Startup: validate config (already done by the CLI), take the
singleton lock, open storage, size rate limits, load the series catalog,
sweep, then start. Shutdown on SIGINT/SIGTERM: stop the feed, cancel loops,
flush buffers.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import timedelta
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bot.api.auth import Signer
from bot.api.rest import KalshiRestClient
from bot.api.ws import KalshiWebSocket
from bot.clock import Clock, SystemClock
from bot.config import AppConfig
from bot.data.orderbook import OrderBookStore
from bot.data.recorder import Recorder
from bot.discovery.catalog import SeriesCatalog
from bot.discovery.results import ResultsUpdater
from bot.discovery.scanner import NewMarket, Scanner
from bot.discovery.watchlist import Watchlist
from bot.monitoring.alerts import Alerter
from bot.monitoring.log import get_logger
from bot.storage.repo import Repository

log = get_logger("app")

HEARTBEAT_SEC = 60.0
# Kalshi meters per account and the market maker shares this account, so
# this bot takes 75% of the budget and leaves the rest as headroom.
RATE_LIMIT_SHARE = 0.75
HEARTBEAT_EVENT_EVERY = 10  # heartbeats


class Bot:
    def __init__(self, cfg: AppConfig, *, clock: Clock | None = None, rest: KalshiRestClient | None = None,
                 ws_connector: Any = None, repo: Repository | None = None) -> None:
        self.cfg = cfg
        self.clock = clock or SystemClock()
        signer = Signer.from_file(cfg.secrets.kalshi_api_key_id, cfg.secrets.kalshi_private_key_path, self.clock)
        self.rest = rest or KalshiRestClient(cfg.rest_base_url, signer, writes_allowed=cfg.writes_allowed,
                                             clock=self.clock)
        self.repo = repo or Repository.from_url(cfg.file.storage.database_url, self.clock)
        self.books = OrderBookStore()
        self.alerts = Alerter.from_config(cfg, self.clock)
        d, r = cfg.file.discovery, cfg.file.recording
        self.catalog = SeriesCatalog(self.rest, d.include_categories, d.exclude_series, self.clock)
        self.watchlist = Watchlist(self.clock, d.watch_duration_min, on_watch=self._watch, on_unwatch=self._unwatch,
                                   max_size=r.max_watched)
        self.scanner = Scanner(self.rest, self.catalog, self.repo, self.clock, window_min=d.new_market_window_min,
                               sweep_lookback_hours=d.sweep_lookback_hours, on_new=self._on_new_market,
                               category_sample=d.category_sample)
        db_dir = self._db_dir(cfg.file.storage.database_url)
        self.recorder = Recorder(self.books, self.repo, self.clock, tickers=lambda: sorted(self.ws.desired),
                                 min_interval_sec=r.snapshot_min_interval_sec, heartbeat_sec=r.snapshot_heartbeat_sec,
                                 depth_levels=r.depth_levels, disk_path=db_dir, min_free_gb=r.min_free_disk_gb,
                                 on_low_disk=self._on_low_disk)
        self.results = ResultsUpdater(self.rest, self.repo, self.clock)
        self.ws = KalshiWebSocket(cfg.ws_url, signer, self.books, self, clock=self.clock,
                                  **({"connector": ws_connector} if ws_connector else {}))
        self._stop = asyncio.Event()
        self._sweep_now = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.task_failures: dict[str, int] = {}

    @staticmethod
    def _db_dir(url: str) -> Path | None:
        prefix = "sqlite:///"
        if url.startswith(prefix) and ":memory:" not in url:
            return Path(url.removeprefix(prefix)).expanduser().parent
        return None

    def _on_low_disk(self, free_gb: float) -> None:
        self.repo.add_event("recording_paused_low_disk", free_gb=round(free_gb, 2))
        asyncio.ensure_future(self.alerts.alert(
            "disk", f"free disk {free_gb:.1f} GB: book recording paused to protect the droplet", level="error"))

    # ------------------------------------------------ WsHandlers protocol

    async def on_trade(self, msg: dict[str, Any]) -> None:
        if msg.get("market_ticker") in self.ws.desired:
            self.recorder.on_trade(msg)

    async def on_lifecycle(self, kind: str, msg: dict[str, Any]) -> None:
        await self.scanner.on_lifecycle(kind, msg)

    async def on_lifecycle_gap(self) -> None:
        self.repo.add_event("lifecycle_gap")
        self._sweep_now.set()  # something may have been missed: sweep now

    async def on_connected(self) -> None:
        self.repo.add_event("ws_connected", connects=self.ws.stats.connects, watching=len(self.ws.desired))

    # ------------------------------------------------- discovery plumbing

    async def _on_new_market(self, nm: NewMarket) -> None:
        self.repo.add_event("new_market", ticker=nm.ticker, series=nm.series_ticker,
                            opened=nm.open_time.isoformat())
        await self.watchlist.add(nm)

    async def _watch(self, tickers: list[str]) -> None:
        await self.ws.add_markets(tickers)

    async def _unwatch(self, tickers: list[str]) -> None:
        # Phase 4: keep the feed for markets with an open position.
        self.recorder.forget(tickers)
        await self.ws.remove_markets(tickers)

    # ------------------------------------------------------------- loops

    async def _every(self, seconds: float, fn: Callable[[], Awaitable[Any]], wake: asyncio.Event | None = None,
                     *, run_first: bool = True) -> None:
        first = True
        while not self._stop.is_set():
            if run_first or not first:
                await fn()
            first = False
            if wake is None:
                await self.clock.sleep(seconds)
                continue
            try:
                await asyncio.wait_for(wake.wait(), seconds)
            except TimeoutError:
                pass
            wake.clear()

    async def _scan_once(self) -> None:
        await self.scanner.tick()
        await self.watchlist.expire()

    async def _record_once(self) -> None:
        self.recorder.record_due()
        self.recorder.flush_trades()

    async def _heartbeat_loop(self) -> None:
        n = 0
        while not self._stop.is_set():
            await self.clock.sleep(HEARTBEAT_SEC)
            n += 1
            stats = self.stats()
            log.info("heartbeat", **stats)
            if n % HEARTBEAT_EVENT_EVERY == 0:
                self.repo.add_event("heartbeat", **stats)

    def stats(self) -> dict[str, Any]:
        books = [self.books.get(t) for t in self.ws.desired]
        return {
            "watching": len(self.watchlist), "subscribed": len(self.ws.desired),
            "stale_books": sum(1 for b in books if b is not None and b.stale),
            "pending_open": len(self.scanner.pending),
            "ws": asdict(self.ws.stats), "scanner": asdict(self.scanner.stats),
            "recorder": asdict(self.recorder.stats), "task_failures": dict(self.task_failures),
            "skipped_capacity": self.watchlist.skipped_capacity,
        }

    async def _supervise(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - supervisor boundary
                self.task_failures[name] = self.task_failures.get(name, 0) + 1
                log.exception("task_failed", task=name, error=repr(e))
                self.repo.add_event("task_failed", task=name, error=repr(e))
                await self.alerts.alert(f"task:{name}", f"task {name} failed: {e!r}; restarting")
                await self.clock.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    # ---------------------------------------------------------- lifecycle

    async def start(self) -> None:
        log.info("bot_starting", mode=self.cfg.mode.value, env=self.cfg.environment.value,
                 writes_allowed=self.cfg.writes_allowed)
        self.repo.add_event("start", mode=self.cfg.mode.value, env=self.cfg.environment.value)
        await self.rest.configure_rate_limits(share=RATE_LIMIT_SHARE)
        await self.catalog.refresh()
        d, r = self.cfg.file.discovery, self.cfg.file.recording
        await self.scanner.sweep()  # catch anything created while we were down
        # Resume watches a restart interrupted, keeping their original expiry.
        resumed = 0
        for nm, emitted_at in self.scanner.recently_emitted(timedelta(minutes=d.watch_duration_min)):
            resumed += await self.watchlist.add(nm, added_at=emitted_at)
        if resumed:
            log.info("watches_resumed", markets=resumed)
            self.repo.add_event("watches_resumed", markets=resumed)
        await self.scanner.tick()
        specs: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("ws", self.ws.run),
            ("scan", lambda: self._every(d.scan_interval_sec, self._scan_once)),
            # start() already swept, so the loop waits one interval first.
            ("sweep", lambda: self._every(d.sweep_interval_min * 60, self.scanner.sweep, self._sweep_now,
                                          run_first=False)),
            ("record", lambda: self._every(0.5, self._record_once)),
            ("results", lambda: self._every(r.status_refresh_min * 60, self.results.refresh)),
            ("heartbeat", self._heartbeat_loop),
        ]
        self._tasks = [asyncio.create_task(self._supervise(n, f), name=n) for n, f in specs]
        log.info("bot_started", series=len(self.catalog.included), pending_open=len(self.scanner.pending))

    async def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        log.info("bot_stopping")
        await self.ws.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            self.recorder.record_due()
            self.recorder.flush_trades()
        finally:
            self.repo.add_event("stop", **self.stats())
            await self.rest.aclose()
            await self.alerts.aclose()
        log.info("bot_stopped")

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        stop_requested = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_requested.set)
        await self.start()
        try:
            await stop_requested.wait()
        finally:
            await self.stop()


def resolve_db_url(url: str, root: Path) -> str:
    """A relative SQLite path is relative to the project, not the shell's cwd."""
    prefix = "sqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        path = Path(url.removeprefix(prefix))
        if not path.is_absolute():
            return prefix + str((root / path).resolve())
    return url


def lock_path_for(db_url: str, root: Path) -> Path:
    prefix = "sqlite:///"
    if db_url.startswith(prefix) and ":memory:" not in db_url:
        return Path(db_url.removeprefix(prefix)).with_suffix(".lock")
    return root / "data" / "kalshi_bot.lock"
