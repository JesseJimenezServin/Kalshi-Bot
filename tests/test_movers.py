"""Biggest opening movers: best entry by dollars, references, short-lived flag, report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from bot.analysis.census import Book, provisional_taker_fee_cents
from bot.analysis.movers import best_entry, format_movers, measure, run_movers
from bot.clock import FakeClock
from bot.storage.repo import Repository

OPEN = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)


def bk(sec: int, bid: str | None, ask: str | None, bid_sz: str = "100", ask_sz: str = "100") -> Book:
    return Book(OPEN + timedelta(seconds=sec), ((D(bid), D(bid_sz)),) if bid else (),
                ((D(ask), D(ask_sz)),) if ask else ())


def test_dollars_beat_cents() -> None:
    # 40c gap on 3 contracts ($1.2) loses to a ~6c gap on 500 ($30).
    early = [bk(2, "0.18", "0.20", ask_sz="3"), bk(30, "0.50", "0.53", ask_sz="500")]
    e = best_entry(early, D(60), OPEN)
    assert e is not None and e.side == "YES" and e.price == D(53) and e.depth == 500
    assert e.gap == D(60) - D(53) - provisional_taker_fee_cents(D("0.53"))


def test_no_side_and_placeholders() -> None:
    early = [bk(1, "0.01", "0.99"), bk(5, "0.80", "0.82")]  # placeholder ignored
    e = best_entry(early, D(50), OPEN)
    assert e is not None and e.side == "NO" and e.price == D(20) and e.sec_after_open == 5
    assert best_entry([bk(1, "0.49", "0.51")], D(50), OPEN) is None, "no positive gap after the fee"


def test_measure_refs_settlement_and_short() -> None:
    early = [bk(3, "0.20", "0.25")]
    late = [bk(3000, "0.59", "0.61")]
    m = measure("T", "S", "Sports", OPEN, OPEN + timedelta(hours=2), "yes", early, {"+1h": late, "+6h": []})
    assert m.short and m.refs["+1h"] == D(60) and m.refs["+6h"] is None and m.refs["settle"] == D(100)
    assert m.best["+6h"] is None and m.best["settle"] is not None and m.best["settle"].gap > 70
    text = format_movers([m], ref="+1h")
    assert "Sports" in text and "short" in text and "HINDSIGHT" in text


def test_run_movers_reads_recorded_books(tmp_path: Path) -> None:
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'm.sqlite'}", FakeClock(OPEN.timestamp()))
    repo.upsert_market(ticker="T", event_ticker="E", series_ticker="S", category="Politics", status="active",
                       open_time=OPEN, close_time=OPEN + timedelta(days=3), emitted_at=OPEN)
    for b in [bk(3, "0.20", "0.25"), bk(3000, "0.59", "0.61"), bk(6 * 3600, "0.70", "0.72")]:
        repo.add_snapshot("T", b.ts, [[str(p), str(s)] for p, s in b.yes_bids],
                          [[str(p), str(s)] for p, s in b.yes_asks])
    [m] = run_movers(repo, OPEN - timedelta(hours=1))
    assert not m.short and m.refs["+1h"] == D(60) and m.refs["+6h"] == D(71)
    assert m.best["+6h"] is not None and m.best["+6h"].gap > m.best["+1h"].gap  # type: ignore[union-attr]
