"""Recorder, alerts and the singleton lock."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from bot.clock import FakeClock
from bot.config import ConfigError
from bot.data.orderbook import OrderBookStore
from bot.data.recorder import Recorder
from bot.monitoring.alerts import THROTTLE_SEC, Alerter
from bot.singleton_lock import AlreadyRunning, SingletonLock
from bot.storage.models import OrderbookSnapshotRow, TradeRow
from bot.storage.repo import Repository

T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC).timestamp()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Recorder, OrderBookStore, Repository, FakeClock]:
    clock = FakeClock(T0)
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'r.sqlite'}", clock)
    books = OrderBookStore()
    rec = Recorder(books, repo, clock, tickers=lambda: books.tickers(), min_interval_sec=2, heartbeat_sec=30,
                   depth_levels=10)
    return rec, books, repo, clock


def snaps(repo: Repository) -> list[OrderbookSnapshotRow]:
    with repo.session() as s:
        return list(s.query(OrderbookSnapshotRow).order_by(OrderbookSnapshotRow.id))


def test_records_on_change_throttled_and_on_heartbeat(setup: tuple[Recorder, OrderBookStore, Repository, FakeClock]) -> None:
    rec, books, repo, clock = setup
    b = books.ensure("A")
    b.apply_snapshot(yes=[(D("0.40"), D("10"))], no=[(D("0.55"), D("5"))], clock=1, now=clock.now())
    assert rec.record_due() == 1, "first sight is always recorded"
    b.apply_delta("yes", D("0.41"), D("1"), clock=2, now=clock.now())
    clock.advance(1)
    assert rec.record_due() == 0, "changed, but inside min_interval"
    clock.advance(1)
    assert rec.record_due() == 1, "changed and min_interval elapsed"
    clock.advance(10)
    assert rec.record_due() == 0, "unchanged and inside heartbeat"
    clock.advance(20)
    assert rec.record_due() == 1, "heartbeat: a quiet book is still recorded"
    rows = snaps(repo)
    assert rows[0].yes_bids == [["0.40", "10"]] and rows[0].yes_asks == [["0.45", "5"]]
    assert rows[1].yes_bids[0] == ["0.41", "1"]


def test_stale_books_are_never_recorded(setup: tuple[Recorder, OrderBookStore, Repository, FakeClock]) -> None:
    rec, books, repo, _clock = setup
    books.ensure("A")  # never snapshotted -> stale
    assert rec.record_due() == 0 and rec.stats.skipped_stale == 1 and snaps(repo) == []


def test_batch_of_many_markets_is_one_pass(setup: tuple[Recorder, OrderBookStore, Repository, FakeClock]) -> None:
    rec, books, repo, clock = setup
    for i in range(300):  # the 14:00 UTC burst
        books.ensure(f"M{i}").apply_snapshot(yes=[(D("0.5"), D("1"))], no=[], clock=1, now=clock.now())
    assert rec.record_due() == 300 and len(snaps(repo)) == 300


def test_trades_recorded_idempotently(setup: tuple[Recorder, OrderBookStore, Repository, FakeClock]) -> None:
    rec, _books, repo, _clock = setup
    msg = {"trade_id": "t1", "market_ticker": "A", "yes_price_dollars": "0.3600", "count_fp": "136.00",
           "taker_side": "no", "taker_outcome_side": "no", "taker_book_side": "ask", "ts": 1669149841,
           "ts_ms": 1669149841123}
    rec.on_trade(msg)
    rec.on_trade(msg)  # replay after resubscribe
    rec.on_trade({"trade_id": "bad"})  # unparseable, logged and skipped
    assert rec.flush_trades() == 1
    rec.on_trade(msg)
    assert rec.flush_trades() == 0
    with repo.session() as s:
        t = s.query(TradeRow).one()
    assert t.yes_price == D("0.36") and t.count == D("136") and t.taker_side == "no"
    assert t.ts == datetime.fromtimestamp(1669149841.123, UTC)


# ------------------------------------------------------------------ alerts

async def test_alerts_are_throttled_per_key() -> None:
    clock = FakeClock(T0)
    a = Alerter("none", clock)
    assert await a.alert("task:ws", "down") is True
    assert await a.alert("task:ws", "down") is False
    assert await a.alert("task:scan", "other key") is True
    clock.advance(THROTTLE_SEC)
    assert await a.alert("task:ws", "down again") is True
    assert a.sent == 3


async def test_telegram_delivery_reports_suppressed_repeats() -> None:
    sent: list[dict[str, object]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        sent.append({"url": str(r.url), **json.loads(r.content)})
        return httpx.Response(200, json={"ok": True})

    clock = FakeClock(T0)
    a = Alerter("telegram", clock, telegram_token="TOKEN", telegram_chat_id="42",
                transport=httpx.MockTransport(handler))
    await a.alert("k", "first")
    await a.alert("k", "repeat")
    clock.advance(THROTTLE_SEC)
    await a.alert("k", "third")
    await a.aclose()
    assert len(sent) == 2 and sent[0]["chat_id"] == "42" and "botTOKEN/sendMessage" in str(sent[0]["url"])
    assert "(+1 repeats)" in str(sent[1]["text"])


def test_unimplemented_or_unconfigured_channels_refuse_to_start() -> None:
    for ch in ("discord", "email"):
        with pytest.raises(ConfigError):
            Alerter(ch, FakeClock())
    with pytest.raises(ConfigError):
        Alerter("telegram", FakeClock())


# ---------------------------------------------------------- singleton lock

def test_second_instance_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    with SingletonLock(path):
        with pytest.raises(AlreadyRunning):
            SingletonLock(path).acquire()
    with SingletonLock(path):  # released on exit
        pass



def test_disk_guard_pauses_snapshots_with_hysteresis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil as _sh
    from collections import namedtuple

    clock = FakeClock(T0)
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'g.sqlite'}", clock)
    books = OrderBookStore()
    books.ensure("A").apply_snapshot(yes=[(D("0.5"), D("1"))], no=[], clock=1, now=clock.now())
    free = {"gb": 10.0}
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(_sh, "disk_usage", lambda p: Usage(0, 0, int(free["gb"] * 1e9)))
    alerts: list[float] = []
    rec = Recorder(books, repo, clock, tickers=books.tickers, min_interval_sec=0.1, heartbeat_sec=0.1,
                   depth_levels=5, disk_path=tmp_path, min_free_gb=4, on_low_disk=alerts.append)
    assert rec.record_due() == 1
    free["gb"] = 3.9
    clock.advance(31)
    assert rec.record_due() == 0 and rec.stats.paused_low_disk and alerts == [3.9]
    free["gb"] = 4.2  # above 4 but inside the 0.5 GB hysteresis: still paused
    clock.advance(31)
    assert rec.record_due() == 0
    free["gb"] = 4.6
    clock.advance(31)
    assert rec.record_due() == 1 and not rec.stats.paused_low_disk
    assert alerts == [3.9], "alerted once per episode"
