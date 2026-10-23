"""Engine: highest confidence, agreeing average, min-confidence cutoff, logging every result."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import pytest

from bot.config import FairValueConfig
from bot.fair_value.engine import FairValueEngine, build_engine
from bot.fair_value.historical import HistoricalModel, HistoryIndex
from bot.fair_value.sibling import SiblingModel
from bot.fair_value.types import EventContext, FairValue, MarketInfo

M = MarketInfo("T", "E", "S", "between", 1, 2)
CTX = EventContext("E", "S", True, (M,), {}, datetime(2026, 10, 24, tzinfo=UTC))


class Fixed:
    def __init__(self, name: str, cents: str | None, conf: float = 0.0) -> None:
        self.name = name
        self._fv = None if cents is None else FairValue(D(cents), conf, name)

    def estimate(self, market: MarketInfo, ctx: EventContext) -> FairValue | None:
        return self._fv


def engine(*models: Fixed, sink: Any = None) -> FairValueEngine:
    return FairValueEngine(list(models), min_confidence=0.6, agree_within_cents=5, sink=sink)


def test_highest_confidence_wins_when_models_disagree() -> None:
    fv = engine(Fixed("sibling", "40", 0.8), Fixed("historical", "60", 0.5)).estimate(M, CTX)
    assert fv is not None and fv.cents == D(40) and fv.model == "sibling" and fv.confidence == 0.8


def test_agreeing_models_are_confidence_weighted() -> None:
    fv = engine(Fixed("sibling", "40", 0.8), Fixed("historical", "44", 0.4)).estimate(M, CTX)
    assert fv is not None and fv.model == "historical+sibling"
    # Rounded to 4 places so a recurring decimal fits exact 6-place storage.
    assert fv.cents == ((D(40) * D("0.8") + D(44) * D("0.4")) / D("1.2")).quantize(D("0.0001"))  # 41.3333
    assert fv.confidence == 0.8, "agreement does not inflate confidence"


def test_agreement_boundary_is_inclusive_at_5c() -> None:
    assert engine(Fixed("a", "40", 0.8), Fixed("b", "45", 0.4)).estimate(M, CTX).model == "a+b"  # type: ignore[union-attr]
    assert engine(Fixed("a", "40", 0.8), Fixed("b", "45.01", 0.4)).estimate(M, CTX).model == "a"  # type: ignore[union-attr]


def test_three_models_only_the_group_around_the_best_is_averaged() -> None:
    fv = engine(Fixed("a", "40", 0.9), Fixed("b", "43", 0.6), Fixed("c", "70", 0.7)).estimate(M, CTX)
    assert fv is not None and fv.model == "a+b" and fv.inputs["group"] == ["a", "b"]
    assert "c" in fv.inputs["models"], "the outvoted model is still reported"


def test_no_estimate_below_min_confidence() -> None:
    assert engine(Fixed("a", "40", 0.59), Fixed("b", "41", 0.5)).estimate(M, CTX) is None
    assert engine(Fixed("a", None), Fixed("b", None)).estimate(M, CTX) is None
    assert engine(Fixed("a", "40", 0.6)).estimate(M, CTX) is not None, "exactly at the threshold passes"


def test_capped_historical_alone_can_never_trigger() -> None:
    # Decision D: historical max_confidence 0.5 < min_confidence 0.6.
    assert engine(Fixed("sibling", None), Fixed("historical", "30", 0.5)).estimate(M, CTX) is None


def test_every_model_result_is_sent_to_the_sink() -> None:
    seen: list[str] = []
    engine(Fixed("a", "40", 0.9), Fixed("b", "70", 0.2), Fixed("c", None),
           sink=lambda m, c, fv: seen.append(fv.model)).estimate(M, CTX)
    assert seen == ["a", "b"]


def test_build_engine_from_config() -> None:
    cfg = FairValueConfig.model_validate({
        "min_confidence": 0.6, "min_history": 20, "models": ["sibling", "historical"], "agree_within_cents": 5,
        "sibling": {"uncertainty_scale_cents": 15, "liquidity_ref_contracts": 50},
        "historical": {"max_confidence": 0.5, "seasonal_window_days": 45}})
    eng = build_engine(cfg, HistoryIndex([]))
    assert [type(m) for m in eng.models] == [SiblingModel, HistoricalModel]
    with pytest.raises(ValueError, match="no history"):
        build_engine(cfg, None)
    ext = cfg.model_copy(update={"models": ["external"]})
    with pytest.raises(ValueError, match="decision E"):
        build_engine(ext, None)
