"""
Category study (next-steps 2): which categories open far from fair, judged
by what actually TRADED?

For a random sample of series per category, take non-combo markets opened
2-72 h ago (closing at least 75 min after opening, with some volume), and
their public trades in the first 75 min:
  consensus  volume-weighted price of trades 45-75 min after open
  early      trades in the first 10 min
A trade at a price >= 20c from the consensus is direct evidence the price
was really on the book and someone took it.

Complements the book-based census/opening measures. Trades UNDERSTATE
opportunity (a mispriced quote nobody hit leaves no trade); the consensus is
hindsight and can OVERSTATE it (news inside the hour). Unsigned public
requests only, so it never draws on the account's rate budget.
"""

from __future__ import annotations

import random
import statistics as st
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from bot.api.models import Market, Trade
from bot.api.rest import KalshiRestClient

DEFAULT_CATEGORIES = ("Politics", "Elections", "Economics", "Financials", "Entertainment", "Sports", "Mentions",
                      "Climate and Weather", "Science and Technology", "Crypto", "Companies", "World", "Commodities")
HUNDRED = Decimal(100)


@dataclass
class TradeMeasure:
    ticker: str
    consensus: Decimal  # cents
    first_price: Decimal
    first_trade_sec: float
    early_contracts: Decimal
    max_dev: Decimal  # cents, largest |early trade - consensus|
    contracts_ge20: Decimal
    gap_dollars_ge20: Decimal  # sum of contracts x |price - consensus| for those trades


def measure_trades(ticker: str, trades: Sequence[Trade], open_time: datetime) -> TradeMeasure | None:
    tr = sorted(trades, key=lambda t: t.created_time)
    if not tr:
        return None
    late = [t for t in tr if t.created_time >= open_time + timedelta(minutes=45)]
    if not late:
        late = [t for t in tr if t.created_time >= open_time + timedelta(minutes=10)][-3:]
    if not late:
        return None
    vol = sum((t.count_fp for t in late), Decimal(0))
    if vol == 0:
        return None
    cons = sum((t.yes_price_dollars * HUNDRED * t.count_fp for t in late), Decimal(0)) / vol
    early = [t for t in tr if t.created_time < open_time + timedelta(minutes=10)]
    devs = [(abs(t.yes_price_dollars * HUNDRED - cons), t.count_fp) for t in early]
    ge20 = [(d, n) for d, n in devs if d >= 20]
    return TradeMeasure(ticker, cons, tr[0].yes_price_dollars * HUNDRED,
                        (tr[0].created_time - open_time).total_seconds(),
                        sum((n for _, n in devs), Decimal(0)), max((d for d, _ in devs), default=Decimal(0)),
                        sum((n for _, n in ge20), Decimal(0)), sum((d * n / HUNDRED for d, n in ge20), Decimal(0)))


@dataclass
class CategoryStudy:
    category: str
    pool: int = 0
    sampled: int = 0
    measures: list[TradeMeasure] = field(default_factory=list)


def eligible(m: Market, now: datetime, min_volume: Decimal) -> bool:
    if m.open_time is None or m.close_time is None:
        return False
    return (now - timedelta(hours=72) <= m.open_time <= now - timedelta(hours=2)
            and m.close_time >= m.open_time + timedelta(minutes=75)
            and (m.volume_fp or Decimal(0)) >= min_volume)


async def study(rest: KalshiRestClient, categories: Sequence[str], now: datetime, *, series_per_category: int = 60,
                markets_per_category: int = 60, min_volume: Decimal = Decimal(100),
                seed: int = 11) -> list[CategoryStudy]:
    rnd = random.Random(seed)
    out: list[CategoryStudy] = []
    since = int((now - timedelta(hours=72)).timestamp())
    for cat in categories:
        cs = CategoryStudy(cat)
        series = [s.ticker for s in await rest.get_series_list(category=cat)]
        pool: list[Market] = []
        for st_ in rnd.sample(series, min(series_per_category, len(series))):
            page = await rest.get_markets(series_ticker=st_, min_created_ts=since, limit=200, mve_filter="exclude")
            pool += [m for m in page.markets if eligible(m, now, min_volume)]
        cs.pool = len(pool)
        for m in rnd.sample(pool, min(markets_per_category, len(pool))):
            assert m.open_time is not None
            cs.sampled += 1
            trades = [t async for t in rest.iter_trades(ticker=m.ticker, min_ts=int(m.open_time.timestamp()),
                                                         max_ts=int((m.open_time + timedelta(minutes=75)).timestamp()))]
            if (tm := measure_trades(m.ticker, trades, m.open_time)) is not None:
                cs.measures.append(tm)
        out.append(cs)
    return out


def format_study(results: Sequence[CategoryStudy]) -> str:
    lines = [f"{'category':<24}{'pool':>6}{'traded':>7}{'early%':>7}{'1st dev':>8}{'>=10c':>7}{'>=20c':>7}"
             f"{'ct@20c':>8}{'$gap@20c':>9}"]
    def key(c: CategoryStudy) -> Decimal:
        return sum((m.contracts_ge20 for m in c.measures), Decimal(0)) / max(1, len(c.measures))
    examples: list[tuple[str, TradeMeasure]] = []
    for c in sorted(results, key=key, reverse=True):
        ms = c.measures
        if not ms:
            lines.append(f"{c.category:<24}{c.pool:>6}{0:>7}   (no measurable early trading)")
            continue
        early = sum(1 for m in ms if m.early_contracts > 0)
        lines.append(f"{c.category:<24}{c.pool:>6}{len(ms):>7}{100 * early / len(ms):>6.0f}%"
                     f"{st.median(abs(m.first_price - m.consensus) for m in ms):>8.1f}"
                     f"{sum(m.max_dev >= 10 for m in ms):>7}{sum(m.max_dev >= 20 for m in ms):>7}"
                     f"{sum((m.contracts_ge20 for m in ms), Decimal(0)):>8.0f}"
                     f"{sum((m.gap_dollars_ge20 for m in ms), Decimal(0)):>9.0f}")
        examples += [(c.category, m) for m in ms if m.contracts_ge20 >= 20]
    lines += ["  1st dev = median |first trade - consensus| (c). >=Nc = markets with an early trade N+ cents from",
              "  consensus. ct/$gap@20c = contracts traded 20c+ off in the first 10 min, and that gap in dollars.",
              "  Trades understate opportunity; the consensus is hindsight. See bot/analysis/category_study.py."]
    if examples:
        lines.append("largest examples:")
        for cat, m in sorted(examples, key=lambda e: e[1].gap_dollars_ge20, reverse=True)[:12]:
            lines.append(f"  {cat:<20} {m.ticker:<46} first {m.first_price:5.1f}c @{m.first_trade_sec:5.0f}s "
                         f"consensus {m.consensus:5.1f}c {m.contracts_ge20:6.0f} ct ${m.gap_dollars_ge20:.0f}")
    return "\n".join(lines)
