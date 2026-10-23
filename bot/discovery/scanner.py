"""
Strategy step 1: discover new markets (docs/API_NOTES.md sections 8 and 10).

Primary source is the market_lifecycle_v2 channel. Weather markets are
CREATED a median 4.4 h before they OPEN, so a `created` message registers the
market as pending, and the market becomes NEW when trading opens. A REST
sweep of the included series covers anything the channel missed.

Definition of new. The spec: "ticker not in the markets table and open time
within new_market_window_min of now". Because we now learn of markets hours
before they open, the ticker usually IS in the table by then. The intent is
kept and made precise:

    new  <=>  open_time <= now <= open_time + window      (it just opened)
          and first_seen_at <= open_time + window          (we saw it in time)
          and it has not been emitted before (markets.emitted_at, persisted)

so a market first seen long after opening never counts as new, and a market
is emitted exactly once, across restarts too.

The same channel carries determined/settled with the result, which feeds
the status/result updater (plan 2.7).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from bot.api.models import Market
from bot.api.rest import KalshiRestClient
from bot.clock import Clock
from bot.discovery.catalog import EventInfo, SeriesCatalog
from bot.monitoring.log import get_logger
from bot.storage.repo import Repository

log = get_logger(__name__)


@dataclass(frozen=True)
class NewMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    category: str | None
    open_time: datetime
    close_time: datetime | None


def is_new(open_time: datetime | None, first_seen_at: datetime, now: datetime, window: timedelta) -> bool:
    if open_time is None:
        return False
    return open_time <= now <= open_time + window and first_seen_at <= open_time + window


def _ts(v: Any) -> datetime | None:
    return datetime.fromtimestamp(int(v), UTC) if v not in (None, "", 0) else None


@dataclass
class ScannerStats:
    lifecycle_seen: int = 0
    lifecycle_included: int = 0
    registered: int = 0
    emitted: int = 0
    results: int = 0
    sweeps: int = 0


class Scanner:
    def __init__(self, rest: KalshiRestClient, catalog: SeriesCatalog, repo: Repository, clock: Clock, *,
                 window_min: float, sweep_lookback_hours: float,
                 on_new: Callable[[NewMarket], Awaitable[None]]) -> None:
        self._rest = rest
        self._catalog = catalog
        self._repo = repo
        self._clock = clock
        self._window = timedelta(minutes=window_min)
        self._lookback = timedelta(hours=sweep_lookback_hours)
        self._on_new = on_new
        self._pending: dict[str, NewMarket] = {}  # registered, not yet emitted
        self._last_sweep: datetime | None = None
        self.stats = ScannerStats()

    # ------------------------------------------------------- registration

    async def _register(self, ticker: str, info: EventInfo, open_time: datetime | None,
                        close_time: datetime | None, status: str, **extra: Any) -> None:
        category = self._catalog.category_of(info.series_ticker)
        existing = self._repo.get_market(ticker)
        self._repo.upsert_market(ticker=ticker, event_ticker=info.event_ticker, series_ticker=info.series_ticker,
                                 category=category, open_time=open_time, close_time=close_time, status=status,
                                 **{k: v for k, v in extra.items() if v is not None})
        if existing is None:
            self.stats.registered += 1
        already = existing is not None and existing.emitted_at is not None
        if not already and open_time is not None:
            self._pending[ticker] = NewMarket(ticker, info.event_ticker, info.series_ticker, category,
                                              open_time, close_time)

    async def register_market(self, mk: Market) -> None:
        info = await self._catalog.confirm(mk.event_ticker)
        if info is None:
            return
        await self._register(mk.ticker, info, mk.open_time, mk.close_time, mk.status,
                             title=mk.yes_sub_title, rules=mk.rules_primary, result=mk.result,
                             strike_type=mk.strike_type, floor_strike=mk.floor_strike, cap_strike=mk.cap_strike,
                             exchange_index=mk.exchange_index)

    # ---------------------------------------------------------- lifecycle

    async def on_lifecycle(self, kind: str, msg: dict[str, Any]) -> None:
        if kind != "market_lifecycle_v2":
            return  # event_lifecycle / event_fee_update: nothing to do in Phase 2
        self.stats.lifecycle_seen += 1
        ticker = msg.get("market_ticker")
        event_type = msg.get("event_type")
        if not ticker:
            return
        if event_type == "created":
            meta = msg.get("additional_metadata") or {}
            event_ticker = meta.get("event_ticker") or ticker.rsplit("-", 1)[0]
            info = await self._catalog.confirm(event_ticker)
            if info is None:
                return
            self.stats.lifecycle_included += 1
            await self._register(ticker, info, _ts(msg.get("open_ts")), _ts(msg.get("close_ts")), "initialized",
                                 title=meta.get("yes_sub_title") or meta.get("title"),
                                 rules=meta.get("rules_primary"), strike_type=meta.get("strike_type"),
                                 floor_strike=meta.get("floor_strike"), cap_strike=meta.get("cap_strike"),
                                 exchange_index=msg.get("exchange_index"))
            return

        row = self._repo.get_market(ticker)
        if row is None:
            return  # a market we never registered: not ours
        self.stats.lifecycle_included += 1
        if event_type == "activated":
            self._repo.upsert_market(ticker=ticker, status="active")
        elif event_type == "deactivated":
            self._repo.upsert_market(ticker=ticker, status="inactive")
        elif event_type == "close_date_updated" and msg.get("close_ts"):
            self._repo.upsert_market(ticker=ticker, close_time=_ts(msg["close_ts"]))
        elif event_type in ("determined", "settled"):
            result = msg.get("result") or None
            self._repo.upsert_market(ticker=ticker, status="determined" if event_type == "determined" else "finalized",
                                     **({"result": result} if result else {}))
            self.stats.results += 1
            self._pending.pop(ticker, None)

    # ------------------------------------------------------------ emission

    async def tick(self) -> list[NewMarket]:
        """Emit every pending market that has just opened. Cheap; runs every scan_interval."""
        now = self._clock.utcnow()
        emitted: list[NewMarket] = []
        for ticker, nm in list(self._pending.items()):
            if nm.open_time > now:
                continue
            self._pending.pop(ticker)
            row = self._repo.get_market(ticker)
            if row is None or row.emitted_at is not None:
                continue
            if not is_new(nm.open_time, row.first_seen_at, now, self._window):
                continue  # opened too long ago / seen too late
            self._repo.upsert_market(ticker=ticker, emitted_at=now)
            self.stats.emitted += 1
            emitted.append(nm)
            log.info("new_market", ticker=ticker, series=nm.series_ticker, opened=nm.open_time.isoformat())
            await self._on_new(nm)
        return emitted

    # --------------------------------------------------------------- sweep

    async def sweep(self) -> int:
        """REST backstop over the included series. Returns markets seen."""
        await self._catalog.refresh_if_due()
        now = self._clock.utcnow()
        since = self._last_sweep - timedelta(minutes=5) if self._last_sweep else now - self._lookback
        seen = 0
        for series in sorted(self._catalog.included):
            async for mk in self._rest.iter_markets(series_ticker=series, min_created_ts=int(since.timestamp()),
                                                    mve_filter="exclude"):
                seen += 1
                await self.register_market(mk)
        self._last_sweep = now
        self.stats.sweeps += 1
        self._repo.add_event("sweep", series=len(self._catalog.included), markets=seen, since=since.isoformat())
        log.info("sweep_done", series=len(self._catalog.included), markets=seen)
        return seen

    @property
    def pending(self) -> dict[str, NewMarket]:
        return dict(self._pending)
