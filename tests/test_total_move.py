"""Total move open -> pre-event: start estimate, informative candles, gaps, nothing in-play."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from bot.analysis.total_move import Study, estimate_start, format_study, measure, pick_markets
from bot.api.models import Candlestick, Market

OPEN = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
KICK = datetime(2026, 10, 26, 0, 20, tzinfo=UTC)
RULE = "If the game is postponed but begins within 48 hours from its originally scheduled start time, ..."


def mk(**kw) -> Market:  # type: ignore[no-untyped-def]
    base = dict(ticker="G-SEA", event_ticker="G", status="finalized", open_time=OPEN,
                close_time=KICK + timedelta(hours=3), expiration_time=KICK + timedelta(hours=48),
                rules_secondary=RULE, result="yes")
    base.update(kw)
    return Market.model_validate(base)


def c(t: datetime, bid: str, ask: str, vol: str = "10") -> Candlestick:
    return Candlestick.model_validate({"end_period_ts": int(t.timestamp()), "yes_bid": {"close_dollars": bid},
                                       "yes_ask": {"close_dollars": ask}, "volume_fp": vol})


def test_start_from_the_48h_rule_and_skips() -> None:
    assert estimate_start(mk(), "Sports") == (KICK, "rules48")
    assert estimate_start(mk(rules_secondary="no clause"), "Sports") == (None, "sports-unknown")
    m = mk(rules_secondary="no clause")
    assert estimate_start(m, "Politics") == (m.close_time, "close")
    assert estimate_start(mk(expiration_time=OPEN - timedelta(days=1) + timedelta(hours=48)), "Sports")[0] is None


def test_open_and_pre_prices_ignore_placeholders_and_the_live_game() -> None:
    early = [c(OPEN + timedelta(hours=1), "0.00", "0.99"),  # placeholder: not informative
             c(OPEN + timedelta(hours=2), "0.40", "0.45", vol="500")]
    late = [c(KICK - timedelta(hours=2), "0.58", "0.60", vol="100"),
            c(KICK - timedelta(minutes=20), "0.59", "0.61", vol="100"),  # inside the 1 h buffer: not 'pre'
            c(KICK + timedelta(hours=2), "0.95", "0.96", vol="900")]  # in-play: never the reference
    tm = measure(mk(), "KXNFLGAME", "Sports", KICK, "rules48", early, late)
    assert tm is not None
    assert (tm.open_bid, tm.open_ask, tm.pre_bid, tm.pre_ask) == (D(40), D(45), D(58), D(60))
    assert tm.hours_to_informative == 2 and round(tm.lead_days, 1) == 24.8
    assert tm.move == D("16.5") and tm.best_side == "YES"
    assert tm.yes_gap == D(59) - D(45) - D("0.07") * D("0.45") * D("0.55") * 100
    assert tm.inplay_jump == 900 / 200
    assert tm.open_day_volume == D(510), "all first-day volume, placeholder hour included"


def test_unmeasurable_without_both_books() -> None:
    assert measure(mk(), "S", "Sports", KICK, "rules48", [c(OPEN + timedelta(hours=1), "0", "1")],
                   [c(KICK - timedelta(hours=2), "0.5", "0.52")]) is None


def test_mirror_sides_count_once() -> None:
    two = [mk(ticker="G-A"), mk(ticker="G-B")]
    many = [mk(ticker=f"F-{i}", event_ticker="F") for i in range(6)]
    got = pick_markets(two + many, random.Random(1))
    assert sum(m.event_ticker == "G" for m in got) == 1 and sum(m.event_ticker == "F" for m in got) == 3


def test_report() -> None:
    early = [c(OPEN + timedelta(hours=2), "0.40", "0.45")]
    late = [c(KICK - timedelta(hours=2), "0.58", "0.60")]
    res = Study()
    for i in range(5):
        tm = measure(mk(ticker=f"G{i}"), "KXNFLGAME", "Sports", KICK, "rules48", early, late)
        assert tm is not None
        res.moves.append(tm)
    res.skip("lead<24h")
    text = format_study(res)
    assert "14-60 d" in text and "KXNFLGAME" in text and "HINDSIGHT" in text and "lead<24h 1" in text


def test_outcome_known_before_close_is_cut_off() -> None:
    """Live smoke 2026-10-27: 'will X happen by Oct 1' markets closed at 97/99 because X happened."""
    m = mk(rules_secondary="none", close_time=KICK)
    early = [c(OPEN + timedelta(hours=1), "0.06", "0.15")]
    late = [c(KICK - timedelta(hours=50), "0.20", "0.25"),
            c(KICK - timedelta(hours=30), "0.96", "0.99"),  # the news: pinned from here on
            c(KICK - timedelta(hours=5), "0.97", "0.99")]
    tm = measure(m, "S", "Politics", KICK, "close", early, late)
    assert tm is not None and tm.resolved_early and (tm.pre_bid, tm.pre_ask) == (D(20), D(25))


def test_pinned_for_the_whole_window_falls_back_to_daily_candles() -> None:
    m = mk(rules_secondary="none", close_time=KICK)
    early = [c(OPEN + timedelta(hours=1), "0.06", "0.15")]
    late = [c(KICK - timedelta(hours=h), "0.97", "0.99") for h in (60, 30, 5)]
    daily = [c(OPEN + timedelta(days=d), "0.10", "0.14") for d in range(1, 10)] + \
            [c(OPEN + timedelta(days=d), "0.97", "0.99") for d in range(10, 25)]
    from bot.analysis.total_move import needs_daily
    assert needs_daily(late)
    tm = measure(m, "S", "Politics", KICK, "close", early, late, daily)
    assert tm is not None and tm.resolved_early and tm.pre_mid == D(12)
    assert measure(m, "S", "Politics", KICK, "close", early, late) is None, "no daily candles: unmeasurable"
