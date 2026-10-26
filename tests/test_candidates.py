"""Census candidates: arbitrage and convergence tests on a flagged gap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from bot.analysis.candidates import format_candidates, inspect
from bot.analysis.census import Book, event_observations

OPEN = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def bk(sec: int, bid: str | None, ask: str | None, size: str = "100") -> Book:
    return Book(OPEN + timedelta(seconds=sec), ((D(bid), D(size)),) if bid else (),
                ((D(ask), D(size)),) if ask else ())


def top_obs(books: dict[str, list[Book]], ticker: str):  # type: ignore[no-untyped-def]
    return max((o for o in event_observations(books, OPEN, HOUR) if o.ticker == ticker),
               key=lambda o: o.best_gap(D(50)) or D(-999))


def test_cheap_leg_that_converges_is_confirmed_and_sum_of_asks_shows_the_arb() -> None:
    # A/B fair, C cheap at open then repriced at minute 10: sum of asks 30+30+8 = 68 < 100.
    books = {"A": [bk(0, "0.29", "0.30")], "B": [bk(0, "0.29", "0.30")],
             "C": [bk(0, "0.06", "0.08"), bk(600, "0.38", "0.40")]}
    c = inspect("E", "Sports", top_obs(books, "C"), books, D(50))
    assert c.side == "yes" and c.gap > 20
    assert c.arb_buy_all is not None and c.arb_buy_all > 25 and c.leg_depth_buy == 100  # 100 - 68 - fees
    assert c.later_mid[15] == D("39")
    assert c.converged and c.verdict == "ARB"


def test_gap_from_wide_siblings_that_never_converges_is_not_confirmed() -> None:
    # The siblings' books are wide, so their mids are soft; nobody trades through, nothing moves.
    books = {"A": [bk(0, "0.02", "0.38")], "B": [bk(0, "0.02", "0.38")],
             "C": [bk(0, "0.30", "0.32"), bk(1800, "0.30", "0.32")]}
    c = inspect("E", "Politics", top_obs(books, "C"), books, D(50))
    assert c.gap >= 20, "the mid model says C is ~28c cheap (fair 60 vs ask 32)"
    assert c.arb_buy_all is not None and c.arb_buy_all < 0 and c.arb_sell_all is not None and c.arb_sell_all < 0
    assert not c.converged and c.verdict == "NOT CONFIRMED"
    assert c.sibling_spread_median == D(36)
    text = format_candidates([c])
    assert "NOT CONFIRMED" in text and "Politics" in text
