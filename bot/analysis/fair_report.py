"""
Fair-value report (plan 3.11): every model's estimate beside the live price.

For recorded events that are open right now, one GET /events/{ticker} per
event returns every market's best bid/ask and sizes; the engine runs on that.
Read-only: nothing is written, and the engine's sink is off.

Per market: live bid/ask, each model's estimate and confidence, the engine's
combined estimate, and the best executable gap after the (provisional) fee
-- the number the Phase 4 signal will be built on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from bot.analysis.census import provisional_taker_fee_cents
from bot.api.rest import KalshiRestClient
from bot.fair_value.engine import FairValueEngine
from bot.fair_value.types import HUNDRED, EventContext, FairValue, MarketInfo, Quote
from bot.storage.models import MarketRow
from bot.storage.repo import Repository


@dataclass
class Row:
    ticker: str
    quote: Quote
    per_model: dict[str, FairValue]
    combined: FairValue | None
    gap_cents: Decimal | None
    side: str | None


def executable_gap(fair: Decimal, q: Quote) -> tuple[Decimal | None, str | None]:
    """Best of: buy YES at the ask, or buy NO at 100 - bid; after the fee."""
    best: tuple[Decimal | None, str | None] = (None, None)
    if q.ask is not None:
        g = fair - q.ask - provisional_taker_fee_cents(q.ask / HUNDRED)
        best = (g, "yes")
    if q.bid is not None:
        g = q.bid - fair - provisional_taker_fee_cents((HUNDRED - q.bid) / HUNDRED)
        if best[0] is None or g > best[0]:
            best = (g, "no")
    return best


def open_events(repo: Repository, now: datetime, limit: int, series_prefix: str | None = None) -> list[str]:
    with repo.session() as s:
        q = (select(MarketRow.event_ticker, MarketRow.open_time)
             .where(MarketRow.open_time <= now, MarketRow.close_time > now, MarketRow.result.is_(None)))
        if series_prefix:
            q = q.where(MarketRow.series_ticker.startswith(series_prefix))
        rows = s.execute(q).all()
    latest: dict[str, datetime] = {}
    for ev, ot in rows:
        if ot is not None and (ev not in latest or ot > latest[ev]):
            latest[ev] = ot
    return sorted(latest, key=lambda e: latest[e], reverse=True)[:limit]


async def event_rows(rest: KalshiRestClient, engine: FairValueEngine, event_ticker: str,
                     now: datetime) -> tuple[EventContext, list[Row]]:
    resp = await rest.get_event(event_ticker)
    ev = resp.event
    infos = tuple(MarketInfo(m.ticker, m.event_ticker, ev.series_ticker, m.strike_type, m.floor_strike,
                             m.cap_strike, m.close_time) for m in resp.markets if m.status == "active")
    quotes = {m.ticker: Quote.from_dollars(m.yes_bid_dollars, m.yes_ask_dollars, m.yes_bid_size_fp,
                                           m.yes_ask_size_fp) for m in resp.markets}
    ctx = EventContext(ev.event_ticker, ev.series_ticker, ev.mutually_exclusive, infos, quotes, now)
    rows: list[Row] = []
    for mi in ctx.sorted_markets():
        per = {fv.model: fv for fv in engine.results(mi, ctx)}
        comb = engine.estimate(mi, ctx)
        gap, side = executable_gap(comb.cents, quotes[mi.ticker]) if comb is not None else (None, None)
        rows.append(Row(mi.ticker, quotes[mi.ticker], per, comb, gap, side))
    return ctx, rows


def _fmt(x: Decimal | None, width: int = 6) -> str:
    return f"{x:{width}.1f}" if x is not None else " " * (width - 1) + "-"


def format_event(ctx: EventContext, rows: list[Row]) -> str:
    lines = [f"{ctx.event_ticker}  ({'exclusive' if ctx.mutually_exclusive else 'not exclusive'}, "
             f"{len(rows)} markets)",
             f"  {'market':<16} {'bid':>5} {'ask':>5} | {'sibling':>13} | {'historical':>13} | "
             f"{'FAIR':>6} {'conf':>5} | {'gap':>6}"]
    total_mid = Decimal(0)
    for r in rows:
        mh = r.quote.mid_and_halfwidth()
        if mh:
            total_mid += mh[0]

        def m(name: str) -> str:
            fv = r.per_model.get(name)
            return f"{fv.cents:6.1f} @{fv.confidence:4.2f}" if fv else f"{'-':>13}"

        fair = r.combined
        lines.append(f"  {r.ticker.split('-')[-1]:<16} {_fmt(r.quote.bid, 5)} {_fmt(r.quote.ask, 5)} | "
                     f"{m('sibling')} | {m('historical')} | "
                     f"{_fmt(fair.cents if fair else None)} {(f'{fair.confidence:5.2f}' if fair else '    -')} | "
                     f"{_fmt(r.gap_cents)}{' ' + r.side if r.side else ''}")
    if ctx.mutually_exclusive:  # only meaningful when exactly one market can win
        lines.append(f"  sum of mids {total_mid:.1f}c (100 if the event is priced coherently)")
    return "\n".join(lines)


async def fair_report(repo: Repository, rest: KalshiRestClient, engine: FairValueEngine, now: datetime,
                      limit: int, series_prefix: str | None = None) -> str:
    events = open_events(repo, now, limit, series_prefix)
    if not events:
        return "no open recorded events"
    blocks: list[str] = []
    gaps: list[Decimal] = []
    estimated = total = 0
    for ev in events:
        ctx, rows = await event_rows(rest, engine, ev, now)
        blocks.append(format_event(ctx, rows))
        total += len(rows)
        estimated += sum(r.combined is not None for r in rows)
        gaps += [r.gap_cents for r in rows if r.gap_cents is not None]
    over = {t: sum(g >= t for g in gaps) for t in (5, 10, 20)}
    blocks.append(f"summary: {len(events)} events, {estimated}/{total} markets with an estimate "
                  f"(confidence >= min); executable gaps >= 5/10/20c: {over[5]}/{over[10]}/{over[20]}\n"
                  "gap = fair - ask - fee (buy YES) or bid - fair - fee (buy NO), the better of the two. "
                  "FEES PROVISIONAL.")
    return "\n\n".join(blocks)
