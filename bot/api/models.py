"""
Typed views of Kalshi REST responses (OpenAPI 3.34.0, docs/API_NOTES.md).

Money is Decimal dollars and contract counts are Decimal, parsed directly
from Kalshi's fixed-point strings -- never float (API_NOTES section 5).
Unknown fields are ignored because Kalshi adds fields often; the fields the
bot depends on are typed, so a breaking rename fails loudly at parse time.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _blank_to_none(v: object) -> object:
    return None if v == "" else v


Dollars = Decimal
Count = Decimal
OptionalStr = Annotated[str | None, BeforeValidator(_blank_to_none)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PriceRange(_Model):
    start: Dollars
    end: Dollars
    step: Dollars


MarketStatus = Literal["initialized", "inactive", "active", "closed", "determined", "disputed", "amended", "finalized"]
StrikeType = Literal[
    "greater", "greater_or_equal", "less", "less_or_equal", "between", "functional", "custom", "structured"
]


class Market(_Model):
    ticker: str
    event_ticker: str
    status: MarketStatus
    market_type: str | None = None
    yes_sub_title: str | None = None
    no_sub_title: str | None = None
    created_time: datetime | None = None
    open_time: datetime | None = None
    close_time: datetime | None = None
    expected_expiration_time: datetime | None = None
    expiration_time: datetime | None = None
    settlement_ts: datetime | None = None
    yes_bid_dollars: Dollars | None = None
    yes_bid_size_fp: Count | None = None
    yes_ask_dollars: Dollars | None = None
    yes_ask_size_fp: Count | None = None
    no_bid_dollars: Dollars | None = None
    no_ask_dollars: Dollars | None = None
    last_price_dollars: Dollars | None = None
    volume_fp: Count | None = None
    result: Annotated[Literal["yes", "no", "scalar"] | None, BeforeValidator(_blank_to_none)] = None
    settlement_value_dollars: Dollars | None = None
    strike_type: StrikeType | None = None
    floor_strike: float | None = None
    cap_strike: float | None = None
    rules_primary: str | None = None
    rules_secondary: str | None = None
    price_level_structure: str | None = None
    price_ranges: list[PriceRange] = Field(default_factory=list)
    exchange_index: int | None = None


class MarketsPage(_Model):
    markets: list[Market]
    cursor: OptionalStr = None


class MarketResponse(_Model):
    market: Market


class Event(_Model):
    event_ticker: str
    series_ticker: str
    title: str | None = None
    sub_title: str | None = None
    mutually_exclusive: bool
    collateral_return_type: str | None = None
    strike_date: datetime | None = None
    exchange_index: int | None = None


class EventResponse(_Model):
    event: Event
    markets: list[Market] = Field(default_factory=list)


FeeType = Literal["quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees", "flat"]


class Series(_Model):
    ticker: str
    title: str | None = None
    frequency: str | None = None
    category: str
    categories: list[str] = Field(default_factory=list)
    tags: list[str] | None = None
    # Optional: a category-wide sweep meets old series that may omit these, and
    # nothing reads them yet (Phase 4's fee function will, per market).
    fee_type: FeeType | None = None
    fee_multiplier: float | None = None
    exchange_index: int | None = None


class SeriesResponse(_Model):
    series: Series


class SeriesList(_Model):
    series: list[Series]


PriceLevel = tuple[Dollars, Count]


class OrderbookFp(_Model):
    """Bids only, ascending -- best bid is LAST (API_NOTES section 4)."""

    yes_dollars: list[PriceLevel] = Field(default_factory=list)
    no_dollars: list[PriceLevel] = Field(default_factory=list)


class OrderbookResponse(_Model):
    orderbook_fp: OrderbookFp


class IndexedBalance(_Model):
    exchange_index: int
    balance: Dollars


class Balance(_Model):
    balance: int  # cents
    balance_dollars: Dollars
    portfolio_value: int
    updated_ts: int
    balance_breakdown: list[IndexedBalance] = Field(default_factory=list)

    def for_shard(self, exchange_index: int) -> Dollars | None:
        """Balances are per exchange shard (API_NOTES section 11)."""
        for b in self.balance_breakdown:
            if b.exchange_index == exchange_index:
                return b.balance
        return None


class MarketPosition(_Model):
    ticker: str
    exchange_index: int | None = None
    position_fp: Count  # signed: positive = long YES
    market_exposure_dollars: Dollars
    realized_pnl_dollars: Dollars
    fees_paid_dollars: Dollars
    total_traded_dollars: Dollars | None = None


class PositionsPage(_Model):
    market_positions: list[MarketPosition]
    cursor: OptionalStr = None


BookSide = Literal["bid", "ask"]


class Order(_Model):
    order_id: str
    client_order_id: str | None = None
    ticker: str
    book_side: BookSide
    outcome_side: Literal["yes", "no"] | None = None
    type: str | None = None
    status: Literal["resting", "canceled", "executed"]
    yes_price_dollars: Dollars
    fill_count_fp: Count
    remaining_count_fp: Count
    initial_count_fp: Count | None = None
    taker_fees_dollars: Dollars | None = None
    maker_fees_dollars: Dollars | None = None
    created_time: datetime | None = None
    last_update_time: datetime | None = None
    expiration_time: datetime | None = None
    exchange_index: int | None = None


class OrdersPage(_Model):
    orders: list[Order]
    cursor: OptionalStr = None


class Fill(_Model):
    fill_id: str
    trade_id: str
    order_id: str
    market_ticker: str
    book_side: BookSide
    outcome_side: Literal["yes", "no"] | None = None
    count_fp: Count
    yes_price_dollars: Dollars
    is_taker: bool
    fee_cost: Dollars
    created_time: datetime | None = None
    ts: int | None = None
    exchange_index: int | None = None


class FillsPage(_Model):
    fills: list[Fill]
    cursor: OptionalStr = None


class CreateOrderResponse(_Model):
    order_id: str
    client_order_id: str | None = None
    fill_count: Count
    remaining_count: Count
    average_fill_price: Dollars | None = None
    average_fee_paid: Dollars | None = None
    ts_ms: int


class CancelOrderResponse(_Model):
    order_id: str
    client_order_id: str | None = None
    reduced_by: Count
    ts_ms: int


class BucketLimit(_Model):
    bucket_capacity: float
    refill_rate: float


class AccountLimits(_Model):
    usage_tier: str
    read: BucketLimit
    write: BucketLimit


class EndpointCost(_Model):
    method: str
    path: str
    cost: float


class EndpointCosts(_Model):
    default_cost: float
    endpoint_costs: list[EndpointCost] = Field(default_factory=list)


class HistoricalCutoff(_Model):
    market_settled_ts: datetime
    trades_created_ts: datetime
    orders_updated_ts: datetime


class Trade(_Model):
    """A public trade print (GET /markets/trades)."""

    trade_id: str
    ticker: str
    yes_price_dollars: Dollars
    count_fp: Count
    created_time: datetime
    taker_side: str | None = None


class TradesPage(_Model):
    trades: list[Trade]
    cursor: OptionalStr = None


class CandleOHLC(_Model):
    open_dollars: Dollars | None = None
    high_dollars: Dollars | None = None
    low_dollars: Dollars | None = None
    close_dollars: Dollars | None = None


class Candlestick(_Model):
    """One period of GET /series/{s}/markets/{t}/candlesticks. yes_bid/yes_ask
    are the book's best prices over the period; close = at the period's end."""

    end_period_ts: int
    yes_bid: CandleOHLC = Field(default_factory=CandleOHLC)
    yes_ask: CandleOHLC = Field(default_factory=CandleOHLC)
    price: CandleOHLC = Field(default_factory=CandleOHLC)
    volume_fp: Count = Decimal(0)
    open_interest_fp: Count | None = None


class Candlesticks(_Model):
    ticker: str
    candlesticks: list[Candlestick] = Field(default_factory=list)
