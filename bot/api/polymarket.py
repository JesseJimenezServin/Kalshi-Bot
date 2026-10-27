"""
Read-only Polymarket public data, used as an outside fair-price reference
for Kalshi sports markets. No key: the Gamma (events/markets) and CLOB
price-history endpoints are public. The owner's Polymarket key
(~/.polymarket/credentials.env) is deliberately NOT used here; nothing
trades on Polymarket.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MAX_HISTORY_SPAN_SEC = 12 * 86400


@dataclass(frozen=True)
class PmMoneyline:
    """One Polymarket game's moneyline market."""

    event_slug: str
    teams: tuple[str, str]  # slug abbreviations, slug order (away, home)
    outcomes: tuple[str, str]  # outcome names, same order as teams
    token_ids: tuple[str, str]
    game_start: datetime
    created: datetime | None
    volume: Decimal


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace(" ", "T").replace("+00", "+00:00") if s.endswith("+00") else s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(UTC)


def parse_moneyline(event: dict[str, Any], league: str) -> PmMoneyline | None:
    """From a Gamma event whose slug is '<league>-<away>-<home>-YYYY-MM-DD' (a game, not props)."""
    slug = event.get("slug") or ""
    parts = slug.split("-")
    if len(parts) != 6 or parts[0] != league:
        return None
    ml = [m for m in event.get("markets", []) if m.get("sportsMarketType") == "moneyline"]
    if len(ml) != 1:
        return None
    m = ml[0]
    outcomes = json.loads(m.get("outcomes") or "[]")
    tokens = json.loads(m.get("clobTokenIds") or "[]")
    start = _dt(m.get("gameStartTime"))
    if len(outcomes) != 2 or len(tokens) != 2 or start is None:
        return None
    return PmMoneyline(slug, (parts[1], parts[2]), (outcomes[0], outcomes[1]), (tokens[0], tokens[1]), start,
                       _dt(m.get("createdAt")), Decimal(str(m.get("volumeNum") or 0)))


class PolymarketPublic:
    def __init__(self, min_interval_sec: float = 0.25) -> None:
        self._http = httpx.AsyncClient(timeout=30)
        self._gap = min_interval_sec
        self._last = 0.0

    async def __aenter__(self) -> PolymarketPublic:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        for attempt in range(5):
            loop = asyncio.get_running_loop()
            wait = self._last + self._gap - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = loop.time()
            r = await self._http.get(url, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    async def closed_game_moneylines(self, tag: str, league: str, since: datetime,
                                     max_pages: int = 40) -> list[PmMoneyline]:
        out: list[PmMoneyline] = []
        for page in range(max_pages):
            evs = await self._get(f"{GAMMA}/events", {"tag_slug": tag, "closed": "true", "limit": 100,
                                                      "offset": 100 * page, "order": "endDate",
                                                      "ascending": "false"})
            if not evs:
                break
            for e in evs:
                ml = parse_moneyline(e, league)
                if ml is not None and ml.game_start >= since:
                    out.append(ml)
            last_end = _dt(evs[-1].get("endDate"))
            if last_end is not None and last_end < since:
                break
        return out

    async def price_history(self, token_id: str, start_ts: int, end_ts: int,
                            fidelity_min: int = 60) -> list[tuple[int, Decimal]]:
        """Chunked: the endpoint refuses ranges much over ~2 weeks ("interval is too long"; 13 d ok, 27 d not)."""
        out: dict[int, Decimal] = {}
        a = start_ts
        while a < end_ts:
            b = min(end_ts, a + MAX_HISTORY_SPAN_SEC)
            d = await self._get(f"{CLOB}/prices-history", {"market": token_id, "startTs": a, "endTs": b,
                                                           "fidelity": fidelity_min})
            out.update((int(p["t"]), Decimal(str(p["p"]))) for p in d.get("history", []))
            a = b
        return sorted(out.items())
