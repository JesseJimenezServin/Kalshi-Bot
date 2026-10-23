"""Category study: trade-based measure, eligibility, and an end-to-end run on the fake API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import httpx
import pytest

from bot.analysis.category_study import eligible, format_study, measure_trades, study
from bot.api.models import Market, Trade
from bot.api.rest import KalshiRestClient, WriteForbidden
from bot.clock import FakeClock
from tests.fake_rest import FakeRest, market

OPEN = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
NOW = OPEN + timedelta(hours=10)


def tr(sec: int, price: str, n: str) -> Trade:
    return Trade(trade_id=f"t{sec}", ticker="T", yes_price_dollars=D(price), count_fp=D(n),
                 created_time=OPEN + timedelta(seconds=sec))


def test_measure_trades_consensus_and_early_mispricing() -> None:
    m = measure_trades("T", [tr(60, "0.30", "40"), tr(120, "0.58", "10"), tr(3000, "0.60", "30"),
                             tr(3600, "0.62", "10")], OPEN)
    assert m is not None
    assert m.consensus == (D(60) * 30 + D(62) * 10) / 40  # 60.5
    assert m.first_price == D(30) and m.first_trade_sec == 60
    assert m.max_dev == D("30.5") and m.contracts_ge20 == D(40)
    assert m.gap_dollars_ge20 == D("30.5") * 40 / 100


def test_measure_trades_needs_a_consensus() -> None:
    assert measure_trades("T", [], OPEN) is None
    assert measure_trades("T", [tr(60, "0.30", "1")], OPEN) is None, "nothing after minute 10"


def test_eligibility_window() -> None:
    base: dict[str, Any] = dict(ticker="T", event_ticker="E", status="active", volume_fp="500",
                                open_time=OPEN, close_time=OPEN + timedelta(days=1))
    assert eligible(Market(**base), NOW, D(100))
    assert not eligible(Market(**{**base, "close_time": OPEN + timedelta(minutes=30)}), NOW, D(100))
    assert not eligible(Market(**{**base, "volume_fp": "5"}), NOW, D(100))
    assert not eligible(Market(**base), OPEN + timedelta(hours=1), D(100)), "opened too recently"


async def test_unsigned_client_sends_no_auth_and_refuses_writes() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    rest = KalshiRestClient("https://external-api.kalshi.com/trade-api/v2", None, writes_allowed=True,
                            transport=httpx.MockTransport(handler), clock=FakeClock())
    await rest.get_markets()
    assert "KALSHI-ACCESS-KEY" not in seen[0].headers
    with pytest.raises(WriteForbidden, match="unsigned"):
        await rest._request("POST", "/portfolio/events/orders")


async def test_study_end_to_end_on_fake_api() -> None:
    fake = FakeRest()
    fake.series = [{"ticker": "KXPOL", "category": "Politics"}]  # no fee fields: must still parse
    fake.markets = [market("KXPOL-26OCT-A", "KXPOL-26OCT", open_time="2026-10-22T14:00:00Z",
                           close_time="2026-10-30T00:00:00Z", volume_fp="900")]
    trades = [{"trade_id": "a", "ticker": "KXPOL-26OCT-A", "yes_price_dollars": "0.0200", "count_fp": "40.00",
               "created_time": "2026-10-22T14:01:38Z"},
              {"trade_id": "b", "ticker": "KXPOL-26OCT-A", "yes_price_dollars": "0.2600", "count_fp": "20.00",
               "created_time": "2026-10-22T14:50:00Z"}]
    fake.routes["/markets/trades"] = lambda r: {"trades": trades, "cursor": ""}
    clock = FakeClock(NOW.timestamp())
    rest = KalshiRestClient("https://external-api.kalshi.com/trade-api/v2", None, writes_allowed=False,
                            transport=httpx.MockTransport(fake), clock=clock)
    res = await study(rest, ["Politics"], NOW)
    assert res[0].pool == 1 and len(res[0].measures) == 1
    m = res[0].measures[0]
    assert m.consensus == D(26) and m.contracts_ge20 == D(40)
    text = format_study(res)
    assert "Politics" in text and "KXPOL-26OCT-A" in text
    assert all(r.method == "GET" and "KALSHI-ACCESS-KEY" not in r.headers for r in fake.calls)
