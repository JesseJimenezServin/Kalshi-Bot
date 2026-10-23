"""
Sibling model (plan 3.2-3.4): price a market from the OTHER markets in its
event. The market's own price is never an input -- that is the point: it is
what we are testing for mispricing.

Mutually exclusive events (all daily weather events, docs/API_NOTES.md
section 9): at most one market resolves Yes, so if the set is also
EXHAUSTIVE (exactly one resolves Yes)
        fair_i = 100 - sum(mid_j for j != i),  clipped to 1-99.
If it is mutually exclusive but we cannot verify exhaustiveness, the formula
is only an upper bound, so confidence is halved.

Strike ladders (non-exclusive "above X" / "below X" markets in one event):
P(above X) can only fall as X rises. Fit the siblings' mids with isotonic
(monotone) regression, then interpolate linearly at this market's strike.
No extrapolation: a market at either end of the ladder gets no estimate.

Confidence = exp(-u / scale) * liquidity * structure, where
  u          combined price uncertainty of the inputs, cents. A sibling's
             uncertainty is half its spread; one-sided, it is half the range
             it could lie in (Quote.mid_and_halfwidth). Independent errors
             add in quadrature.
  liquidity  0.5 + 0.5 * mean(min(1, depth_j / ref)): thin siblings can
             halve confidence but never zero it.
  structure  1.0, or 0.5 for an exclusive set not proven exhaustive.
"""

from __future__ import annotations

import math
from decimal import Decimal

from bot.fair_value.types import HUNDRED, EventContext, FairValue, MarketInfo, Quote, clip_cents

UP_TYPES = {"greater", "greater_or_equal"}
DOWN_TYPES = {"less", "less_or_equal"}


def is_exhaustive_buckets(markets: list[MarketInfo]) -> bool:
    """A lower tail, contiguous 'between' buckets, and an upper tail.

    Contiguous allows a gap of at most 1 between one bucket's cap and the
    next floor, because weather strikes are whole degrees ('59-60' then
    '61-62' leaves nothing between)."""
    lows = [m for m in markets if m.strike_type in DOWN_TYPES]
    highs = [m for m in markets if m.strike_type in UP_TYPES]
    mids = sorted((m for m in markets if m.strike_type == "between"), key=lambda m: m.floor_strike or 0.0)
    if len(lows) != 1 or len(highs) != 1 or len(lows) + len(highs) + len(mids) != len(markets):
        return False
    edges: list[tuple[float | None, float | None]] = [(None, lows[0].cap_strike)]
    edges += [(m.floor_strike, m.cap_strike) for m in mids]
    edges.append((highs[0].floor_strike, None))
    for (_, prev_cap), (nxt_floor, _) in zip(edges, edges[1:], strict=False):
        if prev_cap is None or nxt_floor is None or not 0 <= nxt_floor - prev_cap <= 1:
            return False
    return True


def _liquidity(quotes: list[Quote], ref: float) -> float:
    if not quotes:
        return 0.0
    return 0.5 + 0.5 * sum(min(1.0, float(q.depth) / ref) for q in quotes) / len(quotes)


class SiblingModel:
    name = "sibling"

    def __init__(self, uncertainty_scale_cents: float, liquidity_ref_contracts: float) -> None:
        self._scale = uncertainty_scale_cents
        self._ref = liquidity_ref_contracts

    def estimate(self, market: MarketInfo, ctx: EventContext) -> FairValue | None:
        if ctx.mutually_exclusive:
            return self._exclusive(market, ctx)
        if market.strike_type in UP_TYPES | DOWN_TYPES:
            return self._ladder(market, ctx)
        return None

    # ------------------------------------------------------ mutually exclusive

    def _exclusive(self, market: MarketInfo, ctx: EventContext) -> FairValue | None:
        others = [m for m in ctx.markets if m.ticker != market.ticker]
        if not others:
            return None
        quotes: list[Quote] = []
        mids: dict[str, str] = {}
        var = Decimal(0)
        total = Decimal(0)
        for m in others:
            q = ctx.quotes.get(m.ticker)
            mh = q.mid_and_halfwidth() if q is not None else None
            if q is None or mh is None:
                # A sibling with no price at all makes 100 - sum undefined.
                return None
            mid, hw = mh
            total += mid
            var += hw * hw
            quotes.append(q)
            mids[m.ticker] = str(mid)
        u = float(var.sqrt())
        exhaustive = is_exhaustive_buckets(list(ctx.markets))
        conf = math.exp(-u / self._scale) * _liquidity(quotes, self._ref) * (1.0 if exhaustive else 0.5)
        raw = HUNDRED - total
        return FairValue(clip_cents(raw), round(conf, 6), self.name, {
            "kind": "exclusive", "raw_cents": str(raw), "sibling_mids": mids, "uncertainty_cents": round(u, 4),
            "exhaustive": exhaustive, "two_sided_siblings": sum(q.two_sided for q in quotes),
            "siblings": len(quotes),
        })

    # ------------------------------------------------------------- ladders

    def _ladder(self, market: MarketInfo, ctx: EventContext) -> FairValue | None:
        up = market.strike_type in UP_TYPES
        same = [m for m in ctx.markets if (m.strike_type in UP_TYPES) == up and m.strike_type in UP_TYPES | DOWN_TYPES]
        x0 = _strike(market)
        if x0 is None:
            return None
        pts: list[tuple[float, Decimal, Decimal, Quote]] = []
        for m in same:
            if m.ticker == market.ticker:
                continue
            x = _strike(m)
            q = ctx.quotes.get(m.ticker)
            mh = q.mid_and_halfwidth() if q is not None else None
            if x is None or q is None or mh is None:
                continue
            pts.append((x, mh[0], mh[1], q))
        lo = [p for p in pts if p[0] < x0]
        hi = [p for p in pts if p[0] > x0]
        if not lo or not hi:
            return None  # no extrapolation off either end of the ladder
        pts.sort(key=lambda p: p[0])
        fitted = _isotonic([p[1] for p in pts], [1 / float(p[2] ** 2 + Decimal("0.25")) for p in pts],
                           increasing=not up)
        i_lo = max(i for i, p in enumerate(pts) if p[0] < x0)
        i_hi = min(i for i, p in enumerate(pts) if p[0] > x0)
        x_lo, x_hi = pts[i_lo][0], pts[i_hi][0]
        f_lo, f_hi = fitted[i_lo], fitted[i_hi]
        w = Decimal(str((x0 - x_lo) / (x_hi - x_lo)))
        fair = f_lo + (f_hi - f_lo) * w
        # Price uncertainty of the two neighbours, plus interpolation risk:
        # anywhere between the neighbours is consistent with monotonicity.
        u_price = math.sqrt(float(pts[i_lo][2] ** 2 + pts[i_hi][2] ** 2)) / 2
        u_interp = abs(float(f_lo - f_hi)) / 4
        u = u_price + u_interp
        conf = math.exp(-u / self._scale) * _liquidity([pts[i_lo][3], pts[i_hi][3]], self._ref)
        return FairValue(clip_cents(fair), round(conf, 6), self.name, {
            "kind": "ladder", "direction": "above" if up else "below", "strike": x0,
            "neighbours": {str(x_lo): str(f_lo), str(x_hi): str(f_hi)},
            "uncertainty_cents": round(u, 4), "points": len(pts),
        })


def _strike(m: MarketInfo) -> float | None:
    if m.strike_type in UP_TYPES:
        return m.floor_strike
    if m.strike_type in DOWN_TYPES:
        return m.cap_strike
    return None


def _isotonic(values: list[Decimal], weights: list[float], increasing: bool) -> list[Decimal]:
    """Weighted pool-adjacent-violators: the closest monotone sequence."""
    vals = values if increasing else [-v for v in values]
    blocks: list[list[float]] = []  # [weighted sum, weight, count]
    for v, w in zip(vals, weights, strict=True):
        blocks.append([float(v) * w, w, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, wt, n = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += wt
            blocks[-1][2] += n
    out: list[Decimal] = []
    for s, wt, n in blocks:
        out += [Decimal(str(round(s / wt, 6)))] * int(n)
    return out if increasing else [-v for v in out]
