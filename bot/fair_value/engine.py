"""
Fair-value engine (plan 3.8): run every enabled model and combine them.

The spec: "use the highest-confidence result, or a confidence-weighted
average when two or more agree within 5c. If no model returns confidence
>= min_confidence, return no estimate."

Interpretation (docs/PLAN.md): the agreeing group is the models within
`agree_within_cents` of the highest-confidence result. Its confidence-
weighted average is the estimate, and its confidence is the BEST member's,
not a sum: two models agreeing is evidence, but this does not pretend to
know how independent they are. Calibration (Phase 5) can revisit that.

Every model result -- not only the winner -- goes to the sink (the
fair_values table), so each model can be calibrated on its own later.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

from bot.config import FairValueConfig
from bot.fair_value.historical import HistoricalModel, HistoryIndex
from bot.fair_value.sibling import SiblingModel
from bot.fair_value.types import EventContext, FairValue, FairValueModel, MarketInfo, clip_cents

Sink = Callable[[MarketInfo, EventContext, FairValue], None]


class FairValueEngine:
    def __init__(self, models: Sequence[FairValueModel], *, min_confidence: float, agree_within_cents: float,
                 sink: Sink | None = None) -> None:
        if not models:
            raise ValueError("at least one model is required")
        self.models = list(models)
        self._min = min_confidence
        self._agree = Decimal(str(agree_within_cents))
        self._sink = sink

    def results(self, market: MarketInfo, ctx: EventContext) -> list[FairValue]:
        out = []
        for m in self.models:
            fv = m.estimate(market, ctx)
            if fv is not None:
                out.append(fv)
                if self._sink is not None:
                    self._sink(market, ctx, fv)
        return out

    def estimate(self, market: MarketInfo, ctx: EventContext) -> FairValue | None:
        results = self.results(market, ctx)
        if not results:
            return None
        best = max(results, key=lambda r: r.confidence)
        if best.confidence < self._min:
            return None
        group = [r for r in results if abs(r.cents - best.cents) <= self._agree]
        considered = {r.model: {"cents": str(r.cents), "confidence": r.confidence} for r in results}
        if len(group) < 2:
            return FairValue(best.cents, best.confidence, best.model,
                             {"chosen": best.model, "models": considered, "detail": best.inputs})
        total_w = sum(Decimal(str(r.confidence)) for r in group)
        if total_w == 0:
            return None
        cents = sum((r.cents * Decimal(str(r.confidence)) for r in group), Decimal(0)) / total_w
        combined = FairValue(clip_cents(cents.quantize(Decimal("0.0001"))), best.confidence,
                             "+".join(sorted(r.model for r in group)),
                             {"chosen": "agreeing average", "group": sorted(r.model for r in group),
                              "models": considered})
        return combined


def build_engine(cfg: FairValueConfig, history: HistoryIndex | None, sink: Sink | None = None) -> FairValueEngine:
    models: list[FairValueModel] = []
    for name in cfg.models:
        if name == "sibling":
            models.append(SiblingModel(cfg.sibling.uncertainty_scale_cents, cfg.sibling.liquidity_ref_contracts))
        elif name == "historical":
            if history is None:
                raise ValueError("historical model enabled but no history index was loaded")
            models.append(HistoricalModel(history, min_history=cfg.min_history,
                                          max_confidence=cfg.historical.max_confidence,
                                          seasonal_window_days=cfg.historical.seasonal_window_days))
        elif name == "external":
            raise ValueError("the external model is not built yet (docs/PLAN.md decision E)")
    return FairValueEngine(models, min_confidence=cfg.min_confidence, agree_within_cents=cfg.agree_within_cents,
                           sink=sink)
