"""
Thin async Kalshi REST client (httpx). docs/API_NOTES.md sections 1-6, 11-12.

Every request goes through _request(), which in order:
  1. enforces the WRITE GUARD -- a non-GET request is refused outright unless
     the config allows writes (paper mode never does; see AppConfig.writes_allowed),
  2. waits on the client-side token bucket (read or write),
  3. signs, sends, and retries with exponential backoff.

Retry policy is deliberately asymmetric. Reads retry on 429, 5xx and transport
errors. Writes retry ONLY on 429, which the exchange guarantees was not
executed: a timeout or 5xx on order creation may mean the order WAS placed,
and blindly resending could double it. Those surface as errors for the caller
to reconcile (by client_order_id).
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from bot.api import models as m
from bot.api.auth import Signer
from bot.api.ratelimit import TokenBucket
from bot.clock import Clock, SystemClock
from bot.monitoring.log import get_logger
from bot.risk.approval import Approved, CancelRequest, OrderRequest

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_COST = 10.0
# Measured Basic tier, 2026-10-22 (API_NOTES section 3).
BASIC_READ = (200.0, 600.0)
BASIC_WRITE = (100.0, 100.0)


class KalshiAPIError(Exception):
    def __init__(self, status: int, method: str, path: str, body: Any) -> None:
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        err = body.get("error", body) if isinstance(body, dict) else body
        self.code = err.get("code") if isinstance(err, dict) else None
        self.message = err.get("message") if isinstance(err, dict) else str(err)
        super().__init__(f"{method} {path} -> HTTP {status}: {self.code or ''} {self.message or ''}".strip())


class WriteForbidden(PermissionError):
    """A write was attempted in a mode that does not allow writes."""


def _path_matches(pattern: str, path: str) -> bool:
    """Kalshi endpoint-cost patterns use :param and *rest segments."""
    p_parts, s_parts = pattern.strip("/").split("/"), path.strip("/").split("/")
    for i, p in enumerate(p_parts):
        if p.startswith("*"):
            return True
        if i >= len(s_parts):
            return False
        if not p.startswith(":") and p != s_parts[i]:
            return False
    return len(p_parts) == len(s_parts)


class KalshiRestClient:
    def __init__(
        self,
        base_url: str,
        signer: Signer,
        *,
        writes_allowed: bool,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 5,
        backoff_base_sec: float = 0.5,
        backoff_cap_sec: float = 8.0,
        rng: random.Random | None = None,
        timeout_sec: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._base_path = urlsplit(self._base_url).path  # "/trade-api/v2", signed with every path
        self._signer = signer
        self._writes_allowed = writes_allowed
        self._clock = clock or SystemClock()
        self._max_retries = max_retries
        self._backoff_base = backoff_base_sec
        self._backoff_cap = backoff_cap_sec
        self._rng = rng or random.Random()
        self.read_bucket = TokenBucket(*BASIC_READ, clock=self._clock)
        self.write_bucket = TokenBucket(*BASIC_WRITE, clock=self._clock)
        self._default_cost = DEFAULT_COST
        self._costs: list[tuple[str, str, float]] = []
        self._http = httpx.AsyncClient(base_url=self._base_url, transport=transport, timeout=timeout_sec)

    @property
    def writes_allowed(self) -> bool:
        return self._writes_allowed

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> KalshiRestClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- core

    def _cost(self, method: str, path: str) -> float:
        full = self._base_path + path
        for c_method, c_path, cost in self._costs:
            if c_method == method and _path_matches(c_path, full):
                return cost
        return self._default_cost

    def _backoff(self, attempt: int) -> float:
        delay = min(self._backoff_cap, self._backoff_base * 2**attempt)
        return delay * (0.5 + self._rng.random() / 2)  # jitter in [0.5x, 1x]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        method = method.upper()
        is_write = method != "GET"
        if is_write and not self._writes_allowed:
            # The write guard. Checked before anything else so a refused
            # request never consumes budget, never gets signed, never leaves.
            log.error("write_refused", method=method, path=path)
            raise WriteForbidden(f"{method} {path} refused: writes are disabled in this mode")

        params = {k: v for k, v in (params or {}).items() if v is not None}
        bucket = self.write_bucket if is_write else self.read_bucket
        cost = self._cost(method, path)

        attempt = 0
        while True:
            await bucket.acquire(cost)
            headers = self._signer.headers(method, self._base_path + path)
            try:
                resp = await self._http.request(method, path, params=params, json=json, headers=headers)
            except httpx.TransportError as e:
                if is_write or attempt >= self._max_retries:
                    raise
                delay = self._backoff(attempt)
                log.warning("transport_error_retry", method=method, path=path, error=str(e), delay=delay)
                await self._clock.sleep(delay)
                attempt += 1
                continue

            if resp.status_code < 400:
                return resp.json() if resp.content else None

            retryable = resp.status_code == 429 or (not is_write and resp.status_code in RETRYABLE_STATUS)
            if retryable and attempt < self._max_retries:
                delay = self._backoff(attempt)
                log.warning("http_retry", method=method, path=path, status=resp.status_code, delay=delay)
                await self._clock.sleep(delay)
                attempt += 1
                continue

            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text[:500]
            raise KalshiAPIError(resp.status_code, method, path, body)

    async def _get(self, path: str, model: type[T], **params: Any) -> T:
        return model.model_validate(await self._request("GET", path, params=params))

    async def _paginate(
        self, fetch: Callable[[str | None], Any], items: Callable[[Any], list[Any]]
    ) -> AsyncIterator[Any]:
        cursor: str | None = None
        while True:
            page = await fetch(cursor)
            for item in items(page):
                yield item
            cursor = page.cursor
            if not cursor:
                return

    # ------------------------------------------------------------- account

    async def get_account_limits(self) -> m.AccountLimits:
        return await self._get("/account/limits", m.AccountLimits)

    async def get_endpoint_costs(self) -> m.EndpointCosts:
        return await self._get("/account/endpoint_costs", m.EndpointCosts)

    async def configure_rate_limits(self, share: float = 1.0) -> m.AccountLimits:
        """Size the client buckets to this account's real tier and costs.

        Limits are per ACCOUNT (docs/API_NOTES.md section 3), so another
        program on the same account -- the liquidity lab -- spends from the
        same buckets. `share` keeps that fraction for this client and leaves
        the rest as headroom."""
        if not 0 < share <= 1:
            raise ValueError("share must be in (0, 1]")
        limits = await self.get_account_limits()
        costs = await self.get_endpoint_costs()
        self.read_bucket.reconfigure(limits.read.refill_rate * share, limits.read.bucket_capacity * share)
        self.write_bucket.reconfigure(limits.write.refill_rate * share, limits.write.bucket_capacity * share)
        self._default_cost = costs.default_cost
        self._costs = [(c.method.upper(), c.path, c.cost) for c in costs.endpoint_costs]
        log.info("rate_limits_configured", tier=limits.usage_tier, share=share,
                 read_rate=self.read_bucket.rate, write_rate=self.write_bucket.rate)
        return limits

    # --------------------------------------------------------- market data

    async def get_markets(
        self,
        status: str | None = None,
        cursor: str | None = None,
        *,
        limit: int = 1000,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        min_created_ts: int | None = None,
        max_created_ts: int | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        min_settled_ts: int | None = None,
        max_settled_ts: int | None = None,
        mve_filter: str | None = None,
        tickers: list[str] | None = None,
    ) -> m.MarketsPage:
        """status: unopened | open | closed | settled (the filter values, not
        Market.status). mve_filter="exclude" drops combo markets."""
        return await self._get(
            "/markets", m.MarketsPage, status=status, cursor=cursor, limit=limit,
            series_ticker=series_ticker, event_ticker=event_ticker,
            min_created_ts=min_created_ts, max_created_ts=max_created_ts,
            min_close_ts=min_close_ts, max_close_ts=max_close_ts,
            min_settled_ts=min_settled_ts, max_settled_ts=max_settled_ts, mve_filter=mve_filter,
            tickers=",".join(tickers) if tickers else None,
        )

    def iter_markets(self, **filters: Any) -> AsyncIterator[m.Market]:
        return self._paginate(lambda c: self.get_markets(cursor=c, **filters), lambda p: p.markets)

    async def get_market(self, ticker: str) -> m.Market:
        return (await self._get(f"/markets/{ticker}", m.MarketResponse)).market

    async def get_event(self, event_ticker: str) -> m.EventResponse:
        return await self._get(f"/events/{event_ticker}", m.EventResponse)

    async def get_series(self, series_ticker: str) -> m.Series:
        return (await self._get(f"/series/{series_ticker}", m.SeriesResponse)).series

    async def get_series_list(self, category: str | None = None) -> list[m.Series]:
        return (await self._get("/series", m.SeriesList, category=category)).series

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> m.OrderbookFp:
        return (await self._get(f"/markets/{ticker}/orderbook", m.OrderbookResponse, depth=depth)).orderbook_fp

    async def get_historical_cutoff(self) -> m.HistoricalCutoff:
        return await self._get("/historical/cutoff", m.HistoricalCutoff)

    async def get_historical_market(self, ticker: str) -> m.Market | None:
        """A market settled before the historical cutoff. None if unknown (404)."""
        try:
            return (await self._get(f"/historical/markets/{ticker}", m.MarketResponse)).market
        except KalshiAPIError as e:
            if e.status == 404:
                return None
            raise

    async def get_historical_markets(self, cursor: str | None = None, *, limit: int = 1000,
                                     series_ticker: str | None = None,
                                     event_ticker: str | None = None) -> m.MarketsPage:
        return await self._get("/historical/markets", m.MarketsPage, cursor=cursor, limit=limit,
                               series_ticker=series_ticker, event_ticker=event_ticker)

    def iter_historical_markets(self, **filters: Any) -> AsyncIterator[m.Market]:
        return self._paginate(lambda c: self.get_historical_markets(cursor=c, **filters), lambda p: p.markets)

    # ----------------------------------------------------------- portfolio

    async def get_balance(self) -> m.Balance:
        return await self._get("/portfolio/balance", m.Balance)

    async def get_positions(self, cursor: str | None = None, *, limit: int = 1000,
                            ticker: str | None = None, settlement_status: str | None = None) -> m.PositionsPage:
        return await self._get("/portfolio/positions", m.PositionsPage, cursor=cursor, limit=limit,
                               ticker=ticker, settlement_status=settlement_status)

    async def get_orders(self, cursor: str | None = None, *, limit: int = 1000,
                         ticker: str | None = None, status: str | None = None) -> m.OrdersPage:
        return await self._get("/portfolio/orders", m.OrdersPage, cursor=cursor, limit=limit,
                               ticker=ticker, status=status)

    async def get_fills(self, cursor: str | None = None, *, limit: int = 1000, ticker: str | None = None,
                        order_id: str | None = None, min_ts: int | None = None,
                        max_ts: int | None = None) -> m.FillsPage:
        return await self._get("/portfolio/fills", m.FillsPage, cursor=cursor, limit=limit, ticker=ticker,
                               order_id=order_id, min_ts=min_ts, max_ts=max_ts)

    # -------------------------------------------------------------- writes

    async def place_order(self, approval: Approved) -> m.CreateOrderResponse:
        req = approval.spend(self._clock.now())
        if not isinstance(req, OrderRequest):
            raise TypeError("place_order needs an approved OrderRequest")
        body = await self._request("POST", "/portfolio/events/orders", json=req.to_wire())
        return m.CreateOrderResponse.model_validate(body)

    async def cancel_order(self, approval: Approved) -> m.CancelOrderResponse:
        req = approval.spend(self._clock.now())
        if not isinstance(req, CancelRequest):
            raise TypeError("cancel_order needs an approved CancelRequest")
        body = await self._request(
            "DELETE", f"/portfolio/events/orders/{req.order_id}",
            params={"market_ticker": req.ticker, "exchange_index": req.exchange_index},
        )
        return m.CancelOrderResponse.model_validate(body)
