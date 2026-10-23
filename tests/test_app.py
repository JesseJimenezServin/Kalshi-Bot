"""End to end: the whole Phase 2 bot against fake REST and a fake WebSocket server."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from websockets.asyncio.client import connect

from bot.app import Bot, lock_path_for, resolve_db_url
from bot.clock import SystemClock
from bot.config import load_config
from bot.storage.models import BotEventRow, OrderbookSnapshotRow, TradeRow
from bot.storage.repo import Repository
from tests.fake_rest import FakeRest, market
from tests.fake_ws import FakeKalshiWS


async def until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    async def _w() -> None:
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_w(), timeout)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_bot_discovers_subscribes_records_and_stops(tmp_path: Path, config_data: dict[str, Any],
                                                          write_config: Any, env_file: Any) -> None:
    config_data["discovery"]["scan_interval_sec"] = 0.05
    cfg = load_config(write_config(config_data), env_file())
    clock = SystemClock()
    now = clock.utcnow()

    fake = FakeRest()
    fake.markets = [market("KXHIGHPHIL-26OCT23-B63.5", "KXHIGHPHIL-26OCT23", open_time=iso(now - timedelta(seconds=30)),
                           close_time=iso(now + timedelta(hours=30)))]
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'bot.sqlite'}", clock)

    async with FakeKalshiWS() as server:
        async def connector(url: str, **kw: Any) -> Any:
            # The bot dials the real wss:// URL with a TLS context; the fake is plain ws://.
            assert url.startswith("wss://") and "ssl" in kw
            kw.pop("ssl")
            return await connect(server.url, **kw)

        bot = Bot(cfg, clock=clock, rest=fake.client(clock), ws_connector=connector, repo=repo)
        await bot.start()
        try:
            # Startup sweep found the just-opened market and the feed subscribed to it.
            # Both ticker subscriptions: the bot creates them one after the other,
            # and on a slow host (the droplet) the trade one can lag behind.
            await until(lambda: {s["channel"] for s in server.subs.values()} >= {"orderbook_delta", "trade"})
            sid = server.sid_for("orderbook_delta")
            assert server.subs[sid]["tickers"] == {"KXHIGHPHIL-26OCT23-B63.5"}

            await server.push({"type": "orderbook_snapshot", "sid": sid, "seq": 1,
                               "msg": {"market_ticker": "KXHIGHPHIL-26OCT23-B63.5", "clock": 1,
                                       "yes_dollars_fp": [["0.62", "100.00"]], "no_dollars_fp": [["0.37", "80.00"]]}})
            tsid = server.sid_for("trade")
            await server.push({"type": "trade", "sid": tsid, "seq": 1,
                               "msg": {"trade_id": "tr-1", "market_ticker": "KXHIGHPHIL-26OCT23-B63.5",
                                       "yes_price_dollars": "0.6200", "count_fp": "5.00", "taker_side": "no",
                                       "ts": int(now.timestamp())}})

            def recorded() -> bool:
                with repo.session() as s:
                    return (s.scalars(select(OrderbookSnapshotRow)).first() is not None and
                            s.scalars(select(TradeRow)).first() is not None)
            await until(recorded)

            # A second market announced on the lifecycle channel, opening now.
            lsid = server.sid_for("market_lifecycle_v2")
            await server.push({"type": "market_lifecycle_v2", "sid": lsid, "seq": 1, "msg": {
                "event_type": "created", "market_ticker": "KXHIGHPHIL-26OCT23-B65.5", "exchange_index": 0,
                "open_ts": int(now.timestamp()), "close_ts": int((now + timedelta(hours=30)).timestamp()),
                "additional_metadata": {"event_ticker": "KXHIGHPHIL-26OCT23", "strike_type": "between"}}})
            await until(lambda: "KXHIGHPHIL-26OCT23-B65.5" in server.subs[sid]["tickers"])
            assert len(bot.watchlist) == 2
        finally:
            await bot.stop()

    with repo.session() as s:
        kinds = [e.kind for e in s.scalars(select(BotEventRow).order_by(BotEventRow.id))]
    assert kinds[0] == "start" and kinds[-1] == "stop"
    assert kinds.count("new_market") == 2 and "ws_connected" in kinds and "sweep" in kinds
    assert bot.task_failures == {}
    # Paper mode: not one write request reached the REST API.
    assert all(r.method == "GET" for r in fake.calls)


def test_relative_sqlite_path_resolves_against_project_root(tmp_path: Path) -> None:
    url = resolve_db_url("sqlite:///data/x.sqlite", tmp_path)
    assert url == f"sqlite:///{(tmp_path / 'data' / 'x.sqlite').resolve()}"
    assert resolve_db_url("sqlite:////abs/x.sqlite", tmp_path) == "sqlite:////abs/x.sqlite"
    assert lock_path_for(url, tmp_path).name == "x.lock"


async def test_failing_loop_is_restarted_and_recorded(tmp_path: Path, config_data: dict[str, Any],
                                                      write_config: Any, env_file: Any) -> None:
    cfg = load_config(write_config(config_data), env_file())
    clock = SystemClock()
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'f.sqlite'}", clock)
    bot = Bot(cfg, clock=clock, rest=FakeRest().client(clock), repo=repo)
    calls = {"n": 0}

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    await asyncio.wait_for(bot._supervise("flaky", flaky), 5)
    assert calls["n"] == 2 and bot.task_failures == {"flaky": 1}
    with repo.session() as s:
        ev = s.scalars(select(BotEventRow).where(BotEventRow.kind == "task_failed")).one()
    assert ev.detail["task"] == "flaky"
    await bot.rest.aclose()
