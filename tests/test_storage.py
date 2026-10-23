from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import StatementError

from bot.clock import FakeClock
from bot.storage.repo import Repository

T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository.from_url(f"sqlite:///{tmp_path / 'test.sqlite'}", clock=FakeClock(T0.timestamp()))


def test_all_spec_tables_exist(repo: Repository) -> None:
    tables = set(inspect(repo.engine).get_table_names())
    assert {"markets", "orderbook_snapshots", "fair_values", "signals", "orders",
            "fills", "positions", "daily_pnl"} <= tables


def test_sqlite_runs_in_wal_mode(repo: Repository) -> None:
    with repo.engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_database_directory_is_created(tmp_path: Path) -> None:
    Repository.from_url(f"sqlite:///{tmp_path / 'nested' / 'dir' / 'x.sqlite'}")
    assert (tmp_path / "nested" / "dir" / "x.sqlite").exists()


def _market(repo: Repository, **kw: object) -> None:
    base: dict[str, object] = dict(ticker="KXHIGHPHIL-26OCT23-B63.5", event_ticker="KXHIGHPHIL-26OCT23",
                                   series_ticker="KXHIGHPHIL", category="Climate and Weather", status="active",
                                   open_time=T0, close_time=T0 + timedelta(hours=39))
    base.update(kw)
    repo.upsert_market(**base)


def test_upsert_market_keeps_first_seen_and_updates_status(repo: Repository) -> None:
    _market(repo)
    first = repo.get_market("KXHIGHPHIL-26OCT23-B63.5")
    assert first is not None and first.first_seen_at == T0
    repo._clock.advance(3600)  # type: ignore[attr-defined]
    _market(repo, status="finalized", result="yes", first_seen_at=T0 + timedelta(days=9))
    row = repo.get_market("KXHIGHPHIL-26OCT23-B63.5")
    assert row is not None
    assert row.status == "finalized" and row.result == "yes"
    assert row.first_seen_at == T0, "first_seen_at must never move"
    assert repo.has_market("KXHIGHPHIL-26OCT23-B63.5") and not repo.has_market("OTHER")


def test_decimals_round_trip_exactly(repo: Repository) -> None:
    for v in [Decimal("0.000001"), Decimal("0.3"), Decimal("0.1") + Decimal("0.2"), Decimal("99.99"),
              Decimal("-12.345678"), Decimal("1234567.5")]:
        repo.add_fair_value("T", T0, "sibling", v, 0.9, {"v": str(v)})
    with repo.engine.connect() as c:
        raw = [r[0] for r in c.execute(text("SELECT cents FROM fair_values ORDER BY id"))]
    assert raw[0] == 1 and raw[2] == 300000  # stored as exact scaled integers
    from bot.storage.models import FairValueRow
    with repo.session() as s:
        got = [r.cents for r in s.query(FairValueRow).order_by(FairValueRow.id)]
    assert got[2] == Decimal("0.3") and got[0] == Decimal("0.000001") and got[4] == Decimal("-12.345678")


def test_more_than_six_decimals_is_refused_not_rounded(repo: Repository) -> None:
    with pytest.raises(StatementError, match="6 decimal places"):
        repo.add_fair_value("T", T0, "sibling", Decimal("0.0000001"), 0.9, {})


def test_timestamps_come_back_utc_aware_and_naive_is_refused(repo: Repository) -> None:
    est = timezone(timedelta(hours=-5))
    _market(repo, open_time=datetime(2026, 10, 22, 9, 0, tzinfo=est))
    row = repo.get_market("KXHIGHPHIL-26OCT23-B63.5")
    assert row is not None and row.open_time == T0 and row.open_time.tzinfo is UTC
    with pytest.raises(StatementError, match="naive"):
        _market(repo, ticker="NAIVE", open_time=datetime(2026, 10, 22, 14, 0))


def _order_row(repo: Repository, cid: str = "c1", status: str = "pending") -> int:
    row = repo.add_order(client_order_id=cid, ticker="T", side="bid", action="buy_yes", price=Decimal("0.42"),
                         count=Decimal("10"), filled_count=Decimal("0"), time_in_force="good_till_canceled",
                         expiration_time=None, status=status, mode="paper", exchange_index=0)
    return row.id


def test_orders_and_idempotent_fills(repo: Repository) -> None:
    oid = _order_row(repo)
    fill = dict(order_id=oid, trade_id="tr1", ticker="T", price=Decimal("0.42"), count=Decimal("2.5"),
                fee=Decimal("0.004375"), is_taker=True, mode="paper", ts=T0)
    assert repo.add_fill(**fill) is True
    assert repo.add_fill(**fill) is False, "reconciliation may see the same fill twice"
    repo.update_order("c1", status="resting", filled_count=Decimal("2.5"))
    o = repo.get_order("c1")
    assert o is not None and o.status == "resting" and o.filled_count == Decimal("2.5")
    assert [r.client_order_id for r in repo.open_orders("paper")] == ["c1"]
    assert repo.open_orders("live") == []


def test_client_order_id_is_unique(repo: Repository) -> None:
    _order_row(repo, "dup")
    with pytest.raises(Exception):
        _order_row(repo, "dup")


def test_positions_open_and_close(repo: Repository) -> None:
    p = repo.open_position(ticker="T", event_ticker="E", side="yes", count=Decimal("10"), avg_entry=Decimal("0.30"),
                           fair_at_entry=Decimal("55"), opened_at=T0, closed_at=None, avg_exit=None,
                           exit_reason=None, realized_pnl=None, fees=Decimal("0.0147"), mode="paper")
    assert [x.id for x in repo.open_positions("paper")] == [p.id]
    repo.update_position(p.id, closed_at=T0 + timedelta(hours=1), avg_exit=Decimal("0.50"),
                         exit_reason="take_profit", realized_pnl=Decimal("1.97"))
    assert repo.open_positions("paper") == []


def test_daily_pnl_upsert(repo: Repository) -> None:
    d = date(2026, 10, 22)
    repo.upsert_daily_pnl(d, "paper", Decimal("1.5"), Decimal("-0.25"), Decimal("0.1"))
    repo.upsert_daily_pnl(d, "paper", Decimal("2.0"), Decimal("0"), Decimal("0.2"))
    from bot.storage.models import DailyPnlRow
    with repo.session() as s:
        rows = s.query(DailyPnlRow).all()
    assert len(rows) == 1 and rows[0].realized == Decimal("2.0")


def test_snapshots_store_json(repo: Repository) -> None:
    repo.add_snapshot("T", T0, yes_bids=[["0.62", "100.00"]], yes_asks=[["0.63", "50.00"]])
    from bot.storage.models import OrderbookSnapshotRow
    with repo.session() as s:
        row = s.query(OrderbookSnapshotRow).one()
    assert row.yes_asks == [["0.63", "50.00"]]
