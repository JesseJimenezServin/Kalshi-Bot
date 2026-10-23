"""Opening-move measure: consensus, executable gaps vs it, depth, durations, per-category aggregation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from bot.analysis.census import Book, provisional_taker_fee_cents
from bot.analysis.opening import aggregate, format_opening, opening_result, run_opening
from bot.clock import FakeClock
from bot.storage.repo import Repository

OPEN = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)


def bk(sec: int, bid: str | None, ask: str | None, bid_sz: str = "100", ask_sz: str = "100") -> Book:
    return Book(OPEN + timedelta(seconds=sec), ((D(bid), D(bid_sz)),) if bid else (),
                ((D(ask), D(ask_sz)),) if ask else ())


def settles_at_60(*early: Book) -> list[Book]:
    """Early books, then the market sits at 59/61 (mid 60) from minute 45."""
    return [*early, bk(2700, "0.59", "0.61"), bk(3600, "0.59", "0.61"), bk(4500, "0.59", "0.61")]


def test_underpriced_open_is_a_yes_gap_vs_later_consensus() -> None:
    r = opening_result("T", "Sports", settles_at_60(bk(3, "0.20", "0.25"), bk(300, "0.58", "0.62")), OPEN, D(50))
    assert r.consensus == D(60) and r.first_mid == D("22.5") and r.first_book_sec == 3
    assert r.max_gap_with_depth == D(60) - D(25) - provisional_taker_fee_cents(D("0.25"))  # 33.6875
    assert r.seconds_ge[20] == 297  # the cheap book stood from 3 s until 300 s


def test_overpriced_open_is_a_no_gap() -> None:
    r = opening_result("T", "Politics", settles_at_60(bk(1, "0.90", "0.95")), OPEN, D(50))
    assert r.max_gap_with_depth == D(90) - D(60) - provisional_taker_fee_cents(D("0.10"))  # 29.37


def test_depth_rule() -> None:
    r = opening_result("T", "Sports", settles_at_60(bk(3, "0.20", "0.25", ask_sz="10")), OPEN, D(50))
    assert r.max_gap is not None and r.max_gap > 30, "the gap exists..."
    assert r.max_gap_with_depth is not None and r.max_gap_with_depth < 0, "...but only 10 contracts at the ask"


def test_fair_open_has_no_gap() -> None:
    r = opening_result("T", "Climate and Weather", settles_at_60(bk(2, "0.58", "0.62")), OPEN, D(50))
    assert r.max_gap_with_depth is not None and r.max_gap_with_depth < 0


def test_unmeasurable_without_a_late_two_sided_book() -> None:
    r = opening_result("T", "Mentions", [bk(5, "0.20", "0.25")], OPEN, D(50))
    assert not r.measurable and r.first_book_sec == 5
    assert not opening_result("T", "Mentions", [], OPEN, D(50)).measurable


def test_only_the_first_10_minutes_count_as_early() -> None:
    r = opening_result("T", "Sports", settles_at_60(bk(1, "0.58", "0.62"), bk(900, "0.20", "0.25")), OPEN, D(50))
    assert r.max_gap_with_depth is not None and r.max_gap_with_depth < 20, "a gap at minute 15 is not 'at open'"


def test_aggregate_and_format_per_category() -> None:
    rs = [opening_result("A", "Sports", settles_at_60(bk(3, "0.20", "0.25")), OPEN, D(50)),
          opening_result("B", "Sports", settles_at_60(bk(3, "0.58", "0.62")), OPEN, D(50)),
          opening_result("C", "Mentions", [bk(5, "0.20", "0.25")], OPEN, D(50))]
    cats = aggregate(rs)
    assert cats["Sports"].markets == 2 and cats["Sports"].measurable == 2
    assert cats["Sports"].ge[20] == 1 and cats["Sports"].contracts_ge20 == D(100)
    assert cats["Mentions"].measurable == 0
    text = format_opening(cats, D(50))
    assert "Sports" in text and "UPPER BOUND" in text


def test_run_opening_reads_recorded_books(tmp_path: Path) -> None:
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'o.sqlite'}", FakeClock(OPEN.timestamp()))
    repo.upsert_market(ticker="T", event_ticker="E", series_ticker="S", category="Sports", status="active",
                       open_time=OPEN, close_time=OPEN + timedelta(days=1), emitted_at=OPEN)
    for b in settles_at_60(bk(3, "0.20", "0.25")):
        repo.add_snapshot("T", b.ts, [[str(p), str(s)] for p, s in b.yes_bids],
                          [[str(p), str(s)] for p, s in b.yes_asks])
    cats = run_opening(repo, OPEN - timedelta(hours=1), D(50))
    assert cats["Sports"].ge[20] == 1



def test_short_lived_markets_are_not_scored() -> None:
    """Regression, first live run: an hourly temperature market's 45-75 min
    'consensus' is the reading arriving, so its move is outcome, not mispricing."""
    hourly = settles_at_60(bk(3, "0.20", "0.25"))
    r = opening_result("T", "Climate and Weather", hourly, OPEN, D(50), close_time=OPEN + timedelta(hours=1))
    assert r.short_lived and not r.measurable and r.max_gap_with_depth is None and r.first_book_sec == 3
    cats = aggregate([r])
    assert cats["Climate and Weather"].short_lived == 1 and cats["Climate and Weather"].ge[20] == 0
    long_lived = opening_result("T", "Sports", hourly, OPEN, D(50), close_time=OPEN + timedelta(days=2))
    assert long_lived.measurable and long_lived.max_gap_with_depth is not None
