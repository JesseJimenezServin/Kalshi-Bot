"""
Client-side token bucket mirroring Kalshi's (docs/API_NOTES.md section 3).

Kalshi meters per ACCOUNT with separate read and write buckets; most calls
cost 10 tokens. Waiting here before sending keeps us under the exchange's
limit instead of relying on 429s. The defaults are the measured Basic tier
(read 200/s, capacity 600; write 100/s, capacity 100), overridable from
GET /account/limits at startup.
"""

from __future__ import annotations

import asyncio

from bot.clock import Clock

_EPSILON = 1e-6
_MIN_WAIT_SEC = 0.001


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float, clock: Clock) -> None:
        if rate_per_sec <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = rate_per_sec
        self.capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._last = clock.now()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock.now()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, cost: float) -> None:
        """Wait until `cost` tokens are available, then spend them."""
        if cost > self.capacity:
            raise ValueError(f"cost {cost} exceeds bucket capacity {self.capacity}")
        async with self._lock:
            while True:
                self._refill()
                # Tolerance and a floor on the wait, because at unix-time
                # magnitude (~1.8e9 s) a float cannot represent sub-microsecond
                # steps: a bucket a hair short would sleep an amount too small
                # to move the clock, refill nothing, and spin forever.
                if self._tokens >= cost - _EPSILON:
                    self._tokens = max(0.0, self._tokens - cost)
                    return
                await self._clock.sleep(max(_MIN_WAIT_SEC, (cost - self._tokens) / self.rate))

    def reconfigure(self, rate_per_sec: float, capacity: float) -> None:
        self._refill()
        self.rate = rate_per_sec
        self.capacity = capacity
        self._tokens = min(self._tokens, capacity)
