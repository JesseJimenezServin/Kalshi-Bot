"""A routing mock of the Kalshi REST API on httpx.MockTransport."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from bot.api.auth import Signer
from bot.api.rest import KalshiRestClient
from bot.clock import Clock

BASE = "https://external-api.kalshi.com/trade-api/v2"

Handler = Callable[[httpx.Request], Any]


def market(ticker: str, event: str, *, status: str = "active", open_time: str = "2026-10-22T14:00:00Z",
           close_time: str = "2026-10-24T05:00:00Z", result: str = "", **kw: Any) -> dict[str, Any]:
    return {"ticker": ticker, "event_ticker": event, "status": status, "open_time": open_time,
            "close_time": close_time, "result": result, "strike_type": "between", "floor_strike": 63,
            "cap_strike": 64, "yes_sub_title": "63-64", "rules_primary": "rules", "exchange_index": 0, **kw}


class FakeRest:
    def __init__(self) -> None:
        self.routes: dict[str, Handler] = {}
        self.calls: list[httpx.Request] = []
        self.series = [{"ticker": "KXHIGHPHIL", "category": "Climate and Weather", "fee_type": "quadratic",
                        "fee_multiplier": 1, "frequency": "daily"}]
        self.events: dict[str, dict[str, Any]] = {
            "KXHIGHPHIL-26OCT23": {"event_ticker": "KXHIGHPHIL-26OCT23", "series_ticker": "KXHIGHPHIL",
                                   "mutually_exclusive": True, "title": "High temp Philly"}}
        self.markets: list[dict[str, Any]] = []
        self.historical: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path.removeprefix("/trade-api/v2")
        if path in self.routes:
            return httpx.Response(200, json=self.routes[path](request))
        if path == "/account/limits":
            return httpx.Response(200, json={"usage_tier": "basic", "grants": [],
                                             "read": {"bucket_capacity": 600, "refill_rate": 200},
                                             "write": {"bucket_capacity": 100, "refill_rate": 100}})
        if path == "/account/endpoint_costs":
            return httpx.Response(200, json={"default_cost": 10, "endpoint_costs": []})
        if path == "/series":
            return httpx.Response(200, json={"series": self.series})
        if path.startswith("/events/"):
            ev = self.events.get(path.removeprefix("/events/"))
            if ev is None:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(200, json={"event": ev, "markets": []})
        if path == "/markets":
            p = request.url.params
            out = self.markets
            if "series_ticker" in p:
                out = [m for m in out if m["event_ticker"].split("-")[0] == p["series_ticker"]]
            if "tickers" in p:
                wanted = set(p["tickers"].split(","))
                out = [m for m in out if m["ticker"] in wanted]
            return httpx.Response(200, json={"markets": out, "cursor": ""})
        if path.startswith("/historical/markets/"):
            mk = self.historical.get(path.removeprefix("/historical/markets/"))
            if mk is None:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(200, json={"market": mk})
        return httpx.Response(404, json={"error": {"code": "unrouted", "message": path}})

    def client(self, clock: Clock) -> KalshiRestClient:
        signer = Signer("kid", ed25519.Ed25519PrivateKey.generate(), clock)
        return KalshiRestClient(BASE, signer, writes_allowed=False, clock=clock,
                                transport=httpx.MockTransport(self), rng=random.Random(0))

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix("/trade-api/v2") for r in self.calls]
