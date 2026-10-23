"""Opportunity census: sibling-model gaps checked against hand-computed values."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from bot.analysis.census import (
    Book,
    event_observations,
    format_report,
    open_sum_deviation,
    provisional_taker_fee_cents,
    run_census,
    summarize,
)
from bot.clock import FakeClock
from bot.storage.repo import Repository
from tests.fake_rest import FakeRest

OPEN = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def bk(t: datetime, bid: str | None, ask: str | None, bid_sz: str = "100", ask_sz: str = "100") -> Book:
    return Book(t, ((D(bid), D(bid_sz)),) if bid else (), ((D(ask), D(ask_sz)),) if ask else ())


def at(sec: int) -> datetime:
    return OPEN + timedelta(seconds=sec)


def test_fee_formula_values() -> None:
    assert provisional_taker_fee_cents(D("0.50")) == D("1.75")
    assert provisional_taker_fee_cents(D("0.08")) == D("0.5152")


def test_consistent_event_shows_no_opportunity() -> None:
    # Mids 30 + 30 + 40 = 100: fair == mid, so every gap is -(half spread) - fee.
    books = {"A": [bk(at(0), "0.29", "0.31")], "B": [bk(at(0), "0.29", "0.31")], "C": [bk(at(0), "0.39", "0.41")]}
    obs = event_observations(books, OPEN, HOUR)
    assert len(obs) == 3
    a = next(o for o in obs if o.ticker == "A")
    assert a.fair_cents == D("30")
    assert a.yes_gap_cents == D("30") - D("31") - provisional_taker_fee_cents(D("0.31"))
    assert all((o.best_gap() or D(0)) < 0 for o in obs)


def test_underpriced_market_yes_gap_exact() -> None:
    # A, B at 30; C quoted 5/8 (mid 6.5). Sibling fair for C = 100 - 30 - 30 = 40.
    books = {"A": [bk(at(0), "0.29", "0.31")], "B": [bk(at(0), "0.29", "0.31")], "C": [bk(at(0), "0.05", "0.08")]}
    obs = {o.ticker: o for o in event_observations(books, OPEN, HOUR)}
    assert obs["C"].fair_cents == D("40")
    assert obs["C"].yes_gap_cents == D("40") - D("8") - D("0.5152")  # 31.4848
    # The event's mids sum to 66.5, so A and B ALSO look cheap against their
    # siblings: gaps cluster by event. That is inherent to the sibling model.
    assert obs["A"].fair_cents == D("63.5") and obs["A"].best_gap() > 20


def test_no_gap_uses_bid_and_fee_at_no_price() -> None:
    books = {"A": [bk(at(0), "0.29", "0.31")], "B": [bk(at(0), "0.29", "0.31")], "C": [bk(at(0), "0.70", "0.72")]}
    c = next(o for o in event_observations(books, OPEN, HOUR) if o.ticker == "C")
    assert c.fair_cents == D("40")
    assert c.no_gap_cents == D("70") - D("40") - provisional_taker_fee_cents(D("0.30"))  # 28.53
    assert c.best_gap() == c.no_gap_cents


def test_fair_is_clipped_to_1_99() -> None:
    books = {"A": [bk(at(0), "0.70", "0.72")], "B": [bk(at(0), "0.60", "0.62")]}
    obs = {o.ticker: o for o in event_observations(books, OPEN, HOUR)}
    assert obs["A"].fair_cents == D("39") and obs["B"].fair_cents == D("29")
    books = {"A": [bk(at(0), "0.01", "0.02")], "B": [bk(at(0), None, "0.01")]}
    obs = {o.ticker: o for o in event_observations(books, OPEN, HOUR)}
    assert obs["A"].fair_cents == D("99")  # 100 - 0.5 = 99.5 -> clipped


def test_depth_requirement() -> None:
    books = {"A": [bk(at(0), "0.29", "0.31")], "B": [bk(at(0), "0.29", "0.31")],
             "C": [bk(at(0), "0.05", "0.08", ask_sz="10")]}
    c = next(o for o in event_observations(books, OPEN, HOUR) if o.ticker == "C")
    assert c.best_gap() is not None and c.best_gap() > 20
    # Only 10 contracts at the ask, so the YES side drops out; the NO side
    # (100 contracts) is still eligible, and its gap is negative.
    assert c.best_gap(min_depth=D("50")) == c.no_gap_cents < 0
    assert c.best_gap(min_depth=D("10")) == c.yes_gap_cents


def test_missing_side_mid_rules() -> None:
    assert bk(at(0), None, "0.04").mid() == D("0.02"), "no bids: worth less than its ask"
    assert bk(at(0), "0.96", None).mid() == D("0.98"), "no asks: worth more than its bid"
    assert bk(at(0), None, None).mid() is None


def test_as_of_join_and_skip_until_every_sibling_has_a_book() -> None:
    books = {"A": [bk(at(0), "0.29", "0.31"), bk(at(30), "0.39", "0.41")],
             "B": [bk(at(10), "0.59", "0.61")]}
    obs = event_observations(books, OPEN, HOUR)
    assert {o.ts for o in obs} == {at(10), at(30)}, "t=0 skipped: B had no book yet"
    a10 = next(o for o in obs if o.ticker == "A" and o.ts == at(10))
    a30 = next(o for o in obs if o.ticker == "A" and o.ts == at(30))
    assert a10.fair_cents == D("40") and a30.fair_cents == D("40")
    assert a10.dt_sec == 20 and a30.dt_sec == 3600 - 30


def test_window_excludes_later_snapshots() -> None:
    books = {"A": [bk(at(0), "0.29", "0.31"), bk(at(7200), "0.01", "0.02")],
             "B": [bk(at(0), "0.69", "0.71")]}
    obs = event_observations(books, OPEN, HOUR)
    assert all(o.ts <= OPEN + HOUR for o in obs) and len({o.ts for o in obs}) == 1


def test_summary_counts_markets_time_and_episodes() -> None:
    # C underpriced for the first 120 s, then corrected.
    books = {"A": [bk(at(0), "0.29", "0.31")], "B": [bk(at(0), "0.29", "0.31")],
             "C": [bk(at(0), "0.05", "0.08"), bk(at(120), "0.39", "0.41")]}
    obs = event_observations(books, OPEN, timedelta(minutes=10))
    rep = summarize([("E", obs, D(0))], min_depth=D("50"))
    st20 = rep.by_threshold[20]
    assert rep.events == 1 and rep.markets == 3
    assert st20.markets_any == 3, "all three look mispriced while the event sums to 66.5"
    assert st20.seconds_with_depth == 3 * 120
    assert sorted(st20.episodes) == [120.0, 120.0, 120.0]
    assert rep.observed_seconds == 3 * 600
    text = format_report(rep, D("50"))
    assert "PROVISIONAL" in text and "20c" in text


def test_open_sum_deviation() -> None:
    books = {"A": [bk(at(0), "0.29", "0.31")], "B": [bk(at(0), "0.29", "0.31")], "C": [bk(at(5), "0.05", "0.08")]}
    assert open_sum_deviation(books, OPEN, HOUR) == D("33.5")  # 30 + 30 + 6.5 = 66.5


async def test_run_census_on_recorded_data_skips_non_exclusive_events(tmp_path: Path) -> None:
    clock = FakeClock((OPEN + timedelta(hours=2)).timestamp())
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'c.sqlite'}", clock)
    fake = FakeRest()
    fake.events["KXRAIN-26OCT24"] = {"event_ticker": "KXRAIN-26OCT24", "series_ticker": "KXRAIN",
                                     "mutually_exclusive": False}
    for ticker, ev, bid, ask in [("KXHIGHPHIL-26OCT23-A", "KXHIGHPHIL-26OCT23", "0.29", "0.31"),
                                 ("KXHIGHPHIL-26OCT23-B", "KXHIGHPHIL-26OCT23", "0.29", "0.31"),
                                 ("KXHIGHPHIL-26OCT23-C", "KXHIGHPHIL-26OCT23", "0.05", "0.08"),
                                 ("KXRAIN-26OCT24-X", "KXRAIN-26OCT24", "0.40", "0.42"),
                                 ("KXRAIN-26OCT24-Y", "KXRAIN-26OCT24", "0.40", "0.42")]:
        repo.upsert_market(ticker=ticker, event_ticker=ev, series_ticker=ev.split("-")[0], status="active",
                           open_time=OPEN, close_time=OPEN + timedelta(days=1), emitted_at=OPEN)
        repo.add_snapshot(ticker, OPEN + timedelta(seconds=1), [[bid, "100"]], [[ask, "100"]])
    reports = await run_census(repo, fake.client(clock), OPEN - HOUR, HOUR, D("50"))
    rep = reports["ALL"]
    assert rep.events == 1 and rep.events_skipped_not_exclusive == 1
    assert rep.by_threshold[20].markets_any_with_depth == 3
    assert rep.max_gap_cents is not None and rep.max_gap_cents > 30
    # Per category: the rows carry no category here, so they group under "?".
    assert set(reports) == {"?", "ALL"} and reports["?"].events == 1


@pytest.mark.parametrize("p", ["0.01", "0.5", "0.99"])
def test_fee_is_nonnegative_and_symmetric(p: str) -> None:
    x = D(p)
    assert provisional_taker_fee_cents(x) == provisional_taker_fee_cents(1 - x) >= 0



def test_placeholder_quotes_carry_no_price() -> None:
    """Regression, live 2026-10-23: 1c/99c opening placeholders read as mid 50c
    made a ~30-bucket event 'sum' to ~1,434c."""
    assert bk(at(0), "0.01", "0.99").mid() is None and not bk(at(0), "0.01", "0.99").informative
    assert bk(at(0), "0.30", "0.75").mid() == D("0.525")  # wide but under the limit: still a price
    books = {"A": [bk(at(0), "0.01", "0.99"), bk(at(30), "0.29", "0.31")],
             "B": [bk(at(0), "0.69", "0.71")]}
    obs = event_observations(books, OPEN, HOUR)
    assert {o.ts for o in obs} == {at(30)}, "the placeholder moment is skipped, not scored"
    assert open_sum_deviation(books, OPEN, HOUR) == D(0)  # first INFORMATIVE moment: 30 + 70



def test_one_sided_placeholders_carry_no_price_either() -> None:
    """Regression, live: an ask-only 99c placeholder's 'mid' 49.5c x 29 buckets ~ 1,434c."""
    assert bk(at(0), None, "0.99").mid() is None
    assert bk(at(0), "0.01", None).mid() is None
    assert bk(at(0), None, "0.04").mid() == D("0.02"), "a tight one-sided bound is still a price"
    assert bk(at(0), "0.96", None).mid() == D("0.98")
