"""
Census candidates, inspected (Phase 4.0a): were the sibling census's >= 20c
gaps real?

The census's sibling fair is 100 minus the other markets' MIDS. Wide or
one-sided sibling books make that number soft, so a big "gap" can be an
artefact. For each top candidate this prints every sibling's book at that
moment and applies two tests that do not depend on mids:

  arbitrage    could the whole event be traded at once? Buying YES on every
               market costs sum(asks) and pays 100 if exactly one resolves
               YES, so 100 - sum(asks) - fees > 0 is riskless (likewise
               sum(bids) - 100 - fees selling every market). Selling all is
               safe for any mutually exclusive event (at most one pays out);
               buying all also needs the event to be exhaustive, which
               Kalshi's mutually_exclusive does not promise -- check by eye.
  convergence  did the flagged market's own mid later move at least halfway
               toward the sibling fair (+5/+15/+60 min)? If it did, the
               market was the one out of line; if the siblings moved instead,
               or nothing moved, the "gap" was the model's.

Neither test is the strategy; both say whether the census number deserves
belief. Read-only over recorded data, plus one unsigned get_event per event.
"""

from __future__ import annotations

import statistics as st
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from bot.analysis.census import Book, Observation, _AsOf, event_observations, provisional_taker_fee_cents

if TYPE_CHECKING:
    from bot.api.rest import KalshiRestClient
    from bot.storage.repo import Repository

HUNDRED = Decimal(100)
LATER = (5, 15, 60)  # minutes after the gap


@dataclass
class SiblingQuote:
    ticker: str
    bid: Decimal | None  # cents
    ask: Decimal | None
    bid_size: Decimal
    ask_size: Decimal
    mid: Decimal | None


@dataclass
class Candidate:
    event: str
    category: str
    ticker: str
    ts: datetime
    side: str  # "yes" (buy YES at ask) or "no" (buy NO by hitting the YES bid)
    gap: Decimal  # cents, after fee, as the census scored it
    depth: Decimal
    fair: Decimal
    quotes: list[SiblingQuote] = field(default_factory=list)  # every market in the event, at ts
    arb_buy_all: Decimal | None = None  # cents after fees; None if some market has no ask
    arb_sell_all: Decimal | None = None
    leg_depth_buy: Decimal = Decimal(0)  # smallest ask size across legs
    leg_depth_sell: Decimal = Decimal(0)
    mid_at: Decimal | None = None
    later_mid: dict[int, Decimal | None] = field(default_factory=dict)
    sibling_spread_median: Decimal | None = None

    @property
    def converged(self) -> bool:
        """Mid moved >= halfway to the sibling fair within 15 min."""
        if self.mid_at is None or self.fair == self.mid_at:
            return False
        for m in (5, 15):
            later = self.later_mid.get(m)
            if later is not None and (later - self.mid_at) / (self.fair - self.mid_at) >= Decimal("0.5"):
                return True
        return False

    @property
    def verdict(self) -> str:
        if (self.arb_buy_all is not None and self.arb_buy_all > 0) or \
                (self.arb_sell_all is not None and self.arb_sell_all > 0):
            return "ARB"
        if self.converged:
            return "CONVERGED"
        return "NOT CONFIRMED"


def _cents(x: Decimal | None) -> Decimal | None:
    return None if x is None else x * HUNDRED


def inspect(event: str, category: str, obs: Observation, books: dict[str, list[Book]], min_depth: Decimal,
            fee: Callable[[Decimal], Decimal] = provisional_taker_fee_cents) -> Candidate:
    yes_ok = obs.yes_gap_cents is not None and obs.yes_depth >= min_depth
    no_ok = obs.no_gap_cents is not None and obs.no_depth >= min_depth
    yes_better = yes_ok and (not no_ok or obs.yes_gap_cents >= obs.no_gap_cents)  # type: ignore[operator]
    side, gap, depth = ("yes", obs.yes_gap_cents, obs.yes_depth) if yes_better else \
        ("no", obs.no_gap_cents, obs.no_depth)
    c = Candidate(event, category, obs.ticker, obs.ts, side, gap, depth, obs.fair_cents)  # type: ignore[arg-type]
    series = {t: _AsOf(bs) for t, bs in books.items()}
    for t, s in sorted(series.items()):
        b = s.at(obs.ts)
        if b is None:
            c.quotes.append(SiblingQuote(t, None, None, Decimal(0), Decimal(0), None))
            continue
        c.quotes.append(SiblingQuote(t, _cents(b.bid), _cents(b.ask), b.bid_depth(), b.ask_depth(), _cents(b.mid())))
    asks = [q.ask for q in c.quotes]
    bids = [q.bid for q in c.quotes]
    if all(a is not None for a in asks):
        c.arb_buy_all = HUNDRED - sum(asks, Decimal(0)) - sum(  # type: ignore[arg-type]
            (fee(a / HUNDRED) for a in asks), Decimal(0))  # type: ignore[operator]
        c.leg_depth_buy = min(q.ask_size for q in c.quotes)
    if all(b is not None for b in bids):
        c.arb_sell_all = sum(bids, Decimal(0)) - HUNDRED - sum(  # type: ignore[arg-type]
            (fee(Decimal(1) - b / HUNDRED) for b in bids), Decimal(0))  # type: ignore[operator]
        c.leg_depth_sell = min(q.bid_size for q in c.quotes)
    spreads = [q.ask - q.bid for q in c.quotes if q.ticker != obs.ticker and q.ask is not None and q.bid is not None]
    c.sibling_spread_median = Decimal(str(st.median(spreads))) if spreads else None
    own = series[obs.ticker]
    b0 = own.at(obs.ts)
    c.mid_at = _cents(b0.mid()) if b0 is not None else None
    for m in LATER:
        b = own.at(obs.ts + timedelta(minutes=m))
        c.later_mid[m] = _cents(b.mid()) if b is not None and b.ts > obs.ts else None
    return c


async def find_candidates(repo: Repository, rest: KalshiRestClient, since: datetime, window: timedelta,
                          min_depth: Decimal, threshold: Decimal = Decimal(20), top: int = 20,
                          per_event: int = 2) -> list[Candidate]:
    """The top census gaps (>= threshold, with depth), at most `per_event` per event, inspected."""
    from sqlalchemy import select

    from bot.storage.models import MarketRow, OrderbookSnapshotRow

    with repo.session() as s:
        events = sorted({m.event_ticker for m in s.scalars(select(MarketRow).where(MarketRow.emitted_at >= since))})
    found: list[Candidate] = []
    for ev in events:
        with repo.session() as s:
            rows = list(s.scalars(select(MarketRow).where(MarketRow.event_ticker == ev)))
        opens = [r.open_time for r in rows if r.open_time is not None]
        if len(rows) < 2 or not opens:
            continue
        if not (await rest.get_event(ev)).event.mutually_exclusive:
            continue
        open_time = min(opens)
        cat = next((r.category for r in rows if r.category), None) or "?"
        with repo.session() as s:
            snaps = list(s.scalars(
                select(OrderbookSnapshotRow)
                .where(OrderbookSnapshotRow.ticker.in_([r.ticker for r in rows]),
                       OrderbookSnapshotRow.ts >= open_time,
                       OrderbookSnapshotRow.ts <= open_time + window + timedelta(minutes=max(LATER)))
                .order_by(OrderbookSnapshotRow.ts)))
        books: dict[str, list[Book]] = {r.ticker: [] for r in rows}
        for sn in snaps:
            books[sn.ticker].append(Book.from_row(sn.ts, sn.yes_bids, sn.yes_asks))  # type: ignore[arg-type]
        books = {t: b for t, b in books.items() if b}
        if len(books) < 2:
            continue
        best: dict[str, Observation] = {}
        for o in event_observations(books, open_time, window):
            g = o.best_gap(min_depth)
            if g is not None and g >= threshold and (o.ticker not in best or g > best[o.ticker].best_gap(min_depth)):  # type: ignore[operator]
                best[o.ticker] = o
        chosen = sorted(best.values(), key=lambda o: o.best_gap(min_depth), reverse=True)[:per_event]  # type: ignore[arg-type, return-value]
        found += [inspect(ev, cat, o, books, min_depth) for o in chosen]
        found = sorted(found, key=lambda c: c.gap, reverse=True)[: top * 3]  # bound memory
    return sorted(found, key=lambda c: c.gap, reverse=True)[:top]


def _f(x: Decimal | None, w: int = 5) -> str:
    return f"{x:{w}.1f}" if x is not None else f"{'-':>{w}}"


def format_candidates(cands: Sequence[Candidate]) -> str:
    lines: list[str] = []
    tally: dict[str, dict[str, int]] = {}
    for c in cands:
        tally.setdefault(c.category, {}).setdefault(c.verdict, 0)
        tally[c.category][c.verdict] += 1
        sm = sum((q.mid for q in c.quotes if q.mid is not None), Decimal(0))
        lines += [
            f"\n[{c.verdict}] {c.category} | {c.ticker} @ {c.ts:%Y-%m-%d %H:%M:%S}Z",
            f"  census: buy {c.side.upper()}  gap {c.gap:.1f}c  depth {c.depth:.0f}  sibling fair {c.fair:.1f}c",
            f"  own mid {_f(c.mid_at)}c -> +5m {_f(c.later_mid.get(5))}  +15m {_f(c.later_mid.get(15))}  "
            f"+60m {_f(c.later_mid.get(60))}",
            f"  event: {len(c.quotes)} markets, sum of mids {sm:.1f}c, median sibling spread "
            f"{_f(c.sibling_spread_median)}c",
            f"  buy-all arb {_f(c.arb_buy_all)}c (min leg depth {c.leg_depth_buy:.0f})   "
            f"sell-all arb {_f(c.arb_sell_all)}c (min leg depth {c.leg_depth_sell:.0f})",
        ]
        for q in c.quotes[:12]:
            mark = " <--" if q.ticker == c.ticker else ""
            lines.append(f"    {q.ticker:<52} {_f(q.bid)} x{q.bid_size:<6.0f} / {_f(q.ask)} x{q.ask_size:<6.0f}{mark}")
        if len(c.quotes) > 12:
            lines.append(f"    ... {len(c.quotes) - 12} more markets")
    head = ["verdicts by category (top candidates only):"]
    for cat, v in sorted(tally.items()):
        head.append(f"  {cat:<22} " + "  ".join(f"{k} {n}" for k, n in sorted(v.items())))
    head.append("  ARB = the whole event was tradeable for a riskless profit after fees (check it is exhaustive).")
    head.append("  CONVERGED = the market's own mid moved >= halfway to the sibling fair within 15 min.")
    head.append("  NOT CONFIRMED = neither; the gap is most likely the mid-based model, not the market.")
    return "\n".join(head + lines)
