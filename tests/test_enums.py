from anybroker import Env, OrderType, Side, TimeInForce
from anybroker.venues.ibkr import IB_ORDER_TYPES, IB_TIF


def test_sides():
    assert Side.BUY.opposite is Side.SELL and Side.SELL.opposite is Side.BUY
    assert Side.BUY.sign == 1 and Side.SELL.sign == -1


def test_env_has_no_third_state():
    assert {e.value for e in Env} == {'paper', 'live'}
    assert Env.LIVE.is_live and not Env.PAPER.is_live


def test_every_neutral_order_type_maps_to_an_ib_string():
    assert set(IB_ORDER_TYPES) == set(OrderType)
    assert IB_ORDER_TYPES[OrderType.MARKET] == 'MKT'
    assert IB_ORDER_TYPES[OrderType.STOP_LIMIT] == 'STP LMT'
    assert IB_ORDER_TYPES[OrderType.TRAILING_STOP] == 'TRAIL'


def test_every_time_in_force_maps_to_an_ib_string():
    assert set(IB_TIF) == set(TimeInForce)
    assert IB_TIF[TimeInForce.DAY] == 'DAY'
