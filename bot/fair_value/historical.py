"""
Historical model (plan 3.6): the base rate of Yes among settled markets of
the same series with similar parameters.

"Similar parameters" (docs/PLAN.md decision D, proposal): same series, same
number of markets in the event, same POSITION on the strike axis (bottom
tail = 0 ... top tail = n-1), preferring events within
`seasonal_window_days` of the same day of year. Position rather than the raw
strike because Kalshi places each day's buckets around that day's expected
value: "the middle bucket" means the same thing in July and January, while
"63-64F" does not. Measured on KXHIGHPHIL, 641 historical events: Yes rates
by position were 8%, 10%, 22%, 29%, 20%, 12%.

Confidence is about how well the rate is ESTIMATED, then capped by
`max_confidence`. The cap matters more than the estimate: a base rate built
from 700 events is precise, but it ignores the day's forecast, which the
live price contains. Capped below min_confidence, this model can corroborate
the sibling model but never trigger a trade alone (decision D).

No look-ahead: only events that closed before ctx.now are used, so a replay
of past data cannot see outcomes that had not happened yet.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bot.fair_value.types import EventContext, FairValue, MarketInfo, clip_cents


@dataclass(frozen=True)
class SettledEvent:
    event_ticker: str
    series_ticker: str
    close_time: datetime
    markets: tuple[MarketInfo, ...]  # sorted on the strike axis
    yes: tuple[bool, ...]  # aligned with markets


def _doy_distance(a: datetime, b: datetime) -> int:
    d = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(d, 365 - d)


class HistoryIndex:
    """(series, n_markets, position) -> [(close_time, resolved_yes)], sorted by time."""

    def __init__(self, events: Iterable[SettledEvent]) -> None:
        self._rows: dict[tuple[str, int, int], list[tuple[datetime, bool]]] = defaultdict(list)
        self.events = 0
        for ev in events:
            n = len(ev.markets)
            for pos, yes in enumerate(ev.yes):
                self._rows[(ev.series_ticker, n, pos)].append((ev.close_time, yes))
            self.events += 1
        for v in self._rows.values():
            v.sort(key=lambda r: r[0])

    def outcomes(self, series: str, n: int, pos: int, before: datetime) -> list[tuple[datetime, bool]]:
        return [r for r in self._rows.get((series, n, pos), []) if r[0] < before]


@dataclass(frozen=True)
class SettledRow:
    ticker: str
    event_ticker: str
    series_ticker: str
    strike_type: str | None
    floor_strike: float | None
    cap_strike: float | None
    close_time: datetime
    result: str


def build_events(rows: Iterable[SettledRow]) -> list[SettledEvent]:
    """Group settled markets into events. Only complete events, with every
    market resolved yes/no, are kept."""
    by_event: dict[str, list[SettledRow]] = defaultdict(list)
    for r in rows:
        by_event[r.event_ticker].append(r)
    out: list[SettledEvent] = []
    for ev, ms in by_event.items():
        if len(ms) < 2 or any(r.result not in ("yes", "no") for r in ms):
            continue
        infos = sorted((MarketInfo(r.ticker, ev, r.series_ticker, r.strike_type, r.floor_strike, r.cap_strike,
                                   r.close_time) for r in ms), key=lambda m: m.order_key)
        result = {r.ticker: r.result for r in ms}
        out.append(SettledEvent(ev, ms[0].series_ticker, max(r.close_time for r in ms), tuple(infos),
                                tuple(result[m.ticker] == "yes" for m in infos)))
    return out


class HistoricalModel:
    name = "historical"

    def __init__(self, index: HistoryIndex, *, min_history: int, max_confidence: float,
                 seasonal_window_days: int) -> None:
        self._index = index
        self._min = min_history
        self._cap = max_confidence
        self._window = seasonal_window_days

    def estimate(self, market: MarketInfo, ctx: EventContext) -> FairValue | None:
        ordered = ctx.sorted_markets()
        try:
            pos = [m.ticker for m in ordered].index(market.ticker)
        except ValueError:
            return None
        n = len(ordered)
        history = self._index.outcomes(ctx.series_ticker, n, pos, before=ctx.now)
        when = market.close_time or ctx.now
        seasonal = [r for r in history if _doy_distance(r[0], when) <= self._window]
        if len(seasonal) >= self._min:
            sample, basis = seasonal, f"seasonal +/-{self._window}d"
        elif len(history) >= self._min:
            sample, basis = history, "all seasons"
        else:
            return None
        k, total = sum(1 for _, y in sample if y), len(sample)
        p = (k + 0.5) / (total + 1)  # shrunk slightly toward 1/2 so 0/n is not "0%"
        half_width = 1.96 * math.sqrt(p * (1 - p) / total)
        conf = min(self._cap, max(0.0, 1 - half_width / 0.25))
        return FairValue(clip_cents(Decimal(str(round(p * 100, 4)))), round(conf, 6), self.name, {
            "position": pos, "n_markets": n, "yes": k, "sample": total, "basis": basis,
            "half_width_95": round(half_width, 4), "capped": conf == self._cap,
        })
