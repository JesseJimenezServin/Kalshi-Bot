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
import json
import re
import statistics as st
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from bot.analysis.census import provisional_taker_fee_cents
from bot.api.models import Candlestick, Market, Trade
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

# league -> (Kalshi game series, Polymarket tag, Polymarket slug prefix, kickoff method)
#   rules48     scheduled start = expiration_time - 48 h (the rules' postponement clause)
#   ticker_et   start time is in the ticker as YYMMMDDHHMM, US Eastern (MLB)
#   exp_2h      expected_expiration_time - 2 h, loose (soccer: no start field, ~2 h matches)
LEAGUES: dict[str, tuple[str, str, str, str]] = {
    "nfl": ("KXNFLGAME", "nfl", "nfl", "rules48"),
    "cfb": ("KXNCAAFGAME", "cfb", "cfb", "rules48"),
    "nba": ("KXNBAGAME", "nba", "nba", "rules48"),
    "nhl": ("KXNHLGAME", "nhl", "nhl", "rules48"),
    "mlb": ("KXMLBGAME", "mlb", "mlb", "ticker_et"),
    "epl": ("KXEPLGAME", "epl", "epl", "exp_2h"),
    "laliga": ("KXLALIGAGAME", "la-liga", "lal", "exp_2h"),
    "seriea": ("KXSERIEAGAME", "sea", "sea", "exp_2h"),
    "bundesliga": ("KXBUNDESLIGAGAME", "bundesliga", "bun", "exp_2h"),
    "ligue1": ("KXLIGUE1GAME", "ligue-1", "fl1", "exp_2h"),
    "ucl": ("KXUCLGAME", "ucl", "ucl", "exp_2h"),
    "uel": ("KXUELGAME", "uel", "uel", "exp_2h"),
    "mls": ("KXMLSGAME", "mls", "mls", "exp_2h"),
}
TOLERANCE = {"rules48": timedelta(hours=1), "ticker_et": timedelta(hours=1), "exp_2h": timedelta(hours=3)}
_DATE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})(\d{4})?")


def kalshi_kickoff(market: Market, method: str) -> datetime | None:
    if method == "rules48":
        return market.expiration_time - timedelta(hours=48) if market.expiration_time else None
    if method == "ticker_et":
        mo = _DATE.match(market.event_ticker.split("-", 1)[1])
        if not mo or not mo.group(2):
            return None
        local = datetime.strptime(mo.group(1) + mo.group(2), "%y%b%d%H%M").replace(tzinfo=ZoneInfo("America/New_York"))
        return local.astimezone(UTC)
    if method == "exp_2h":
        return market.expected_expiration_time - timedelta(hours=2) if market.expected_expiration_time else None
    raise ValueError(method)


def kalshi_teams(market: Market) -> tuple[str, str]:
    """(teams string, side): 'KXNHLGAME-26OCT26TORCGY-CGY' -> ('TORCGY', 'CGY'); a draw market's side is 'TIE'."""
    rest = market.event_ticker.split("-", 1)[1]
    mo = _DATE.match(rest)
    return (rest[mo.end():] if mo else rest), market.ticker.rsplit("-", 1)[1]


def event_teams(markets: Sequence[Market]) -> dict[str, tuple[str, str]]:
    """event -> (first team, second team) abbreviations, from the team markets' own tickers."""
    sides: dict[str, set[str]] = {}
    teams_of: dict[str, str] = {}
    for m in markets:
        teams, side = kalshi_teams(m)
        teams_of[m.event_ticker] = teams
        if side != "TIE":
            sides.setdefault(m.event_ticker, set()).add(side)
    out: dict[str, tuple[str, str]] = {}
    for ev, ss in sides.items():
        t = teams_of[ev]
        for x in ss:
            if t.startswith(x) and t[len(x):]:
                out[ev] = (x, t[len(x):])
                break
            if t.endswith(x) and t[:-len(x)]:
                out[ev] = (t[:-len(x)], x)
                break
    return out


def _candidates(kick: datetime, method: str, games: Sequence[PmMoneyline]) -> list[PmMoneyline]:
    return [g for g in games if abs(g.game_start - kick) <= TOLERANCE[method]]


def learn_abbreviations(markets: Sequence[Market], league: str, games: Sequence[PmMoneyline],
                        passes: int = 5) -> dict[str, str]:
    """Kalshi abbreviation -> Polymarket abbreviation, learned from games that kick off together where the
    OTHER team's abbreviation matches (LA@VGK vs lak-las is learned from LA vs lak-tor etc.), and from games
    that are the only Polymarket game in their kickoff window (both teams vote). Repeated: each learned code
    makes the next pass's matches possible (Bundesliga: almost no code agrees, BMU/bay, SCF/fre, BVB/dor).
    A pair needs >= 2 votes and >= 75% of that abbreviation's votes; otherwise identity."""
    method = LEAGUES[league][3]
    teams = event_teams(markets)
    slots: list[tuple[str, str, list[PmMoneyline]]] = []
    seen: set[str] = set()
    for m in markets:
        if m.event_ticker in seen or m.event_ticker not in teams:
            continue
        seen.add(m.event_ticker)
        kick = kalshi_kickoff(m, method)
        if kick is not None:
            slots.append((*teams[m.event_ticker], _candidates(kick, method, games)))
    out: dict[str, str] = {}
    for _ in range(passes):
        votes: dict[str, dict[str, int]] = {}

        def vote(k: str, v: str) -> None:
            votes.setdefault(k, {}).setdefault(v, 0)
            votes[k][v] += 1

        for a, h, cands in slots:
            ma, mh = out.get(a, a.lower()), out.get(h, h.lower())
            if len(cands) == 1:
                vote(a, cands[0].teams[0])
                vote(h, cands[0].teams[1])
                continue
            for g in cands:
                p1, p2 = g.teams
                if ma == p1:
                    vote(h, p2)
                if mh == p2:
                    vote(a, p1)
        new: dict[str, str] = {}
        for k, vs in votes.items():
            top, n = max(vs.items(), key=lambda kv: kv[1])
            if n >= 2 and n / sum(vs.values()) >= 0.75:
                new[k] = top
        if new == out:
            break
        out = new
    return out


def match_side(market: Market, league: str, games: Sequence[PmMoneyline],
               teams: dict[str, tuple[str, str]] | None = None,
               abbr: dict[str, str] | None = None) -> tuple[PmMoneyline, int] | None:
    """The Polymarket game and outcome index for this Kalshi market, or None. Kickoff must agree and BOTH
    teams must match exactly, in order, after the learned abbreviation map."""
    method = LEAGUES[league][3]
    kick = kalshi_kickoff(market, method)
    pair = teams.get(market.event_ticker) if teams is not None else None
    if pair is None:
        t, side = kalshi_teams(market)
        if side == "TIE":
            return None
        pair = (side, t[len(side):]) if t.startswith(side) else (t[:-len(side)], side) if t.endswith(side) else None
    if kick is None or pair is None:
        return None
    _, side = kalshi_teams(market)
    amap = abbr or {}
    want = (amap.get(pair[0], pair[0].lower()), amap.get(pair[1], pair[1].lower()))
    hits = [g for g in _candidates(kick, method, games) if g.teams == want]
    if len(hits) != 1:
        return None
    g = hits[0]
    idx = 2 if side == "TIE" else 0 if side == pair[0] else 1
    return (g, idx) if idx < len(g.token_ids) else None


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
    # Real prints (GET /markets/trades) during the pre-game life, only for markets with a >= TRADE_GAP hour:
    # contracts that changed hands at a price >= TRADE_GAP better than Polymarket's as-of price (either side),
    # and the edge on them. An upper bound on what one trader could have had: others competed for these.
    trades_seen: int | None = None
    trade_contracts: Decimal = Decimal(0)
    trade_edge_dollars: Decimal = Decimal(0)


TRADE_GAP = 10
PLACEHOLDER_BAND = (Decimal("0.49"), Decimal("0.51"))
PLACEHOLDER_RUN = 12


def _placeholder(pm: Sequence[tuple[int, Decimal]], i: int) -> bool:
    """Polymarket sits at ~50c before anyone trades a game (Howard @ Indiana: 50c for 211 h vs Kalshi
    99/100). A price in 49-51c that has not left that band for the previous PLACEHOLDER_RUN points
    is treated as no price. A genuine coin-flip that never moves is lost too; that is the cost."""
    lo, hi = PLACEHOLDER_BAND
    window = pm[max(0, i - PLACEHOLDER_RUN): i + 1]
    # Early in a history (fewer than PLACEHOLDER_RUN points yet) a market that has only ever sat at
    # ~50c is untraded too: Howard @ Indiana's history began at 50c and the filter missed it.
    return (len(window) > PLACEHOLDER_RUN or window[0] == pm[0]) and all(lo <= p <= hi for _, p in window)


def fair_at(g: PmMoneyline, pm: Sequence[tuple[int, Decimal]], ts: int) -> Decimal | None:
    """Polymarket's as-of price in cents at unix ts, with compare()'s filters (listing settle, staleness,
    placeholder), or None."""
    i = bisect.bisect_right([t for t, _ in pm], ts) - 1
    if i < 0 or ts - pm[i][0] > PM_MAX_AGE.total_seconds():
        return None
    if g.created is not None and datetime.fromtimestamp(ts, UTC) < g.created + PM_SETTLE:
        return None
    if _placeholder(pm, i):
        return None
    return pm[i][1] * HUNDRED


def score_trades(r: SideResult, g: PmMoneyline, trades: Sequence[Trade], pm: Sequence[tuple[int, Decimal]],
                 fee: Callable[[Decimal], Decimal] = provisional_taker_fee_cents) -> None:
    """Fill r.trades_* from the market's prints: each pre-game print at yes price p is a contract one could
    have bought YES at p (edge fair - p - fee) or NO at 1 - p (edge p - fair - fee)."""
    last_ok = g.game_start - PRE_BUFFER
    r.trades_seen = 0
    for t in trades:
        if t.created_time > last_ok:
            continue
        r.trades_seen += 1
        fair = fair_at(g, pm, int(t.created_time.timestamp()))
        if fair is None:
            continue
        p = t.yes_price_dollars
        edge = max(fair - p * HUNDRED - fee(p), p * HUNDRED - fair - fee(Decimal(1) - p))
        if edge >= TRADE_GAP:
            r.trade_contracts += t.count_fp
            r.trade_edge_dollars += t.count_fp * edge / HUNDRED


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
        if _placeholder(pm, i):
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
    series, tag, prefix, _ = LEAGUES[league]
    games = await pm.closed_game_moneylines(tag, prefix, since)
    skipped: dict[str, int] = {}
    markets = [m async for m in rest.iter_markets(status="settled", series_ticker=series,
                                                  min_settled_ts=int(since.timestamp()), limit=1000)]
    if progress:
        progress(f"{league}: {len(games)} Polymarket games, {len(markets)} Kalshi markets")
    teams = event_teams(markets)
    abbr = learn_abbreviations(markets, league, games)
    if progress:
        progress(f"{league}: learned {sum(k.lower() != v for k, v in abbr.items())} abbreviation differences, "
                 f"e.g. {dict(list((k, v) for k, v in abbr.items() if k.lower() != v)[:6])}")
    out: list[SideResult] = []
    for mk in markets:
        hit = match_side(mk, league, games, teams, abbr)
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
        r = compare(mk, g, idx, candles, hist)
        if r.hours_ge.get(TRADE_GAP, 0) > 0:
            seen: dict[str, Trade] = {}
            for it in (rest.iter_historical_trades, rest.iter_trades):  # the tiers overlap: dedupe by id
                async for t in it(ticker=mk.ticker, min_ts=op, max_ts=ko):
                    seen[t.trade_id] = t
            trades = list(seen.values())
            score_trades(r, g, trades, hist)
        out.append(r)
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
    tr = [r for r in scored if r.trades_seen is not None]
    if tr:
        lines.append(f"real Kalshi prints >= {TRADE_GAP}c better than Polymarket, pre-game, in the {len(tr)} markets "
                     f"with such an hour: {sum(r.trade_contracts for r in tr):.0f} contracts, "
                     f"${sum(r.trade_edge_dollars for r in tr):.2f} of edge in total "
                     f"(of {sum(r.trades_seen or 0 for r in tr)} pre-game prints)")
    lines += ["", "largest gaps:",
              f"  {'kalshi ticker':<34}{'gap':>6}{'side':>5}{'ask':>6}{'bid':>6}{'pm':>6}{'h after open':>13}"
              f"{'h>=5c':>6}{'+6h k/pm':>11}{'res':>5}{'ctr>=10c':>10}{'edge$':>8}"]
    for r in sorted(scored, key=lambda r: -(r.best_gap or 0))[:15]:
        lines.append(f"  {r.ticker:<34}{r.best_gap:>6.1f}{r.best_side:>5}{r.best_ask_bid[0]:>6.0f}"
                     f"{r.best_ask_bid[1]:>6.0f}{r.best_fair:>6.1f}{r.best_at_hours_after_open:>13.0f}"
                     f"{r.hours_ge[5]:>6}"
                     f"{(f'{r.kalshi_mid_later:.0f}/{r.pm_later:.0f}' if r.pm_later is not None and r.kalshi_mid_later is not None else '-'):>11}"
                     f"{(r.result or '-'):>5}"
                     + (f"{r.trade_contracts:>10.0f}{r.trade_edge_dollars:>8.2f}" if r.trades_seen is not None else ""))
    lines.append("skipped: " + (", ".join(f"{k} {v}" for k, v in skipped.items()) or "none"))
    lines.append("Hourly closes only (an hour's best price may have been better); Polymarket hourly price is a last/mid, "
                 "not a book; FEES PROVISIONAL.")
    return "\n".join(lines)


# ------------------------------------------------------------------ futures

# name -> (Kalshi series, Kalshi event, Polymarket event slug). Season futures: each team's market,
# every hour of its life so far. CAVEAT: these trade through the season, so some hours fall during that
# team's games; an hour where either venue's price is > PM_MAX_AGE stale is skipped, nothing more.
FUTURES: dict[str, tuple[str, str, str]] = {
    "nfl-2027": ("KXSB", "KXSB-27", "pro-football-2027-champion-20260729185915366"),
    "nba-2027": ("KXNBA", "KXNBA-27", "nba-2027-champion"),
    "nhl-2027": ("KXNHL", "KXNHL-27", "nhl-2027-champion-20260612185656162"),
}
KALSHI_CANDLE_SPAN = 30 * 86400


def match_future(kalshi_name: str, pm_titles: Sequence[str]) -> int | None:
    """'Los Angeles R' -> 'Los Angeles Rams', 'Arizona' -> 'Arizona Cardinals': unique prefix match."""
    k = kalshi_name.lower().strip()
    hits = [i for i, t in enumerate(pm_titles) if t.lower().startswith(k)]
    return hits[0] if len(hits) == 1 else None


async def run_future(rest: KalshiRestClient, pm: PolymarketPublic, name: str, now: datetime,
                     progress: Callable[[str], None] | None = None) -> tuple[list[SideResult], dict[str, int]]:
    from bot.api.polymarket import _dt

    series, event, slug = FUTURES[name]
    ev = (await pm._get("https://gamma-api.polymarket.com/events", {"slug": slug}))[0]
    pm_mkts = [m for m in ev["markets"] if m.get("groupItemTitle")]
    titles = [m["groupItemTitle"] for m in pm_mkts]
    kms = [m async for m in rest.iter_markets(event_ticker=event, limit=1000)]
    if progress:
        progress(f"{name}: {len(kms)} Kalshi teams, {len(pm_mkts)} Polymarket teams")
    out: list[SideResult] = []
    skipped: dict[str, int] = {}
    for mk in kms:
        i = match_future(mk.yes_sub_title or "", titles)
        if i is None or mk.open_time is None:
            skipped["unmatched"] = skipped.get("unmatched", 0) + 1
            continue
        pmm = pm_mkts[i]
        tok = json.loads(pmm["clobTokenIds"])[0]
        g = PmMoneyline(slug, ("", ""), (titles[i],), (tok,), now + PRE_BUFFER, _dt(pmm.get("createdAt")), Decimal(0))
        a, end = int(mk.open_time.timestamp()), int(now.timestamp())
        candles: list[Candlestick] = []
        t = a
        while t < end:
            candles += await rest.get_candlesticks(series, mk.ticker, t, min(end, t + KALSHI_CANDLE_SPAN), 60)
            t += KALSHI_CANDLE_SPAN
        hist = await pm.price_history(tok, a - 6 * 3600, end, 60)
        if not hist:
            skipped["no polymarket history"] = skipped.get("no polymarket history", 0) + 1
            continue
        out.append(compare(mk, g, 0, candles, hist))
    return out, skipped
