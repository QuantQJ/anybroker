"""Tradovate, against a fake tradovate-client. No credentials, no network."""

import pytest

from anybroker import (
    ConfigError,
    Env,
    NotFoundError,
    OrderSpec,
    OrderType,
    RejectedError,
    Side,
    TimeInForce,
    UnsupportedError,
)
from anybroker.venues.tradovate import (
    TV_ENV,
    TV_ORDER_TYPES,
    TV_SIDE,
    TV_TIF,
    TradovateVenue,
)


class FakeTradovate:
    """Records calls. Mirrors the surface TradovateVenue uses."""

    def __init__(self, *, positions=None, orders=(), reply=None, raises=None):
        self.calls = []
        self._positions = positions if positions is not None else {}
        self._orders = list(orders)
        self._reply = reply if reply is not None else {'orderId': 4242}
        self._raises = raises

    def _log(self, name, *a, **kw):
        self.calls.append({'name': name, 'args': a, 'kw': kw})
        if self._raises:
            raise self._raises

    # reads
    def snapshot(self, account=None):
        self._log('snapshot', account=account)
        import types

        views = {
            s: types.SimpleNamespace(net_pos=q, net_price=p, contract_id=cid, symbol=s)
            for s, (q, p, cid) in self._positions.items()
        }
        return types.SimpleNamespace(
            account=types.SimpleNamespace(name='DEMO1234', id=7),
            cash=types.SimpleNamespace(
                total_cash=50000.0, open_pnl=25.0, realized_pnl=-10.0
            ),
            positions=views,
        )

    def orders(self):
        self._log('orders')
        return list(self._orders)

    def working_orders(self):
        self._log('working_orders')
        return list(self._orders)

    def order(self, order_id):
        self._log('order', order_id)
        return {'orderId': order_id, 'ordStatus': 'Working'}

    # writes
    def place(self, **kw):
        self._log('place', **kw)
        return dict(self._reply)

    def place_oso(self, **kw):
        self._log('place_oso', **kw)
        return dict(self._reply)

    def bracket_leg(self, action, order_type, **kw):
        self._log('bracket_leg', action, order_type, **kw)
        return {'action': action, 'orderType': order_type, **kw}

    def cancel(self, order_id):
        self._log('cancel', order_id)
        return {'orderId': order_id, 'ordStatus': 'Cancelled'}

    def cancel_working(self):
        self._log('cancel_working')
        return [1, 2]

    def modify(self, order_id, **kw):
        self._log('modify', order_id, **kw)
        return {'orderId': order_id}

    def liquidate(self, contract_id, **kw):
        self._log('liquidate', contract_id, **kw)
        return {'orderId': 99}

    def flatten_all(self, cancel_first=True):
        self._log('flatten_all', cancel_first=cancel_first)
        return {'cancelled': [1], 'liquidated': [11]}


def venue(env=Env.PAPER, **kw):
    client = kw.pop('client', None) or FakeTradovate(**kw)
    return TradovateVenue(env, client=client)


def last(v, name):
    return [c for c in v.client.calls if c['name'] == name][-1]


# ── environment ─────────────────────────────────────────────────────────────


def test_paper_and_live_are_tradovates_two_hosts():
    assert TV_ENV == {Env.PAPER: 'demo', Env.LIVE: 'live'}


def test_env_is_required():
    with pytest.raises(TypeError):
        TradovateVenue(client=FakeTradovate())


def test_tradovate_has_a_paper_environment():
    assert venue().capabilities.supports_paper is True


def test_the_env_reaches_the_client_library(monkeypatch):
    """Built without an injected client, the venue hands the host string over."""
    import sys
    import types

    seen = {}

    module = types.ModuleType('tradovate_client')

    class Client:
        def __init__(self, env, **kw):
            seen['env'] = env
            seen['kw'] = kw

    module.TradovateClient = Client
    monkeypatch.setitem(sys.modules, 'tradovate_client', module)
    TradovateVenue(Env.LIVE, credential_path='/k.json').connect()
    assert seen['env'] == 'live'
    assert seen['kw']['credential_path'] == '/k.json'
    assert 'credentials' not in seen['kw'], 'unset options are not passed'


def test_a_missing_client_library_says_how_to_get_it(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, 'tradovate_client', None)
    with pytest.raises(ConfigError, match='tradovate-client is not installed'):
        TradovateVenue(Env.PAPER).connect()


# ── the published vocabulary ─────────────────────────────────────────────────


def test_every_neutral_order_type_maps_to_a_tradovate_string():
    assert set(TV_ORDER_TYPES) == set(OrderType)
    assert TV_ORDER_TYPES[OrderType.MARKET] == 'Market'
    assert TV_ORDER_TYPES[OrderType.STOP_LIMIT] == 'StopLimit'
    assert TV_ORDER_TYPES[OrderType.TRAILING_STOP] == 'TrailingStop'


def test_every_time_in_force_maps():
    assert set(TV_TIF) == set(TimeInForce)
    assert TV_TIF[TimeInForce.DAY] == 'Day' and TV_TIF[TimeInForce.GTC] == 'GTC'


def test_sides_use_tradovates_capitalisation():
    assert TV_SIDE == {Side.BUY: 'Buy', Side.SELL: 'Sell'}


# ── orders ──────────────────────────────────────────────────────────────────


def test_a_market_order():
    v = venue()
    order = v.market('MNQZ6', Side.BUY, 2)
    kw = last(v, 'place')['kw']
    assert kw['action'] == 'Buy' and kw['symbol'] == 'MNQZ6'
    assert kw['qty'] == 2 and kw['order_type'] == 'Market'
    assert kw['time_in_force'] == 'Day'
    assert 'price' not in kw and 'stop_price' not in kw
    assert order.id == '4242' and order.symbol == 'MNQZ6'


def test_a_limit_order_carries_its_price():
    v = venue()
    v.limit('MNQZ6', Side.BUY, 1, 20150.25)
    kw = last(v, 'place')['kw']
    assert kw['order_type'] == 'Limit' and kw['price'] == 20150.25


def test_a_stop_order_carries_its_stop():
    v = venue()
    v.stop('MNQZ6', Side.SELL, 1, 20100.0)
    kw = last(v, 'place')['kw']
    assert kw['order_type'] == 'Stop' and kw['stop_price'] == 20100.0


def test_a_stop_limit_carries_both():
    v = venue()
    v.stop_limit('MNQZ6', Side.SELL, 1, 20095.0, 20100.0)
    kw = last(v, 'place')['kw']
    assert kw['order_type'] == 'StopLimit'
    assert kw['price'] == 20095.0 and kw['stop_price'] == 20100.0


def test_a_trailing_stop_by_amount_goes_through_as_a_stop_price():
    v = venue()
    v.trailing_stop('MNQZ6', Side.SELL, 1, trail_amount=25.0)
    kw = last(v, 'place')['kw']
    assert kw['order_type'] == 'TrailingStop' and kw['stop_price'] == 25.0


def test_a_percent_trail_is_refused_rather_than_guessed():
    v = venue()
    with pytest.raises(UnsupportedError, match='does not take a trailing percent'):
        v.trailing_stop('MNQZ6', Side.SELL, 1, trail_percent=1.5)
    assert not [c for c in v.client.calls if c['name'] == 'place']


def test_tradovate_declares_an_absolute_trail_only():
    assert venue().capabilities.trailing_forms == frozenset({'amount'})


def test_time_in_force_is_passed():
    v = venue()
    v.place(
        OrderSpec(
            'MNQZ6',
            Side.BUY,
            1,
            OrderType.LIMIT,
            limit_price=1.0,
            time_in_force=TimeInForce.GTC,
        )
    )
    assert last(v, 'place')['kw']['time_in_force'] == 'GTC'


def test_a_client_order_id_is_passed():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 1, client_order_id='rule-7'))
    assert last(v, 'place')['kw']['client_order_id'] == 'rule-7'


def test_a_label_becomes_text_when_there_is_no_client_id():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 1, label='james'))
    assert last(v, 'place')['kw']['text'] == 'james'


def test_reduce_only_is_refused_rather_than_ignored():
    v = venue()
    with pytest.raises(UnsupportedError, match='reduce-only'):
        v.place(OrderSpec('MNQZ6', Side.SELL, 1, reduce_only=True))


def test_extra_passes_tradovate_only_fields_through():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 1, extra={'max_show': 1}))
    assert last(v, 'place')['kw']['max_show'] == 1


def test_an_account_is_passed_when_given():
    v = TradovateVenue(Env.PAPER, client=FakeTradovate(), account='DEMO1234')
    v.market('MNQZ6', Side.BUY, 1)
    assert last(v, 'place')['kw']['account'] == 'DEMO1234'


# ── brackets: one request, no naked entry ───────────────────────────────────


def test_a_bracket_uses_tradovates_one_sends_other():
    v = venue()
    order = v.place(
        OrderSpec('MNQZ6', Side.BUY, 2, take_profit=20200.0, stop_loss=20050.0)
    )
    assert not [c for c in v.client.calls if c['name'] == 'place'], (
        'a bracket must not go out as a bare entry first'
    )
    kw = last(v, 'place_oso')['kw']
    assert kw['action'] == 'Buy' and kw['order_type'] == 'Market'
    assert kw['bracket1']['action'] == 'Sell'
    assert kw['bracket1']['orderType'] == 'Limit'
    assert kw['bracket1']['price'] == 20200.0
    assert kw['bracket2']['orderType'] == 'Stop'
    assert kw['bracket2']['stop_price'] == 20050.0
    assert len(order.children) == 2


def test_a_stop_only_bracket_sends_one_leg():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 1, stop_loss=20050.0))
    kw = last(v, 'place_oso')['kw']
    assert 'bracket1' in kw and 'bracket2' not in kw


def test_a_short_entry_brackets_with_buys():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.SELL, 1, take_profit=20000.0, stop_loss=20100.0))
    kw = last(v, 'place_oso')['kw']
    assert kw['bracket1']['action'] == 'Buy'
    assert kw['bracket2']['action'] == 'Buy'


# ── reads ───────────────────────────────────────────────────────────────────


def test_positions_come_back_named():
    v = venue(positions={'MNQZ6': (2, 20100.5, 11), 'MESZ6': (-1, 5800.0, 12)})
    held = {p.symbol: p for p in v.list_positions()}
    assert held['MNQZ6'].qty == 2 and held['MNQZ6'].side is Side.BUY
    assert held['MESZ6'].qty == -1 and held['MESZ6'].side is Side.SELL
    assert held['MNQZ6'].raw['contract_id'] == 11


def test_the_account_is_normalised():
    account = venue().get_account()
    assert account.id == 'DEMO1234' and account.cash == 50000.0
    assert account.open_pnl == 25.0 and account.realized_pnl == -10.0


def test_orders_are_listed():
    v = venue(orders=[{'orderId': 1, 'ordStatus': 'Working'}])
    assert [o.id for o in v.list_orders()] == ['1']


# ── cancel and flatten ──────────────────────────────────────────────────────


def test_cancel_reaches_the_client():
    v = venue()
    v.cancel(55)
    assert last(v, 'cancel')['args'] == (55,)


def test_cancel_all_uses_the_clients_own_sweep():
    v = venue()
    assert v.cancel_all() == [1, 2]


def test_flatten_liquidates_by_contract_id():
    v = venue(positions={'MNQZ6': (2, 20100.0, 11)})
    v.flatten('MNQZ6')
    assert last(v, 'liquidate')['args'] == (11,)


def test_flattening_nothing_raises():
    with pytest.raises(NotFoundError, match='no open position'):
        venue().flatten('MNQZ6')


def test_flatten_all_renames_liquidated_to_flattened():
    v = venue()
    assert v.flatten_all() == {'cancelled': [1], 'flattened': [11]}


# ── failures ────────────────────────────────────────────────────────────────


def test_a_refusal_in_the_reply_raises():
    v = venue(
        reply={'failureReason': 'InsufficientMargin', 'failureText': 'not enough'}
    )
    with pytest.raises(RejectedError, match='InsufficientMargin'):
        v.market('MNQZ6', Side.BUY, 1)


def test_an_unexpected_client_error_becomes_a_transport_error():
    from anybroker import TransportError

    v = venue(raises=RuntimeError('socket went away'))
    with pytest.raises(TransportError, match='socket went away'):
        v.market('MNQZ6', Side.BUY, 1)
