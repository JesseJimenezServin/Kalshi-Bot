"""
Data access. The only module that opens sessions.

SQLite runs in WAL mode with a busy timeout: the market maker lost data to
"database is locked" until it switched (its 2026-10-04 notes), because a
reader such as a backup or report blocked the writer.

Phase 1 covers the writes every later phase needs plus the reads restart
recovery needs. Later phases add queries here rather than opening sessions
elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from bot.clock import Clock, SystemClock
from bot.storage.models import (
    Base,
    BotEventRow,
    DailyPnlRow,
    FairValueRow,
    FillRow,
    MarketRow,
    OrderbookSnapshotRow,
    OrderRow,
    PositionRow,
    SignalRow,
    TradeRow,
)


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


class Repository:
    def __init__(self, engine: Engine, clock: Clock | None = None) -> None:
        self.engine = engine
        self._clock = clock or SystemClock()
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, database_url: str, clock: Clock | None = None) -> Repository:
        repo = cls(make_engine(database_url), clock)
        repo.create_schema()
        return repo

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._sessions() as s, s.begin():
            yield s

    # ------------------------------------------------------------ markets

    def has_market(self, ticker: str) -> bool:
        with self.session() as s:
            return s.get(MarketRow, ticker) is not None

    def get_market(self, ticker: str) -> MarketRow | None:
        with self.session() as s:
            return s.get(MarketRow, ticker)

    def upsert_market(self, **fields: Any) -> MarketRow:
        """Insert, or update the mutable fields. first_seen_at never changes."""
        now = self._clock.utcnow()
        with self.session() as s:
            row = s.get(MarketRow, fields["ticker"])
            if row is None:
                row = MarketRow(first_seen_at=fields.pop("first_seen_at", now), updated_at=now, **fields)
                s.add(row)
            else:
                fields.pop("first_seen_at", None)
                for k, v in fields.items():
                    setattr(row, k, v)
                row.updated_at = now
            return row

    # ------------------------------------------------- observations / logs

    def add_snapshot(self, ticker: str, ts: datetime, yes_bids: list[list[str]], yes_asks: list[list[str]]) -> None:
        with self.session() as s:
            s.add(OrderbookSnapshotRow(ticker=ticker, ts=ts, yes_bids=yes_bids, yes_asks=yes_asks))

    def add_snapshots(self, rows: list[dict[str, Any]]) -> None:
        """Batch insert, one transaction: the 14:00 UTC open burst is ~290 markets."""
        if not rows:
            return
        with self.session() as s:
            s.add_all([OrderbookSnapshotRow(**r) for r in rows])

    def add_trades(self, rows: list[dict[str, Any]]) -> int:
        """Batch insert, idempotent on trade_id. Returns how many were new."""
        if not rows:
            return 0
        now = self._clock.utcnow()
        with self.session() as s:
            ids = [r["trade_id"] for r in rows]
            existing = set(s.scalars(select(TradeRow.trade_id).where(TradeRow.trade_id.in_(ids))))
            fresh: dict[str, dict[str, Any]] = {}
            for r in rows:
                if r["trade_id"] not in existing:
                    fresh.setdefault(r["trade_id"], r)
            s.add_all([TradeRow(received_at=now, **r) for r in fresh.values()])
            return len(fresh)

    def add_fair_value(self, ticker: str, ts: datetime, model: str, cents: Decimal,
                       confidence: float, inputs: dict[str, Any]) -> None:
        with self.session() as s:
            s.add(FairValueRow(ticker=ticker, ts=ts, model=model, cents=cents, confidence=confidence, inputs=inputs))

    def add_signal(self, **fields: Any) -> None:
        with self.session() as s:
            s.add(SignalRow(**fields))

    def add_trade(self, **fields: Any) -> bool:
        """Idempotent on trade_id (a resubscription can replay a trade)."""
        with self.session() as s:
            if s.get(TradeRow, fields["trade_id"]) is not None:
                return False
            s.add(TradeRow(received_at=self._clock.utcnow(), **fields))
            return True

    def add_event(self, kind: str, **detail: Any) -> None:
        with self.session() as s:
            s.add(BotEventRow(ts=self._clock.utcnow(), kind=kind, detail=detail))

    def unresolved_markets(self, limit: int = 1000) -> list[MarketRow]:
        """Markets we recorded that have no result yet (status/result updater)."""
        with self.session() as s:
            q = (select(MarketRow).where(MarketRow.result.is_(None), MarketRow.status != "finalized")
                 .order_by(MarketRow.close_time).limit(limit))
            return list(s.scalars(q))

    def count(self, model: type[Any], since: datetime | None = None, ts_col: str = "ts") -> int:
        with self.session() as s:
            q = select(func.count()).select_from(model)
            if since is not None:
                q = q.where(getattr(model, ts_col) >= since)
            return int(s.scalar(q) or 0)

    # ------------------------------------------------------------- orders

    def add_order(self, **fields: Any) -> OrderRow:
        now = self._clock.utcnow()
        with self.session() as s:
            row = OrderRow(created_at=now, updated_at=now, **fields)
            s.add(row)
            s.flush()
            return row

    def update_order(self, client_order_id: str, **fields: Any) -> OrderRow:
        with self.session() as s:
            row = s.scalars(select(OrderRow).where(OrderRow.client_order_id == client_order_id)).one()
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = self._clock.utcnow()
            return row

    def get_order(self, client_order_id: str) -> OrderRow | None:
        with self.session() as s:
            return s.scalars(select(OrderRow).where(OrderRow.client_order_id == client_order_id)).one_or_none()

    def open_orders(self, mode: str) -> list[OrderRow]:
        with self.session() as s:
            q = select(OrderRow).where(OrderRow.mode == mode, OrderRow.status.in_(("pending", "resting")))
            return list(s.scalars(q))

    def add_fill(self, **fields: Any) -> bool:
        """Idempotent on (trade_id, mode): reconciliation may see a fill twice.
        Returns False if it was already recorded."""
        with self.session() as s:
            exists = s.scalars(
                select(FillRow.id).where(FillRow.trade_id == fields["trade_id"], FillRow.mode == fields["mode"])
            ).first()
            if exists is not None:
                return False
            s.add(FillRow(**fields))
            return True

    # ---------------------------------------------------------- positions

    def open_position(self, **fields: Any) -> PositionRow:
        with self.session() as s:
            row = PositionRow(**fields)
            s.add(row)
            s.flush()
            return row

    def update_position(self, position_id: int, **fields: Any) -> PositionRow:
        with self.session() as s:
            row = s.get_one(PositionRow, position_id)
            for k, v in fields.items():
                setattr(row, k, v)
            return row

    def open_positions(self, mode: str) -> list[PositionRow]:
        with self.session() as s:
            q = select(PositionRow).where(PositionRow.mode == mode, PositionRow.closed_at.is_(None))
            return list(s.scalars(q))

    # ------------------------------------------------------------ daily P&L

    def upsert_daily_pnl(self, day: date, mode: str, realized: Decimal, unrealized: Decimal, fees: Decimal) -> None:
        with self.session() as s:
            row = s.get(DailyPnlRow, (day, mode))
            now = self._clock.utcnow()
            if row is None:
                s.add(DailyPnlRow(date=day, mode=mode, realized=realized, unrealized=unrealized,
                                  fees=fees, updated_at=now))
            else:
                row.realized, row.unrealized, row.fees, row.updated_at = realized, unrealized, fees, now
