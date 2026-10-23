from __future__ import annotations

import pytest

from bot.api.ratelimit import TokenBucket
from bot.clock import FakeClock


async def test_burst_up_to_capacity_without_waiting() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_sec=200, capacity=600, clock=clock)
    t0 = clock.now()
    for _ in range(60):  # 60 requests x 10 tokens = the full 3 s burst
        await b.acquire(10)
    assert clock.now() == t0


async def test_waits_exactly_for_refill_when_empty() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_sec=100, capacity=100, clock=clock)
    await b.acquire(100)
    t0 = clock.now()
    await b.acquire(10)
    assert clock.now() - t0 == pytest.approx(0.1, abs=1e-3)


async def test_refills_over_time_but_never_above_capacity() -> None:
    clock = FakeClock()
    b = TokenBucket(rate_per_sec=10, capacity=50, clock=clock)
    await b.acquire(50)
    clock.advance(1000)
    assert b.available == 50


async def test_cost_above_capacity_is_an_error() -> None:
    b = TokenBucket(rate_per_sec=10, capacity=50, clock=FakeClock())
    with pytest.raises(ValueError):
        await b.acquire(51)


def test_reconfigure_clamps_tokens() -> None:
    b = TokenBucket(rate_per_sec=200, capacity=600, clock=FakeClock())
    b.reconfigure(rate_per_sec=100, capacity=100)
    assert b.available == 100 and b.rate == 100


async def test_does_not_spin_when_float_precision_hides_tiny_waits() -> None:
    """Regression: at unix-time magnitude a sub-microsecond sleep doesn't move
    the clock, so a bucket a hair short used to loop forever."""
    clock = FakeClock(start=1_800_000_000.0)
    b = TokenBucket(rate_per_sec=100, capacity=100, clock=clock)
    for _ in range(200):
        await b.acquire(10)
    assert clock.now() - 1_800_000_000.0 == pytest.approx(19.0, abs=0.01)
