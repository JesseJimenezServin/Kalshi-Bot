"""
SQLAlchemy tables: everything needed to audit a decision and measure
calibration later. Postgres-portable -- nothing here is SQLite-specific.

Two column types matter:
  Fixed6       Decimal stored as BIGINT scaled by 10**6. SQLite has no exact
               decimal type (SQLAlchemy would store floats), and prices,
               fees and fractional counts must never round. 6 places covers
               Kalshi's finest precision ($0.000001 fees, 0.01 contracts).
               Still summable in SQL: SUM(col) / 1e6.
  UTCDateTime  always timezone-aware UTC on the way out. SQLite drops tzinfo.

Deviations from the spec's column lists, all additive and all from
docs/API_NOTES.md: book_side/intent on orders (V2 orders are bid/ask on
YES only, section 6); exchange_index (balances are per shard, section 11);
strike fields on markets (sibling model, section 9); inputs JSON on signals
(the spec asks for every decision with its inputs). Two extra tables:
`trades`, the raw public tape for watched markets -- the liquidity lab's
costliest gap was not persisting it (its DATA.md section 3) -- and
`bot_events`, operational events (reconnects, gaps, resyncs, sweeps,
errors) for the Phase 2 run report.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

SCALE = 10**6


class Fixed6(TypeDecorator[Decimal]):
    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: Decimal | int | str | None, dialect: Dialect) -> int | None:
        if value is None:
            return None
        d = Decimal(value) if not isinstance(value, Decimal) else value
        scaled = d * SCALE
        if scaled != scaled.to_integral_value():
            raise ValueError(f"{value} has more than 6 decimal places")
        return int(scaled)

    def process_result_value(self, value: int | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value) / SCALE


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: all timestamps must be timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {Decimal: Fixed6, datetime: UTCDateTime, dict[str, Any]: JSON, date: Date}


class MarketRow(Base):
    __tablename__ = "markets"

    ticker: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_ticker: Mapped[str] = mapped_column(String(128), index=True)
    series_ticker: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[str | None] = mapped_column(Text)
    open_time: Mapped[datetime | None]
    close_time: Mapped[datetime | None]
    first_seen_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[str | None] = mapped_column(String(8))
    strike_type: Mapped[str | None] = mapped_column(String(24))
    floor_strike: Mapped[float | None] = mapped_column(Float)
    cap_strike: Mapped[float | None] = mapped_column(Float)
    exchange_index: Mapped[int | None] = mapped_column(Integer)
    # When discovery declared this market new. Persisted so a restart never
    # announces the same market twice.
    emitted_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]


class OrderbookSnapshotRow(Base):
    __tablename__ = "orderbook_snapshots"
    __table_args__ = (Index("ix_snapshots_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128))
    ts: Mapped[datetime]
    # [[price_dollars, count], ...] as strings, best price first. yes_asks are
    # derived from NO bids (ask = 1 - no_bid) -- Kalshi sends bids only.
    yes_bids: Mapped[dict[str, Any]] = mapped_column(JSON)
    yes_asks: Mapped[dict[str, Any]] = mapped_column(JSON)


class FairValueRow(Base):
    __tablename__ = "fair_values"
    __table_args__ = (Index("ix_fair_values_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128))
    ts: Mapped[datetime]
    model: Mapped[str] = mapped_column(String(32))
    cents: Mapped[Decimal]
    confidence: Mapped[float] = mapped_column(Float)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)


class SignalRow(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128))
    ts: Mapped[datetime]
    side: Mapped[str] = mapped_column(String(8))  # "yes" | "no": which contract we would buy
    price: Mapped[Decimal]  # dollars, the executable entry price of that contract
    fair_cents: Mapped[Decimal]
    gap_after_fees: Mapped[Decimal]  # cents
    depth: Mapped[Decimal]
    decision: Mapped[str] = mapped_column(String(16))  # approved | rejected
    reject_reason: Mapped[str | None] = mapped_column(Text)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(4))  # V2 book side: bid | ask (YES leg)
    action: Mapped[str] = mapped_column(String(16))  # intent: buy_yes | sell_yes | buy_no | sell_no
    price: Mapped[Decimal]  # YES-leg dollars
    count: Mapped[Decimal]
    filled_count: Mapped[Decimal]
    time_in_force: Mapped[str] = mapped_column(String(24))
    expiration_time: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16))  # pending | resting | canceled | executed | rejected
    mode: Mapped[str] = mapped_column(String(8))
    exchange_index: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("trade_id", "mode", name="uq_fills_trade_mode"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    trade_id: Mapped[str] = mapped_column(String(64))  # exchange trade id, or a synthetic one in paper mode
    ticker: Mapped[str] = mapped_column(String(128), index=True)
    price: Mapped[Decimal]  # YES-leg dollars
    count: Mapped[Decimal]
    fee: Mapped[Decimal]  # dollars
    is_taker: Mapped[bool] = mapped_column(Boolean)
    mode: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime]


class PositionRow(Base):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_open", "mode", "closed_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128), index=True)
    event_ticker: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(4))  # yes | no: the contract held
    count: Mapped[Decimal]
    avg_entry: Mapped[Decimal]  # dollars, price of the contract held
    fair_at_entry: Mapped[Decimal]  # cents, fair value of the contract held
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    avg_exit: Mapped[Decimal | None]
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    realized_pnl: Mapped[Decimal | None]  # dollars, net of fees
    fees: Mapped[Decimal]
    mode: Mapped[str] = mapped_column(String(8))


class DailyPnlRow(Base):
    __tablename__ = "daily_pnl"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    realized: Mapped[Decimal]
    unrealized: Mapped[Decimal]
    fees: Mapped[Decimal]
    updated_at: Mapped[datetime]


class TradeRow(Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_ticker_ts", "ticker", "ts"),)

    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128))
    ts: Mapped[datetime]
    yes_price: Mapped[Decimal]
    count: Mapped[Decimal]
    taker_side: Mapped[str | None] = mapped_column(String(4))  # contract the taker bought: yes | no
    taker_book_side: Mapped[str | None] = mapped_column(String(4))  # bid | ask on the YES leg
    received_at: Mapped[datetime]


class BotEventRow(Base):
    __tablename__ = "bot_events"
    __table_args__ = (Index("ix_bot_events_kind_ts", "kind", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    ts: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
