"""
Opening-move measure (next-steps 1): did a market open far from where it
settled down, at a price you could have traded?

The census needs siblings, so it cannot score single-market events (common
in Politics and Mentions). This measure uses each market's OWN later price
instead:

  consensus   mean mid of two-sided snapshots 45-75 min after open
  early       snapshots in the first 10 min after open
  gap         executable, after the fee, vs consensus:
                buy YES: consensus - ask - fee(ask)
                buy NO:  bid - consensus - fee(1 - bid)
              counted "with depth" only if >= min_depth sits at that price

UPPER BOUND. Consensus is hindsight: if news arrives in the first hour,
a move toward the new price looks like a correction of a mispricing. A large
gap here says "worth a look", not "free money". Siblings (the census) are
the stronger test where they exist.

SHORT-LIVED MARKETS ARE EXCLUDED. Markets resolving within MIN_LIFETIME of
opening are not scored. Found on the first live run, 2026-10-23: hourly
temperature markets open ~1 h before the hour they measure, so their 45-75
min "consensus" is formed while real readings arrive. NYC "above 51.99F"
went from ~70c to 5c because the temperature came in low. That is the
outcome being learned, not a mispricing being corrected.
"""

from __future__ import annotations

import statistics as st
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from bot.analysis.census import Book, provisional_taker_fee_cents

if TYPE_CHECKING:
    from bot.storage.repo import Repository

HUNDRED = Decimal(100)
EARLY = timedelta(minutes=10)
MIN_LIFETIME = timedelta(hours=6)
CONSENSUS_FROM = timedelta(minutes=45)
CONSENSUS_TO = timedelta(minutes=75)


@dataclass
class OpeningResult:
    ticker: str
    category: str
    first_book_sec: float | None  # how long after open the first book was recorded
    consensus: Decimal | None  # cents
    first_mid: Decimal | None
    max_gap: Decimal | None  # best executable gap in the first 10 min, any depth
    max_gap_with_depth: Decimal | None
    depth_at_gap: Decimal  # contracts at the best with-depth gap
    seconds_ge: dict[int, float] = field(default_factory=dict)  # with depth, time-weighted
    short_lived: bool = False  # resolves within MIN_LIFETIME of open: not scored

    @property
    def measurable(self) -> bool:
        return not self.short_lived and self.consensus is not None and self.first_mid is not None


def _gaps(b: Book, consensus: Decimal, fee: Callable[[Decimal], Decimal]) -> list[tuple[Decimal, Decimal]]:
    """(gap cents, contracts available) for buying YES and buying NO."""
    out = []
    if b.ask is not None:
        out.append((consensus - b.ask * HUNDRED - fee(b.ask), b.ask_depth()))
    if b.bid is not None:
        out.append((b.bid * HUNDRED - consensus - fee(Decimal(1) - b.bid), b.bid_depth()))
    return out


def opening_result(ticker: str, category: str, books: list[Book], open_time: datetime, min_depth: Decimal,
                   thresholds: tuple[int, ...] = (10, 20),
                   fee: Callable[[Decimal], Decimal] = provisional_taker_fee_cents,
                   close_time: datetime | None = None) -> OpeningResult:
    books = sorted((b for b in books if open_time <= b.ts <= open_time + CONSENSUS_TO), key=lambda b: b.ts)
    res = OpeningResult(ticker, category, None, None, None, None, None, Decimal(0),
                        {t: 0.0 for t in thresholds},
                        short_lived=close_time is not None and close_time - open_time < MIN_LIFETIME)
    if not books:
        return res
    if res.short_lived:
        res.first_book_sec = (books[0].ts - open_time).total_seconds()
        return res
    res.first_book_sec = (books[0].ts - open_time).total_seconds()
    late = [b for b in books if b.ts >= open_time + CONSENSUS_FROM and b.two_sided]
    if not late:
        return res
    mids = [b.mid() for b in late]
    res.consensus = sum(mids, Decimal(0)) / len(mids) * HUNDRED  # type: ignore[arg-type]
    first_mid = books[0].mid()
    res.first_mid = first_mid * HUNDRED if first_mid is not None else None
    early = [b for b in books if b.ts < open_time + EARLY]
    for k, b in enumerate(early):
        nxt = early[k + 1].ts if k + 1 < len(early) else min(open_time + EARLY, books[-1].ts)
        dt = max(0.0, (nxt - b.ts).total_seconds())
        for gap, depth in _gaps(b, res.consensus, fee):
            if res.max_gap is None or gap > res.max_gap:
                res.max_gap = gap
            if depth >= min_depth:
                if res.max_gap_with_depth is None or gap > res.max_gap_with_depth:
                    res.max_gap_with_depth, res.depth_at_gap = gap, depth
                for t in thresholds:
                    if gap >= t:
                        res.seconds_ge[t] += dt
    return res


@dataclass
class CategoryOpening:
    category: str
    markets: int = 0
    short_lived: int = 0
    measurable: int = 0
    first_book_sec: list[float] = field(default_factory=list)
    first_vs_consensus: list[Decimal] = field(default_factory=list)
    ge: dict[int, int] = field(default_factory=dict)  # markets with a with-depth gap >= t
    ge_any_depth: dict[int, int] = field(default_factory=dict)
    contracts_ge20: Decimal = Decimal(0)
    seconds_ge20: list[float] = field(default_factory=list)


def aggregate(results: Iterable[OpeningResult], thresholds: tuple[int, ...] = (10, 20)) -> dict[str, CategoryOpening]:
    out: dict[str, CategoryOpening] = {}
    for r in results:
        c = out.setdefault(r.category, CategoryOpening(r.category, ge={t: 0 for t in thresholds},
                                                      ge_any_depth={t: 0 for t in thresholds}))
        c.markets += 1
        c.short_lived += r.short_lived
        if r.first_book_sec is not None:
            c.first_book_sec.append(r.first_book_sec)
        if not r.measurable:
            continue
        c.measurable += 1
        c.first_vs_consensus.append(abs(r.first_mid - r.consensus))  # type: ignore[operator]
        for t in thresholds:
            c.ge[t] += r.max_gap_with_depth is not None and r.max_gap_with_depth >= t
            c.ge_any_depth[t] += r.max_gap is not None and r.max_gap >= t
        if r.max_gap_with_depth is not None and r.max_gap_with_depth >= 20:
            c.contracts_ge20 += r.depth_at_gap
            c.seconds_ge20.append(r.seconds_ge.get(20, 0.0))
    return out


def format_opening(cats: dict[str, CategoryOpening], min_depth: Decimal) -> str:
    lines = [f"{'category':<24}{'mkts':>6}{'short':>6}{'measur.':>8}{'1st book s':>11}{'|1st-cons|':>11}"
             f"{'>=10c':>7}{'>=20c':>7}{'>=20c any':>10}{'ct@20c':>8}{'sec>=20c':>9}"]
    for c in sorted(cats.values(), key=lambda c: -(c.ge.get(20, 0) / max(1, c.measurable))):
        fb = f"{st.median(c.first_book_sec):>11.0f}" if c.first_book_sec else f"{'-':>11}"
        fc = f"{st.median(c.first_vs_consensus):>11.1f}" if c.first_vs_consensus else f"{'-':>11}"
        sec = f"{st.median(c.seconds_ge20):>9.0f}" if c.seconds_ge20 else f"{'-':>9}"
        lines.append(f"{c.category:<24}{c.markets:>6}{c.short_lived:>6}{c.measurable:>8}{fb}{fc}{c.ge.get(10, 0):>7}{c.ge.get(20, 0):>7}"
                     f"{c.ge_any_depth.get(20, 0):>10}{c.contracts_ge20:>8.0f}{sec}")
    lines += [
        f"  >=Nc = markets with an executable gap of N cents or more vs their own 45-75 min consensus, within",
        f"  10 min of open, after the fee, with >= {min_depth} contracts at the price ('any' ignores depth).",
        "  sec>=20c = median seconds such a gap lasted. 1st book s = delay from open to the first recorded book.",
        f"  short = resolve within {MIN_LIFETIME.total_seconds() / 3600:g} h of open: not scored, their consensus is the "
        "outcome arriving.",
        "  UPPER BOUND: consensus is hindsight; news in the first hour reads as a 'mispricing'. FEES PROVISIONAL.",
    ]
    return "\n".join(lines)


def run_opening(repo: Repository, since: datetime, min_depth: Decimal) -> dict[str, CategoryOpening]:
    from sqlalchemy import select

    from bot.storage.models import MarketRow, OrderbookSnapshotRow

    with repo.session() as s:
        markets = list(s.scalars(select(MarketRow).where(MarketRow.emitted_at >= since)))
    results: list[OpeningResult] = []
    for m in markets:
        if m.open_time is None:
            continue
        with repo.session() as s:
            snaps = list(s.scalars(select(OrderbookSnapshotRow)
                                   .where(OrderbookSnapshotRow.ticker == m.ticker,
                                          OrderbookSnapshotRow.ts >= m.open_time,
                                          OrderbookSnapshotRow.ts <= m.open_time + CONSENSUS_TO)
                                   .order_by(OrderbookSnapshotRow.ts)))
        books = [Book.from_row(sn.ts, sn.yes_bids, sn.yes_asks) for sn in snaps]  # type: ignore[arg-type]
        results.append(opening_result(m.ticker, m.category or "?", books, m.open_time, min_depth,
                                      close_time=m.close_time))
    return aggregate(results)
