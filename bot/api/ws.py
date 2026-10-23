"""
Kalshi WebSocket client (docs/API_NOTES.md sections 4, 8).

One authenticated connection carrying three kinds of subscription:
  market_lifecycle_v2   all markets (no ticker filter exists); discovery and
                        results. Also delivers event_lifecycle and
                        event_fee_update on the same sid.
  orderbook_delta       watched tickers only -> applied to the OrderBookStore
  trade                 watched tickers only -> raw tape, forwarded

The client owns the DESIRED set of watched tickers. Commands sent while
disconnected just update that set; every (re)connect resubscribes to it.
Ticker-scoped channels are only subscribed while at least one ticker is
wanted: subscribing without market_tickers would mean every market.

Integrity: each message carries `seq`, counting per subscription. A gap on a
book subscription marks its books stale (nothing may trade on a stale book)
and requests fresh snapshots via update_subscription/get_snapshot. A gap on
lifecycle triggers on_lifecycle_gap so discovery can sweep over REST.

Kalshi caps markets per subscription at an unpublished number (error 26).
When that happens the client opens another subscription for the rest.
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import certifi
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from bot.api.auth import Signer
from bot.clock import Clock, SystemClock
from bot.data.orderbook import OrderBookStore
from bot.monitoring.log import get_logger

log = get_logger(__name__)

ERR_MARKET_LIMIT = 26

# Verify TLS against certifi's CA bundle, the same one httpx uses for REST.
# The system default fails on python.org macOS builds, which ship without a
# CA store (found in the first live run, 2026-10-23), and pinning one bundle
# makes behaviour identical on every host.
_TLS = ssl.create_default_context(cafile=certifi.where())
COMMAND_TIMEOUT_SEC = 10.0
STABLE_CONNECTION_SEC = 60.0


class WsCommandError(Exception):
    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        super().__init__(f"ws command error {code}: {message}")


class WsHandlers(Protocol):
    async def on_trade(self, msg: dict[str, Any]) -> None: ...
    async def on_lifecycle(self, kind: str, msg: dict[str, Any]) -> None: ...
    async def on_lifecycle_gap(self) -> None: ...
    async def on_connected(self) -> None: ...


@dataclass
class _Sub:
    channel: str
    sid: int
    tickers: set[str] = field(default_factory=set)
    next_seq: int | None = None


@dataclass
class WsStats:
    connects: int = 0
    disconnects: int = 0
    gaps: int = 0
    resyncs: int = 0
    messages: int = 0
    errors: int = 0


Connector = Callable[..., Awaitable[ClientConnection]]


def _levels(raw: list[list[str]] | None) -> list[tuple[Decimal, Decimal]]:
    return [(Decimal(p), Decimal(s)) for p, s in raw or []]


class KalshiWebSocket:
    def __init__(
        self,
        url: str,
        signer: Signer,
        books: OrderBookStore,
        handlers: WsHandlers,
        *,
        clock: Clock | None = None,
        connector: Connector | None = None,
        rng: random.Random | None = None,
        backoff_base_sec: float = 1.0,
        backoff_cap_sec: float = 60.0,
        subscribe_lifecycle: bool = True,
    ) -> None:
        self._url = url
        self._signer = signer
        self.books = books
        self._handlers = handlers
        self._clock = clock or SystemClock()
        self._connector: Connector = connector or connect
        self._rng = rng or random.Random()
        self._backoff_base = backoff_base_sec
        self._backoff_cap = backoff_cap_sec
        self._subscribe_lifecycle = subscribe_lifecycle

        self.desired: set[str] = set()
        self._subs: dict[int, _Sub] = {}
        self._ws: ClientConnection | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._cmd_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self.stats = WsStats()
        # The reader must never await a command reply -- only the reader can
        # deliver it, so that would deadlock. Handler calls (which may add
        # markets) go through this queue to a separate worker; resyncs run as
        # their own tasks.
        self._events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._bg: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ public

    async def add_markets(self, tickers: list[str]) -> None:
        new = [t for t in tickers if t not in self.desired]
        self.desired.update(new)
        for t in new:
            self.books.ensure(t)
        if new and self.connected.is_set():
            await self._subscribe_tickers(new)

    async def remove_markets(self, tickers: list[str]) -> None:
        gone = [t for t in tickers if t in self.desired]
        self.desired.difference_update(gone)
        for t in gone:
            self.books.drop(t)
        if gone and self.connected.is_set():
            await self._unsubscribe_tickers(gone)

    async def run(self) -> None:
        """Connect, serve, reconnect with backoff -- until stop()."""
        attempt = 0
        while not self._stop.is_set():
            started = self._clock.now()
            try:
                await self._serve_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure means reconnect
                self.stats.errors += 1
                log.warning("ws_disconnected", error=repr(e))
            finally:
                self._on_disconnect()
            if self._stop.is_set():
                return
            if self._clock.now() - started >= STABLE_CONNECTION_SEC:
                attempt = 0
            delay = min(self._backoff_cap, self._backoff_base * 2**attempt) * (0.5 + self._rng.random() / 2)
            attempt += 1
            log.info("ws_reconnecting", delay=round(delay, 2), attempt=attempt)
            await self._clock.sleep(delay)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    # -------------------------------------------------------- connection

    async def _serve_once(self) -> None:
        headers = self._signer.headers("GET", self._url)
        kwargs: dict[str, Any] = {"additional_headers": headers, "open_timeout": 15}
        if self._url.startswith("wss://"):
            kwargs["ssl"] = _TLS
        ws = await self._connector(self._url, **kwargs)
        self._ws = ws
        self.stats.connects += 1
        log.info("ws_connected", url=self._url, watching=len(self.desired))
        reader = asyncio.create_task(self._read_loop(ws))
        worker = asyncio.create_task(self._event_worker())
        try:
            if self._subscribe_lifecycle:
                await self._subscribe("market_lifecycle_v2", None)
            if self.desired:
                await self._subscribe_tickers(sorted(self.desired))
            self.connected.set()
            await self._handlers.on_connected()
            await reader  # returns/raises when the connection ends
        finally:
            for t in (reader, worker, *self._bg):
                if not t.done():
                    t.cancel()
            self._bg.clear()

    def _on_disconnect(self) -> None:
        if self._ws is not None:
            self.stats.disconnects += 1
        self._ws = None
        self.connected.clear()
        self._subs.clear()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("websocket closed"))
                # The waiter may already be gone (cancelled during shutdown);
                # retrieve the exception so asyncio doesn't report it as lost.
                fut.add_done_callback(lambda f: f.exception())
        self._pending.clear()
        # Every book is unverifiable until its resubscription snapshot arrives.
        self.books.mark_stale()

    def _spawn(self, coro: Awaitable[None]) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg_done)

    def _bg_done(self, task: asyncio.Task[None]) -> None:
        self._bg.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.stats.errors += 1
            log.error("ws_background_task_failed", error=repr(task.exception()))

    async def _event_worker(self) -> None:
        while True:
            kind, msg = await self._events.get()
            try:
                if kind == "trade":
                    await self._handlers.on_trade(msg)
                elif kind == "lifecycle_gap":
                    await self._handlers.on_lifecycle_gap()
                else:
                    await self._handlers.on_lifecycle(kind, msg)
            except Exception as e:  # noqa: BLE001 - a handler bug must not kill the feed
                self.stats.errors += 1
                log.exception("ws_handler_failed", kind=kind, error=repr(e))

    async def _read_loop(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                await self._dispatch(json.loads(raw))
        except ConnectionClosed as e:
            raise ConnectionError(f"closed: {e}") from e

    # ----------------------------------------------------------- commands

    async def _command(self, cmd: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._ws is None:
            raise ConnectionError("not connected")
        async with self._cmd_lock:
            cid = self._next_id
            self._next_id += 1
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[cid] = fut
            await self._ws.send(json.dumps({"id": cid, "cmd": cmd, "params": params}))
        try:
            reply = await asyncio.wait_for(fut, COMMAND_TIMEOUT_SEC)
        finally:
            self._pending.pop(cid, None)
        if reply.get("type") == "error":
            m = reply.get("msg") or {}
            raise WsCommandError(m.get("code"), str(m.get("msg")))
        return reply

    async def _subscribe(self, channel: str, tickers: list[str] | None) -> _Sub:
        params: dict[str, Any] = {"channels": [channel]}
        if tickers is not None:
            if not tickers:
                raise ValueError("refusing to subscribe a ticker channel with no tickers (would mean ALL markets)")
            params["market_tickers"] = tickers
        reply = await self._command("subscribe", params)
        sid = int(reply["msg"]["sid"])
        sub = _Sub(channel, sid, set(tickers or []))
        self._subs[sid] = sub
        log.info("ws_subscribed", channel=channel, sid=sid, tickers=len(sub.tickers))
        return sub

    def _ticker_subs(self, channel: str) -> list[_Sub]:
        return [s for s in self._subs.values() if s.channel == channel]

    async def _subscribe_tickers(self, tickers: list[str]) -> None:
        for channel in ("orderbook_delta", "trade"):
            remaining = [t for t in tickers if not any(t in s.tickers for s in self._ticker_subs(channel))]
            while remaining:
                subs = self._ticker_subs(channel)
                try:
                    if subs:
                        target = subs[-1]
                        await self._command("update_subscription",
                                            {"sids": [target.sid], "market_tickers": remaining,
                                             "action": "add_markets"})
                        target.tickers.update(remaining)
                    else:
                        await self._subscribe(channel, remaining)
                    remaining = []
                except WsCommandError as e:
                    if e.code != ERR_MARKET_LIMIT or not subs:
                        raise
                    log.warning("ws_market_limit", channel=channel, sid=subs[-1].sid,
                                per_sub=len(subs[-1].tickers), adding=len(remaining))
                    await self._subscribe(channel, remaining)
                    remaining = []

    async def _unsubscribe_tickers(self, tickers: list[str]) -> None:
        drop = set(tickers)
        for sub in list(self._subs.values()):
            if sub.channel not in ("orderbook_delta", "trade"):
                continue
            hit = sorted(sub.tickers & drop)
            if not hit:
                continue
            if hit == sorted(sub.tickers):
                await self._command("unsubscribe", {"sids": [sub.sid]})
                self._subs.pop(sub.sid, None)
            else:
                await self._command("update_subscription",
                                    {"sids": [sub.sid], "market_tickers": hit, "action": "delete_markets"})
                sub.tickers.difference_update(hit)

    async def _resync(self, sub: _Sub) -> None:
        # The books were already marked stale, synchronously, when the gap was
        # seen. Marking them again here could re-stale a snapshot that landed
        # in between (a flaky test exposed it, 2026-10-23).
        self.stats.resyncs += 1
        tickers = sorted(sub.tickers)
        log.warning("ws_resync", sid=sub.sid, tickers=len(tickers))
        await self._command("update_subscription",
                            {"sids": [sub.sid], "market_tickers": tickers, "action": "get_snapshot"})

    # ----------------------------------------------------------- dispatch

    def _check_seq(self, sub: _Sub, seq: int | None) -> bool:
        """True if in order. Snapshots reset the expectation."""
        if seq is None:
            return True
        ok = sub.next_seq is None or seq == sub.next_seq
        sub.next_seq = seq + 1
        return ok

    async def _dispatch(self, data: dict[str, Any]) -> None:
        self.stats.messages += 1
        kind = data.get("type")
        cid = data.get("id")
        if cid is not None and cid in self._pending and kind in ("subscribed", "ok", "unsubscribed", "error"):
            # A reply can carry a seq on its subscription (Kalshi's `ok` does).
            # It is part of that subscription's sequence: skipping it made the
            # next message look like a gap (found live, 2026-10-23).
            reply_sub = self._subs.get(data.get("sid")) if data.get("sid") is not None else None
            if reply_sub is not None and data.get("seq") is not None:
                reply_sub.next_seq = int(data["seq"]) + 1
            fut = self._pending[cid]
            if not fut.done():
                fut.set_result(data)
            return
        if kind == "error":
            self.stats.errors += 1
            log.error("ws_error", msg=data.get("msg"))
            return

        sid = data.get("sid")
        sub = self._subs.get(sid) if sid is not None else None
        msg = data.get("msg") or {}
        now = self._clock.now()

        if sub is not None and not self._check_seq(sub, data.get("seq")):
            self.stats.gaps += 1
            log.warning("ws_seq_gap", channel=sub.channel, sid=sub.sid, got=data.get("seq"))
            if sub.channel == "orderbook_delta" and kind != "orderbook_snapshot":
                self.books.mark_stale(sorted(sub.tickers))  # immediately, before the resync round-trip
                self._spawn(self._resync(sub))
                return  # drop the out-of-order delta; the snapshot will cover it
            if sub.channel == "market_lifecycle_v2":
                self._events.put_nowait(("lifecycle_gap", {}))

        if kind == "orderbook_snapshot":
            ticker = msg.get("market_ticker")
            if ticker in self.desired:
                self.books.ensure(ticker).apply_snapshot(
                    _levels(msg.get("yes_dollars_fp")), _levels(msg.get("no_dollars_fp")), msg.get("clock"), now)
        elif kind == "orderbook_delta":
            ticker = msg.get("market_ticker")
            book = self.books.get(ticker) if ticker in self.desired else None
            if book is not None and not book.stale:
                book.apply_delta(msg["side"], Decimal(msg["price_dollars"]), Decimal(msg["delta_fp"]),
                                 msg.get("clock"), now)
        elif kind == "trade":
            self._events.put_nowait(("trade", msg))
        elif kind in ("market_lifecycle_v2", "event_lifecycle", "event_fee_update"):
            self._events.put_nowait((kind, msg))
