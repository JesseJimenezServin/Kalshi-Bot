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
    assert match_side(mk(), "nfl", [GAME]) == (GAME, 1)
    assert match_side(mk("KXNFLGAME-26OCT25KCSEA-KC"), "nfl", [GAME]) == (GAME, 0)
    la = PmMoneyline("nfl-la-lv-2026-10-25", ("la", "lv"), ("Rams", "Raiders"), ("x", "y"), KICK, None, D(0))
    assert match_side(mk("KXNFLGAME-26OCT25LARLV-LAR"), "nfl", [la], abbr={"LAR": "la"}) == (la, 0)
    assert match_side(mk("KXNFLGAME-26OCT25LARLV-LAR"), "nfl", [la]) is None, "unlearned difference: no guess"
    late = PmMoneyline(GAME.event_slug, GAME.teams, GAME.outcomes, GAME.token_ids, KICK + timedelta(hours=3), None, D(0))
    assert match_side(mk(), "nfl", [late]) is None, "kickoff must agree"


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


def nhl(ticker: str, kick: datetime = KICK) -> Market:
    return Market.model_validate({"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "status": "finalized",
                                  "open_time": OPEN, "expiration_time": kick + timedelta(hours=48)})


def test_abbreviations_are_learned_from_games_whose_other_team_matches() -> None:
    """Regression 2026-10-27: fuzzy letters matched LA@VGK to lak-las the wrong way round, and
    Kent State-Western Michigan to William & Mary-NC Central. Now both teams must match exactly."""
    from bot.analysis.crossvenue import event_teams, learn_abbreviations
    k2, k3 = KICK + timedelta(days=2), KICK + timedelta(days=4)
    pm = lambda slug, t, k: PmMoneyline(slug, t, ("A", "B"), ("x0", "x1"), k, None, D(0))  # noqa: E731
    games = [pm("g1", ("lak", "tor"), KICK), pm("g2", ("sea", "lak"), k2), pm("g3", ("lak", "las"), k3),
             pm("g4", ("tor", "las"), k2), pm("g5", ("las", "sea"), KICK), pm("zz", ("wm", "ncc"), k3)]
    ms = [nhl("KXNHLGAME-26OCT26LATOR-LA"), nhl("KXNHLGAME-26OCT26LATOR-TOR"),
          nhl("KXNHLGAME-26OCT28SEALA-SEA", k2), nhl("KXNHLGAME-26OCT28TORVGK-VGK", k2),
          nhl("KXNHLGAME-26OCT26VGKSEA-VGK"), nhl("KXNHLGAME-26OCT30LAVGK-VGK", k3),
          nhl("KXNHLGAME-26OCT30LAVGK-LA", k3)]
    teams = event_teams(ms)
    assert teams["KXNHLGAME-26OCT30LAVGK"] == ("LA", "VGK")
    abbr = learn_abbreviations(ms, "nhl", games)
    assert abbr["LA"] == "lak" and abbr["VGK"] == "las"
    g3 = games[2]
    assert match_side(ms[5], "nhl", games, teams, abbr) == (g3, 1), "VGK is the second team"
    assert match_side(ms[6], "nhl", games, teams, abbr) == (g3, 0)
    assert match_side(nhl("KXNHLGAME-26OCT30KENTWMU-WMU", k3), "nhl", games, teams, abbr) is None


def test_polymarket_placeholder_fifty_is_not_a_price() -> None:
    from bot.analysis.crossvenue import _placeholder
    flat = [(i, D("0.50")) for i in range(20)]
    assert _placeholder(flat, 15) and _placeholder(flat, 5), "a history that has only ever been 50c"
    late_flat = [(i, D("0.62")) for i in range(10)] + [(i, D("0.50")) for i in range(10, 15)]
    assert not _placeholder(late_flat, 14), "a traded market passing through 50c is a price"
    moving = [(i, D("0.50") if i % 5 else D("0.55")) for i in range(20)]
    assert not _placeholder(moving, 15)


def test_mlb_kickoff_from_the_ticker_in_eastern_time() -> None:
    from bot.analysis.crossvenue import kalshi_kickoff
    m = Market.model_validate({"ticker": "KXMLBGAME-26OCT242000TBMIL-TB", "event_ticker": "KXMLBGAME-26OCT242000TBMIL",
                               "status": "finalized"})
    assert kalshi_kickoff(m, "ticker_et") == datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 20:00 EDT


def test_soccer_three_way_and_the_tie_market() -> None:
    q = lambda text, tok: {"sportsMarketType": "moneyline", "question": text,  # noqa: E731
                           "clobTokenIds": f'["{tok}", "x"]', "gameStartTime": "2026-10-25 17:30:00+00",
                           "createdAt": "2026-10-01T00:00:00Z", "volumeNum": 1}
    ev = {"slug": "epl-sun-lee-2026-10-25", "markets": [
        q("Will Leeds United FC win on 2026-10-25?", "LEE"), q("Will Sunderland AFC vs. Leeds United FC end in a draw?", "D"),
        q("Will Sunderland AFC win on 2026-10-25?", "SUN")]}
    g = parse_moneyline(ev, "epl")
    assert g is not None and g.token_ids == ("SUN", "LEE", "D")
    base = {"event_ticker": "KXEPLGAME-26OCT25SUNLEE", "status": "finalized",
            "expected_expiration_time": datetime(2026, 10, 25, 19, 30, tzinfo=UTC)}
    from bot.analysis.crossvenue import event_teams
    tie = Market.model_validate({**base, "ticker": "KXEPLGAME-26OCT25SUNLEE-TIE"})
    lee = Market.model_validate({**base, "ticker": "KXEPLGAME-26OCT25SUNLEE-LEE"})
    sun = Market.model_validate({**base, "ticker": "KXEPLGAME-26OCT25SUNLEE-SUN"})
    teams = event_teams([tie, lee, sun])
    assert match_side(tie, "epl", [g], teams) == (g, 2) and match_side(lee, "epl", [g], teams) == (g, 1)


def test_futures_match_by_unique_name_prefix() -> None:
    from bot.analysis.crossvenue import match_future
    titles = ["Los Angeles Chargers", "Los Angeles Rams", "Arizona Cardinals", "New York Giants", "New York Jets"]
    assert match_future("Los Angeles R", titles) == 1 and match_future("Arizona", titles) == 2
    assert match_future("New York G", titles) == 3 and match_future("Los Angeles", titles) is None


def test_soccer_names_that_do_not_start_with_the_abbreviation() -> None:
    q = lambda text, tok: {"sportsMarketType": "moneyline", "question": text,  # noqa: E731
                           "clobTokenIds": f'["{tok}", "x"]', "gameStartTime": "2026-10-09 18:30:00+00",
                           "createdAt": "2026-10-01T00:00:00Z", "volumeNum": 1}
    ev = {"slug": "bun-dor-wer-2026-10-09", "markets": [
        q("Will BV Borussia 09 Dortmund win on 2026-10-09?", "DOR"),
        q("Will BV Borussia 09 Dortmund vs. SV Werder Bremen end in a draw?", "D"),
        q("Will SV Werder Bremen win on 2026-10-09?", "WER")]}
    g = parse_moneyline(ev, "bun")
    assert g is not None and g.token_ids == ("DOR", "WER", "D")
    # the abbreviations clearly say the draw question is reversed: refuse rather than guess
    rev = {"slug": "epl-sun-lee-2026-10-25", "markets": [
        q("Will Leeds United FC win on 2026-10-25?", "LEE"), q("Will Leeds United FC vs. Sunderland AFC end in a draw?", "D"),
        q("Will Sunderland AFC win on 2026-10-25?", "SUN")]}
    assert parse_moneyline(rev, "epl") is None


def test_score_trades_counts_prints_far_from_polymarket() -> None:
    from decimal import Decimal as D

    from bot.analysis.crossvenue import SideResult, score_trades
    from bot.api.models import Trade
    from bot.api.polymarket import PmMoneyline
    ko = datetime(2026, 8, 30, 17, 0, tzinfo=UTC)
    g = PmMoneyline("lal-dep-vcf-2026-08-30", ("dep", "vcf"), ("a", "b", "d"), ("1", "2", "3"), ko,
                    datetime(2026, 8, 1, tzinfo=UTC), D(0))
    t0 = int(datetime(2026, 8, 21, tzinfo=UTC).timestamp())
    pm = [(t0 + 3600 * i, D("0.42") + D("0.001") * (i % 3)) for i in range(48)]  # ~42c, moving (not a placeholder)
    tr = lambda tid, price, n, h: Trade.model_validate({  # noqa: E731
        "trade_id": tid, "ticker": "X", "yes_price_dollars": price, "count_fp": n,
        "created_time": datetime.fromtimestamp(t0 + 3600 * h + 60, UTC)})
    r = SideResult("X", g.event_slug, ko, datetime(2026, 8, 16, tzinfo=UTC), None)
    score_trades(r, g, [tr("a", "0.04", "100", 5), tr("b", "0.40", "50", 6),  # 4c vs 42: edge ~38c; 40c: none
                        tr("c", "0.04", "7", 100)], pm)  # Polymarket history ended 52 h earlier: stale
    assert r.trades_seen == 3 and r.trade_contracts == 100
    assert D("37.9") < r.trade_edge_dollars < D("38")  # (42.2 - 4 - 0.27 fee) x 100 contracts


def test_abbreviations_bootstrap_from_lone_kickoff_slots() -> None:
    """Bundesliga: no code agrees (BVB/dor, SVW/wer, BMU/bay), so learning starts from games alone in their
    kickoff window and the next pass resolves a shared window."""
    from bot.analysis.crossvenue import event_teams, learn_abbreviations
    from bot.api.polymarket import PmMoneyline

    def km(ev: str, side: str, ko: datetime) -> Market:
        return Market.model_validate({"ticker": f"{ev}-{side}", "event_ticker": ev, "status": "finalized",
                                      "expected_expiration_time": ko + timedelta(hours=2)})

    def pm(a: str, b: str, ko: datetime) -> PmMoneyline:
        return PmMoneyline(f"bun-{a}-{b}", (a, b), ("", "", ""), ("1", "2", "3"), ko, None, D(0))

    f1, f2, sat = (datetime(2026, 10, d, 18, 30, tzinfo=UTC) for d in (9, 16, 17))
    markets = [km("KXBUNDESLIGAGAME-26OCT09BVBSVW", s, f1) for s in ("BVB", "SVW")]
    markets += [km("KXBUNDESLIGAGAME-26OCT16SVWBVB", s, f2) for s in ("SVW", "BVB")]
    markets += [km("KXBUNDESLIGAGAME-26OCT17BVBBMU", s, sat) for s in ("BVB", "BMU")]
    markets += [km("KXBUNDESLIGAGAME-26OCT17SVWRBL", s, sat) for s in ("SVW", "RBL")]
    games = [pm("dor", "wer", f1), pm("wer", "dor", f2), pm("dor", "bay", sat), pm("wer", "lei", sat)]
    assert event_teams(markets)["KXBUNDESLIGAGAME-26OCT17BVBBMU"] == ("BVB", "BMU")
    abbr = learn_abbreviations(markets, "bundesliga", games)
    assert abbr["BVB"] == "dor" and abbr["SVW"] == "wer"
    # BMU/RBL have one vote each (from the shared Saturday window): below the 2-vote bar, left as identity
    assert "BMU" not in abbr and "RBL" not in abbr
