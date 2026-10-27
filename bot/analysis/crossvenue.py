"""
Kalshi vs Polymarket, sports game markets: was Kalshi ever tradeable against
Polymarket's price, from Kalshi's open until an hour before the game?

Polymarket lists major-league games weeks earlier and trades them heavily,
so its price is a ready-made outside fair value at every hour of a Kalshi
market's pre-game life (e.g. KC@SEA 2026-10-25: Polymarket from Sep 14 at
57-63c; Kalshi opened Oct 13 at 30/75 and converged to it). Per Kalshi side
(each team's market is its own book) and per hourly candle:

    fair  = Polymarket price for that team, as of that hour (hourly history)
    YES   = fair - kalshi_ask - fee(ask)          buy that team on Kalshi
    NO    = kalshi_bid - fair - fee(1 - bid)      buy the other side

Closing bid/ask of each hour are used (conservative: the hour's best ask may
have been lower). Polymarket's hourly price moves ~1-3c hour to hour, so
gaps under ~5c are within its noise. NOTHING IN-PLAY: hours ending later than
PRE_BUFFER before kickoff are excluded (owner rule). Kickoff comes from
Polymarket's gameStartTime and must agree with Kalshi's expiration_time - 48 h.

Read-only, public data on both venues.
"""

from __future__ import annotations

import bisect
import statistics as st
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bot.analysis.census import provisional_taker_fee_cents
from bot.api.models import Candlestick, Market
from bot.api.polymarket import PmMoneyline, PolymarketPublic
from bot.api.rest import KalshiRestClient

HUNDRED = Decimal(100)
PRE_BUFFER = timedelta(hours=1)
KICKOFF_TOLERANCE = timedelta(hours=1)
# Polymarket's own opening is not a reference: ARI@LAC 2026-09-13 listed Aug 4 at 54/46 and moved to 81.5
# within 3 h, to where Kalshi (80/83) had been for weeks. Its first PM_SETTLE after listing is skipped.
PM_SETTLE = timedelta(hours=24)
PM_MAX_AGE = timedelta(hours=3)  # an as-of Polymarket price older than this is stale
CONVERGE_AFTER = timedelta(hours=6)

# league key -> (Kalshi game series, Polymarket tag slug, Polymarket slug prefix)
LEAGUES: dict[str, tuple[str, str, str]] = {
    "nfl": ("KXNFLGAME", "nfl", "nfl"),
}


def split_teams(kalshi_event: str, series: str) -> str:
    """'KXNFLGAME-26OCT25KCSEA' -> 'KCSEA' (date prefix is 7 chars: YYMMMDD)."""
    return kalshi_event.split("-", 1)[1][7:]


def _compatible(kalshi_abbr: str, pm_abbr: str) -> bool:
    a, b = kalshi_abbr.upper(), pm_abbr.upper()
    return a == b or a.startswith(b) or b.startswith(a)


def match_side(market: Market, series: str, games: Sequence[PmMoneyline]) -> tuple[PmMoneyline, int] | None:
    """The Polymarket game and outcome index for this Kalshi team market, or None.
    Kickoff must agree within KICKOFF_TOLERANCE, both teams must match in order."""
    if market.expiration_time is None:
        return None
    kick = market.expiration_time - timedelta(hours=48)
    teams = split_teams(market.event_ticker, series)
    side = market.ticker.rsplit("-", 1)[1]
    hits = []
    for g in games:
        if abs(g.game_start - kick) > KICKOFF_TOLERANCE:
            continue
        for cut in range(1, len(teams)):
            if _compatible(teams[:cut], g.teams[0]) and _compatible(teams[cut:], g.teams[1]):
                idx = 0 if side == teams[:cut] else 1 if side == teams[cut:] else None
                if idx is not None:
                    hits.append((g, idx))
                break
    return hits[0] if len(hits) == 1 else None


@dataclass
class SideResult:
    ticker: str
    pm_slug: str
    kickoff: datetime
    kalshi_open: datetime
    pm_listed_hours_before: float | None  # Polymarket created how long before Kalshi opened
    hours: int = 0  # pre-game Kalshi hours compared
    best_gap: Decimal | None = None
    best_side: str = ""
    best_at_hours_after_open: float = 0.0
    best_ask_bid: tuple[Decimal, Decimal] = (Decimal(0), Decimal(0))
    best_fair: Decimal = Decimal(0)
    hours_ge: dict[int, int] = field(default_factory=dict)
    first_hour_gap: Decimal | None = None
    pre_diff: Decimal | None = None  # Kalshi mid - Polymarket, last pre-game hour (cents)
    # Who was right at the best-gap hour? Prices CONVERGE_AFTER later (both cents), when it is still pre-game.
    kalshi_mid_at: Decimal | None = None
    kalshi_mid_later: Decimal | None = None
    pm_later: Decimal | None = None

    @property
    def converged_to(self) -> str | None:
        """'kalshi->pm' if Kalshi closed >= half the gap toward Polymarket's price, 'pm->kalshi' if
        Polymarket did that toward Kalshi's, 'both'/'neither' otherwise; None if not measurable."""
        if None in (self.kalshi_mid_at, self.kalshi_mid_later, self.pm_later):
            return None
        k0, k1, p0, p1 = self.kalshi_mid_at, self.kalshi_mid_later, self.best_fair, self.pm_later
        d = abs(k0 - p0)  # type: ignore[operator]
        if d == 0:
            return None
        k_moved = abs(k1 - p0) <= d / 2  # type: ignore[operator]
        p_moved = abs(p1 - k0) <= d / 2  # type: ignore[operator]
        return "both" if k_moved and p_moved else "kalshi->pm" if k_moved else "pm->kalshi" if p_moved else "neither"
    result: str | None = None


def compare(market: Market, g: PmMoneyline, idx: int, candles: Sequence[Candlestick],
            pm: Sequence[tuple[int, Decimal]], thresholds: tuple[int, ...] = (5, 10, 20),
            fee: Callable[[Decimal], Decimal] = provisional_taker_fee_cents) -> SideResult:
    assert market.open_time is not None
    r = SideResult(market.ticker, g.event_slug, g.game_start, market.open_time,
                   (market.open_time - g.created).total_seconds() / 3600 if g.created else None,
                   hours_ge={t: 0 for t in thresholds}, result=market.result)
    times = [t for t, _ in pm]
    last_ok = g.game_start - PRE_BUFFER
    for c in sorted(candles, key=lambda c: c.end_period_ts):
        ts = datetime.fromtimestamp(c.end_period_ts, UTC)
        if ts > last_ok:
            break
        i = bisect.bisect_right(times, c.end_period_ts) - 1
        if i < 0 or c.end_period_ts - times[i] > PM_MAX_AGE.total_seconds():
            continue
        if g.created is not None and ts < g.created + PM_SETTLE:
            continue
        fair = pm[i][1] * HUNDRED
        b, a = c.yes_bid.close_dollars, c.yes_ask.close_dollars
        gaps = []
        if a is not None and 0 < a < 1:
            gaps.append(("YES", fair - a * HUNDRED - fee(a)))
        if b is not None and 0 < b < 1:
            gaps.append(("NO", b * HUNDRED - fair - fee(Decimal(1) - b)))
        if not gaps:
            continue
        r.hours += 1
        side, gap = max(gaps, key=lambda x: x[1])
        if r.first_hour_gap is None:
            r.first_hour_gap = gap
        for t in thresholds:
            r.hours_ge[t] += gap >= t
        if r.best_gap is None or gap > r.best_gap:
            r.best_gap, r.best_side = gap, side
            r.best_at_hours_after_open = (ts - market.open_time).total_seconds() / 3600
            r.best_ask_bid = ((a or Decimal(0)) * HUNDRED, (b or Decimal(0)) * HUNDRED)
            r.best_fair = fair
            r.kalshi_mid_at = (a + b) / 2 * HUNDRED if a is not None and b is not None and 0 < b < a < 1 else None
        if a is not None and b is not None and 0 < b < a < 1:
            r.pre_diff = (a + b) / 2 * HUNDRED - fair
    if r.best_gap is not None:
        t_best = market.open_time + timedelta(hours=r.best_at_hours_after_open)
        t_later = t_best + CONVERGE_AFTER
        if t_later <= last_ok:
            later = [c for c in candles if datetime.fromtimestamp(c.end_period_ts, UTC) <= t_later
                     and c.yes_bid.close_dollars and c.yes_ask.close_dollars
                     and 0 < c.yes_bid.close_dollars < c.yes_ask.close_dollars < 1]
            j = bisect.bisect_right(times, int(t_later.timestamp())) - 1
            if later and j >= 0:
                lc = max(later, key=lambda c: c.end_period_ts)
                r.kalshi_mid_later = (lc.yes_bid.close_dollars + lc.yes_ask.close_dollars) / 2 * HUNDRED  # type: ignore[operator]
                r.pm_later = pm[j][1] * HUNDRED
    return r


async def run(rest: KalshiRestClient, pm: PolymarketPublic, league: str, since: datetime,
              progress: Callable[[str], None] | None = None) -> tuple[list[SideResult], dict[str, int]]:
    series, tag, prefix = LEAGUES[league]
    games = await pm.closed_game_moneylines(tag, prefix, since)
    skipped: dict[str, int] = {}
    markets = [m async for m in rest.iter_markets(status="settled", series_ticker=series,
                                                  min_settled_ts=int(since.timestamp()), limit=1000)]
    if progress:
        progress(f"{league}: {len(games)} Polymarket games, {len(markets)} Kalshi markets")
    out: list[SideResult] = []
    for mk in markets:
        hit = match_side(mk, series, games)
        if hit is None or mk.open_time is None:
            skipped["unmatched"] = skipped.get("unmatched", 0) + 1
            continue
        g, idx = hit
        op, ko = int(mk.open_time.timestamp()), int(g.game_start.timestamp())
        candles = await rest.get_candlesticks(series, mk.ticker, op, ko, 60)
        hist = await pm.price_history(g.token_ids[idx], op - 6 * 3600, ko, 60)
        if not hist:
            skipped["no polymarket history"] = skipped.get("no polymarket history", 0) + 1
            continue
        out.append(compare(mk, g, idx, candles, hist))
    return out, skipped


def _p(xs: Sequence[Decimal], q: float) -> Decimal:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def format_report(rs: Sequence[SideResult], skipped: dict[str, int], league: str) -> str:
    scored = [r for r in rs if r.best_gap is not None]
    lines = [f"{league.upper()}: {len(scored)} Kalshi team markets compared with Polymarket, every pre-game hour "
             f"(nothing within {PRE_BUFFER} of kickoff).",
             "gap = cents per contract after Kalshi's provisional fee, trading Kalshi against Polymarket's price "
             "for the same team.", ""]
    if not scored:
        return "\n".join(lines + ["nothing compared; skipped: " + str(skipped)])
    best = [r.best_gap for r in scored]  # type: ignore[misc]
    lead = [r.pm_listed_hours_before / 24 for r in scored if r.pm_listed_hours_before is not None]
    hrs = sum(r.hours for r in scored)
    lines += [
        f"Polymarket listed the game a median {st.median(lead):.1f} days before Kalshi opened it." if lead else "",
        f"best gap per market:  p50 {_p(best, .5):.1f}  p90 {_p(best, .9):.1f}  max {max(best):.1f}",
        f"markets with any hour >= 5c: {sum(r.hours_ge[5] > 0 for r in scored)}   >= 10c: "
        f"{sum(r.hours_ge[10] > 0 for r in scored)}   >= 20c: {sum(r.hours_ge[20] > 0 for r in scored)}",
        f"share of all {hrs} market-hours with a gap >= 5c: {100 * sum(r.hours_ge[5] for r in scored) / hrs:.2f}%"
        f"   >= 10c: {100 * sum(r.hours_ge[10] for r in scored) / hrs:.2f}%",
        f"first Kalshi hour, gap: p50 {_p([r.first_hour_gap for r in scored if r.first_hour_gap is not None], .5):.1f}"
        f"  max {max(r.first_hour_gap for r in scored if r.first_hour_gap is not None):.1f}",
    ]
    diffs = [abs(r.pre_diff) for r in scored if r.pre_diff is not None]
    if diffs:
        lines.append(f"|Kalshi mid - Polymarket| in the last pre-game hour: p50 {_p(diffs, .5):.1f}  "
                     f"p90 {_p(diffs, .9):.1f}  max {max(diffs):.1f}")
    big = [r for r in scored if r.best_gap is not None and r.best_gap >= 10 and r.converged_to is not None]
    if big:
        from collections import Counter
        cnt = Counter(r.converged_to for r in big)
        lines.append(f"who moved in the {CONVERGE_AFTER} after a >= 10c gap ({len(big)} markets): "
                     + ", ".join(f"{k} {v}" for k, v in cnt.most_common()))
    lines += ["", "largest gaps:",
              f"  {'kalshi ticker':<34}{'gap':>6}{'side':>5}{'ask':>6}{'bid':>6}{'pm':>6}{'h after open':>13}"
              f"{'h>=5c':>6}{'+6h k/pm':>11}{'res':>5}"]
    for r in sorted(scored, key=lambda r: -(r.best_gap or 0))[:15]:
        lines.append(f"  {r.ticker:<34}{r.best_gap:>6.1f}{r.best_side:>5}{r.best_ask_bid[0]:>6.0f}"
                     f"{r.best_ask_bid[1]:>6.0f}{r.best_fair:>6.1f}{r.best_at_hours_after_open:>13.0f}"
                     f"{r.hours_ge[5]:>6}"
                     f"{(f'{r.kalshi_mid_later:.0f}/{r.pm_later:.0f}' if r.pm_later is not None and r.kalshi_mid_later is not None else '-'):>11}"
                     f"{(r.result or '-'):>5}")
    lines.append("skipped: " + (", ".join(f"{k} {v}" for k, v in skipped.items()) or "none"))
    lines.append("Hourly closes only (an hour's best price may have been better); Polymarket hourly price is a last/mid, "
                 "not a book; FEES PROVISIONAL.")
    return "\n".join(lines)
