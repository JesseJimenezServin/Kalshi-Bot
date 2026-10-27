"""
Total move, open to pre-event (owner's goal, 2026-10-26): how far does a
market's opening price sit from where it trades just before the event starts?

A game a month out that opens mispriced is the target. The first hour is not
the question, and NOTHING DURING THE LIVE EVENT COUNTS (owner rule: never trade
live events), so the reference is the last price BEFORE the event starts.

Per settled market, from Kalshi's public hourly candlesticks (no recording):
  open      first hour whose closing book is informative (bid > 0, ask < 1,
            spread <= MAX_SPREAD); its bid/ask is the executable opening price
  pre       last informative hour ending >= PRE_BUFFER before the event starts
  move      pre mid - open mid (signed)
  gap       hindsight edge of trading at the open and exiting at the pre mid:
              YES: pre_mid - open_ask - fee(open_ask)
              NO:  open_bid - pre_mid - fee(1 - open_bid)
            best = the better side, chosen in HINDSIGHT. It measures how much
            movement was there to catch, not a rule anyone could follow.

Event start is not an API field. Sports games: rules say a game "postponed
but begins within 48 hours" stays open and expiration_time sits exactly 48 h
after the scheduled start (verified on KXNFLGAME-26OCT25KCSEA), so start =
expiration_time - 48 h when the rules carry that clause. Each such market also
reports the in-play volume jump (3 h after start / 3 h before) so a series where
the rule is wrong shows up. Other categories: start = close_time (most close
before the outcome is known; flagged per row). Sports markets without the
clause are skipped, never guessed.

OUTCOME ALREADY KNOWN counts as live too. A "will X happen by <date>" market
closes early when X happens, so its last price before close is the news, not
a pre-event price. If the price ends pinned (bid >= 95c or ask <= 5c) through
to the end, the reference is taken from before that run began, searching daily
candles over the market's life when the run covers the whole last 3 days.
Such rows are flagged; a favourite legitimately trading at 96c pre-event is
also cut this way, which only shortens the measured move.

Unsigned public requests only, live tier only (markets settled after
GET /historical/cutoff).
"""

from __future__ import annotations

import random
import re
import statistics as st
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bot.analysis.census import provisional_taker_fee_cents
from bot.api.models import Candlestick, Market
from bot.api.rest import KalshiRestClient

HUNDRED = Decimal(100)
MAX_SPREAD = Decimal("0.20")
PRE_BUFFER = timedelta(hours=1)
WINDOW = timedelta(days=3)  # candles fetched after open and before start
MIN_LEAD = timedelta(hours=24)
LEAD_BUCKETS = ((1, "1-3 d"), (3, "3-14 d"), (14, "14-60 d"), (60, "60 d+"))
_RULE_48H = re.compile(r"within\s+48\s+hours", re.I)


def estimate_start(m: Market, category: str) -> tuple[datetime | None, str]:
    """(event start, method). None = unknown: skip the market."""
    rules = f"{m.rules_primary or ''} {m.rules_secondary or ''}"
    if m.expiration_time is not None and _RULE_48H.search(rules):
        start = m.expiration_time - timedelta(hours=48)
        if m.open_time is not None and m.close_time is not None and m.open_time < start <= m.close_time:
            return start, "rules48"
        return None, "rules48-implausible"
    if category == "Sports":
        return None, "sports-unknown"
    return (m.close_time, "close") if m.close_time is not None else (None, "no-close")


def informative(c: Candlestick) -> bool:
    b, a = c.yes_bid.close_dollars, c.yes_ask.close_dollars
    return b is not None and a is not None and b > 0 and a < 1 and a - b <= MAX_SPREAD


def _ts(c: Candlestick) -> datetime:
    return datetime.fromtimestamp(c.end_period_ts, UTC)


EXTREME_BID = Decimal("0.95")
EXTREME_ASK = Decimal("0.05")


def _extreme(c: Candlestick) -> bool:
    b, a = c.yes_bid.close_dollars, c.yes_ask.close_dollars
    return (b is not None and b >= EXTREME_BID) or (a is not None and 0 < a <= EXTREME_ASK)


def pinned_from(cs: Sequence[Candlestick]) -> datetime | None:
    """Start of the trailing run of pinned books (outcome effectively known), or None."""
    run: datetime | None = None
    for c in sorted(cs, key=lambda c: c.end_period_ts, reverse=True):
        if not _extreme(c):
            break
        run = _ts(c)
    return run


def needs_daily(late: Sequence[Candlestick]) -> bool:
    """True when the hourly pre-event window has no unpinned book to use."""
    p = pinned_from(late)
    return not late or (p is not None and p <= min(_ts(c) for c in late))


@dataclass
class TotalMove:
    ticker: str
    series: str
    category: str
    start_method: str
    open_time: datetime
    start: datetime
    lead_days: float
    hours_to_informative: float
    open_bid: Decimal  # cents
    open_ask: Decimal
    pre_bid: Decimal
    pre_ask: Decimal
    open_day_volume: Decimal
    inplay_jump: float | None  # volume 3 h after start / 3 h before (rules48 only)
    result: str | None
    resolved_early: bool = False  # the pre price was taken before a pinned (outcome-known) run
    yes_gap: Decimal = Decimal(0)
    no_gap: Decimal = Decimal(0)

    @property
    def open_mid(self) -> Decimal:
        return (self.open_bid + self.open_ask) / 2

    @property
    def pre_mid(self) -> Decimal:
        return (self.pre_bid + self.pre_ask) / 2

    @property
    def move(self) -> Decimal:
        return self.pre_mid - self.open_mid

    @property
    def best_gap(self) -> Decimal:
        return max(self.yes_gap, self.no_gap)

    @property
    def best_side(self) -> str:
        return "YES" if self.yes_gap >= self.no_gap else "NO"


def measure(m: Market, series: str, category: str, start: datetime, method: str, early: Sequence[Candlestick],
            late: Sequence[Candlestick], daily: Sequence[Candlestick] = (),
            fee: Callable[[Decimal], Decimal] = provisional_taker_fee_cents) -> TotalMove | None:
    """`daily`: life-of-market daily candles, only needed when needs_daily(late)."""
    assert m.open_time is not None
    op = next((c for c in sorted(early, key=lambda c: c.end_period_ts) if informative(c)), None)
    if op is None:
        return None
    limit = start - PRE_BUFFER
    pin = pinned_from([c for c in late if _ts(c) <= limit])
    resolved_early = False
    if needs_daily([c for c in late if _ts(c) <= limit]):
        pin_d = pinned_from([c for c in daily if _ts(c) <= limit])
        if pin_d is None and not late:
            pool = [c for c in daily if _ts(c) <= limit]
        elif pin_d is None:
            return None
        else:
            resolved_early = True
            pool = [c for c in daily if _ts(c) <= pin_d - timedelta(hours=24)]
    else:
        if pin is not None:
            resolved_early = True
            limit = min(limit, pin - PRE_BUFFER)
        pool = [c for c in late if _ts(c) <= limit]
    before = [c for c in pool if informative(c)]
    if not before:
        return None
    pre = max(before, key=lambda c: c.end_period_ts)
    if _ts(pre) <= _ts(op):
        return None
    day = [c for c in early if _ts(c) <= m.open_time + timedelta(hours=24)]
    jump = None
    if method == "rules48":
        vb = sum((c.volume_fp for c in late if start - timedelta(hours=3) < _ts(c) <= start), Decimal(0))
        va = sum((c.volume_fp for c in late if start < _ts(c) <= start + timedelta(hours=3)), Decimal(0))
        jump = float(va / vb) if vb > 0 else (float("inf") if va > 0 else None)
    ob, oa = op.yes_bid.close_dollars, op.yes_ask.close_dollars
    pb, pa = pre.yes_bid.close_dollars, pre.yes_ask.close_dollars
    assert ob is not None and oa is not None and pb is not None and pa is not None
    tm = TotalMove(m.ticker, series, category, method, m.open_time, start,
                   (_ts(pre) - m.open_time).total_seconds() / 86400,
                   (_ts(op) - m.open_time).total_seconds() / 3600,
                   ob * HUNDRED, oa * HUNDRED, pb * HUNDRED, pa * HUNDRED,
                   sum((c.volume_fp for c in day), Decimal(0)), jump, m.result, resolved_early)
    tm.yes_gap = tm.pre_mid - tm.open_ask - fee(oa)
    tm.no_gap = tm.open_bid - tm.pre_mid - fee(Decimal(1) - ob)
    return tm


@dataclass
class Study:
    moves: list[TotalMove] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def pick_markets(markets: Sequence[Market], rnd: random.Random, per_event: int = 3) -> list[Market]:
    """One market per two-outcome event (the sides mirror each other), up to
    `per_event` otherwise."""
    by_event: dict[str, list[Market]] = {}
    for mk in markets:
        by_event.setdefault(mk.event_ticker, []).append(mk)
    out: list[Market] = []
    for ev in sorted(by_event):
        ms = sorted(by_event[ev], key=lambda x: x.ticker)
        out += rnd.sample(ms, 1 if len(ms) == 2 else min(per_event, len(ms)))
    return out


async def study(rest: KalshiRestClient, categories: Sequence[str], now: datetime, *, days: int = 60,
                series_per_category: int = 400, markets_per_category: int = 700, events_per_series: int = 10,
                min_volume: Decimal = Decimal(500), seed: int = 7,
                progress: Callable[[str], None] | None = None) -> Study:
    rnd = random.Random(seed)
    cutoff = (await rest.get_historical_cutoff()).market_settled_ts
    since = max(now - timedelta(days=days), cutoff)
    res = Study()
    for cat in categories:
        series = sorted(s.ticker for s in await rest.get_series_list(category=cat))
        pool: list[tuple[str, Market]] = []
        for s in rnd.sample(series, min(series_per_category, len(series))):
            page = await rest.get_markets(status="settled", series_ticker=s, min_settled_ts=int(since.timestamp()),
                                          limit=1000, mve_filter="exclude")
            ok = []
            for mk in page.markets:
                if mk.open_time is None or (mk.volume_fp or 0) < min_volume:
                    continue
                start, method = estimate_start(mk, cat)
                if start is None:
                    res.skip(method)
                    continue
                if start - mk.open_time < MIN_LEAD:
                    res.skip("lead<24h")
                    continue
                ok.append(mk)
            chosen = pick_markets(ok, rnd)
            evs = sorted({x.event_ticker for x in chosen})
            keep = set(rnd.sample(evs, min(events_per_series, len(evs))))
            pool += [(s, x) for x in chosen if x.event_ticker in keep]
        sample = rnd.sample(pool, min(markets_per_category, len(pool)))
        if progress:
            progress(f"{cat}: {len(series)} series, {len(pool)} eligible markets, measuring {len(sample)}")
        for s, mk in sample:
            assert mk.open_time is not None
            start, method = estimate_start(mk, cat)
            assert start is not None
            op = int(mk.open_time.timestamp())
            st_ = int(start.timestamp())
            early = await rest.get_candlesticks(s, mk.ticker, op, min(op + int(WINDOW.total_seconds()), st_), 60)
            late = await rest.get_candlesticks(s, mk.ticker, max(op, st_ - int(WINDOW.total_seconds())),
                                               st_ + 3 * 3600, 60)
            daily: list[Candlestick] = []
            if needs_daily([c for c in late if _ts(c) <= start - PRE_BUFFER]):
                daily = await rest.get_candlesticks(s, mk.ticker, op, st_, 1440)
            tm = measure(mk, s, cat, start, method, early, late, daily)
            if tm is None:
                res.skip("no informative open/pre book")
            else:
                res.moves.append(tm)
    return res


# ------------------------------------------------------------------ report

def _bucket(lead_days: float) -> str:
    name = LEAD_BUCKETS[0][1]
    for lo, n in LEAD_BUCKETS:
        if lead_days >= lo:
            name = n
    return name


def _p(xs: Sequence[Decimal], q: float) -> Decimal:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def format_study(res: Study) -> str:
    ms = res.moves
    lines = ["Cents. move = pre-event mid - opening mid. gap = hindsight edge of trading at the opening bid/ask and",
             "exiting at the pre-event mid, after the provisional fee, on the better side. Nothing in-play counts.", ""]
    hdr = (f"{'category':<22}{'lead':<10}{'n':>5}{'|move| p50':>11}{'p75':>6}{'p90':>6}{'gap>=5':>8}{'>=10':>6}"
           f"{'>=20':>6}{'open spread':>12}")
    lines.append(hdr)
    for cat in sorted({m.category for m in ms}):
        for _, b in LEAD_BUCKETS:
            g = [m for m in ms if m.category == cat and _bucket(m.lead_days) == b]
            if not g:
                continue
            mv = [abs(m.move) for m in g]
            n = len(g)
            lines.append(f"{cat:<22}{b:<10}{n:>5}{_p(mv, .5):>11.1f}{_p(mv, .75):>6.1f}{_p(mv, .9):>6.1f}"
                         f"{100 * sum(m.best_gap >= 5 for m in g) / n:>7.0f}%{100 * sum(m.best_gap >= 10 for m in g) / n:>5.0f}%"
                         f"{100 * sum(m.best_gap >= 20 for m in g) / n:>5.0f}%"
                         f"{st.median(m.open_ask - m.open_bid for m in g):>12.1f}")
    by_series: dict[str, list[TotalMove]] = {}
    for m in ms:
        by_series.setdefault(f"{m.category} | {m.series}", []).append(m)
    big = [(k, v) for k, v in by_series.items() if len(v) >= 5]
    lines += ["", "series with >= 5 markets, by median |move|:",
              f"  {'series':<46}{'n':>4}{'lead d':>7}{'|move| p50':>11}{'gap>=10':>8}{'open spr':>9}{'inplay x':>9}"]
    for k, v in sorted(big, key=lambda kv: -st.median(abs(m.move) for m in kv[1]))[:25]:
        jumps = [m.inplay_jump for m in v if m.inplay_jump is not None and m.inplay_jump != float("inf")]
        lines.append(f"  {k[:45]:<46}{len(v):>4}{st.median(m.lead_days for m in v):>7.1f}"
                     f"{st.median(abs(m.move) for m in v):>11.1f}{100 * sum(m.best_gap >= 10 for m in v) / len(v):>7.0f}%"
                     f"{st.median(m.open_ask - m.open_bid for m in v):>9.1f}"
                     f"{(f'{st.median(jumps):9.1f}' if jumps else f'{chr(45):>9}')}")
    lines += ["", "largest hindsight gaps:",
              f"  {'ticker':<46}{'lead d':>7}{'open b/a':>12}{'pre b/a':>12}{'side':>5}{'gap':>6}{'day vol':>9}{'res':>5}"]
    for m in sorted(ms, key=lambda m: -m.best_gap)[:20]:
        lines.append(f"  {m.ticker[:45]:<46}{m.lead_days:>7.1f}{m.open_bid:>6.0f}/{m.open_ask:<5.0f}"
                     f"{m.pre_bid:>6.0f}/{m.pre_ask:<5.0f}{m.best_side:>5}{m.best_gap:>6.1f}{m.open_day_volume:>9.0f}"
                     f"{(m.result or '-'):>5}")
    jumps = [m.inplay_jump for m in ms if m.start_method == "rules48" and m.inplay_jump is not None]
    if jumps:
        ok = sum(j >= 2 for j in jumps)
        lines.append(f"\nstart-time check (Sports, start = expiration - 48 h): volume rose >= 2x in the 3 h after the "
                     f"estimated start for {ok}/{len(jumps)} markets.")
    lines.append(f"pre price taken before an outcome-known (pinned) run: {sum(m.resolved_early for m in ms)}/{len(ms)}")
    lines.append("skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(res.skipped.items(), key=lambda kv: -kv[1])))
    lines.append("HINDSIGHT and PROVISIONAL FEES. Candles carry no depth: 'day vol' (contracts traded in the first 24 h)"
                 " is the only size proxy.")
    return "\n".join(lines)
