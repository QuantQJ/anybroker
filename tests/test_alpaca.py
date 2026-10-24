"""Alpaca, against a fake trading client.

The tests that assert the exact request Alpaca receives need the real
``alpaca-py`` installed, because the venue deliberately builds the SDK's own
request objects rather than dicts -- that way Alpaca's validation runs before
anything leaves the machine. Those tests skip when the SDK is absent and run
in CI, which installs it. Everything else runs always.
"""

from __future__ import annotations

import importlib.util
import json
import os
import types

import pytest

from anybroker import (
    ConfigError,
    DuplicateOrderError,
    Env,
    NotFoundError,
    Order,
    OrderSpec,
    OrderType,
    RejectedError,
    Side,
    ThrottledError,
    TimeInForce,
    TransportError,
    UnsupportedError,
)
from anybroker.venues.alpaca import (
    ALPACA_ORDER_TYPES,
    ALPACA_TIF,
    DUPLICATE_CLIENT_ID,
    AlpacaVenue,
)

needs_sdk = pytest.mark.skipif(
    importlib.util.find_spec('alpaca') is None,
    reason='alpaca-py is not installed here; CI installs it',
)


# ── a fake trading client ───────────────────────────────────────────────────


class FakeAPIError(Exception):
    """Shaped like alpaca.common.exceptions.APIError."""

    def __init__(self, message, *, code=None, status_code=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


FakeAPIError.__name__ = 'APIError'


def obj(**kw):
    return types.SimpleNamespace(**kw)


class FakeAlpaca:
    def __init__(self, *, positions=(), orders=(), raises=None, account=None):
        self.submitted = []
        self.cancelled = []
        self.closed = []
        self.replaced = []
        self._positions = list(positions)
        self._orders = list(orders)
        self._raises = raises
        self._account = account or obj(
            account_number='PA123',
            cash='24000',
            equity='25000',
            last_equity='24900',
            currency='USD',
        )
        self._next = 0

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return list(self._positions)

    def get_orders(self, filter=None):
        return list(self._orders)

    def get_order_by_id(self, order_id, filter=None):
        for o in self._orders:
            if str(o.id) == str(order_id):
                return o
        raise FakeAPIError('order not found', status_code=404)

    def get_order_by_client_id(self, client_id):
        for o in self._orders:
            if getattr(o, 'client_order_id', None) == client_id:
                return o
        raise FakeAPIError('order not found', status_code=404)

    def submit_order(self, order_data=None):
        if self._raises:
            raise self._raises
        self.submitted.append(order_data)
        self._next += 1
        return obj(
            id='al-%d' % self._next,
            client_order_id=getattr(order_data, 'client_order_id', None),
            symbol=getattr(order_data, 'symbol', ''),
            side=str(getattr(order_data, 'side', '')),
            qty=getattr(order_data, 'qty', 0),
            order_type=str(getattr(order_data, 'type', 'market')),
            status='new',
            filled_qty='0',
            filled_avg_price=None,
            limit_price=getattr(order_data, 'limit_price', None),
            stop_price=getattr(order_data, 'stop_price', None),
            legs=None,
        )

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(str(order_id))

    def cancel_orders(self):
        return [obj(id='c1'), obj(id='c2')]

    def replace_order_by_id(self, order_id, order_data=None):
        self.replaced.append({'id': str(order_id), 'data': order_data})
        return obj(
            id=str(order_id),
            symbol='AAPL',
            side='buy',
            qty='10',
            order_type='limit',
            status='new',
            filled_qty='0',
            filled_avg_price=None,
            limit_price='228',
            stop_price=None,
            client_order_id=None,
            legs=None,
        )

    def close_position(self, symbol_or_asset_id, close_options=None):
        self.closed.append(str(symbol_or_asset_id))
        return obj(
            id='close-1',
            symbol=str(symbol_or_asset_id),
            side='sell',
            qty='10',
            order_type='market',
            status='accepted',
            filled_qty='0',
            filled_avg_price=None,
            limit_price=None,
            stop_price=None,
            client_order_id=None,
            legs=None,
        )

    def close_all_positions(self, cancel_orders=None):
        self.closed.append('ALL(cancel=%s)' % cancel_orders)
        return [obj(symbol='AAPL'), obj(symbol='MSFT')]


def venue(env=Env.PAPER, **kw):
    return AlpacaVenue(env, client=FakeAlpaca(**kw))


def position(symbol, qty, side='long', avg=100.0, pl=0.0):
    return obj(
        symbol=symbol,
        qty=str(qty),
        side=side,
        avg_entry_price=str(avg),
        unrealized_pl=str(pl),
    )


def order(order_id, **kw):
    base = {
        'id': order_id,
        'symbol': 'AAPL',
        'side': 'buy',
        'qty': '10',
        'order_type': 'limit',
        'status': 'new',
        'filled_qty': '0',
        'filled_avg_price': None,
        'limit_price': '227.5',
        'stop_price': None,
        'client_order_id': None,
        'legs': None,
    }
    base.update(kw)
    return obj(**base)


# ── what Alpaca can and cannot do ───────────────────────────────────────────


def test_the_order_types_are_alpacas_own_five():
    assert set(ALPACA_ORDER_TYPES.values()) == {
        'market',
        'limit',
        'stop',
        'stop_limit',
        'trailing_stop',
    }


@needs_sdk
def test_there_is_no_trailing_stop_limit():
    """Our vocabulary has one; Alpaca does not. It must be refused."""
    assert OrderType.TRAILING_STOP_LIMIT not in ALPACA_ORDER_TYPES
    v = venue()
    with pytest.raises(UnsupportedError, match='does not offer trailing_stop_limit'):
        v.place(
            OrderSpec(
                'AAPL',
                Side.SELL,
                1,
                OrderType.TRAILING_STOP_LIMIT,
                limit_price=1.0,
                trail_amount=1.0,
            )
        )
    assert v.client.submitted == []


def test_the_times_in_force_are_alpacas_own_four():
    assert set(ALPACA_TIF.values()) == {'day', 'gtc', 'ioc', 'fok'}
    assert TimeInForce.GTD not in ALPACA_TIF


@needs_sdk
def test_good_til_date_is_refused_and_points_at_opg_and_cls():
    v = venue()
    with pytest.raises(UnsupportedError) as caught:
        v.place(
            OrderSpec(
                'AAPL',
                Side.BUY,
                1,
                time_in_force=TimeInForce.GTD,
                good_til='2026-12-01',
            )
        )
    assert 'no good-til-date' in str(caught.value)
    assert 'opg' in str(caught.value) and 'cls' in str(caught.value)
    assert v.client.submitted == []


@needs_sdk
def test_reduce_only_is_refused_and_points_at_position_intent():
    v = venue()
    with pytest.raises(UnsupportedError) as caught:
        v.place(OrderSpec('AAPL', Side.SELL, 1, reduce_only=True))
    assert 'position_intent' in str(caught.value)
    assert 'flatten()' in str(caught.value)


def test_it_declares_what_it_can_do():
    caps = venue().capabilities
    assert caps.supports_paper is True
    assert caps.supports_brackets is True
    assert caps.supports_modify is True
    assert caps.supports_reduce_only is False
    assert caps.supports_quotes is False, 'quotes are the market-data client'


# ── paper and live ──────────────────────────────────────────────────────────


def test_env_is_required():
    with pytest.raises(TypeError):
        AlpacaVenue(client=FakeAlpaca())


def test_paper_sets_the_sdks_own_paper_flag(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **kw):
            seen.update(kw)

    module = types.ModuleType('alpaca.trading.client')
    module.TradingClient = Client
    import sys

    monkeypatch.setitem(sys.modules, 'alpaca.trading.client', module)
    AlpacaVenue(Env.PAPER, api_key='k', secret_key='s').connect()
    assert seen['paper'] is True
    seen.clear()
    AlpacaVenue(Env.LIVE, api_key='k', secret_key='s').connect()
    assert seen['paper'] is False


def test_a_missing_sdk_says_how_to_get_it(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, 'alpaca.trading.client', None)
    with pytest.raises(ConfigError, match='alpaca-py is not installed'):
        AlpacaVenue(Env.PAPER, api_key='k', secret_key='s').connect()


# ── credentials: no default path ────────────────────────────────────────────


def test_keys_passed_in_are_used():
    v = AlpacaVenue(Env.PAPER, api_key='k', secret_key='s')
    assert v._resolve_keys() == ('k', 's')


def test_the_environment_can_carry_them(monkeypatch):
    monkeypatch.setenv('APCA_API_KEY_ID', 'ek')
    monkeypatch.setenv('APCA_API_SECRET_KEY', 'es')
    assert AlpacaVenue(Env.PAPER)._resolve_keys() == ('ek', 'es')


def test_a_named_file_is_read(tmp_path, monkeypatch):
    monkeypatch.delenv('APCA_API_KEY_ID', raising=False)
    path = tmp_path / 'k.json'
    path.write_text(json.dumps({'APCA_API_KEY_ID': 'fk', 'APCA_API_SECRET_KEY': 'fs'}))
    os.chmod(path, 0o600)
    v = AlpacaVenue(Env.PAPER, credential_path=str(path))
    assert v._resolve_keys() == ('fk', 'fs')


def test_a_world_readable_file_is_refused(tmp_path):
    path = tmp_path / 'k.json'
    path.write_text(json.dumps({'APCA_API_KEY_ID': 'a', 'APCA_API_SECRET_KEY': 'b'}))
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError, match='readable by others'):
        AlpacaVenue(Env.PAPER, credential_path=str(path))._resolve_keys()


def test_no_keys_anywhere_lists_the_options(monkeypatch):
    for name in (
        'APCA_API_KEY_ID',
        'APCA_API_SECRET_KEY',
        'ALPACA_API_KEY',
        'ALPACA_API_SECRET',
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError) as caught:
        AlpacaVenue(Env.PAPER)._resolve_keys()
    text = str(caught.value)
    assert 'api_key=' in text and 'credential_path=' in text
    assert 'APCA_API_KEY_ID' in text


# ── the request Alpaca actually receives ────────────────────────────────────


@needs_sdk
def test_a_market_order():
    v = venue()
    v.market('AAPL', Side.BUY, 10)
    req = v.client.submitted[-1]
    assert req.symbol == 'AAPL' and float(req.qty) == 10
    assert req.side.value == 'buy' and req.type.value == 'market'
    assert req.time_in_force.value == 'day'


@needs_sdk
def test_a_limit_order_carries_its_price():
    v = venue()
    v.limit('AAPL', Side.BUY, 10, 227.50)
    req = v.client.submitted[-1]
    assert float(req.limit_price) == 227.50
    assert req.type.value == 'limit'


@needs_sdk
def test_a_stop_order_carries_its_stop():
    v = venue()
    v.stop('AAPL', Side.SELL, 10, 220.0)
    assert float(v.client.submitted[-1].stop_price) == 220.0


@needs_sdk
def test_a_stop_limit_carries_both():
    v = venue()
    v.stop_limit('AAPL', Side.SELL, 10, 219.5, 220.0)
    req = v.client.submitted[-1]
    assert float(req.limit_price) == 219.5 and float(req.stop_price) == 220.0


@needs_sdk
def test_an_absolute_trail_uses_trail_price():
    v = venue()
    v.trailing_stop('AAPL', Side.SELL, 10, trail_amount=2.5)
    req = v.client.submitted[-1]
    assert float(req.trail_price) == 2.5
    assert req.trail_percent is None


@needs_sdk
def test_a_percent_trail_uses_trail_percent():
    """Unlike Tradovate, Alpaca takes a percent trail, so it is not refused."""
    v = venue()
    v.trailing_stop('AAPL', Side.SELL, 10, trail_percent=1.5)
    req = v.client.submitted[-1]
    assert float(req.trail_percent) == 1.5
    assert req.trail_price is None


@needs_sdk
def test_time_in_force_is_passed():
    v = venue()
    v.place(
        OrderSpec(
            'AAPL',
            Side.BUY,
            1,
            OrderType.LIMIT,
            limit_price=1.0,
            time_in_force=TimeInForce.GTC,
        )
    )
    assert v.client.submitted[-1].time_in_force.value == 'gtc'


@needs_sdk
def test_extra_reaches_the_request():
    v = venue()
    v.place(OrderSpec('AAPL', Side.BUY, 1, extra={'extended_hours': True}))
    assert v.client.submitted[-1].extended_hours is True


# ── brackets: one request, Alpaca's own order class ─────────────────────────


@needs_sdk
def test_both_children_make_it_a_bracket():
    v = venue()
    v.place(OrderSpec('AAPL', Side.BUY, 10, take_profit=240.0, stop_loss=220.0))
    req = v.client.submitted[-1]
    assert req.order_class.value == 'bracket'
    assert float(req.take_profit.limit_price) == 240.0
    assert float(req.stop_loss.stop_price) == 220.0
    assert len(v.client.submitted) == 1, 'a bracket is a single request'


@needs_sdk
def test_one_child_makes_it_one_triggers_other():
    v = venue()
    v.place(OrderSpec('AAPL', Side.BUY, 10, stop_loss=220.0))
    req = v.client.submitted[-1]
    assert req.order_class.value == 'oto'
    assert req.take_profit is None


@needs_sdk
def test_no_bracket_means_no_order_class_fuss():
    v = venue()
    v.market('AAPL', Side.BUY, 1)
    req = v.client.submitted[-1]
    assert req.take_profit is None and req.stop_loss is None


@needs_sdk
def test_the_client_order_id_is_passed_so_alpaca_enforces_it_too():
    v = venue()
    v.place(OrderSpec('AAPL', Side.BUY, 1, client_order_id='rule:2026-10-24'))
    assert v.client.submitted[-1].client_order_id == 'rule:2026-10-24'


# ── Alpaca's own idempotency ────────────────────────────────────────────────


@needs_sdk
def test_alpacas_duplicate_rejection_becomes_our_duplicate_error():
    """The broker enforces client_order_id uniqueness. Code 40010001."""
    existing = order('al-7', client_order_id='k1')
    client = FakeAlpaca(
        orders=[existing],
        raises=FakeAPIError(
            'client_order_id must be unique', code=DUPLICATE_CLIENT_ID, status_code=422
        ),
    )
    v = AlpacaVenue(Env.PAPER, client=client)
    with pytest.raises(DuplicateOrderError) as caught:
        v.place(OrderSpec('AAPL', Side.BUY, 1, client_order_id='k1'))
    assert 'enforces that itself' in str(caught.value)
    assert caught.value.order is not None
    assert caught.value.order.id == 'al-7', 'it should hand back the real order'


@needs_sdk
def test_a_duplicate_without_a_lookup_still_raises_cleanly():
    client = FakeAlpaca(
        raises=FakeAPIError(
            'client_order_id must be unique', code=DUPLICATE_CLIENT_ID, status_code=422
        )
    )
    v = AlpacaVenue(Env.PAPER, client=client)
    with pytest.raises(DuplicateOrderError) as caught:
        v.place(OrderSpec('AAPL', Side.BUY, 1, client_order_id='nope'))
    assert caught.value.order is None


@needs_sdk
def test_our_own_guard_still_works_on_top():
    v = venue()
    first = v.market('AAPL', Side.BUY, 10)
    sent = len(v.client.submitted)
    again = v.market('AAPL', Side.BUY, 10)
    assert len(v.client.submitted) == sent
    assert again.id == first.id


# ── reads ───────────────────────────────────────────────────────────────────


def test_positions_are_normalised():
    v = venue(
        positions=[
            position('AAPL', 10, 'long', 220.0, 85.0),
            position('MSFT', 4, 'short', 400.0, -12.0),
        ]
    )
    held = {p.symbol: p for p in v.list_positions()}
    assert held['AAPL'].qty == 10 and held['AAPL'].side is Side.BUY
    assert held['AAPL'].open_pnl == 85.0
    assert held['MSFT'].qty == -4, 'a short must come back negative'
    assert held['MSFT'].side is Side.SELL


def test_the_account_is_normalised():
    account = venue().get_account()
    assert account.id == 'PA123' and account.equity == 25000.0
    assert account.cash == 24000.0
    assert round(account.open_pnl, 2) == 100.0, 'equity minus last equity'


def test_orders_are_listed_and_fetched():
    v = venue(orders=[order('al-1'), order('al-2', status='filled')])
    assert [o.id for o in v.list_orders()] == ['al-1', 'al-2']
    assert v.get_order('al-2').is_done is True
    assert v.get_order('nope') is None


def test_alpacas_underscored_statuses_are_understood():
    """partially_filled must count as working, done_for_day as finished."""
    assert Order(id='x', venue='alpaca', status='partially_filled').is_working
    assert Order(id='x', venue='alpaca', status='pending_new').is_working
    assert Order(id='x', venue='alpaca', status='held').is_working
    assert Order(id='x', venue='alpaca', status='done_for_day').is_done
    assert Order(id='x', venue='alpaca', status='canceled').is_done


def test_an_order_can_be_found_by_the_id_you_gave_it():
    v = venue(orders=[order('al-9', client_order_id='mine')])
    assert v.get_order_by_client_id('mine').id == 'al-9'
    assert v.get_order_by_client_id('absent') is None


# ── cancel, modify, flatten ─────────────────────────────────────────────────


def test_cancel_reaches_the_client():
    v = venue()
    v.cancel('al-4')
    assert v.client.cancelled == ['al-4']


def test_cancel_all_uses_alpacas_single_call():
    v = venue(orders=[order('al-1'), order('al-2')])
    assert v.cancel_all() == ['c1', 'c2']


@needs_sdk
def test_modify_replaces_the_order():
    v = venue(orders=[order('al-5')])
    v.modify('al-5', limit_price=228.0)
    sent = v.client.replaced[-1]
    assert sent['id'] == 'al-5' and float(sent['data'].limit_price) == 228.0


def test_modify_refuses_a_field_alpaca_cannot_replace():
    v = venue(orders=[order('al-5')])
    with pytest.raises(ConfigError, match='cannot have'):
        v.modify('al-5', symbol='MSFT')


def test_flatten_lets_alpaca_size_the_close():
    v = venue(positions=[position('AAPL', 10)])
    v.flatten('AAPL')
    assert v.client.closed == ['AAPL']


def test_flattening_nothing_raises():
    with pytest.raises(NotFoundError, match='no open position'):
        venue().flatten('AAPL')


def test_flatten_all_closes_and_cancels_in_one_call():
    v = venue()
    result = v.flatten_all()
    assert result['flattened'] == ['AAPL', 'MSFT']
    assert v.client.closed == ['ALL(cancel=True)']


# ── errors ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    'status,expected',
    [
        (401, 'AuthError'),
        (403, 'AuthError'),
        (404, 'NotFoundError'),
        (429, 'ThrottledError'),
        (422, 'RejectedError'),
    ],
)
def test_statuses_map_to_the_right_exception(status, expected):
    v = AlpacaVenue(Env.PAPER, client=FakeAlpaca())
    translated = v._translate(FakeAPIError('no', status_code=status))
    assert type(translated).__name__ == expected


@needs_sdk
def test_a_server_error_is_ambiguous_because_the_order_may_be_live():
    v = AlpacaVenue(
        Env.PAPER, client=FakeAlpaca(raises=FakeAPIError('boom', status_code=503))
    )
    with pytest.raises(TransportError) as caught:
        v.market('AAPL', Side.BUY, 1)
    assert caught.value.ambiguous is True
    assert len(v.unresolved()) == 1


@needs_sdk
def test_a_rejection_is_not_ambiguous():
    v = AlpacaVenue(
        Env.PAPER, client=FakeAlpaca(raises=FakeAPIError('bad symbol', status_code=422))
    )
    with pytest.raises(RejectedError):
        v.market('ZZZZ', Side.BUY, 1)
    assert v.unresolved() == []
    assert v.submissions() == {}, 'a clean refusal leaves nothing to block on'


@needs_sdk
def test_a_timeout_is_ambiguous():
    v = AlpacaVenue(Env.PAPER, client=FakeAlpaca(raises=TimeoutError('slow')))
    with pytest.raises(TransportError) as caught:
        v.market('AAPL', Side.BUY, 1)
    assert caught.value.ambiguous is True


def test_a_throttle_on_a_read_is_translated():
    v = AlpacaVenue(Env.PAPER, client=FakeAlpaca(raises=None))
    v.client.get_account = lambda: (_ for _ in ()).throw(
        FakeAPIError('slow down', status_code=429)
    )
    with pytest.raises(ThrottledError):
        v.get_account()
