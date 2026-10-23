"""WebSocket client against a fake Kalshi server on a real socket."""

from __future__ import annotations

import asyncio
import base64
import random
from collections.abc import Callable
from decimal import Decimal as D
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from bot.api.auth import Signer
from bot.api.ws import KalshiWebSocket
from bot.data.orderbook import OrderBookStore
from tests.fake_ws import FakeKalshiWS


class Recorder:
    def __init__(self) -> None:
        self.trades: list[dict[str, Any]] = []
        self.lifecycle: list[tuple[str, dict[str, Any]]] = []
        self.gaps = 0
        self.connects = 0

    async def on_trade(self, msg: dict[str, Any]) -> None:
        self.trades.append(msg)

    async def on_lifecycle(self, kind: str, msg: dict[str, Any]) -> None:
        self.lifecycle.append((kind, msg))

    async def on_lifecycle_gap(self) -> None:
        self.gaps += 1

    async def on_connected(self) -> None:
        self.connects += 1


async def until(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    async def _wait() -> None:
        while not pred():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(_wait(), timeout)


def snapshot(sid: int, seq: int, ticker: str, yes: list[list[str]], no: list[list[str]]) -> dict[str, Any]:
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "clock": seq, "yes_dollars_fp": yes, "no_dollars_fp": no}}


def delta(sid: int, seq: int, ticker: str, side: str, price: str, d: str) -> dict[str, Any]:
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "clock": seq, "price_dollars": price, "delta_fp": d, "side": side}}


@pytest.fixture
async def setup() -> Any:
    key = ed25519.Ed25519PrivateKey.generate()
    async with FakeKalshiWS() as server:
        rec = Recorder()
        books = OrderBookStore()
        client = KalshiWebSocket(server.url, Signer("kid", key), books, rec,
                                 backoff_base_sec=0.01, backoff_cap_sec=0.05, rng=random.Random(0))
        task = asyncio.create_task(client.run())
        await asyncio.wait_for(client.connected.wait(), 3)
        yield server, client, books, rec, key
        await client.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_handshake_is_signed_over_ws_path(setup: Any) -> None:
    server, _client, _books, _rec, key = setup
    h = {k.lower(): v for k, v in server.handshake_headers[0].items()}
    assert h["kalshi-access-key"] == "kid"
    text = f"{h['kalshi-access-timestamp']}GET/trade-api/ws/v2".encode()
    key.public_key().verify(base64.b64decode(h["kalshi-access-signature"]), text)


async def test_subscribes_lifecycle_only_until_markets_are_wanted(setup: Any) -> None:
    server, client, *_ = setup
    subs = server.cmds("subscribe")
    assert [c["params"]["channels"] for c in subs] == [["market_lifecycle_v2"]]
    assert "market_tickers" not in subs[0]["params"]


async def test_refuses_to_subscribe_ticker_channel_without_tickers(setup: Any) -> None:
    _server, client, *_ = setup
    with pytest.raises(ValueError, match="ALL markets"):
        await client._subscribe("orderbook_delta", [])


async def test_add_markets_subscribes_books_and_trades_then_applies_updates(setup: Any) -> None:
    server, client, books, rec, _ = setup
    await client.add_markets(["A", "B"])
    chans = {tuple(c["params"]["channels"]): c["params"].get("market_tickers") for c in server.cmds("subscribe")}
    assert chans[("orderbook_delta",)] == ["A", "B"] and chans[("trade",)] == ["A", "B"]
    assert books.get("A").stale  # until its snapshot

    sid = server.sid_for("orderbook_delta")
    await server.push(snapshot(sid, 1, "A", [["0.40", "10.00"]], [["0.55", "20.00"]]))
    await server.push(delta(sid, 2, "A", "yes", "0.41", "5.00"))
    await until(lambda: books.get("A").best_yes_bid() == (D("0.41"), D("5")))
    assert not books.get("A").stale and books.get("A").best_yes_ask() == (D("0.45"), D("20"))

    tsid = server.sid_for("trade")
    await server.push({"type": "trade", "sid": tsid, "seq": 1,
                       "msg": {"trade_id": "t1", "market_ticker": "A", "yes_price_dollars": "0.41"}})
    await until(lambda: len(rec.trades) == 1)


async def test_sequence_gap_marks_stale_and_requests_snapshot(setup: Any) -> None:
    server, client, books, _rec, _ = setup
    await client.add_markets(["A"])
    sid = server.sid_for("orderbook_delta")
    nxt = lambda: server.next_seq(sid)  # noqa: E731 - one counter, like the real server
    await server.push(snapshot(sid, nxt(), "A", [["0.40", "10.00"]], []))
    await until(lambda: not books.get("A").stale)

    for _ in range(3):
        nxt()  # three messages lost in transit
    await server.push(delta(sid, nxt(), "A", "yes", "0.40", "-10.00"))
    await until(lambda: any(c["params"].get("action") == "get_snapshot" for c in server.cmds("update_subscription")))
    assert books.get("A").stale, "nothing may trade on a book with a hole in it"
    assert books.get("A").best_yes_bid() == (D("0.40"), D("10")), "the out-of-order delta was dropped"
    assert client.stats.gaps == 1 and client.stats.resyncs == 1

    # The resync's ok reply consumed a seq; the snapshot that answers it is next.
    await until(lambda: server.seqs[sid] == 6)
    await server.push(snapshot(sid, nxt(), "A", [["0.39", "3.00"]], []))
    await until(lambda: not books.get("A").stale)
    assert books.get("A").yes_bids == {D("0.39"): D("3")}
    assert client.stats.gaps == 1, "the reply's seq is counted, so the snapshot is in order"


async def test_lifecycle_forwarded_and_gap_reported(setup: Any) -> None:
    server, _client, _books, rec, _ = setup
    sid = server.sid_for("market_lifecycle_v2")
    await server.push({"type": "market_lifecycle_v2", "sid": sid, "seq": 1,
                       "msg": {"event_type": "created", "market_ticker": "NEW-1", "open_ts": 1}})
    await server.push({"type": "event_lifecycle", "sid": sid, "seq": 2, "msg": {"event_ticker": "E"}})
    await server.push({"type": "market_lifecycle_v2", "sid": sid, "seq": 9, "msg": {"event_type": "settled"}})
    await until(lambda: len(rec.lifecycle) == 3 and rec.gaps == 1)
    assert rec.lifecycle[0][0] == "market_lifecycle_v2" and rec.lifecycle[1][0] == "event_lifecycle"


async def test_reconnect_resubscribes_and_books_are_stale_until_snapshot(setup: Any) -> None:
    server, client, books, rec, _ = setup
    await client.add_markets(["A"])
    sid = server.sid_for("orderbook_delta")
    await server.push(snapshot(sid, 1, "A", [["0.40", "10.00"]], []))
    await until(lambda: not books.get("A").stale)

    before = len(server.cmds("subscribe"))
    await server.drop()
    await until(lambda: books.get("A").stale)
    await until(lambda: client.connected.is_set() and len(server.cmds("subscribe")) >= before + 3)
    resub = server.cmds("subscribe")[before:]
    assert {tuple(c["params"]["channels"]) for c in resub} == {("market_lifecycle_v2",), ("orderbook_delta",), ("trade",)}
    assert all(c["params"].get("market_tickers") in (None, ["A"]) for c in resub)
    assert client.stats.connects == 2 and rec.connects == 2


async def test_add_while_disconnected_is_applied_on_reconnect(setup: Any) -> None:
    server, client, *_ = setup
    await server.drop()
    await until(lambda: not client.connected.is_set())
    await client.add_markets(["LATE"])
    await until(lambda: client.connected.is_set() and
                any(c["params"].get("market_tickers") == ["LATE"] for c in server.cmds("subscribe")))


async def test_remove_markets_deletes_then_unsubscribes(setup: Any) -> None:
    server, client, books, *_ = setup
    await client.add_markets(["A", "B"])
    await client.remove_markets(["A"])
    dels = [c for c in server.cmds("update_subscription") if c["params"]["action"] == "delete_markets"]
    assert dels and all(c["params"]["market_tickers"] == ["A"] for c in dels)
    assert books.get("A") is None
    await client.remove_markets(["B"])
    assert len(server.cmds("unsubscribe")) == 2  # book + trade subs now empty
    assert not any(s["channel"] in ("orderbook_delta", "trade") for s in server.subs.values())


async def test_market_limit_opens_another_subscription() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    async with FakeKalshiWS(market_limit=2) as server:
        books = OrderBookStore()
        client = KalshiWebSocket(server.url, Signer("kid", key), books, Recorder(),
                                 backoff_base_sec=0.01, rng=random.Random(0))
        task = asyncio.create_task(client.run())
        await asyncio.wait_for(client.connected.wait(), 3)
        await client.add_markets(["A", "B"])
        await client.add_markets(["C"])  # first sub is full -> error 26 -> new sub
        book_subs = [s for s in server.subs.values() if s["channel"] == "orderbook_delta"]
        assert sorted(sorted(s["tickers"]) for s in book_subs) == [["A", "B"], ["C"]]
        await client.stop()
        task.cancel()


async def test_command_replies_consume_seq_and_are_not_gaps(setup: Any) -> None:
    """Regression, found live 2026-10-23: Kalshi's `ok` reply to
    update_subscription carries the subscription's next seq. Ignoring it made
    the following delta look like a gap -> resync -> another ok -> another
    'gap', a loop that kept books stale (and unrecorded)."""
    server, client, books, _rec, _ = setup
    await client.add_markets(["A"])
    sid = server.sid_for("orderbook_delta")
    await server.push(snapshot(sid, server.next_seq(sid), "A", [["0.40", "10.00"]], []))
    await until(lambda: not books.get("A").stale)
    await client.add_markets(["B"])  # -> update_subscription -> ok carrying the next seq
    await server.push(delta(sid, server.next_seq(sid), "A", "yes", "0.41", "1.00"))
    await until(lambda: books.get("A").best_yes_bid() == (D("0.41"), D("1")))
    assert client.stats.gaps == 0 and client.stats.resyncs == 0
    assert not books.get("A").stale
