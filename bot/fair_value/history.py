"""
Settled-market history: backfill from the API and load into a HistoryIndex.

Settled markets live in two tiers (docs/API_NOTES.md section 12): recent ones
on GET /markets?status=settled, older ones only on GET /historical/markets.
The backfill reads both for every included series and upserts into
`settled_markets`, so it is safe to re-run.

The backfill runs beside the live bot (and the market maker) on one
account's rate limit, so it uses a small share of it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from bot.api.models import Market
from bot.api.rest import KalshiRestClient
from bot.clock import Clock
from bot.fair_value.historical import HistoryIndex, SettledRow, build_events
from bot.monitoring.log import get_logger
from bot.storage.models import SettledMarketRow
from bot.storage.repo import Repository

log = get_logger(__name__)

BACKFILL_RATE_SHARE = 0.2


def _row(mk: Market, series: str, source: str, now: datetime) -> dict[str, object] | None:
    if mk.result not in ("yes", "no") or mk.close_time is None:
        return None
    return {"ticker": mk.ticker, "event_ticker": mk.event_ticker, "series_ticker": series,
            "strike_type": mk.strike_type, "floor_strike": mk.floor_strike, "cap_strike": mk.cap_strike,
            "close_time": mk.close_time, "result": mk.result, "source": source, "fetched_at": now}


def upsert_settled(repo: Repository, rows: list[dict[str, object]]) -> int:
    """Insert unseen tickers. A market can appear in BOTH tiers while Kalshi
    hands it from live to historical (docs/API_NOTES.md section 12) -- found
    on the first real backfill, 2026-10-23 -- so the batch is deduplicated
    first; the first occurrence (live) wins."""
    if not rows:
        return 0
    unique: dict[object, dict[str, object]] = {}
    for r in rows:
        unique.setdefault(r["ticker"], r)
    with repo.session() as s:
        existing = set(s.scalars(select(SettledMarketRow.ticker)
                                 .where(SettledMarketRow.ticker.in_(list(unique)))))
        fresh = [SettledMarketRow(**r) for t, r in unique.items() if t not in existing]  # type: ignore[arg-type]
        s.add_all(fresh)
        return len(fresh)


async def backfill(rest: KalshiRestClient, repo: Repository, series: list[str], clock: Clock) -> dict[str, int]:
    """Fetch every settled market of each series from both tiers. Returns new rows per series."""
    added: dict[str, int] = {}
    for st in series:
        now = clock.utcnow()
        rows: list[dict[str, object]] = []
        async for mk in rest.iter_markets(series_ticker=st, status="settled", mve_filter="exclude"):
            if (r := _row(mk, st, "live", now)) is not None:
                rows.append(r)
        async for mk in rest.iter_historical_markets(series_ticker=st):
            if (r := _row(mk, st, "historical", now)) is not None:
                rows.append(r)
        added[st] = upsert_settled(repo, rows)
        if added[st]:
            log.info("history_backfilled", series=st, fetched=len(rows), new=added[st])
    return added


def load_index(repo: Repository, series: list[str] | None = None) -> HistoryIndex:
    with repo.session() as s:
        q = select(SettledMarketRow)
        if series:
            q = q.where(SettledMarketRow.series_ticker.in_(series))
        rows = [SettledRow(r.ticker, r.event_ticker, r.series_ticker, r.strike_type, r.floor_strike,
                           r.cap_strike, r.close_time, r.result) for r in s.scalars(q)]
    return HistoryIndex(build_events(rows))
