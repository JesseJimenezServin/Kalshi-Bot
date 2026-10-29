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
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MAX_HISTORY_SPAN_SEC = 12 * 86400
_DRAW = re.compile(r"Will (.+) vs\. (.+) end in a draw")


@dataclass(frozen=True)
class PmMoneyline:
    """One Polymarket game's result market(s). Index 0 = first team in the slug, 1 = second;
    soccer adds index 2 = draw (three separate Yes/No markets; the Yes token of each)."""

    event_slug: str
    teams: tuple[str, str]  # slug abbreviations, slug order
    outcomes: tuple[str, ...]  # labels, same order as token_ids
    token_ids: tuple[str, ...]
    game_start: datetime
    created: datetime | None
    volume: Decimal


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace(" ", "T").replace("+00", "+00:00") if s.endswith("+00") else s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(UTC)


def _subseq(abbr: str, name: str) -> bool:
    """'sun' in 'Sunderland AFC', 'mun' in 'Manchester United' (letters in order, first letter first)."""
    name = name.lower()
    if not abbr or not name.lstrip().startswith(abbr[0]):
        return False
    it = iter(name)
    return all(ch in it for ch in abbr)


def parse_moneyline(event: dict[str, Any], league: str) -> PmMoneyline | None:
    """From a Gamma event whose slug is '<league>-<team1>-<team2>-YYYY-MM-DD' (a game, not props)."""
    slug = event.get("slug") or ""
    parts = slug.split("-")
    if len(parts) != 6 or parts[0] != league:
        return None
    teams = (parts[1], parts[2])
    ml = [m for m in event.get("markets", []) if m.get("sportsMarketType") == "moneyline"]
    if len(ml) == 1:
        m = ml[0]
        outcomes = json.loads(m.get("outcomes") or "[]")
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if len(outcomes) != 2 or len(tokens) != 2:
            return None
        labels, toks = tuple(outcomes), tuple(tokens)
    elif len(ml) == 3:  # soccer: "Will A win?", "Will B win?", "Will A vs. B end in a draw?"
        draw = [m for m in ml if "draw" in (m.get("question") or "").lower()]
        if len(draw) != 1:
            return None
        mo = _DRAW.match(draw[0].get("question") or "")
        if not mo:
            return None
        names = mo.groups()  # slug order: held in all 106 games checkable by abbreviation on 2026-10-27
        if _subseq(teams[0], names[1]) and _subseq(teams[1], names[0]) and not (
                _subseq(teams[0], names[0]) and _subseq(teams[1], names[1])):
            return None  # the abbreviations say the draw question is in the other order
        ordered = []
        for name in names:
            hit = [m for m in ml if m is not draw[0] and (m.get("question") or "").startswith(f"Will {name} win")]
            if len(hit) != 1:
                return None
            ordered.append(hit[0])
        ordered.append(draw[0])
        if ordered[0] is ordered[1]:
            return None
        toks = tuple(json.loads(m.get("clobTokenIds") or "[]")[0] for m in ordered)  # the Yes token
        labels = tuple(m.get("question") or "" for m in ordered)
        m = ml[0]
    else:
        return None
    start = _dt(m.get("gameStartTime"))
    if start is None:
        return None
    return PmMoneyline(slug, teams, labels, toks, start, _dt(min((x.get("createdAt") or "9") for x in ml)),
                       Decimal(str(sum(float(x.get("volumeNum") or 0) for x in ml))))


class PolymarketPublic:
    def __init__(self, min_interval_sec: float = 0.25) -> None:
        self._http = httpx.AsyncClient(timeout=60)
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
            try:
                r = await self._http.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError):  # transient: 5 league runs died on one 2026-10-27
                if attempt == 4:
                    raise
                await asyncio.sleep(3.0 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    async def closed_game_moneylines(self, tag: str, league: str, since: datetime,
                                     max_pages: int = 60) -> list[PmMoneyline]:
        out: list[PmMoneyline] = []
        for page in range(max_pages):
            try:
                evs = await self._get(f"{GAMMA}/events", {"tag_slug": tag, "closed": "true", "limit": 100,
                                                          "offset": 100 * page, "order": "endDate",
                                                          "ascending": "false",
                                                          "end_date_min": since.strftime("%Y-%m-%dT%H:%M:%SZ")})
            except httpx.HTTPStatusError as e:  # deep offsets answer 422 (seen at offset 2100)
                if e.response.status_code == 422:
                    break
                raise
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
