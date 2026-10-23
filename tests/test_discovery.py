"""Discovery: series filtering, the definition of new, lifecycle handling, sweep, results, watchlist."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bot.clock import FakeClock
from bot.discovery.catalog import SeriesCatalog, series_prefix
from bot.discovery.results import ResultsUpdater
from bot.discovery.scanner import NewMarket, Scanner, is_new
from bot.discovery.watchlist import Watchlist
from bot.storage.repo import Repository
from tests.fake_rest import FakeRest, market

OPEN = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
W = timedelta(minutes=10)


# ------------------------------------------------------------ is_new rule

def test_new_only_inside_the_window_after_open() -> None:
    seen = OPEN - timedelta(hours=4)
    assert not is_new(OPEN, seen, OPEN - timedelta(seconds=1), W), "not open yet"
    assert is_new(OPEN, seen, OPEN, W)
    assert is_new(OPEN, seen, OPEN + W, W)
    assert not is_new(OPEN, seen, OPEN + W + timedelta(seconds=1), W), "opened too long ago"


def test_first_seen_long_after_open_is_not_new() -> None:
    assert is_new(OPEN, OPEN + timedelta(minutes=3), OPEN + timedelta(minutes=5), W), "restart at 14:03 still counts"
    assert not is_new(OPEN, OPEN + timedelta(hours=1), OPEN + timedelta(hours=1), W)


def test_no_open_time_is_never_new() -> None:
    assert not is_new(None, OPEN, OPEN, W)


# --------------------------------------------------------------- fixtures

class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = FakeClock((OPEN - timedelta(hours=4)).timestamp())
        self.fake = FakeRest()
        self.rest = self.fake.client(self.clock)
        self.repo = Repository.from_url(f"sqlite:///{tmp_path / 'd.sqlite'}", self.clock)
        self.catalog = SeriesCatalog(self.rest, ["Climate and Weather"], [], self.clock)
        self.new: list[NewMarket] = []

        async def on_new(nm: NewMarket) -> None:
            self.new.append(nm)

        self.scanner = Scanner(self.rest, self.catalog, self.repo, self.clock, window_min=10,
                               sweep_lookback_hours=24, on_new=on_new)

    def created(self, ticker: str = "KXHIGHPHIL-26OCT23-B63.5", event: str = "KXHIGHPHIL-26OCT23",
                open_ts: datetime = OPEN) -> dict[str, Any]:
        return {"event_type": "created", "market_ticker": ticker, "exchange_index": 0,
                "open_ts": int(open_ts.timestamp()), "close_ts": int((open_ts + timedelta(hours=39)).timestamp()),
                "additional_metadata": {"event_ticker": event, "yes_sub_title": "63-64", "strike_type": "between",
                                        "floor_strike": 63, "cap_strike": 64, "rules_primary": "r"}}

    def at(self, t: datetime) -> None:
        self.clock.advance(t.timestamp() - self.clock.now())


@pytest.fixture
async def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    await e.catalog.refresh()
    return e


# --------------------------------------------------------------- catalog

def test_series_prefix() -> None:
    assert series_prefix("KXHIGHPHIL-26OCT23") == "KXHIGHPHIL"


async def test_unincluded_series_are_dropped_without_any_rest_call(env: Env) -> None:
    before = len(env.fake.calls)
    await env.scanner.on_lifecycle("market_lifecycle_v2",
                                   env.created("KXMVECROSS-ABC-1", "KXMVECROSS-ABC"))
    assert len(env.fake.calls) == before, "the free prefix filter must not hit REST"
    assert not env.repo.has_market("KXMVECROSS-ABC-1")


async def test_excluded_series_are_dropped(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.catalog = SeriesCatalog(e.rest, ["Climate and Weather"], ["KXHIGHPHIL"], e.clock)
    await e.catalog.refresh()
    assert e.catalog.included == set()


async def test_event_lookups_are_cached(env: Env) -> None:
    for i in range(5):
        await env.scanner.on_lifecycle("market_lifecycle_v2", env.created(f"KXHIGHPHIL-26OCT23-B{i}"))
    assert env.fake.paths().count("/events/KXHIGHPHIL-26OCT23") == 1


# ------------------------------------------------------- lifecycle → new

async def test_created_registers_then_emits_exactly_once_at_open(env: Env) -> None:
    await env.scanner.on_lifecycle("market_lifecycle_v2", env.created())
    row = env.repo.get_market("KXHIGHPHIL-26OCT23-B63.5")
    assert row is not None and row.category == "Climate and Weather" and row.series_ticker == "KXHIGHPHIL"
    assert row.open_time == OPEN and row.floor_strike == 63

    assert await env.scanner.tick() == [], "not open yet"
    env.at(OPEN + timedelta(seconds=3))
    emitted = await env.scanner.tick()
    assert [n.ticker for n in emitted] == ["KXHIGHPHIL-26OCT23-B63.5"]
    assert await env.scanner.tick() == []
    # A later duplicate 'created' (or sweep) must not re-emit.
    await env.scanner.on_lifecycle("market_lifecycle_v2", env.created())
    assert await env.scanner.tick() == []
    assert len(env.new) == 1


async def test_emission_survives_restart(env: Env, tmp_path: Path) -> None:
    await env.scanner.on_lifecycle("market_lifecycle_v2", env.created())
    env.at(OPEN + timedelta(minutes=1))
    await env.scanner.tick()
    # New scanner on the same DB, as after a restart, rediscovering via sweep.
    env.fake.markets = [market("KXHIGHPHIL-26OCT23-B63.5", "KXHIGHPHIL-26OCT23")]
    out: list[NewMarket] = []

    async def on_new(nm: NewMarket) -> None:
        out.append(nm)

    s2 = Scanner(env.rest, env.catalog, env.repo, env.clock, window_min=10, sweep_lookback_hours=24, on_new=on_new)
    await s2.sweep()
    assert await s2.tick() == [] and out == []


async def test_missed_lifecycle_is_caught_by_sweep_after_restart(env: Env) -> None:
    env.at(OPEN + timedelta(minutes=3))  # bot comes up three minutes after the open
    env.fake.markets = [market("KXHIGHPHIL-26OCT23-B63.5", "KXHIGHPHIL-26OCT23"),
                        market("OTHERSERIES-X-1", "OTHERSERIES-X")]
    seen = await env.scanner.sweep()
    assert seen == 1, "only included series are queried"
    assert [n.ticker for n in await env.scanner.tick()] == ["KXHIGHPHIL-26OCT23-B63.5"]
    sweep_calls = [r for r in env.fake.calls if r.url.path.endswith("/markets")]
    assert all(r.url.params["mve_filter"] == "exclude" and "series_ticker" in r.url.params for r in sweep_calls)


async def test_market_found_long_after_open_is_not_new(env: Env) -> None:
    env.at(OPEN + timedelta(hours=2))
    env.fake.markets = [market("KXHIGHPHIL-26OCT23-B63.5", "KXHIGHPHIL-26OCT23")]
    await env.scanner.sweep()
    assert await env.scanner.tick() == []
    assert env.repo.has_market("KXHIGHPHIL-26OCT23-B63.5"), "still recorded, just not new"


async def test_results_and_status_from_lifecycle(env: Env) -> None:
    await env.scanner.on_lifecycle("market_lifecycle_v2", env.created())
    t = "KXHIGHPHIL-26OCT23-B63.5"
    await env.scanner.on_lifecycle("market_lifecycle_v2", {"event_type": "activated", "market_ticker": t})
    assert env.repo.get_market(t).status == "active"  # type: ignore[union-attr]
    new_close = int((OPEN + timedelta(hours=50)).timestamp())
    await env.scanner.on_lifecycle("market_lifecycle_v2",
                                   {"event_type": "close_date_updated", "market_ticker": t, "close_ts": new_close})
    assert env.repo.get_market(t).close_time == OPEN + timedelta(hours=50)  # type: ignore[union-attr]
    await env.scanner.on_lifecycle("market_lifecycle_v2",
                                   {"event_type": "determined", "market_ticker": t, "result": "yes"})
    row = env.repo.get_market(t)
    assert row is not None and row.status == "determined" and row.result == "yes"
    await env.scanner.on_lifecycle("market_lifecycle_v2", {"event_type": "settled", "market_ticker": t})
    row = env.repo.get_market(t)
    assert row is not None and row.status == "finalized" and row.result == "yes", "settled keeps the result"


async def test_lifecycle_for_unknown_market_is_ignored(env: Env) -> None:
    await env.scanner.on_lifecycle("market_lifecycle_v2",
                                   {"event_type": "determined", "market_ticker": "NOT-OURS", "result": "no"})
    assert not env.repo.has_market("NOT-OURS")


# ---------------------------------------------------------------- results

async def test_results_updater_backfills_closed_markets_incl_historical(env: Env) -> None:
    for t in ("KXHIGHPHIL-26OCT23-B63.5", "KXHIGHPHIL-26OCT23-B65.5"):
        await env.scanner.on_lifecycle("market_lifecycle_v2", env.created(t))
    env.at(OPEN + timedelta(hours=48))  # both closed
    env.fake.markets = [market("KXHIGHPHIL-26OCT23-B63.5", "KXHIGHPHIL-26OCT23", status="finalized", result="yes")]
    env.fake.historical = {"KXHIGHPHIL-26OCT23-B65.5": market("KXHIGHPHIL-26OCT23-B65.5", "KXHIGHPHIL-26OCT23",
                                                              status="finalized", result="no")}
    updated = await ResultsUpdater(env.rest, env.repo, env.clock).refresh()
    assert updated == 2
    assert env.repo.get_market("KXHIGHPHIL-26OCT23-B63.5").result == "yes"  # type: ignore[union-attr]
    assert env.repo.get_market("KXHIGHPHIL-26OCT23-B65.5").result == "no"  # type: ignore[union-attr]
    assert env.repo.unresolved_markets() == []


async def test_results_updater_ignores_markets_not_yet_closed(env: Env) -> None:
    await env.scanner.on_lifecycle("market_lifecycle_v2", env.created())
    assert await ResultsUpdater(env.rest, env.repo, env.clock).refresh() == 0


# -------------------------------------------------------------- watchlist

async def test_watchlist_add_expire_remove() -> None:
    clock = FakeClock(OPEN.timestamp())
    watched: list[list[str]] = []
    unwatched: list[list[str]] = []

    async def on_w(t: list[str]) -> None:
        watched.append(t)

    async def on_u(t: list[str]) -> None:
        unwatched.append(t)

    wl = Watchlist(clock, 60, on_watch=on_w, on_unwatch=on_u)
    nm = NewMarket("A", "E", "S", "Climate and Weather", OPEN, None)
    assert await wl.add(nm) is True and await wl.add(nm) is False
    await wl.add(NewMarket("B", "E", "S", None, OPEN, None))
    assert watched == [["A"], ["B"]] and len(wl) == 2 and "A" in wl
    clock.advance(59 * 60)
    assert await wl.expire() == []
    await wl.remove("B", reason="position_opened")
    clock.advance(61)
    assert await wl.expire() == ["A"]
    assert unwatched == [["B"], ["A"]] and len(wl) == 0


async def test_restart_resumes_watches_with_original_expiry(env: Env) -> None:
    await env.scanner.on_lifecycle("market_lifecycle_v2", env.created())
    env.at(OPEN + timedelta(seconds=5))
    await env.scanner.tick()  # emitted at 14:00:05
    env.at(OPEN + timedelta(minutes=40))  # restart 40 minutes later
    found = env.scanner.recently_emitted(timedelta(minutes=60))
    assert [nm.ticker for nm, _ in found] == ["KXHIGHPHIL-26OCT23-B63.5"]
    watched: list[list[str]] = []

    async def on_w(t: list[str]) -> None:
        watched.append(t)

    async def on_u(t: list[str]) -> None:
        pass

    wl = Watchlist(env.clock, 60, on_watch=on_w, on_unwatch=on_u)
    nm, at_ = found[0]
    assert await wl.add(nm, added_at=at_) is True and watched == [["KXHIGHPHIL-26OCT23-B63.5"]]
    env.at(OPEN + timedelta(minutes=60, seconds=6))
    assert await wl.expire() == ["KXHIGHPHIL-26OCT23-B63.5"], "expires 60 min after the ORIGINAL emission"
    late = Watchlist(env.clock, 60, on_watch=on_w, on_unwatch=on_u)
    assert await late.add(nm, added_at=at_) is False, "a watch that would already have ended is not resumed"
