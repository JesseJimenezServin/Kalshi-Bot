"""Fair-value report: gap arithmetic, event selection, rendering against a fake API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from bot.analysis.census import provisional_taker_fee_cents
from bot.analysis.fair_report import executable_gap, fair_report, open_events
from bot.clock import FakeClock
from bot.fair_value.engine import FairValueEngine
from bot.fair_value.sibling import SiblingModel
from bot.fair_value.types import Quote
from bot.storage.repo import Repository
from tests.fake_rest import FakeRest

NOW = datetime(2026, 10, 24, 14, 10, tzinfo=UTC)


def test_executable_gap_picks_the_better_side_after_fees() -> None:
    q = Quote(D(5), D(8), D(100), D(100))
    gap, side = executable_gap(D(40), q)
    assert side == "yes" and gap == D(40) - D(8) - provisional_taker_fee_cents(D("0.08"))
    gap, side = executable_gap(D(1), Quote(D(30), D(32), D(100), D(100)))
    assert side == "no" and gap == D(30) - D(1) - provisional_taker_fee_cents(D("0.70"))
    assert executable_gap(D(40), Quote(None, None)) == (None, None)


def _rows(repo: Repository) -> None:
    for t, ev, opened, closes, result in [
        ("A-1", "A", NOW - timedelta(minutes=10), NOW + timedelta(hours=30), None),   # open: newest
        ("B-1", "B", NOW - timedelta(hours=3), NOW + timedelta(hours=30), None),      # open: older
        ("C-1", "C", NOW + timedelta(hours=2), NOW + timedelta(hours=40), None),      # not open yet
        ("D-1", "D", NOW - timedelta(days=2), NOW - timedelta(hours=1), None),        # closed
        ("E-1", "E", NOW - timedelta(hours=1), NOW + timedelta(hours=5), "yes"),      # resolved
    ]:
        repo.upsert_market(ticker=t, event_ticker=ev, series_ticker="S", status="active", open_time=opened,
                           close_time=closes, **({"result": result} if result else {}))


def test_open_events_are_open_unresolved_newest_first(tmp_path: Path) -> None:
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'r.sqlite'}", FakeClock(NOW.timestamp()))
    _rows(repo)
    assert open_events(repo, NOW, limit=5) == ["A", "B"]
    assert open_events(repo, NOW, limit=1) == ["A"]
    assert open_events(repo, NOW, limit=5, series_prefix="X") == []


def _api_market(t: str, st: str, fl: float | None, cp: float | None, bid: str, ask: str) -> dict[str, Any]:
    return {"ticker": t, "event_ticker": "KXHIGHPHIL-26OCT25", "status": "active", "strike_type": st,
            "floor_strike": fl, "cap_strike": cp, "close_time": "2026-10-26T05:00:00Z",
            "yes_bid_dollars": bid, "yes_bid_size_fp": "100.00" if bid != "0.0000" else "0.00",
            "yes_ask_dollars": ask, "yes_ask_size_fp": "100.00"}


async def test_report_renders_estimates_beside_live_prices(tmp_path: Path) -> None:
    clock = FakeClock(NOW.timestamp())
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'r.sqlite'}", clock)
    repo.upsert_market(ticker="KXHIGHPHIL-26OCT25-B63.5", event_ticker="KXHIGHPHIL-26OCT25",
                       series_ticker="KXHIGHPHIL", status="active", open_time=NOW - timedelta(minutes=10),
                       close_time=NOW + timedelta(hours=30))
    markets = [_api_market("KXHIGHPHIL-26OCT25-T59", "less", None, 59, "0.0400", "0.0600"),
               _api_market("KXHIGHPHIL-26OCT25-B59.5", "between", 59, 60, "0.0900", "0.1100"),
               _api_market("KXHIGHPHIL-26OCT25-B61.5", "between", 61, 62, "0.2400", "0.2600"),
               _api_market("KXHIGHPHIL-26OCT25-B63.5", "between", 63, 64, "0.0500", "0.0800"),  # cheap
               _api_market("KXHIGHPHIL-26OCT25-B65.5", "between", 65, 66, "0.1900", "0.2100"),
               _api_market("KXHIGHPHIL-26OCT25-T66", "greater", 66, None, "0.0400", "0.0600")]
    fake = FakeRest()
    fake.routes["/events/KXHIGHPHIL-26OCT25"] = lambda r: {
        "event": {"event_ticker": "KXHIGHPHIL-26OCT25", "series_ticker": "KXHIGHPHIL", "mutually_exclusive": True},
        "markets": markets}
    engine = FairValueEngine([SiblingModel(15, 50)], min_confidence=0.6, agree_within_cents=5)
    text = await fair_report(repo, fake.client(clock), engine, NOW, limit=5)
    assert "KXHIGHPHIL-26OCT25" in text and "B63.5" in text
    # B63.5 sibling fair = 100 - (5+10+25+20+5) = 35 vs ask 8: a ~26.5c gap after fees.
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("B63.5"))
    assert "35.0" in line and line.rstrip().endswith("yes")
    assert "gaps >= 5/10/20c:" in text and "FEES PROVISIONAL" in text
    assert "sum of mids 71.5c" in text  # 5+10+25+6.5+20+5: B63.5 is cheap, so the event sums low
    assert all(r.method == "GET" for r in fake.calls)
