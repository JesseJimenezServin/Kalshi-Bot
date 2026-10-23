"""REST client against a mocked transport: signing, pagination, retries, the write guard, parsing."""

from __future__ import annotations

import base64
import json
import random
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from bot.api.auth import HEADER_SIGNATURE, HEADER_TIMESTAMP, Signer
from bot.api.rest import KalshiAPIError, KalshiRestClient, WriteForbidden, _path_matches
from bot.clock import FakeClock
from bot.risk.approval import CancelRequest, OrderRequest, _issue

BASE = "https://external-api.kalshi.com/trade-api/v2"

MARKET = {"ticker": "KXHIGHPHIL-26OCT23-B63.5", "event_ticker": "KXHIGHPHIL-26OCT23", "status": "active",
          "strike_type": "between", "floor_strike": 63, "cap_strike": 64, "result": "",
          "yes_bid_dollars": "0.6200", "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
          "open_time": "2026-10-22T14:00:00Z", "brand_new_field_kalshi_added": 1}


class Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make_client(handler: Callable[[httpx.Request], httpx.Response], *, writes_allowed: bool = False,
                key: ed25519.Ed25519PrivateKey | None = None) -> tuple[KalshiRestClient, Recorder, FakeClock]:
    clock = FakeClock()
    rec = Recorder(handler)
    signer = Signer("kid", key or ed25519.Ed25519PrivateKey.generate(), clock)
    client = KalshiRestClient(BASE, signer, writes_allowed=writes_allowed, clock=clock,
                              transport=httpx.MockTransport(rec), rng=random.Random(0))
    return client, rec, clock


def ok(body: Any) -> httpx.Response:
    return httpx.Response(200, json=body)


# ---------------------------------------------------------------- signing

async def test_requests_are_signed_over_prefixed_path_without_query(ed25519_key: ed25519.Ed25519PrivateKey) -> None:
    client, rec, _ = make_client(lambda r: ok({"markets": [], "cursor": ""}), key=ed25519_key)
    await client.get_markets(status="open", limit=5)
    req = rec.requests[0]
    assert req.url.path == "/trade-api/v2/markets"
    assert req.url.params["status"] == "open"
    text = f"{req.headers[HEADER_TIMESTAMP]}GET/trade-api/v2/markets".encode()
    ed25519_key.public_key().verify(base64.b64decode(req.headers[HEADER_SIGNATURE]), text)


async def test_none_params_are_not_sent() -> None:
    client, rec, _ = make_client(lambda r: ok({"markets": [], "cursor": ""}))
    await client.get_markets()
    assert "series_ticker" not in rec.requests[0].url.params


# ------------------------------------------------------------- pagination

async def test_iter_markets_follows_cursor_until_empty() -> None:
    pages = {None: {"markets": [MARKET], "cursor": "c2"},
             "c2": {"markets": [dict(MARKET, ticker="T2")], "cursor": ""}}
    client, rec, _ = make_client(lambda r: ok(pages[r.url.params.get("cursor")]))
    tickers = [mk.ticker async for mk in client.iter_markets(series_ticker="KXHIGHPHIL")]
    assert tickers == ["KXHIGHPHIL-26OCT23-B63.5", "T2"]
    assert len(rec.requests) == 2
    assert all(r.url.params["series_ticker"] == "KXHIGHPHIL" for r in rec.requests)


# ---------------------------------------------------------------- retries

async def test_get_retries_429_then_succeeds_with_backoff() -> None:
    responses = iter([httpx.Response(429, json={"error": "too many requests"}), ok({"market": MARKET})])
    client, rec, clock = make_client(lambda r: next(responses))
    t0 = clock.now()
    mk = await client.get_market("X")
    assert mk.ticker == MARKET["ticker"]
    assert len(rec.requests) == 2
    assert clock.now() > t0  # it backed off


async def test_get_retries_5xx_then_gives_up() -> None:
    client, rec, _ = make_client(lambda r: httpx.Response(503, json={"error": {"code": "service_unavailable"}}))
    with pytest.raises(KalshiAPIError) as e:
        await client.get_market("X")
    assert e.value.status == 503 and e.value.code == "service_unavailable"
    assert len(rec.requests) == 6  # 1 + max_retries


async def test_get_retries_transport_errors() -> None:
    calls = {"n": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom", request=r)
        return ok({"market": MARKET})

    client, _, _ = make_client(handler)
    assert (await client.get_market("X")).ticker == MARKET["ticker"]


async def test_4xx_is_not_retried() -> None:
    client, rec, _ = make_client(lambda r: httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}}))
    with pytest.raises(KalshiAPIError):
        await client.get_market("X")
    assert len(rec.requests) == 1


def _create_ok(r: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"order_id": "o1", "client_order_id": "c1", "fill_count": "0.00",
                                     "remaining_count": "10.00", "ts_ms": 1})


def _order(**kw: Any) -> OrderRequest:
    fields: dict[str, Any] = dict(ticker="T", intent="buy_yes", count=Decimal("10"), yes_price=Decimal("0.4"),
                                  client_order_id="c1")
    fields.update(kw)
    return OrderRequest(**fields)


async def test_writes_are_not_retried_on_5xx() -> None:
    """A 5xx on order creation may mean it WAS placed. Never resend blindly."""
    client, rec, clock = make_client(lambda r: httpx.Response(503, json={}), writes_allowed=True)
    with pytest.raises(KalshiAPIError):
        await client.place_order(_issue(_order(), now=clock.now()))
    assert len(rec.requests) == 1


async def test_writes_are_not_retried_on_transport_error() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=r)

    client, rec, clock = make_client(boom, writes_allowed=True)
    with pytest.raises(httpx.ReadTimeout):
        await client.place_order(_issue(_order(), now=clock.now()))
    assert len(rec.requests) == 1


async def test_writes_are_retried_on_429() -> None:
    """429 is guaranteed not executed, so it is safe to resend."""
    responses = iter([httpx.Response(429, json={}), _create_ok(None)])  # type: ignore[arg-type]
    client, rec, clock = make_client(lambda r: next(responses), writes_allowed=True)
    resp = await client.place_order(_issue(_order(), now=clock.now()))
    assert resp.order_id == "o1" and len(rec.requests) == 2


# ------------------------------------------------------------- write guard

@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH"])
async def test_write_guard_refuses_every_non_get_before_sending(method: str) -> None:
    client, rec, _ = make_client(_create_ok, writes_allowed=False)
    with pytest.raises(WriteForbidden):
        await client._request(method, "/portfolio/events/orders")
    assert rec.requests == [], "nothing may leave the process"
    assert client.write_bucket.available == client.write_bucket.capacity, "no budget consumed"


async def test_write_guard_blocks_place_and_cancel_even_with_valid_approval() -> None:
    client, rec, clock = make_client(_create_ok, writes_allowed=False)
    with pytest.raises(WriteForbidden):
        await client.place_order(_issue(_order(), now=clock.now()))
    with pytest.raises(WriteForbidden):
        await client.cancel_order(_issue(CancelRequest(order_id="o1", ticker="T"), now=clock.now()))
    assert rec.requests == []


async def test_reads_still_work_when_writes_are_forbidden() -> None:
    client, _, _ = make_client(lambda r: ok({"market": MARKET}), writes_allowed=False)
    assert (await client.get_market("X")).ticker == MARKET["ticker"]


# --------------------------------------------------- order gate on the client

async def test_place_order_sends_v2_body_and_consumes_approval() -> None:
    client, rec, clock = make_client(_create_ok, writes_allowed=True)
    approval = _issue(_order(intent="buy_no", yes_price=Decimal("0.63")), now=clock.now())
    resp = await client.place_order(approval)
    req = rec.requests[0]
    assert req.method == "POST" and req.url.path == "/trade-api/v2/portfolio/events/orders"
    body = json.loads(req.content)
    assert body["side"] == "ask" and body["price"] == "0.6300"
    assert resp.remaining_count == Decimal("10.00")
    with pytest.raises(PermissionError):  # single-use
        await client.place_order(approval)


async def test_cancel_routes_by_market_ticker() -> None:
    client, rec, clock = make_client(
        lambda r: ok({"order_id": "o1", "reduced_by": "10.00", "ts_ms": 1}), writes_allowed=True)
    await client.cancel_order(_issue(CancelRequest(order_id="o1", ticker="KXT", exchange_index=0), now=clock.now()))
    req = rec.requests[0]
    assert req.method == "DELETE" and req.url.path == "/trade-api/v2/portfolio/events/orders/o1"
    assert req.url.params["market_ticker"] == "KXT"


async def test_place_order_rejects_cancel_approval_and_vice_versa() -> None:
    client, _, clock = make_client(_create_ok, writes_allowed=True)
    with pytest.raises(TypeError):
        await client.place_order(_issue(CancelRequest(order_id="o", ticker="T"), now=clock.now()))
    with pytest.raises(TypeError):
        await client.cancel_order(_issue(_order(), now=clock.now()))


# ----------------------------------------------------------------- parsing

async def test_market_parses_decimals_blank_result_and_ignores_unknown_fields() -> None:
    client, _, _ = make_client(lambda r: ok({"market": MARKET}))
    mk = await client.get_market("X")
    assert mk.yes_bid_dollars == Decimal("0.62") and isinstance(mk.yes_bid_dollars, Decimal)
    assert mk.result is None
    assert mk.price_ranges[0].step == Decimal("0.01")
    assert mk.open_time is not None and mk.open_time.tzinfo is not None


async def test_orderbook_parses_fixed_point_levels() -> None:
    book = {"orderbook_fp": {"yes_dollars": [["0.0100", "200.00"], ["0.4200", "13.50"]],
                             "no_dollars": [["0.5600", "17.00"]]}}
    client, _, _ = make_client(lambda r: ok(book))
    ob = await client.get_orderbook("X")
    assert ob.yes_dollars[-1] == (Decimal("0.42"), Decimal("13.5"))  # best bid is last
    assert ob.no_dollars == [(Decimal("0.56"), Decimal("17"))]


async def test_balance_is_read_per_shard() -> None:
    body = {"balance": 1234, "balance_dollars": "12.34", "portfolio_value": 0, "updated_ts": 1,
            "balance_breakdown": [{"exchange_index": 0, "balance": "10.00"}, {"exchange_index": 2, "balance": "2.34"}]}
    client, _, _ = make_client(lambda r: ok(body))
    bal = await client.get_balance()
    assert bal.for_shard(0) == Decimal("10.00") and bal.for_shard(3) is None


async def test_configure_rate_limits_applies_tier_and_endpoint_costs() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/account/limits"):
            return ok({"usage_tier": "advanced", "grants": [], "read": {"bucket_capacity": 300, "refill_rate": 300},
                       "write": {"bucket_capacity": 900, "refill_rate": 300}})
        return ok({"default_cost": 10, "endpoint_costs": [
            {"method": "GET", "path": "/trade-api/v2/margin/balance", "cost": 5},
            {"method": "GET", "path": "/trade-api/v2/cfbenchmarks/*endpoint", "cost": 50},
            {"method": "DELETE", "path": "/trade-api/v2/communications/quotes/:quote_id", "cost": 2}]})

    client, _, _ = make_client(handler)
    limits = await client.configure_rate_limits()
    assert limits.usage_tier == "advanced"
    assert client.read_bucket.rate == 300 and client.write_bucket.capacity == 900
    assert client._cost("GET", "/margin/balance") == 5
    assert client._cost("GET", "/cfbenchmarks/a/b") == 50
    assert client._cost("DELETE", "/communications/quotes/q123") == 2
    assert client._cost("GET", "/markets") == 10


def test_cost_path_matching() -> None:
    assert _path_matches("/a/:id/c", "/a/42/c")
    assert not _path_matches("/a/:id/c", "/a/42/d")
    assert not _path_matches("/a/:id", "/a/42/c")
    assert _path_matches("/a/*rest", "/a/x/y/z")
