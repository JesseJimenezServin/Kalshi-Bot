"""Sibling model: outcomes summing to 100, strike monotonicity, clipping, missing siblings."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from bot.fair_value.sibling import SiblingModel, _isotonic, is_exhaustive_buckets
from bot.fair_value.types import EventContext, MarketInfo, Quote

NOW = datetime(2026, 10, 24, 14, 5, tzinfo=UTC)
MODEL = SiblingModel(uncertainty_scale_cents=15, liquidity_ref_contracts=50)


def mk(t: str, st: str, floor: float | None, cap: float | None, ev: str = "E") -> MarketInfo:
    return MarketInfo(t, ev, "S", st, floor, cap)


# The real KXHIGHPHIL-26OCT23 ladder (docs/API_NOTES.md section 9).
PHIL = [mk("T59", "less", None, 59), mk("B59.5", "between", 59, 60), mk("B61.5", "between", 61, 62),
        mk("B63.5", "between", 63, 64), mk("B65.5", "between", 65, 66), mk("T66", "greater", 66, None)]


def q(bid: str | None, ask: str | None, size: str = "100") -> Quote:
    return Quote(D(bid) if bid else None, D(ask) if ask else None,
                 D(size) if bid else D(0), D(size) if ask else D(0))


def ctx(markets: list[MarketInfo], quotes: dict[str, Quote | None], exclusive: bool = True) -> EventContext:
    return EventContext("E", "S", exclusive, tuple(markets), quotes, NOW)


def test_real_weather_ladder_is_exhaustive() -> None:
    assert is_exhaustive_buckets(PHIL)
    assert not is_exhaustive_buckets(PHIL[1:]), "missing lower tail"
    assert not is_exhaustive_buckets(PHIL[:-1]), "missing upper tail"
    gap = [m for m in PHIL if m.ticker != "B61.5"]
    assert not is_exhaustive_buckets(gap), "a missing middle bucket leaves a hole"


def test_exclusive_fair_is_100_minus_sibling_mids() -> None:
    # Live mids on 2026-10-22 summed to 100.5: T59 1.5, B59.5 2.5, B61.5 26.5,
    # B63.5 62.5, B65.5 6.5, T66 1.0 (ask 2, no bid).
    quotes = {"T59": q("1", "2"), "B59.5": q("2", "3"), "B61.5": q("26", "27"), "B63.5": q("62", "63"),
              "B65.5": q("6", "7"), "T66": q(None, "2")}
    fv = MODEL.estimate(PHIL[3], ctx(PHIL, quotes))
    assert fv is not None and fv.model == "sibling"
    assert fv.cents == D(100) - (D("1.5") + D("2.5") + D("26.5") + D("6.5") + D("1"))  # 62.0
    assert fv.inputs["exhaustive"] is True and fv.inputs["siblings"] == 5
    assert "B63.5" not in fv.inputs["sibling_mids"], "own price is never an input"


def test_outcomes_summing_to_100_gives_fair_equal_to_mid() -> None:
    quotes = {"T59": q("4", "6"), "B59.5": q("9", "11"), "B61.5": q("24", "26"), "B63.5": q("34", "36"),
              "B65.5": q("19", "21"), "T66": q("4", "6")}  # mids 5+10+25+35+20+5 = 100
    for m in PHIL:
        fv = MODEL.estimate(m, ctx(PHIL, quotes))
        mid = (quotes[m.ticker].bid + quotes[m.ticker].ask) / 2  # type: ignore[operator]
        assert fv is not None and fv.cents == mid


def test_own_price_does_not_move_its_fair_value() -> None:
    base = {"T59": q("4", "6"), "B59.5": q("9", "11"), "B61.5": q("24", "26"), "B63.5": q("34", "36"),
            "B65.5": q("19", "21"), "T66": q("4", "6")}
    wild = {**base, "B63.5": q("1", "3")}  # B63.5 dumped to 2c
    a = MODEL.estimate(PHIL[3], ctx(PHIL, base))
    b = MODEL.estimate(PHIL[3], ctx(PHIL, wild))
    assert a is not None and b is not None and a.cents == b.cents == D(35)


def test_clipping_to_1_99() -> None:
    hi = {"T59": q("40", "42"), "B59.5": q("40", "42"), "B61.5": q("30", "32"), "B63.5": q("1", "2"),
          "B65.5": q("1", "2"), "T66": q("1", "2")}  # others of B63.5 sum to 115.5
    fv = MODEL.estimate(PHIL[3], ctx(PHIL, hi))
    assert fv is not None and fv.cents == D(1) and D(fv.inputs["raw_cents"]) < 0
    pair = [mk("X", "custom", None, None), mk("Y", "custom", None, None)]
    fv = MODEL.estimate(pair[0], ctx(pair, {"Y": q(None, "1")}))  # Y's mid 0.5 -> raw 99.5
    assert fv is not None and fv.cents == D(99) and D(fv.inputs["raw_cents"]) == D("99.5")


def test_missing_sibling_price_means_no_estimate() -> None:
    quotes: dict[str, Quote | None] = {"T59": q("1", "2"), "B59.5": q("2", "3"), "B61.5": None,
                                       "B63.5": q("62", "63"), "B65.5": q("6", "7"), "T66": q(None, "2")}
    assert MODEL.estimate(PHIL[3], ctx(PHIL, quotes)) is None
    quotes["B61.5"] = q(None, None)  # book exists but is empty
    assert MODEL.estimate(PHIL[3], ctx(PHIL, quotes)) is None


def test_confidence_falls_with_wider_sibling_spreads() -> None:
    def conf(spread: int) -> float:
        h = D(spread) / 2
        mids = {"T59": 5, "B59.5": 10, "B61.5": 25, "B63.5": 35, "B65.5": 20, "T66": 5}
        quotes = {t: Quote(D(m) - h, D(m) + h, D(100), D(100)) for t, m in mids.items()}
        fv = MODEL.estimate(PHIL[3], ctx(PHIL, quotes))
        assert fv is not None
        return fv.confidence

    c1, c5, c20 = conf(1), conf(5), conf(20)
    assert c1 > c5 > c20
    # Exactly: five siblings, half-spread h each -> u = h * sqrt(5).
    assert c5 == pytest.approx(math.exp(-2.5 * math.sqrt(5) / 15), abs=1e-6)


def test_thin_siblings_reduce_confidence_but_never_zero_it() -> None:
    mids = {"T59": 5, "B59.5": 10, "B61.5": 25, "B63.5": 35, "B65.5": 20, "T66": 5}
    deep = {t: Quote(D(m) - 1, D(m) + 1, D(100), D(100)) for t, m in mids.items()}
    thin = {t: Quote(D(m) - 1, D(m) + 1, D(1), D(1)) for t, m in mids.items()}
    a = MODEL.estimate(PHIL[3], ctx(PHIL, deep))
    b = MODEL.estimate(PHIL[3], ctx(PHIL, thin))
    assert a is not None and b is not None
    assert b.confidence == pytest.approx(a.confidence * (0.5 + 0.5 * 0.02), abs=1e-6)
    assert b.confidence > 0


def test_one_sided_siblings_are_bounds_not_guesses() -> None:
    assert Quote(None, D(4)).mid_and_halfwidth() == (D(2), D(2))
    assert Quote(D(96), None).mid_and_halfwidth() == (D(98), D(2))
    assert Quote(None, None).mid_and_halfwidth() is None


def test_unproven_exhaustiveness_halves_confidence() -> None:
    m = [mk("A", "custom", None, None), mk("B", "custom", None, None), mk("C", "custom", None, None)]
    quotes = {"A": q("29", "31"), "B": q("29", "31"), "C": q("39", "41")}
    fv = MODEL.estimate(m[2], ctx(m, quotes))
    phil_like = {"T59": q("4", "6"), "B59.5": q("9", "11"), "B61.5": q("24", "26"), "B63.5": q("34", "36"),
                 "B65.5": q("19", "21"), "T66": q("4", "6")}
    ref = MODEL.estimate(PHIL[3], ctx(PHIL, phil_like))
    assert fv is not None and ref is not None
    assert fv.inputs["exhaustive"] is False and fv.cents == D(40)
    # Same spreads/depth, but 2 siblings instead of 5, so compare the formula directly.
    assert fv.confidence == pytest.approx(math.exp(-math.sqrt(2) / 15) * 1.0 * 0.5, abs=1e-6)


# ----------------------------------------------------------------- ladders

LADDER = [mk(f"A{x}", "greater", x, None) for x in (40, 50, 60, 70, 80)]


def test_ladder_interpolates_between_neighbours() -> None:
    quotes = {"A40": q("89", "91"), "A50": q("69", "71"), "A70": q("29", "31"), "A80": q("9", "11"),
              "A60": q("1", "3")}  # A60 itself is wildly off; it must not matter
    fv = MODEL.estimate(LADDER[2], ctx(LADDER, quotes, exclusive=False))
    assert fv is not None and fv.inputs["kind"] == "ladder"
    assert fv.cents == D(50)  # halfway between 70 (at 50) and 30 (at 70)


def test_ladder_enforces_monotonicity_on_violating_siblings() -> None:
    # A50 quoted BELOW A70: impossible (P(>50) >= P(>70)). Isotonic pools them.
    quotes = {"A40": q("89", "91"), "A50": q("29", "31"), "A70": q("49", "51"), "A80": q("9", "11")}
    fv = MODEL.estimate(LADDER[2], ctx(LADDER, quotes, exclusive=False))
    assert fv is not None
    lo, hi = (D(v) for v in fv.inputs["neighbours"].values())
    assert lo >= hi, "fitted P(>50) can never be below P(>70)"
    assert fv.cents == D(40)  # pooled at 40 -> 40 at the midpoint


def test_ladder_never_extrapolates() -> None:
    quotes = {t.ticker: q("49", "51") for t in LADDER}
    assert MODEL.estimate(LADDER[0], ctx(LADDER, quotes, exclusive=False)) is None
    assert MODEL.estimate(LADDER[-1], ctx(LADDER, quotes, exclusive=False)) is None


def test_below_ladders_increase_with_strike() -> None:
    lad = [mk(f"L{x}", "less", None, x) for x in (40, 50, 60)]
    quotes = {"L40": q("19", "21"), "L60": q("59", "61")}
    fv = MODEL.estimate(lad[1], ctx(lad, quotes, exclusive=False))
    assert fv is not None and fv.cents == D(40) and fv.inputs["direction"] == "below"


def test_isotonic_regression() -> None:
    out = _isotonic([D(3), D(1), D(2)], [1.0, 1.0, 1.0], increasing=True)
    assert out == [D(2), D(2), D(2)]
    out = _isotonic([D(9), D(5), D(7), D(1)], [1.0, 1.0, 1.0, 1.0], increasing=False)
    assert out == [D(9), D(6), D(6), D(1)]


def test_non_ladder_non_exclusive_market_gets_no_estimate() -> None:
    m = [mk("X", "custom", None, None), mk("Y", "custom", None, None)]
    assert MODEL.estimate(m[0], ctx(m, {"Y": q("40", "42")}, exclusive=False)) is None


def test_quote_from_kalshi_dollars_treats_zero_size_as_absent() -> None:
    qq = Quote.from_dollars(D("0.0000"), D("0.0200"), D("0.00"), D("91.00"))
    assert qq.bid is None and qq.ask == D(2) and qq.ask_size == D(91)
    assert Quote.from_dollars(D("0.62"), D("1.0"), D("10"), D("0")).ask is None
