"""
Which series the bot cares about, and cached event -> series lookups.

Category lives on the SERIES (docs/API_NOTES.md section 9). The lifecycle
channel announces every market on the exchange -- tens of thousands a day,
mostly combos -- so filtering must be nearly free:

  1. prefix pre-filter: an event ticker's first segment is its series
     ("KXHIGHPHIL-26OCT23" -> "KXHIGHPHIL"). Not in the included set ->
     dropped with no REST call.
  2. confirmation: anything that passes is confirmed with one cached
     GET /events/{event_ticker}, which names the real series.

A series that broke the naming pattern would slip past (1), but the REST
sweep queries by series_ticker directly, so it is still caught.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.api.models import Event, Series
from bot.api.rest import KalshiAPIError, KalshiRestClient
from bot.clock import Clock
from bot.monitoring.log import get_logger

log = get_logger(__name__)


def series_prefix(event_ticker: str) -> str:
    return event_ticker.split("-", 1)[0]


@dataclass(frozen=True)
class EventInfo:
    event_ticker: str
    series_ticker: str
    mutually_exclusive: bool
    title: str | None


class SeriesCatalog:
    def __init__(self, rest: KalshiRestClient, include_categories: list[str], exclude_series: list[str],
                 clock: Clock, refresh_sec: float = 3600.0) -> None:
        self._rest = rest
        self._categories = include_categories
        self._exclude = set(exclude_series)
        self._clock = clock
        self._refresh_sec = refresh_sec
        self.series: dict[str, Series] = {}
        self._events: dict[str, EventInfo] = {}
        self._loaded_at: float | None = None

    @property
    def included(self) -> set[str]:
        return set(self.series) - self._exclude

    async def refresh(self) -> None:
        found: dict[str, Series] = {}
        for cat in self._categories:
            for s in await self._rest.get_series_list(category=cat):
                found[s.ticker] = s
        self.series = found
        self._loaded_at = self._clock.now()
        log.info("series_catalog_refreshed", categories=self._categories, series=len(found),
                 included=len(self.included))

    async def refresh_if_due(self) -> None:
        if self._loaded_at is None or self._clock.now() - self._loaded_at >= self._refresh_sec:
            await self.refresh()

    def maybe_included(self, event_ticker: str) -> bool:
        """The free pre-filter."""
        return series_prefix(event_ticker) in self.included

    async def event_info(self, event_ticker: str) -> EventInfo | None:
        cached = self._events.get(event_ticker)
        if cached is not None:
            return cached
        try:
            ev: Event = (await self._rest.get_event(event_ticker)).event
        except KalshiAPIError as e:
            log.warning("event_lookup_failed", event_ticker=event_ticker, status=e.status)
            return None
        info = EventInfo(ev.event_ticker, ev.series_ticker, ev.mutually_exclusive, ev.title)
        self._events[event_ticker] = info
        return info

    async def confirm(self, event_ticker: str) -> EventInfo | None:
        """EventInfo if the event belongs to an included series, else None."""
        if not self.maybe_included(event_ticker):
            return None
        info = await self.event_info(event_ticker)
        if info is None or info.series_ticker not in self.included:
            return None
        return info

    def category_of(self, series_ticker: str) -> str | None:
        s = self.series.get(series_ticker)
        return s.category if s else None
