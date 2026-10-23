"""The order gate: only the risk manager can approve; approvals are single-use and short-lived."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot.risk.approval import APPROVAL_TTL_SEC, Approved, CancelRequest, OrderRequest, _issue


def order(**overrides: object) -> OrderRequest:
    base: dict[str, object] = dict(ticker="KXHIGHPHIL-26OCT23-B63.5", intent="buy_yes", count=Decimal("10"),
                                   yes_price=Decimal("0.42"), client_order_id="cid-1")
    base.update(overrides)
    return OrderRequest(**base)  # type: ignore[arg-type]


def test_approved_cannot_be_constructed_directly() -> None:
    with pytest.raises(PermissionError):
        Approved(request=order(), issued_at=0.0)
    with pytest.raises(PermissionError):
        Approved(request=order(), issued_at=0.0, _token=object())


def test_approval_is_single_use() -> None:
    a = _issue(order(), now=100.0)
    a.spend(now=100.0)
    with pytest.raises(PermissionError, match="already used"):
        a.spend(now=100.0)


def test_stale_approval_is_refused() -> None:
    a = _issue(order(), now=100.0)
    with pytest.raises(PermissionError, match="stale"):
        a.spend(now=100.0 + APPROVAL_TTL_SEC + 0.01)


@pytest.mark.parametrize(("intent", "side"), [
    ("buy_yes", "bid"), ("sell_no", "bid"), ("sell_yes", "ask"), ("buy_no", "ask"),
])
def test_intent_maps_to_v2_yes_leg_book_side(intent: str, side: str) -> None:
    # V2 orders quote the YES leg only: buying NO is selling YES.
    assert order(intent=intent).book_side == side


def test_buy_no_wire_format() -> None:
    # Spec: "Buy No at 100 - yes_bid" -> V2: ask on YES at yes_bid.
    body = order(intent="buy_no", yes_price=Decimal("0.63"), count=Decimal("2.5"),
                 expiration_time=1_800_000_030, exchange_index=0).to_wire()
    assert body == {
        "ticker": "KXHIGHPHIL-26OCT23-B63.5", "client_order_id": "cid-1", "side": "ask",
        "count": "2.50", "price": "0.6300", "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross", "post_only": False, "reduce_only": False,
        "expiration_time": 1_800_000_030, "exchange_index": 0,
    }


@pytest.mark.parametrize("bad", [
    dict(count=Decimal("0")),
    dict(count=Decimal("-1")),
    dict(yes_price=Decimal("0")),
    dict(yes_price=Decimal("1")),
    dict(yes_price=Decimal("42")),  # cents passed where dollars belong
    dict(reduce_only=True),  # Kalshi: reduce_only requires IOC
    dict(time_in_force="immediate_or_cancel", expiration_time=1),
])
def test_invalid_order_requests_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        order(**bad)


def test_reduce_only_ioc_is_valid() -> None:
    assert order(reduce_only=True, time_in_force="immediate_or_cancel").to_wire()["reduce_only"] is True


def test_cancel_request_is_gated_too() -> None:
    a = _issue(CancelRequest(order_id="o1", ticker="T"), now=0.0)
    assert isinstance(a.spend(now=0.0), CancelRequest)
