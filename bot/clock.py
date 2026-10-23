"""
The only source of time in the bot.

Nothing else calls time.time() or datetime.now(). Backtest replay (plan 5.4)
must reproduce paper results exactly, which is only possible if every
timestamp and every timeout comes from one injectable clock.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Unix time, seconds."""
        ...

    def utcnow(self) -> datetime:
        ...

    async def sleep(self, seconds: float) -> None:
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def utcnow(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Manually advanced clock for tests and replay. sleep() advances time."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def utcnow(self) -> datetime:
        return datetime.fromtimestamp(self._t, UTC)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time does not run backwards")
        self._t += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)
