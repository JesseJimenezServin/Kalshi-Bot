"""
Status/result updater (plan 2.7).

The lifecycle channel delivers determined/settled with results as they
happen; this is the backstop for anything missed while disconnected. The
historical model and the calibration table both depend on results being
complete, so nothing relies on the push alone.

Markets settled before GET /historical/cutoff disappear from /markets
(docs/API_NOTES.md section 12); those are looked up on /historical/markets.
"""

from __future__ import annotations

from bot.api.rest import KalshiRestClient
from bot.clock import Clock
from bot.monitoring.log import get_logger
from bot.storage.repo import Repository

log = get_logger(__name__)

BATCH = 50


class ResultsUpdater:
    def __init__(self, rest: KalshiRestClient, repo: Repository, clock: Clock) -> None:
        self._rest = rest
        self._repo = repo
        self._clock = clock

    async def refresh(self) -> int:
        """Re-read unresolved markets whose close time has passed. Returns rows updated."""
        now = self._clock.utcnow()
        due = [m for m in self._repo.unresolved_markets() if m.close_time is not None and m.close_time <= now]
        updated = 0
        for i in range(0, len(due), BATCH):
            batch = [m.ticker for m in due[i:i + BATCH]]
            page = await self._rest.get_markets(tickers=batch, limit=len(batch))
            found = {mk.ticker: mk for mk in page.markets}
            for ticker in batch:
                mk = found.get(ticker)
                if mk is None:  # settled before the cutoff: moved to the historical tier
                    mk = await self._rest.get_historical_market(ticker)
                if mk is None:
                    continue
                self._repo.upsert_market(ticker=ticker, status=mk.status,
                                         **({"result": mk.result} if mk.result else {}))
                updated += 1
        if due:
            log.info("results_refreshed", due=len(due), updated=updated)
            self._repo.add_event("results_refresh", due=len(due), updated=updated)
        return updated
