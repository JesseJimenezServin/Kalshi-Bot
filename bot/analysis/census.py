"""
Opportunity census (plan 2.12): does the edge the strategy needs exist at all?

Before building fair-value models and paper trading (Phases 3-4), measure on
recorded data how often a newly opened market is mispriced against its
siblings by the strategy's entry threshold, after fees, at prices you could
actually trade. The market maker's lesson: measure the thing that decides
the project before building everything downstream of it.

Method, per mutually exclusive event, over each market's first
`window` after open:
  - time grid = every snapshot timestamp of any market in the event; each
    market's book at t is its latest snapshot at or before t (as-of join)
  - sibling fair for market i = 100 - sum(mid of every other market), clipped
    to [1, 99] -- the spec's sibling model, mutually exclusive case
  - executable gaps, using the book's ask/bid, never a last trade:
        yes_gap = fair - yes_ask - fee(yes_ask)
        no_gap  = yes_bid - fair - fee(1 - yes_bid)
  - a gap "counts with depth" only if the contracts available at that price
    reach `min_depth`

A missing side is handled explicitly rather than guessed: a market with no
YES bid has mid = ask / 2 (it can only be worth less than its ask), with no
NO bid (no ask) mid = (1 + bid) / 2. Every result carries the share of
sibling books that were two-sided, so thin-book artefacts are visible.

FEE IS PROVISIONAL: 0.07 * p * (1 - p) per contract (taker), from an
unverified secondary source (docs/API_NOTES.md section 7). The owner is to
confirm it against Kalshi's PDF before Phase 4.

Placeholder quotes are not prices. In the first seconds market makers
often post 1c/99c (seen live 2026-10-23); its "mid" of 50c made a
~30-bucket event sum to ~1,434c. A book that leaves the price in a range wider than
MAX_INFORMATIVE_SPREAD (two-sided or one-sided) is treated as having no price, so a moment where any
sibling is placeholder-quoted is skipped rather than scored.

Caveat on the method itself: if a whole event opens mispriced together, the
siblings carry the same error and the sibling model cannot see it. The
census measures what the sibling model can see, which is what the strategy
would trade on.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.api.rest import KalshiRestClient
    from bot.storage.repo import Repository

ONE = Decimal(1)
HUNDRED = Decimal(100)
THRESHOLDS_CENTS = (5, 10, 20)
MAX_INFORMATIVE_SPREAD = Decimal("0.50")  # dollars


def provisional_taker_fee_cents(price: Decimal) -> Decimal:
    """Per-contract taker fee in cents. UNVERIFIED -- see module docstring."""
    return Decimal("0.07") * price * (ONE - price) * HUNDRED


@dataclass(frozen=True)
class Book:
    """One recorded snapshot, best-first levels in dollars."""

    ts: datetime
    yes_bids: tuple[tuple[Decimal, Decimal], ...]
    yes_asks: tuple[tuple[Decimal, Decimal], ...]

    @classmethod
    def from_row(cls, ts: datetime, yes_bids: list[list[str]], yes_asks: list[list[str]]) -> Book:
        return cls(ts, tuple((Decimal(p), Decimal(s)) for p, s in yes_bids),
                   tuple((Decimal(p), Decimal(s)) for p, s in yes_asks))

    @property
    def bid(self) -> Decimal | None:
        return self.yes_bids[0][0] if self.yes_bids else None

    @property
    def ask(self) -> Decimal | None:
        return self.yes_asks[0][0] if self.yes_asks else None

    @property
    def two_sided(self) -> bool:
        return bool(self.yes_bids and self.yes_asks)

    @property
    def informative(self) -> bool:
        """False for placeholder quotes: the range the book leaves the price in
        is wider than MAX_INFORMATIVE_SPREAD. Two-sided that is ask - bid
        (1c/99c); one-sided it is [0, ask] or [bid, 1] -- an ask-only 99c
        placeholder "mid" of 49.5c made a 29-bucket event sum to 1,434c."""
        b, a = self.bid, self.ask
        if b is not None and a is not None:
            width = a - b
        elif a is not None:
            width = a
        elif b is not None:
            width = ONE - b
        else:
            return False
        return width <= MAX_INFORMATIVE_SPREAD

    def mid(self) -> Decimal | None:
        b, a = self.bid, self.ask
        if not self.informative:
            return None
        if b is not None and a is not None:
            return (b + a) / 2
        if a is not None:
            return a / 2
        if b is not None:
            return (ONE + b) / 2
        return None

    def ask_depth(self) -> Decimal:
        """YES contracts buyable at the best ask."""
        return self.yes_asks[0][1] if self.yes_asks else Decimal(0)

    def bid_depth(self) -> Decimal:
        """NO contracts buyable at 1 - best bid (hitting the YES bid)."""
        return self.yes_bids[0][1] if self.yes_bids else Decimal(0)


@dataclass
class Observation:
    ticker: str
    ts: datetime
    dt_sec: float  # how long this observation stands until the next grid point
    fair_cents: Decimal
    yes_gap_cents: Decimal | None
    no_gap_cents: Decimal | None
    yes_depth: Decimal
    no_depth: Decimal
    siblings_two_sided: float  # share of OTHER markets with a two-sided book

    def best_gap(self, min_depth: Decimal | None = None) -> Decimal | None:
        gaps = []
        if self.yes_gap_cents is not None and (min_depth is None or self.yes_depth >= min_depth):
            gaps.append(self.yes_gap_cents)
        if self.no_gap_cents is not None and (min_depth is None or self.no_depth >= min_depth):
            gaps.append(self.no_gap_cents)
        return max(gaps) if gaps else None


class _AsOf:
    def __init__(self, books: list[Book]) -> None:
        self.books = sorted(books, key=lambda b: b.ts)
        self.times = [b.ts for b in self.books]

    def at(self, t: datetime) -> Book | None:
        i = bisect.bisect_right(self.times, t) - 1
        return self.books[i] if i >= 0 else None


def event_observations(books_by_ticker: dict[str, list[Book]], open_time: datetime, window: timedelta,
                       fee: Callable[[Decimal], Decimal] = provisional_taker_fee_cents) -> list[Observation]:
    """Sibling-model gaps for every market in one mutually exclusive event."""
    if len(books_by_ticker) < 2:
        return []
    end = open_time + window
    series = {t: _AsOf([b for b in bs if b.ts <= end]) for t, bs in books_by_ticker.items()}
    grid = sorted({b.ts for s in series.values() for b in s.books if open_time <= b.ts <= end})
    out: list[Observation] = []
    for k, t in enumerate(grid):
        dt = ((grid[k + 1] if k + 1 < len(grid) else end) - t).total_seconds()
        current = {tk: s.at(t) for tk, s in series.items()}
        if any(b is None for b in current.values()):
            continue  # some sibling has no book yet: fair value undefined
        mids = {tk: b.mid() for tk, b in current.items() if b is not None}
        if any(m is None for m in mids.values()):
            continue
        for tk, book in current.items():
            assert book is not None
            others = [tk2 for tk2 in current if tk2 != tk]
            fair = HUNDRED - sum((mids[o] * HUNDRED for o in others), Decimal(0))  # type: ignore[operator]
            fair = min(Decimal(99), max(ONE, fair))
            ask, bid = book.ask, book.bid
            yes_gap = (fair - ask * HUNDRED - fee(ask)) if ask is not None else None
            no_gap = (bid * HUNDRED - fair - fee(ONE - bid)) if bid is not None else None
            two = sum(1 for o in others if current[o].two_sided) / len(others)  # type: ignore[union-attr]
            out.append(Observation(tk, t, dt, fair, yes_gap, no_gap, book.ask_depth(), book.bid_depth(), two))
    return out


@dataclass
class ThresholdStats:
    threshold: int
    markets_any: int = 0
    markets_any_with_depth: int = 0
    seconds: float = 0.0
    seconds_with_depth: float = 0.0
    episodes: list[float] = field(default_factory=list)  # durations (s) of with-depth episodes


@dataclass
class CensusReport:
    events: int = 0
    events_skipped_not_exclusive: int = 0
    events_skipped_single_market: int = 0
    markets: int = 0
    observed_seconds: float = 0.0
    by_threshold: dict[int, ThresholdStats] = field(default_factory=dict)
    max_gap_cents: Decimal | None = None
    max_gap_where: str | None = None
    # |sum of mids - 100| per event at its first grid point, cents
    open_sum_deviation: list[Decimal] = field(default_factory=list)
    clean_share: float = 0.0  # share of observations with all siblings two-sided
    _clean: int = 0
    _total: int = 0

    def add_event(self, obs: list[Observation], open_dev: Decimal | None, min_depth: Decimal) -> None:
        """Fold one event in. Incremental so a multi-day census never holds
        every observation at once (the 72 h run OOM-killed on the 1 GB droplet)."""
        self.events += 1
        if open_dev is not None:
            self.open_sum_deviation.append(open_dev)
        by_ticker: dict[str, list[Observation]] = {}
        for o in obs:
            by_ticker.setdefault(o.ticker, []).append(o)
            self.observed_seconds += o.dt_sec
            self._total += 1
            self._clean += o.siblings_two_sided == 1.0
            g = o.best_gap()
            if g is not None and (self.max_gap_cents is None or g > self.max_gap_cents):
                self.max_gap_cents, self.max_gap_where = g, f"{o.ticker} @ {o.ts.isoformat()}"
        self.markets += len(by_ticker)
        for th, st in self.by_threshold.items():
            for mo in by_ticker.values():
                hit = [o for o in mo if (g := o.best_gap()) is not None and g >= th]
                hit_d = [o for o in mo if (g := o.best_gap(min_depth)) is not None and g >= th]
                st.markets_any += bool(hit)
                st.markets_any_with_depth += bool(hit_d)
                st.seconds += sum(o.dt_sec for o in hit)
                st.seconds_with_depth += sum(o.dt_sec for o in hit_d)
                run = 0.0
                for o in mo:
                    g = o.best_gap(min_depth)
                    if g is not None and g >= th:
                        run += o.dt_sec
                    elif run:
                        st.episodes.append(run)
                        run = 0.0
                if run:
                    st.episodes.append(run)
        self.clean_share = self._clean / self._total if self._total else 0.0


def new_report(thresholds: tuple[int, ...] = THRESHOLDS_CENTS) -> CensusReport:
    return CensusReport(by_threshold={t: ThresholdStats(t) for t in thresholds})


def summarize(per_event: Iterable[tuple[str, list[Observation], Decimal | None]], min_depth: Decimal,
              thresholds: tuple[int, ...] = THRESHOLDS_CENTS) -> CensusReport:
    rep = new_report(thresholds)
    for event_ticker, obs, open_dev in per_event:
        rep.add_event(obs, open_dev, min_depth)
    return rep


def open_sum_deviation(books_by_ticker: dict[str, list[Book]], open_time: datetime,
                       window: timedelta) -> Decimal | None:
    """|sum of mids - 100| at the first moment every market has a book."""
    series = {t: _AsOf(bs) for t, bs in books_by_ticker.items()}
    grid = sorted({b.ts for s in series.values() for b in s.books if open_time <= b.ts <= open_time + window})
    for t in grid:
        cur = [s.at(t) for s in series.values()]
        if all(b is not None and b.mid() is not None for b in cur):
            mids = [b.mid() for b in cur]  # type: ignore[union-attr]
            return abs(sum(mids, Decimal(0)) * HUNDRED - HUNDRED)  # type: ignore[arg-type]
    return None


def format_report(rep: CensusReport, min_depth: Decimal) -> str:
    def pct(a: float, b: float) -> str:
        return f"{100 * a / b:5.1f}%" if b else "  n/a"

    lines = [
        f"events analysed           {rep.events}  (skipped: {rep.events_skipped_not_exclusive} not mutually "
        f"exclusive, {rep.events_skipped_single_market} single-market)",
        f"markets                   {rep.markets}",
        f"market-time observed      {rep.observed_seconds / 3600:.1f} h",
        f"clean observations        {100 * rep.clean_share:.1f}%  (every sibling book two-sided)",
        "",
        f"{'gap >=':>8} {'markets':>9} {'w/ depth':>9} {'time':>8} {'time w/ depth':>14} "
        f"{'episodes':>9} {'median s':>9} {'max s':>8}",
    ]
    for th, st in rep.by_threshold.items():
        eps = sorted(st.episodes)
        med = f"{eps[len(eps) // 2]:9.0f}" if eps else "        -"
        mx = f"{eps[-1]:8.0f}" if eps else "       -"
        lines.append(f"{th:>7}c {st.markets_any:>9} {st.markets_any_with_depth:>9} "
                     f"{pct(st.seconds, rep.observed_seconds):>8} {pct(st.seconds_with_depth, rep.observed_seconds):>14} "
                     f"{len(eps):>9} {med} {mx}")
    lines.append(f"  (with depth = at least {min_depth} contracts at the entry price)")
    if rep.max_gap_cents is not None:
        lines.append(f"largest gap seen          {rep.max_gap_cents:.1f}c at {rep.max_gap_where}")
    if rep.open_sum_deviation:
        devs = sorted(rep.open_sum_deviation)
        lines.append(f"|sum of mids - 100| at open: median {devs[len(devs) // 2]:.1f}c, max {devs[-1]:.1f}c "
                     f"over {len(devs)} events")
    lines.append("FEES ARE PROVISIONAL (0.07*p*(1-p), unverified) -- see docs/API_NOTES.md section 7.")
    return "\n".join(lines)


ALL = "ALL"


async def run_census(repo: Repository, rest: KalshiRestClient, since: datetime, window: timedelta,
                     min_depth: Decimal) -> dict[str, CensusReport]:
    """Census over every event with a market declared new since `since`,
    one report per category plus ALL (next-steps 3)."""
    from sqlalchemy import select

    from bot.storage.models import MarketRow, OrderbookSnapshotRow

    with repo.session() as s:
        new = list(s.scalars(select(MarketRow).where(MarketRow.emitted_at >= since)))
        events = sorted({m.event_ticker for m in new})
        members: dict[str, list[MarketRow]] = {}
        for ev in events:
            members[ev] = list(s.scalars(select(MarketRow).where(MarketRow.event_ticker == ev)))

    reports: dict[str, CensusReport] = {ALL: new_report()}
    category_of: dict[str, str] = {}
    skipped_ne: dict[str, int] = {}
    skipped_single: dict[str, int] = {}
    for ev in events:
        rows = members[ev]
        cat = next((r.category for r in rows if r.category), None) or "?"
        category_of[ev] = cat
        opens = [r.open_time for r in rows if r.open_time is not None]
        if len(rows) < 2 or not opens:
            skipped_single[cat] = skipped_single.get(cat, 0) + 1
            continue
        info = (await rest.get_event(ev)).event
        if not info.mutually_exclusive:
            skipped_ne[cat] = skipped_ne.get(cat, 0) + 1
            continue
        open_time = min(opens)
        with repo.session() as s:
            snaps = list(s.scalars(
                select(OrderbookSnapshotRow)
                .where(OrderbookSnapshotRow.ticker.in_([r.ticker for r in rows]),
                       OrderbookSnapshotRow.ts >= open_time, OrderbookSnapshotRow.ts <= open_time + window)
                .order_by(OrderbookSnapshotRow.ts)))
        books: dict[str, list[Book]] = {r.ticker: [] for r in rows}
        for sn in snaps:
            books[sn.ticker].append(Book.from_row(sn.ts, sn.yes_bids, sn.yes_asks))  # type: ignore[arg-type]
        books = {t: b for t, b in books.items() if b}
        if len(books) < 2:
            skipped_single[cat] = skipped_single.get(cat, 0) + 1
            continue
        obs = event_observations(books, open_time, window)
        dev = open_sum_deviation(books, open_time, window)
        del snaps, books
        for rep in (reports.setdefault(cat, new_report()), reports[ALL]):
            rep.add_event(obs, dev, min_depth)

    cats = sorted({category_of[e] for e in events})
    out: dict[str, CensusReport] = {}
    for cat in [*cats, ALL]:
        rep = reports.get(cat) or new_report()
        rep.events_skipped_not_exclusive = sum(v for k, v in skipped_ne.items() if cat in (ALL, k))
        rep.events_skipped_single_market = sum(v for k, v in skipped_single.items() if cat in (ALL, k))
        out[cat] = rep
    return out
