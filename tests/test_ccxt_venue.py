"""The ccxt venue, against a fake exchange."""

import pytest

from anybroker import (
    Env,
    NotFoundError,
    OrderSpec,
    OrderType,
    Side,
    TimeInForce,
    UnsupportedError,
)
from anybroker.venues.ccxt_venue import CcxtVenue
from fakes import FakeCcxt


def venue(env=Env.LIVE, **kw):
    ex = FakeCcxt(**kw)
    return CcxtVenue('fake', env, exchange=ex)


# ── environment ─────────────────────────────────────────────────────────────


def test_paper_turns_on_the_sandbox():
    v = venue(Env.PAPER)
    assert v.exchange.sandbox_mode is True


def test_live_does_not_touch_the_sandbox():
    assert venue(Env.LIVE).exchange.sandbox_mode is None


def test_paper_is_refused_where_the_exchange_has_no_sandbox():
    with pytest.raises(UnsupportedError, match='no paper environment'):
        CcxtVenue('fake', Env.PAPER, exchange=FakeCcxt(sandbox=False))


def test_capabilities_come_from_ccxts_own_has_table():
    plain = CcxtVenue(
        'fake',
        Env.LIVE,
        exchange=FakeCcxt(has={'createStopOrder': False, 'createTriggerOrder': False}),
    )
    assert OrderType.STOP not in plain.capabilities.order_types
    assert OrderType.MARKET in plain.capabilities.order_types


def test_even_market_and_limit_are_derived_not_assumed():
    """An exchange advertising neither gets neither, rather than both for free."""
    bare = CcxtVenue(
        'fake',
        Env.LIVE,
        exchange=FakeCcxt(
            has={
                'createOrder': False,
                'createMarketOrder': False,
                'createLimitOrder': False,
                'createStopOrder': False,
                'createTriggerOrder': False,
            }
        ),
    )
    assert bare.capabilities.order_types == frozenset()


def test_the_generic_create_order_covers_market_and_limit():
    """All 104 real exchanges report createOrder; ten omit createMarketOrder."""
    generic = CcxtVenue(
        'fake',
        Env.LIVE,
        exchange=FakeCcxt(
            has={
                'createOrder': True,
                'createMarketOrder': False,
                'createLimitOrder': False,
            }
        ),
    )
    assert OrderType.MARKET in generic.capabilities.order_types
    assert OrderType.LIMIT in generic.capabilities.order_types


def test_an_unsupported_order_type_is_refused_before_it_is_sent():
    plain = CcxtVenue(
        'fake',
        Env.LIVE,
        exchange=FakeCcxt(has={'createStopOrder': False, 'createTriggerOrder': False}),
    )
    with pytest.raises(UnsupportedError, match='does not offer stop'):
        plain.stop('BTC/USD', Side.SELL, 1.0, 59000.0)
    assert plain.exchange.created == []


# ── order translation ───────────────────────────────────────────────────────


def test_a_market_order():
    v = venue()
    v.market('BTC/USD', Side.BUY, 0.5)
    call = v.exchange.created[-1]
    assert call['type'] == 'market' and call['side'] == 'buy'
    assert call['amount'] == 0.5 and call['price'] is None


def test_a_limit_order_carries_its_price():
    v = venue()
    v.limit('BTC/USD', Side.BUY, 0.5, 60000.0)
    call = v.exchange.created[-1]
    assert call['type'] == 'limit' and call['price'] == 60000.0


def test_a_stop_becomes_a_trigger_price():
    v = venue()
    v.stop('BTC/USD', Side.SELL, 0.5, 59000.0)
    call = v.exchange.created[-1]
    assert call['params']['triggerPrice'] == 59000.0


def test_time_in_force_is_passed_when_it_is_not_the_default():
    v = venue()
    v.place(
        OrderSpec(
            'BTC/USD',
            Side.BUY,
            1,
            OrderType.LIMIT,
            limit_price=60000.0,
            time_in_force=TimeInForce.IOC,
        )
    )
    assert v.exchange.created[-1]['params']['timeInForce'] == 'IOC'


def test_reduce_only_is_passed_through():
    v = venue()
    v.place(OrderSpec('BTC/USD', Side.SELL, 1, reduce_only=True))
    assert v.exchange.created[-1]['params']['reduceOnly'] is True


def test_a_client_order_id_is_passed_through():
    v = venue()
    v.place(OrderSpec('BTC/USD', Side.BUY, 1, client_order_id='abc'))
    assert v.exchange.created[-1]['params']['clientOrderId'] == 'abc'


def test_a_bracket_is_refused_where_the_exchange_cannot_express_one():
    v = venue()
    with pytest.raises(UnsupportedError, match='cannot attach'):
        v.place(OrderSpec('BTC/USD', Side.BUY, 1, take_profit=61000.0))
    assert v.exchange.created == []


def test_a_bracket_is_sent_where_the_exchange_can():
    v = venue(has={'createOrderWithTakeProfitAndStopLoss': True})
    v.place(OrderSpec('BTC/USD', Side.BUY, 1, take_profit=61000.0, stop_loss=59000.0))
    params = v.exchange.created[-1]['params']
    assert params['takeProfit'] == {'triggerPrice': 61000.0}
    assert params['stopLoss'] == {'triggerPrice': 59000.0}


# ── reads ───────────────────────────────────────────────────────────────────


def test_a_short_position_comes_back_negative():
    v = venue(
        positions=[
            {
                'symbol': 'BTC/USD:USD',
                'contracts': 3,
                'side': 'short',
                'entryPrice': 60000.0,
                'unrealizedPnl': -120.0,
            }
        ]
    )
    pos = v.list_positions()[0]
    assert pos.qty == -3 and pos.side is Side.SELL
    assert pos.open_pnl == -120.0


def test_the_account_picks_a_dollar_currency():
    account = venue().get_account()
    assert account.currency == 'USD' and account.equity == 25000.0


def test_a_quote_has_a_midpoint():
    q = venue().get_quote('BTC/USD')
    assert q.bid == 60000.0 and q.ask == 60010.0 and q.mid == 60005.0


# ── flatten ─────────────────────────────────────────────────────────────────


def test_flatten_closes_with_reduce_only():
    v = venue(
        positions=[
            {
                'symbol': 'BTC/USD:USD',
                'contracts': 2,
                'side': 'long',
                'entryPrice': 60000.0,
            }
        ]
    )
    v.flatten('BTC/USD:USD')
    call = v.exchange.created[-1]
    assert call['side'] == 'sell' and call['amount'] == 2
    assert call['params']['reduceOnly'] is True


def test_flattening_nothing_raises():
    with pytest.raises(NotFoundError, match='no open position'):
        venue().flatten('BTC/USD')
