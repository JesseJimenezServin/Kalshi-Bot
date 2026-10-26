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

import zlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from bot.api.models import Market
from bot.api.rest import KalshiRestClient
from bot.clock import Clock
from bot.discovery.catalog import EventInfo, SeriesCatalog, series_prefix
from bot.monitoring.log import get_logger
from bot.storage.repo import Repository

log = get_logger(__name__)

UNCONFIRMED_RETRY_SEC = 900.0
UNCONFIRMED_RETRY_EVERY_SEC = 30.0


@dataclass(frozen=True)
class NewMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    category: str | None
    open_time: datetime
    close_time: datetime | None


def in_sample(event_ticker: str, fraction: float) -> bool:
    """Deterministic per-EVENT sampling: every market of a sampled event is
    kept together (the sibling model needs the whole event), and a restart
    makes the same choice."""
    if fraction >= 1.0:
        return True
    return zlib.crc32(event_ticker.encode()) / 2**32 < fraction


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
    retried_ok: int = 0
    sampled_out: int = 0


class Scanner:
    def __init__(self, rest: KalshiRestClient, catalog: SeriesCatalog, repo: Repository, clock: Clock, *,
                 window_min: float, sweep_lookback_hours: float,
                 on_new: Callable[[NewMarket], Awaitable[None]],
                 category_sample: Mapping[str, float] | None = None) -> None:
        self._rest = rest
        self._catalog = catalog
        self._repo = repo
        self._clock = clock
        self._window = timedelta(minutes=window_min)
        self._lookback = timedelta(hours=sweep_lookback_hours)
        self._on_new = on_new
        self._sample = dict(category_sample or {})
        self._pending: dict[str, NewMarket] = {}  # registered, not yet emitted
        # `created` messages whose event couldn't be confirmed yet: retried
        # every tick for UNCONFIRMED_RETRY_SEC.
        self._unconfirmed: dict[str, tuple[dict[str, Any], float]] = {}
        self._last_retry: dict[str, float] = {}  # event -> last lookup attempt
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

    def _sampled(self, event_ticker: str) -> bool:
        """Checked BEFORE any event lookup, from the series prefix, so
        sampled-out events cost nothing."""
        cat = self._catalog.category_of(series_prefix(event_ticker))
        if cat is None or in_sample(event_ticker, self._sample.get(cat, 1.0)):
            return True
        self.stats.sampled_out += 1
        return False

    async def register_market(self, mk: Market) -> None:
        if not self._catalog.maybe_included(mk.event_ticker) or not self._sampled(mk.event_ticker):
            return
        info = await self._catalog.confirm(mk.event_ticker)
        if info is None:
            return
        await self._register(mk.ticker, info, mk.open_time, mk.close_time, mk.status,
                             title=mk.yes_sub_title, rules=mk.rules_primary, result=mk.result,
                             strike_type=mk.strike_type, floor_strike=mk.floor_strike, cap_strike=mk.cap_strike,
                             exchange_index=mk.exchange_index)

    # ---------------------------------------------------------- lifecycle

    async def on_lifecycle(self, kind: str, msg: dict[str, Any]) -> None:
        if kind == "event_lifecycle":
            ev, series = msg.get("event_ticker"), msg.get("series_ticker")
            if ev and series:
                self._catalog.remember(EventInfo(ev, series, msg.get("collateral_return_type") == "MECNET",
                                                 msg.get("title")))
            return
        if kind != "market_lifecycle_v2":
            return
        self.stats.lifecycle_seen += 1
        ticker = msg.get("market_ticker")
        event_type = msg.get("event_type")
        if not ticker:
            return
        if event_type == "created":
            meta = msg.get("additional_metadata") or {}
            event_ticker = meta.get("event_ticker") or ticker.rsplit("-", 1)[0]
            if not self._catalog.maybe_included(event_ticker) or not self._sampled(event_ticker):
                return
            info, retry = await self._catalog.confirm_or_retry(event_ticker)
            if info is None:
                if retry:
                    self._unconfirmed.setdefault(ticker, (msg, self._clock.now()))
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

    async def _retry_unconfirmed(self) -> None:
        """One lookup per EVENT, at most every UNCONFIRMED_RETRY_EVERY_SEC: the
        ~09:36 UTC creation burst is ~290 markets in ~50 events, and retrying
        per market every tick would blow the read budget."""
        now = self._clock.now()
        by_event: dict[str, list[str]] = {}
        for ticker, (msg, _first) in self._unconfirmed.items():
            meta = msg.get("additional_metadata") or {}
            by_event.setdefault(meta.get("event_ticker") or ticker.rsplit("-", 1)[0], []).append(ticker)
        for ev, tickers in by_event.items():
            if now - self._last_retry.get(ev, 0.0) < UNCONFIRMED_RETRY_EVERY_SEC:
                continue
            self._last_retry[ev] = now
            info, retry = await self._catalog.confirm_or_retry(ev)
            for ticker in tickers:
                msg, first = self._unconfirmed[ticker]
                if info is not None or not retry or now - first > UNCONFIRMED_RETRY_SEC:
                    del self._unconfirmed[ticker]
                if info is not None:
                    self.stats.retried_ok += 1
                    await self.on_lifecycle("market_lifecycle_v2", msg)
        still_waiting = {e for e, ts in by_event.items() if any(t in self._unconfirmed for t in ts)}
        for ev in list(self._last_retry):
            if ev not in still_waiting:
                del self._last_retry[ev]

    async def tick(self) -> list[NewMarket]:
        """Emit every pending market that has just opened. Cheap; runs every scan_interval."""
        await self._retry_unconfirmed()
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
        """REST backstop: every non-combo market created since the last sweep,
        in one paginated query, filtered locally. Per-series queries stopped
        scaling once Sports was included (~9,000 series, ~10 min of read
        budget per sweep); this is ~7 pages per 6 hours of creations.

        Markets that opened longer ago than the new-market window are skipped
        before any event lookup: they can never be new."""
        await self._catalog.refresh_if_due()
        now = self._clock.utcnow()
        since = self._last_sweep - timedelta(minutes=5) if self._last_sweep else now - self._lookback
        seen = considered = 0
        async for mk in self._rest.iter_markets(min_created_ts=int(since.timestamp()), mve_filter="exclude"):
            seen += 1
            if mk.open_time is not None and mk.open_time < now - self._window:
                continue
            considered += 1
            await self.register_market(mk)
        self._last_sweep = now
        self.stats.sweeps += 1
        self._repo.add_event("sweep", markets=seen, considered=considered, since=since.isoformat())
        log.info("sweep_done", markets=seen, considered=considered)
        return considered

    def recently_emitted(self, within: timedelta) -> list[tuple[NewMarket, datetime]]:
        """Markets declared new within `within`, with when -- so a restart can
        resume watching them instead of silently dropping them."""
        from sqlalchemy import select

        from bot.storage.models import MarketRow

        since = self._clock.utcnow() - within
        with self._repo.session() as s:
            rows = list(s.scalars(select(MarketRow).where(MarketRow.emitted_at >= since)))
        # Re-apply the CURRENT include list and sampling: after a config change
        # that drops a category, its old watches must not come back on restart.
        return [(NewMarket(r.ticker, r.event_ticker, r.series_ticker, r.category, r.open_time, r.close_time),
                 r.emitted_at) for r in rows
                if r.open_time is not None and r.emitted_at is not None
                and self._catalog.maybe_included(r.event_ticker) and self._sampled(r.event_ticker)]

    @property
    def pending(self) -> dict[str, NewMarket]:
        return dict(self._pending)
