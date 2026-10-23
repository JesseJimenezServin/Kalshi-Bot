"""Historical model: base rates by position, min_history, seasonality, the cap, no look-ahead; backfill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from bot.clock import FakeClock
from bot.fair_value.historical import HistoricalModel, HistoryIndex, SettledRow, build_events
from bot.fair_value.history import backfill, load_index
from bot.fair_value.types import EventContext, MarketInfo
from bot.storage.repo import Repository
from tests.fake_rest import FakeRest, market

T0 = datetime(2026, 1, 1, 5, tzinfo=UTC)
LADDER = [("T59", "less", None, 59), ("B59.5", "between", 59, 60), ("B61.5", "between", 61, 62),
          ("T62", "greater", 62, None)]


def event_rows(day: int, winner: int, series: str = "KXHIGHPHIL") -> list[SettledRow]:
    ev = f"{series}-D{day}"
    close = T0 + timedelta(days=day)
    return [SettledRow(f"{ev}-{t}", ev, series, st, fl, cp, close, "yes" if i == winner else "no")
            for i, (t, st, fl, cp) in enumerate(LADDER)]


def ctx_for(now: datetime, close: datetime | None = None, series: str = "KXHIGHPHIL") -> tuple[MarketInfo, EventContext]:
    ms = tuple(MarketInfo(f"LIVE-{t}", "LIVE", series, st, fl, cp, close or now) for t, st, fl, cp in LADDER)
    return ms[2], EventContext("LIVE", series, True, ms, {}, now)


def model(index: HistoryIndex, **kw: object) -> HistoricalModel:
    args: dict[str, object] = dict(min_history=20, max_confidence=0.5, seasonal_window_days=45)
    args.update(kw)
    return HistoricalModel(index, **args)  # type: ignore[arg-type]


def test_build_events_orders_by_strike_and_drops_incomplete() -> None:
    rows = event_rows(1, winner=2)
    rows += [SettledRow("X-1", "X", "KXHIGHPHIL", "between", 1, 2, T0, "yes"),
             SettledRow("X-2", "X", "KXHIGHPHIL", "between", 3, 4, T0, "")]  # unresolved
    evs = build_events(list(reversed(rows)))
    assert len(evs) == 1
    assert [m.ticker.split("-")[-1] for m in evs[0].markets] == ["T59", "B59.5", "B61.5", "T62"]
    assert evs[0].yes == (False, False, True, False)


def test_base_rate_by_position() -> None:
    rows = [r for d in range(40) for r in event_rows(d, winner=2 if d % 4 else 1)]  # pos 2 wins 30/40
    m = model(HistoryIndex(build_events(rows)), seasonal_window_days=183)
    market, ctx = ctx_for(T0 + timedelta(days=100))
    fv = m.estimate(market, ctx)
    assert fv is not None and fv.model == "historical"
    assert fv.inputs["position"] == 2 and fv.inputs["yes"] == 30 and fv.inputs["sample"] == 40
    assert fv.cents == D(str(round((30 + 0.5) / 41 * 100, 4)))


def test_min_history_required() -> None:
    rows = [r for d in range(19) for r in event_rows(d, winner=2)]
    market, ctx = ctx_for(T0 + timedelta(days=100))
    assert model(HistoryIndex(build_events(rows)), seasonal_window_days=183).estimate(market, ctx) is None
    rows += event_rows(19, winner=2)
    assert model(HistoryIndex(build_events(rows)), seasonal_window_days=183).estimate(market, ctx) is not None


def test_no_look_ahead() -> None:
    rows = [r for d in range(30) for r in event_rows(d, winner=2)]
    idx = HistoryIndex(build_events(rows))
    market, ctx = ctx_for(T0 + timedelta(days=10, hours=1))  # only days 0..10 have closed
    assert model(idx, seasonal_window_days=183).estimate(market, ctx) is None
    market, ctx = ctx_for(T0 + timedelta(days=25))
    fv = model(idx, seasonal_window_days=183).estimate(market, ctx)
    assert fv is not None and fv.inputs["sample"] == 25


def test_seasonal_window_preferred_with_fallback() -> None:
    winter = [r for d in range(30) for r in event_rows(d, winner=2)]           # Jan: pos 2 always wins
    summer = [r for d in range(180, 240) for r in event_rows(d, winner=0)]     # Jul-Aug: pos 0 wins
    idx = HistoryIndex(build_events(winter + summer))
    now = T0 + timedelta(days=380)  # mid-January the next year
    market, ctx = ctx_for(now, close=now)
    fv = model(idx).estimate(market, ctx)
    assert fv is not None and fv.inputs["basis"].startswith("seasonal") and fv.inputs["yes"] == 30
    # Mid-April: no seasonal match with enough data -> falls back to all seasons.
    apr = T0 + timedelta(days=465)
    market, ctx = ctx_for(apr, close=apr)
    fv = model(idx).estimate(market, ctx)
    assert fv is not None and fv.inputs["basis"] == "all seasons" and fv.inputs["sample"] == 90


def test_confidence_is_capped_below_trading_threshold() -> None:
    rows = [r for d in range(700) for r in event_rows(d, winner=2 if d % 3 else 1)]
    market, ctx = ctx_for(T0 + timedelta(days=800))
    fv = model(HistoryIndex(build_events(rows)), seasonal_window_days=183).estimate(market, ctx)
    assert fv is not None and fv.confidence == 0.5 and fv.inputs["capped"] is True
    uncapped = model(HistoryIndex(build_events(rows)), seasonal_window_days=183, max_confidence=1.0)
    fv2 = uncapped.estimate(market, ctx)
    assert fv2 is not None and 0.5 < fv2.confidence < 1.0


def test_small_samples_get_low_confidence() -> None:
    rows = [r for d in range(20) for r in event_rows(d, winner=2 if d % 2 else 1)]
    market, ctx = ctx_for(T0 + timedelta(days=50))
    fv = model(HistoryIndex(build_events(rows)), seasonal_window_days=183, max_confidence=1.0).estimate(market, ctx)
    assert fv is not None and fv.confidence < 0.3  # p~0.5, n=20 -> +/-21.4% -> 0.14


def test_different_structure_or_series_does_not_match() -> None:
    rows = [r for d in range(40) for r in event_rows(d, winner=2)]
    idx = HistoryIndex(build_events(rows))
    market, ctx = ctx_for(T0 + timedelta(days=100), series="KXHIGHNY")
    assert model(idx, seasonal_window_days=183).estimate(market, ctx) is None
    five = tuple(MarketInfo(f"L{i}", "L", "KXHIGHPHIL", "between", i, i + 1) for i in range(5))
    ctx5 = EventContext("L", "KXHIGHPHIL", True, five, {}, T0 + timedelta(days=100))
    assert model(idx, seasonal_window_days=183).estimate(five[2], ctx5) is None


def test_doy_distance_wraps_the_year_end() -> None:
    rows = [r for d in range(30) for r in event_rows(d - 10, winner=2)]  # Dec 22 .. Jan 20
    market, ctx = ctx_for(T0 + timedelta(days=370), close=datetime(2027, 1, 3, tzinfo=UTC))
    fv = model(HistoryIndex(build_events(rows)), seasonal_window_days=15).estimate(market, ctx)
    assert fv is not None and fv.inputs["basis"].startswith("seasonal")


# ---------------------------------------------------------------- backfill

async def test_backfill_reads_both_tiers_and_is_idempotent(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 23, tzinfo=UTC).timestamp())
    repo = Repository.from_url(f"sqlite:///{tmp_path / 'h.sqlite'}", clock)
    fake = FakeRest()
    live = [market(f"KXHIGHPHIL-26OCT20-{t}", "KXHIGHPHIL-26OCT20", status="finalized",
                   result="yes" if i == 1 else "no", close_time="2026-10-21T05:00:00Z",
                   strike_type=st, floor_strike=fl, cap_strike=cp) for i, (t, st, fl, cp) in enumerate(LADDER)]
    old = [market(f"KXHIGHPHIL-25JAN05-{t}", "KXHIGHPHIL-25JAN05", status="finalized",
                  result="yes" if i == 3 else "no", close_time="2025-01-06T05:00:00Z",
                  strike_type=st, floor_strike=fl, cap_strike=cp) for i, (t, st, fl, cp) in enumerate(LADDER)]
    fake.markets = live + [market("KXHIGHPHIL-26OCT25-X", "KXHIGHPHIL-26OCT25", result="")]  # unsettled: skipped
    fake.routes["/historical/markets"] = lambda r: {"markets": old, "cursor": ""}
    added = await backfill(fake.client(clock), repo, ["KXHIGHPHIL"], clock)
    assert added == {"KXHIGHPHIL": 8}
    assert await backfill(fake.client(clock), repo, ["KXHIGHPHIL"], clock) == {"KXHIGHPHIL": 0}
    idx = load_index(repo)
    assert idx.events == 2
    settled_calls = [r for r in fake.calls if r.url.path.endswith("/markets") and r.url.params.get("status")]
    assert all(r.url.params["status"] == "settled" for r in settled_calls)


@pytest.mark.parametrize("cap", [0.0, 0.5, 1.0])
def test_confidence_always_within_bounds(cap: float) -> None:
    rows = [r for d in range(60) for r in event_rows(d, winner=d % 4)]
    market, ctx = ctx_for(T0 + timedelta(days=100))
    fv = model(HistoryIndex(build_events(rows)), seasonal_window_days=183, max_confidence=cap).estimate(market, ctx)
    assert fv is not None and 0.0 <= fv.confidence <= cap
