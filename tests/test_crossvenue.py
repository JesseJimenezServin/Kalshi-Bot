"""Kalshi vs Polymarket: matching games, pre-game-only gaps, Polymarket's own opening skipped, who converged."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from bot.analysis.crossvenue import compare, match_side
from bot.api.models import Candlestick, Market
from bot.api.polymarket import PmMoneyline, parse_moneyline

KICK = datetime(2026, 10, 26, 0, 20, tzinfo=UTC)
OPEN = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)
GAME = PmMoneyline("nfl-kc-sea-2026-10-26", ("kc", "sea"), ("Chiefs", "Seahawks"), ("tKC", "tSEA"), KICK,
                   OPEN - timedelta(days=30), D(6_000_000))


def mk(ticker: str = "KXNFLGAME-26OCT25KCSEA-SEA") -> Market:
    return Market.model_validate({"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "status": "finalized",
                                  "open_time": OPEN, "expiration_time": KICK + timedelta(hours=48)})


def c(h: float, bid: str, ask: str) -> Candlestick:
    return Candlestick.model_validate({"end_period_ts": int((OPEN + timedelta(hours=h)).timestamp()),
                                       "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask}})


def test_parse_and_match() -> None:
    ev = {"slug": "nfl-kc-sea-2026-10-26", "markets": [
        {"sportsMarketType": "spreads"},
        {"sportsMarketType": "moneyline", "outcomes": '["Chiefs", "Seahawks"]', "clobTokenIds": '["a", "b"]',
         "gameStartTime": "2026-10-26 00:20:00+00", "createdAt": "2026-09-14T12:00:42.813336Z", "volumeNum": 5}]}
    g = parse_moneyline(ev, "nfl")
    assert g is not None and g.game_start == KICK and g.teams == ("kc", "sea") and g.token_ids == ("a", "b")
    assert parse_moneyline({**ev, "slug": "nfl-kc-sea-2026-10-26-player-props"}, "nfl") is None
    assert match_side(mk(), "KXNFLGAME", [GAME]) == (GAME, 1)
    assert match_side(mk("KXNFLGAME-26OCT25KCSEA-KC"), "KXNFLGAME", [GAME]) == (GAME, 0)
    la = PmMoneyline("nfl-la-lv-2026-10-25", ("la", "lv"), ("Rams", "Raiders"), ("x", "y"), KICK, None, D(0))
    assert match_side(mk("KXNFLGAME-26OCT25LARLV-LAR"), "KXNFLGAME", [la]) == (la, 0), "LAR vs Polymarket's 'la'"
    late = PmMoneyline(GAME.event_slug, GAME.teams, GAME.outcomes, GAME.token_ids, KICK + timedelta(hours=3), None, D(0))
    assert match_side(mk(), "KXNFLGAME", [late]) is None, "kickoff must agree"


def test_gap_vs_polymarket_pre_game_only_and_who_moved() -> None:
    pm = [(int((OPEN + timedelta(hours=h)).timestamp()), D(p)) for h, p in
          [(0, "0.60"), (10, "0.60"), (16, "0.60"), (300, "0.60")]]
    candles = [c(1, "0.30", "0.75"),  # wide: no gap
               c(10, "0.40", "0.42"),  # cheap vs 60: YES gap ~16
               c(16, "0.59", "0.61"),  # 6 h later Kalshi has moved to Polymarket
               c(305, "0.10", "0.12")]  # within an hour of kickoff (KICK = OPEN + ~308 h): excluded
    r = compare(mk(), GAME, 1, candles, pm)
    assert r.best_side == "YES" and r.best_gap is not None and 15 < r.best_gap < 18
    assert r.best_at_hours_after_open == 10 and r.converged_to == "kalshi->pm"
    assert r.hours == 3, "the in-play-adjacent hour is not compared"


def test_polymarket_opening_and_stale_prices_are_not_a_reference() -> None:
    fresh = PmMoneyline(GAME.event_slug, GAME.teams, GAME.outcomes, GAME.token_ids, KICK, OPEN, D(0))
    pm = [(int(OPEN.timestamp()), D("0.50"))]
    r = compare(mk(), fresh, 1, [c(5, "0.80", "0.82")], pm)
    assert r.hours == 0, "inside Polymarket's first 24 h"
    r2 = compare(mk(), GAME, 1, [c(5, "0.80", "0.82")], pm)
    assert r2.hours == 0, "Polymarket price 5 h old is stale"
