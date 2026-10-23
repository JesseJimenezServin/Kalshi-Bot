from __future__ import annotations

from decimal import Decimal as D

from bot.data.orderbook import OrderBook, OrderBookStore


def book() -> OrderBook:
    b = OrderBook("T")
    # The docs' own example: YES bids 0.01x200, 0.42x13; NO bids 0.01x100, 0.56x17.
    b.apply_snapshot(yes=[(D("0.01"), D("200")), (D("0.42"), D("13"))],
                     no=[(D("0.01"), D("100")), (D("0.56"), D("17"))], clock=1, now=0.0)
    return b


def test_asks_are_derived_from_opposite_bids() -> None:
    b = book()
    assert b.best_yes_bid() == (D("0.42"), D("13"))
    assert b.best_yes_ask() == (D("0.44"), D("17"))  # 1 - best NO bid 0.56
    assert b.best_no_bid() == (D("0.56"), D("17"))
    assert b.best_no_ask() == (D("0.58"), D("13"))  # 1 - best YES bid 0.42
    assert b.mid_yes() == D("0.43")
    assert not b.is_crossed()


def test_new_book_is_stale_until_snapshot() -> None:
    b = OrderBook("T")
    assert b.stale and b.best_yes_bid() is None and b.mid_yes() is None
    assert not book().stale


def test_delta_adds_and_reduces_levels() -> None:
    b = book()
    b.apply_delta("yes", D("0.43"), D("5"), clock=2, now=1.0)
    assert b.best_yes_bid() == (D("0.43"), D("5"))
    b.apply_delta("yes", D("0.43"), D("-2"), clock=3, now=2.0)
    assert b.best_yes_bid() == (D("0.43"), D("3"))


def test_phantom_level_regression_fractional_cancel_removes_level() -> None:
    """The liquidity lab's phantom-level bug: float residue (5.55e-17) kept a
    cancelled level alive as the best bid. Decimal cancels exactly."""
    b = book()
    for d in ["0.1", "0.2", "0.3"]:
        b.apply_delta("yes", D("0.50"), D(d), clock=None, now=0.0)
    for d in ["-0.3", "-0.2", "-0.1"]:
        b.apply_delta("yes", D("0.50"), D(d), clock=None, now=0.0)
    assert D("0.50") not in b.yes_bids
    assert b.best_yes_bid() == (D("0.42"), D("13"))
    # In floats this sequence does NOT return to zero, which was the bug.
    assert (0.1 + 0.2 + 0.3) - 0.3 - 0.2 - 0.1 != 0.0


def test_over_cancel_removes_level_rather_than_going_negative() -> None:
    b = book()
    b.apply_delta("no", D("0.56"), D("-20"), clock=None, now=0.0)
    assert D("0.56") not in b.no_bids
    assert b.best_yes_ask() == (D("0.99"), D("100"))


def test_snapshot_drops_zero_levels_and_replaces_state() -> None:
    b = book()
    b.apply_snapshot(yes=[(D("0.30"), D("0")), (D("0.20"), D("5"))], no=[], clock=9, now=5.0)
    assert b.yes_bids == {D("0.20"): D("5")} and b.no_bids == {}
    assert b.best_yes_ask() is None


def test_depth_at_or_better_for_buying_each_contract() -> None:
    b = OrderBook("T")
    b.apply_snapshot(yes=[(D("0.40"), D("30")), (D("0.38"), D("70"))],
                     no=[(D("0.55"), D("20")), (D("0.54"), D("40")), (D("0.50"), D("99"))], clock=1, now=0.0)
    # Buy YES at <= 0.46 takes NO bids at >= 0.54: 20 + 40.
    assert b.depth_at_or_better("yes", D("0.46")) == D("60")
    assert b.depth_at_or_better("yes", D("0.45")) == D("20")
    assert b.depth_at_or_better("yes", D("0.44")) == D("0")
    # Buy NO at <= 0.62 takes YES bids at >= 0.38: 30 + 70.
    assert b.depth_at_or_better("no", D("0.62")) == D("100")
    assert b.bid_depth_at_or_better("yes", D("0.39")) == D("30")


def test_crossed_book_detected() -> None:
    b = OrderBook("T")
    b.apply_snapshot(yes=[(D("0.60"), D("1"))], no=[(D("0.45"), D("1"))], clock=1, now=0.0)
    assert b.is_crossed()


def test_snapshot_levels_are_best_first_strings() -> None:
    bids, asks = book().snapshot_levels(depth=1)
    assert bids == [["0.42", "13"]] and asks == [["0.44", "17"]]
    bids, asks = book().snapshot_levels(depth=10)
    assert [p for p, _ in bids] == ["0.42", "0.01"]
    assert [p for p, _ in asks] == ["0.44", "0.99"]


def test_store_ensure_drop_and_stale() -> None:
    s = OrderBookStore()
    s.ensure("A").apply_snapshot(yes=[], no=[], clock=1, now=0.0)
    s.ensure("B")
    assert sorted(s.tickers()) == ["A", "B"]
    s.mark_stale(["A"])
    assert s.get("A").stale  # type: ignore[union-attr]
    s.drop("A")
    assert s.get("A") is None
