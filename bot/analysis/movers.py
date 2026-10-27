"""
Biggest opening movers: no threshold, just the largest moves you could have
traded, ranked by the dollars on offer.

For every recorded market, every informative book in its first 10 minutes
is an entry point. Against a later reference price R:
    buy YES at the ask:  R - ask - fee(ask)
    buy NO at the bid:   bid - R - fee(1 - bid)
The best entry is the one with the most dollars available, gap x contracts at
that price, so a 40c gap with 3 contracts ranks below a 6c gap with 500.

References, each reported:
    +1h     mean mid 45-75 min after open (the census/opening measure's consensus)
    +6h     mean mid 5h45-6h15 after open
    settle  100 if the market resolved YES, 0 if NO (when known)

THESE ARE HINDSIGHT. They measure how far the opening price was from where the
market went, not a rule you could have followed at the time. A market that closes
within 6 h of opening usually moves because the outcome is arriving (a game is
played, a reading comes in), so those are flagged "short". Settlement is one
draw of the outcome, not a fair price: a 30c contract that wins is not proof
it was mispriced. What matters is the pattern across many markets: which
series, at what size, how often.
"""

from __future__ import annotations

import statistics as st
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from bot.analysis.census import Book, provisional_taker_fee_cents

if TYPE_CHECKING:
    from bot.storage.repo import Repository

HUNDRED = Decimal(100)
ENTRY = timedelta(minutes=10)
SHORT = timedelta(hours=6)
REFS: dict[str, tuple[timedelta, timedelta]] = {
    "+1h": (timedelta(minutes=45), timedelta(minutes=75)),
    "+6h": (timedelta(hours=5, minutes=45), timedelta(hours=6, minutes=15)),
}


@dataclass
class Entry:
    side: str  # "YES" or "NO"
    price: Decimal  # cents paid per contract (YES ask, or 100 - YES bid for NO)
    gap: Decimal  # cents per contract after fee vs the reference
    depth: Decimal
    sec_after_open: float

    @property
    def dollars(self) -> Decimal:
        return self.gap * self.depth / HUNDRED


@dataclass
class Mover:
    ticker: str
    series: str
    category: str
    open_time: datetime
    short: bool
    refs: dict[str, Decimal | None] = field(default_factory=dict)  # cents
    best: dict[str, Entry | None] = field(default_factory=dict)  # per reference
    result: str | None = None


def _mean_mid(books: Iterable[Book]) -> Decimal | None:
    mids = [m for b in books if (m := b.mid()) is not None]
    return sum(mids, Decimal(0)) / len(mids) * HUNDRED if mids else None


def best_entry(early: Sequence[Book], ref: Decimal, open_time: datetime,
               fee: Callable[[Decimal], Decimal] = provisional_taker_fee_cents) -> Entry | None:
    """The early entry with the most dollars available (gap > 0), vs `ref` cents."""
    best: Entry | None = None
    for b in early:
        if not b.informative:
            continue
        sec = (b.ts - open_time).total_seconds()
        cands = []
        if b.ask is not None:
            cands.append(Entry("YES", b.ask * HUNDRED, ref - b.ask * HUNDRED - fee(b.ask), b.ask_depth(), sec))
        if b.bid is not None:
            cands.append(Entry("NO", HUNDRED - b.bid * HUNDRED, b.bid * HUNDRED - ref - fee(Decimal(1) - b.bid),
                               b.bid_depth(), sec))
        for e in cands:
            if e.gap > 0 and (best is None or e.dollars > best.dollars):
                best = e
    return best


def measure(ticker: str, series: str, category: str, open_time: datetime, close_time: datetime | None,
            result: str | None, early: Sequence[Book], ref_books: dict[str, Sequence[Book]]) -> Mover:
    mv = Mover(ticker, series, category, open_time,
               short=close_time is not None and close_time - open_time < SHORT, result=result)
    refs: dict[str, Decimal | None] = {k: _mean_mid(v) for k, v in ref_books.items()}
    refs["settle"] = HUNDRED if result == "yes" else Decimal(0) if result == "no" else None
    mv.refs = refs
    mv.best = {k: (best_entry(early, r, open_time) if r is not None else None) for k, r in refs.items()}
    return mv


def run_movers(repo: Repository, since: datetime) -> list[Mover]:
    from sqlalchemy import select

    from bot.storage.models import MarketRow, OrderbookSnapshotRow as S

    with repo.session() as s:
        markets = [(m.ticker, m.series_ticker, m.category or "?", m.open_time, m.close_time, m.result)
                   for m in s.scalars(select(MarketRow).where(MarketRow.emitted_at >= since))
                   if m.open_time is not None]
    out: list[Mover] = []

    def books(s, ticker: str, a: datetime, b: datetime) -> list[Book]:  # type: ignore[no-untyped-def]
        rows = s.execute(select(S.ts, S.yes_bids, S.yes_asks)
                         .where(S.ticker == ticker, S.ts >= a, S.ts <= b).order_by(S.ts))
        return [Book.from_row(ts, yb, ya) for ts, yb, ya in rows]

    for ticker, series, cat, op, cl, res in markets:
        with repo.session() as s:
            early = books(s, ticker, op, op + ENTRY)
            if not early:
                continue
            refb = {k: books(s, ticker, op + a, op + b) for k, (a, b) in REFS.items()}
        out.append(measure(ticker, series, cat, op, cl, res, early, refb))
    return out


# ------------------------------------------------------------------ report

def _q(xs: list[Decimal], q: float) -> str:
    if not xs:
        return f"{'-':>6}"
    xs = sorted(xs)
    return f"{xs[min(len(xs) - 1, int(q * len(xs)))]:6.1f}"


def format_movers(movers: Sequence[Mover], ref: str = "+1h", top: int = 25, min_depth: Decimal = Decimal(10)) -> str:
    lines = [f"Reference: {ref}. Gaps in cents per contract after the provisional fee; $ = gap x contracts at that price.",
             "HINDSIGHT: how far the opening price was from where the market went. 'short' = closes within 6 h.", ""]
    # per category, long-lived only (short-lived moves are mostly the outcome arriving)
    lines.append(f"{'category':<22}{'mkts':>6}{'short':>6}{'scored':>7}{'p50':>7}{'p90':>7}{'p99':>7}{'max':>7}"
                 f"{'>=5c':>6}{'>=10c':>6}{'>=15c':>6}{'$ total':>9}{'$ top':>8}")
    cats = sorted({m.category for m in movers})
    for cat in cats:
        ms = [m for m in movers if m.category == cat]
        scored = [m.best[ref] for m in ms if not m.short and m.refs.get(ref) is not None]
        # an unscored-but-referenced market had no positive gap: count it as 0
        gaps = [e.gap if e is not None else Decimal(0) for e in scored]
        deep = [e for e in scored if e is not None and e.depth >= min_depth]
        dollars = sorted((e.dollars for e in scored if e is not None), reverse=True)
        lines.append(f"{cat:<22}{len(ms):>6}{sum(m.short for m in ms):>6}{len(scored):>7}{_q(gaps, .5):>7}"
                     f"{_q(gaps, .9):>7}{_q(gaps, .99):>7}{_q(gaps, 1.0):>7}"
                     f"{sum(e.gap >= 5 for e in deep):>6}{sum(e.gap >= 10 for e in deep):>6}{sum(e.gap >= 15 for e in deep):>6}"
                     f"{sum(dollars, Decimal(0)):>9.0f}{(dollars[0] if dollars else Decimal(0)):>8.0f}")
    lines.append(f"  p50..max = distribution of each market's best gap (0 if none). >=Nc counts need >= {min_depth} "
                 "contracts at the price.")
    # per series, by dollars
    by_series: dict[str, list[Entry]] = {}
    for m in movers:
        e = m.best.get(ref)
        if not m.short and e is not None:
            by_series.setdefault(f"{m.category} | {m.series}", []).append(e)
    lines += ["", f"top series by $ available (long-lived, vs {ref}):",
              f"  {'series':<48}{'mkts':>5}{'$ total':>9}{'med gap':>8}{'med ct':>8}"]
    for name, es in sorted(by_series.items(), key=lambda kv: -sum((e.dollars for e in kv[1]), Decimal(0)))[:15]:
        lines.append(f"  {name:<48}{len(es):>5}{sum((e.dollars for e in es), Decimal(0)):>9.0f}"
                     f"{st.median(e.gap for e in es):>8.1f}{st.median(e.depth for e in es):>8.0f}")
    # top individual markets
    ranked = sorted((m for m in movers if m.best.get(ref) is not None), key=lambda m: -m.best[ref].dollars)  # type: ignore[union-attr]
    lines += ["", f"top {top} markets by $ available vs {ref} (all, short-lived marked):",
              f"  {'ticker':<44}{'cat':<10}{'side':>5}{'paid':>6}{'+1h':>6}{'+6h':>6}{'settle':>7}"
              f"{'gap':>6}{'ct':>7}{'$':>7}{'@s':>5}"]
    for m in ranked[:top]:
        e = m.best[ref]
        assert e is not None

        def r(k: str) -> str:
            v = m.refs.get(k)
            return f"{v:6.1f}" if v is not None else f"{'-':>6}"
        lines.append(f"  {m.ticker[:43]:<44}{m.category[:9]:<10}{e.side:>5}{e.price:6.1f}{r('+1h')}{r('+6h')}"
                     f"{r('settle'):>7}{e.gap:6.1f}{e.depth:7.0f}{e.dollars:7.1f}{e.sec_after_open:5.0f}"
                     f"{'  short' if m.short else ''}")
    return "\n".join(lines)
