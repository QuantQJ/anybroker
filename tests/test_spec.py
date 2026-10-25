"""An order is described once and validated before any venue sees it."""

import pytest

from anybroker import ConfigError, OrderSpec, OrderType, Side, TimeInForce


def test_a_market_order_is_the_default():
    spec = OrderSpec('ABCZ6', Side.BUY, 2)
    assert spec.order_type is OrderType.MARKET
    assert spec.time_in_force is TimeInForce.DAY
    assert not spec.is_bracket


def test_strings_are_coerced_to_the_enums():
    spec = OrderSpec('BTC/USD', 'buy', 1, 'limit', limit_price=60000.0)
    assert spec.side is Side.BUY and spec.order_type is OrderType.LIMIT


@pytest.mark.parametrize(
    'order_type', [OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP_LIMIT]
)
def test_types_that_need_a_limit_price_say_so(order_type):
    with pytest.raises(ConfigError, match='limit_price'):
        OrderSpec('ABCZ6', Side.BUY, 1, order_type, stop_price=1.0, trail_amount=1.0)


@pytest.mark.parametrize('order_type', [OrderType.STOP, OrderType.STOP_LIMIT])
def test_types_that_need_a_stop_price_say_so(order_type):
    with pytest.raises(ConfigError, match='stop_price'):
        OrderSpec('ABCZ6', Side.SELL, 1, order_type, limit_price=1.0)


def test_a_trailing_stop_needs_a_distance():
    with pytest.raises(ConfigError, match='trail_amount or trail_percent'):
        OrderSpec('ABCZ6', Side.SELL, 1, OrderType.TRAILING_STOP)


def test_a_trailing_stop_takes_one_distance_not_two():
    with pytest.raises(ConfigError, match='not both'):
        OrderSpec(
            'ABCZ6',
            Side.SELL,
            1,
            OrderType.TRAILING_STOP,
            trail_amount=5.0,
            trail_percent=1.0,
        )


def test_a_market_order_carries_no_price():
    with pytest.raises(ConfigError, match='no limit_price'):
        OrderSpec('ABCZ6', Side.BUY, 1, OrderType.MARKET, limit_price=20000.0)


@pytest.mark.parametrize('qty', [0, -1, -0.5])
def test_quantity_must_be_positive(qty):
    with pytest.raises(ConfigError, match='positive'):
        OrderSpec('ABCZ6', Side.BUY, qty)


def test_a_missing_symbol_is_refused():
    with pytest.raises(ConfigError, match='symbol'):
        OrderSpec('', Side.BUY, 1)


def test_good_til_date_needs_a_date():
    with pytest.raises(ConfigError, match='good_til'):
        OrderSpec('ABCZ6', Side.BUY, 1, time_in_force=TimeInForce.GTD)


# ── brackets ────────────────────────────────────────────────────────────────


def test_children_take_the_opposite_side():
    spec = OrderSpec('ABCZ6', Side.BUY, 2, take_profit=20200.0, stop_loss=20050.0)
    assert spec.is_bracket and spec.exit_side is Side.SELL
    children = spec.child_specs()
    assert [c.label for c in children] == ['take_profit', 'stop_loss']
    assert all(c.side is Side.SELL for c in children)
    assert all(c.qty == 2 for c in children)
    assert all(c.reduce_only for c in children)


def test_a_short_entry_brackets_with_buys():
    spec = OrderSpec('ABCZ6', Side.SELL, 1, take_profit=20000.0, stop_loss=20100.0)
    assert all(c.side is Side.BUY for c in spec.child_specs())


def test_the_target_is_a_limit_and_the_stop_is_a_stop():
    children = OrderSpec(
        'ABCZ6', Side.BUY, 1, take_profit=20200.0, stop_loss=20050.0
    ).child_specs()
    assert children[0].order_type is OrderType.LIMIT
    assert children[0].limit_price == 20200.0
    assert children[1].order_type is OrderType.STOP
    assert children[1].stop_price == 20050.0


def test_one_child_is_allowed():
    assert len(OrderSpec('ABCZ6', Side.BUY, 1, stop_loss=20050.0).child_specs()) == 1


def test_no_bracket_means_no_children():
    assert OrderSpec('ABCZ6', Side.BUY, 1).child_specs() == []


def test_nothing_invents_a_price():
    """A bracket with no numbers is not a bracket. No default distance."""
    spec = OrderSpec('ABCZ6', Side.BUY, 1)
    assert spec.take_profit is None and spec.stop_loss is None
