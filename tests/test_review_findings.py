"""Regression tests for the five defects an external review reproduced.

Each test is named after what went wrong, so a reappearance is unambiguous.
These are the offline reproductions; none of them was tested against a live
broker.
"""

from __future__ import annotations

import json
import tempfile
import types
from pathlib import Path

import pytest

from anybroker import (
    Env,
    OrderSpec,
    OrderType,
    RejectedError,
    Side,
    TransportError,
)
from anybroker.venue import PROTECTIVE_ORDER_TYPES
from anybroker.venues.ibkr import IbkrVenue
from anybroker.venues.tradovate import SUCCESS_REASON, TradovateVenue
from fakes import FakeContract, FakeIB, fake_position


def ib_venue(**kw):
    state = kw.pop('state_path', None)
    positions = kw.pop('positions', ())
    ib = kw.pop('ib', None) or FakeIB(accounts=('DU1234567',), positions=positions)
    return IbkrVenue(
        Env.PAPER,
        client_id=kw.pop('client_id', 5001),
        port=4001,
        ib=ib,
        state_path=state,
        contract_factory=lambda s: FakeContract(s),
        **kw,
    ).connect()


# ══════════════════════════════════════════════════════════════════════════
# 1. typed order objects were handled as dictionaries
# ══════════════════════════════════════════════════════════════════════════


class TypedOrder:
    """What tradovate-client actually returns from a read."""

    def __init__(self, **kw):
        self.id = kw.get('id', 4242)
        self.symbol = kw.get('symbol', 'ABCZ6')
        self.side = kw.get('side', 'Buy')
        self.qty = kw.get('qty', 2)
        self.order_type = kw.get('order_type', 'Limit')
        self.status = kw.get('status', 'Working')
        self.filled_qty = kw.get('filled_qty', 0)
        self.avg_fill_price = kw.get('avg_fill_price', 0)
        self.limit_price = kw.get('limit_price', 20150.25)
        self.stop_price = kw.get('stop_price')
        self.client_order_id = kw.get('client_order_id')
        self.raw = {}


class FakeTradovateClient:
    def __init__(self, *, orders=None, place_reply=None, positions=None):
        self.calls = []
        self._orders = list(orders or [])
        self._place_reply = place_reply or {'orderId': 99}
        self._positions = positions or {}

    def snapshot(self, account=None):
        views = {
            s: types.SimpleNamespace(net_pos=q, net_price=0.0, contract_id=i)
            for i, (s, q) in enumerate(self._positions.items(), start=11)
        }
        return types.SimpleNamespace(
            account=types.SimpleNamespace(name='ACCOUNT_A', id=7),
            cash=types.SimpleNamespace(total_cash=0, open_pnl=0, realized_pnl=0),
            positions=views,
        )

    def orders(self):
        return list(self._orders)

    def working_orders(self):
        return list(self._orders)

    def order(self, order_id):
        return self._orders[0] if self._orders else None

    def place(self, **kw):
        self.calls.append(('place', kw))
        return dict(self._place_reply)

    def flatten_all(self, account=None, cancel_first=True):
        self.calls.append(
            ('flatten_all', {'account': account, 'cancel_first': cancel_first})
        )
        return {'cancelled': [], 'liquidated': []}


def tv_venue(**kw):
    client = kw.pop('client', None) or FakeTradovateClient(**kw)
    return TradovateVenue(Env.PAPER, client=client, account='ACCOUNT_A')


def test_a_typed_order_keeps_its_id_and_status():
    """It was read as a dict, so the id vanished and a working order looked done."""
    v = tv_venue(orders=[TypedOrder()])
    order = v.list_orders()[0]
    assert order.id == '4242', 'the id must survive'
    assert order.status == 'Working'
    assert order.is_working is True, 'a working order must not read as finished'
    assert order.is_done is False


def test_a_typed_order_keeps_its_side_size_and_prices():
    v = tv_venue(orders=[TypedOrder()])
    order = v.list_orders()[0]
    assert order.side is Side.BUY
    assert order.qty == 2
    assert order.order_type is OrderType.LIMIT
    assert order.limit_price == 20150.25
    assert order.symbol == 'ABCZ6'


def test_a_finished_typed_order_still_reads_as_finished():
    v = tv_venue(
        orders=[TypedOrder(status='Filled', filled_qty=2, avg_fill_price=20150.0)]
    )
    order = v.list_orders()[0]
    assert order.is_done and not order.is_working
    assert order.filled_qty == 2 and order.avg_fill_price == 20150.0


def test_working_orders_and_get_order_go_through_the_same_path():
    v = tv_venue(orders=[TypedOrder()])
    assert v.working_orders()[0].id == '4242'
    assert v.get_order(4242).id == '4242'


# ══════════════════════════════════════════════════════════════════════════
# 2a. flattening one account cancelled another account's orders
# ══════════════════════════════════════════════════════════════════════════


def test_flatten_all_is_scoped_to_the_account_it_was_given():
    client = FakeTradovateClient()
    v = TradovateVenue(Env.PAPER, client=client, account='ACCOUNT_A')
    v.flatten_all()
    sent = [kw for name, kw in client.calls if name == 'flatten_all'][0]
    assert sent['account'] == 'ACCOUNT_A', (
        'without the account, the client cancels every working order on the '
        'login, including another account'
    )


def test_a_venue_with_no_account_passes_none_rather_than_guessing():
    client = FakeTradovateClient()
    TradovateVenue(Env.PAPER, client=client).flatten_all()
    sent = [kw for name, kw in client.calls if name == 'flatten_all'][0]
    assert sent['account'] is None


# ══════════════════════════════════════════════════════════════════════════
# 3. an unresolved submission did not survive a restart
# ══════════════════════════════════════════════════════════════════════════


class DyingIB(FakeIB):
    """A socket that dies after the order may already be away."""

    def placeOrder(self, contract, order):
        raise TimeoutError('socket went quiet mid-write')


def test_an_unresolved_submission_is_written_to_disk():
    """The one state that must survive a restart was the only one that did not."""
    state = Path(tempfile.mkdtemp()) / 'state.json'
    v = ib_venue(ib=DyingIB(accounts=('DU1234567',)), state_path=state)
    with pytest.raises(TransportError) as caught:
        v.market('ABCZ6', Side.BUY, 1)
    assert caught.value.ambiguous is True
    assert len(v.unresolved()) == 1
    saved = json.loads(state.read_text())['submissions']
    assert len(saved) == 1, 'the unresolved submission must be on disk'
    assert saved[0]['state'] == 'unresolved'


def test_an_unresolved_submission_survives_a_restart():
    state = Path(tempfile.mkdtemp()) / 'state.json'
    first = ib_venue(
        ib=DyingIB(accounts=('DU1234567',)), state_path=state, client_id=5002
    )
    with pytest.raises(TransportError):
        first.market('ABCZ6', Side.BUY, 1)

    second = IbkrVenue(
        Env.PAPER,
        client_id=5003,
        port=4001,
        ib=FakeIB(accounts=('DU1234567',)),
        state_path=state,
        contract_factory=lambda s: FakeContract(s),
    )
    assert len(second.unresolved()) == 1, (
        'a restart must not forget an order that may be working'
    )


def test_the_same_order_is_still_blocked_after_a_restart():
    """The point of persisting it: the guard has to survive too."""
    from anybroker import UnresolvedOrderError

    state = Path(tempfile.mkdtemp()) / 'state.json'
    first = ib_venue(
        ib=DyingIB(accounts=('DU1234567',)), state_path=state, client_id=5004
    )
    with pytest.raises(TransportError):
        first.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='k1'))

    ib = FakeIB(accounts=('DU1234567',))
    second = IbkrVenue(
        Env.PAPER,
        client_id=5005,
        port=4001,
        ib=ib,
        state_path=state,
        contract_factory=lambda s: FakeContract(s),
    ).connect()
    with pytest.raises(UnresolvedOrderError):
        second.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='k1'))
    assert ib.placed == [], 'and nothing may reach the broker'


def test_resolving_and_forgetting_are_persisted_too():
    state = Path(tempfile.mkdtemp()) / 'state.json'
    v = ib_venue(ib=DyingIB(accounts=('DU1234567',)), state_path=state, client_id=5006)
    with pytest.raises(TransportError):
        v.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='k2'))
    v.forget('k2')
    saved = json.loads(state.read_text())['submissions']
    assert saved == [], 'forgetting must reach the file as well'


# ══════════════════════════════════════════════════════════════════════════
# 4. "protected" did not mean adequately protected
# ══════════════════════════════════════════════════════════════════════════


def test_a_buy_stop_does_not_protect_a_long():
    """The reported case: wrong direction, counted as cover."""
    v = ib_venue(positions=[fake_position('ABCZ6', 5)], client_id=5010)
    v.stop('ABCZ6', Side.BUY, 5, 20500.0)
    assert [p.symbol for p in v.unprotected_positions()] == ['ABCZ6']
    assert v.protection()['ABCZ6']['exposed_qty'] == 5.0
    assert v.protection()['ABCZ6']['protected_qty'] == 0.0


def test_a_sell_stop_does_not_protect_a_short():
    v = ib_venue(positions=[fake_position('ABCZ6', -3)], client_id=5011)
    v.stop('ABCZ6', Side.SELL, 3, 19500.0)
    assert [p.symbol for p in v.unprotected_positions()] == ['ABCZ6']


def test_an_undersized_stop_leaves_the_rest_exposed():
    """The other reported case: one lot of stop against five of position."""
    v = ib_venue(positions=[fake_position('ABCZ6', 5)], client_id=5012)
    v.stop('ABCZ6', Side.SELL, 1, 20000.0)
    cover = v.protection()['ABCZ6']
    assert cover['protected_qty'] == 1.0
    assert cover['exposed_qty'] == 4.0
    assert [p.symbol for p in v.unprotected_positions()] == ['ABCZ6'], (
        'partly covered is not covered'
    )


def test_a_correctly_sized_stop_on_the_right_side_does_protect():
    v = ib_venue(positions=[fake_position('ABCZ6', 5)], client_id=5013)
    v.stop('ABCZ6', Side.SELL, 5, 20000.0)
    assert v.unprotected_positions() == []
    assert v.protection()['ABCZ6']['exposed_qty'] == 0.0


def test_a_short_is_protected_by_a_buy_stop():
    v = ib_venue(positions=[fake_position('ABCZ6', -3)], client_id=5014)
    v.stop('ABCZ6', Side.BUY, 3, 20500.0)
    assert v.unprotected_positions() == []


def test_two_partial_stops_add_up():
    v = ib_venue(positions=[fake_position('ABCZ6', 5)], client_id=5015)
    v.stop('ABCZ6', Side.SELL, 2, 20000.0)
    v.stop('ABCZ6', Side.SELL, 3, 19990.0)
    assert v.protection()['ABCZ6']['protected_qty'] == 5.0
    assert v.unprotected_positions() == []


def test_a_stop_on_another_symbol_protects_nothing():
    v = ib_venue(
        positions=[fake_position('ABCZ6', 2), fake_position('MESZ6', 2)],
        client_id=5016,
    )
    v.stop('MESZ6', Side.SELL, 2, 5800.0)
    assert [p.symbol for p in v.unprotected_positions()] == ['ABCZ6']


def test_a_profit_target_is_not_protection():
    v = ib_venue(positions=[fake_position('ABCZ6', 2)], client_id=5017)
    v.limit('ABCZ6', Side.SELL, 2, 20500.0)
    assert [p.symbol for p in v.unprotected_positions()] == ['ABCZ6']
    assert OrderType.LIMIT not in PROTECTIVE_ORDER_TYPES


def test_protection_reports_every_open_position():
    v = ib_venue(
        positions=[fake_position('ABCZ6', 2), fake_position('MESZ6', -1)],
        client_id=5018,
    )
    cover = v.protection()
    assert set(cover) == {'ABCZ6', 'MESZ6'}
    assert cover['ABCZ6']['side'] is Side.BUY
    assert cover['MESZ6']['side'] is Side.SELL


# ══════════════════════════════════════════════════════════════════════════
# 5. failureReason was read as failure even when it said Success
# ══════════════════════════════════════════════════════════════════════════


def test_success_is_a_documented_failure_reason_value():
    """Tradovate's own PlaceOrderResult lists Success among them."""
    assert SUCCESS_REASON == 'Success'


def test_a_reason_of_success_is_accepted():
    v = tv_venue(place_reply={'orderId': 99, 'failureReason': 'Success'})
    order = v.place(OrderSpec('ABCZ6', Side.BUY, 1))
    assert order.id == '99', 'Success must not be treated as a refusal'


def test_no_reason_at_all_is_accepted():
    v = tv_venue(place_reply={'orderId': 99})
    assert v.place(OrderSpec('ABCZ6', Side.BUY, 1)).id == '99'


@pytest.mark.parametrize(
    'reason',
    [
        'MaxPosLimitReached',
        'InvalidPrice',
        'SessionClosed',
        'Unauthorized',
        'NotEnoughLiquidity',
        'UnknownReason',
    ],
)
def test_a_real_reason_still_raises(reason):
    v = tv_venue(place_reply={'failureReason': reason, 'failureText': 'detail'})
    with pytest.raises(RejectedError, match=reason):
        v.place(OrderSpec('ABCZ6', Side.BUY, 1))


# ══════════════════════════════════════════════════════════════════════════
# Second round. Five more, each reproduced before being touched.
# ══════════════════════════════════════════════════════════════════════════


class TradovateShapedOrder:
    """What tradovate-client's Order actually looks like.

    Tradovate names the direction ``action``, not ``side``, and the size
    ``qty`` from ``orderQty``, with prices in ``price`` and ``stop_price``.
    Reading this library's own names produced side=None and lost the rest.
    """

    def __init__(self, **kw):
        self.id = kw.get('id', 101)
        self.account_id = kw.get('account_id', 7)
        self.action = kw.get('action', 'Sell')
        self.order_type = kw.get('order_type', 'Limit')
        self.status = kw.get('status', 'Working')
        self.qty = kw.get('qty', 3)
        self.price = kw.get('price', 20150.25)
        self.stop_price = kw.get('stop_price')
        self.client_order_id = kw.get('client_order_id', 'k9')
        self.raw = kw.get('raw', {})


def test_the_direction_is_read_from_action_not_side():
    v = tv_venue(orders=[TradovateShapedOrder(action='Sell')])
    assert v.list_orders()[0].side is Side.SELL


def test_a_buy_action_maps_too():
    v = tv_venue(orders=[TradovateShapedOrder(action='Buy')])
    assert v.list_orders()[0].side is Side.BUY


def test_the_size_and_prices_survive_the_mapping():
    v = tv_venue(orders=[TradovateShapedOrder()])
    order = v.list_orders()[0]
    assert order.qty == 3
    assert order.limit_price == 20150.25
    assert order.client_order_id == 'k9'
    assert order.order_type is OrderType.LIMIT


def test_the_raw_payload_is_the_last_resort_not_the_first():
    """An object carrying only the wire names still maps."""

    class WireNamesOnly:
        id = 55
        raw = {
            'action': 'Buy',
            'orderQty': 2,
            'price': 100.0,
            'ordStatus': 'Working',
            'orderType': 'Limit',
        }

    v = tv_venue(orders=[WireNamesOnly()])
    order = v.list_orders()[0]
    assert order.side is Side.BUY and order.qty == 2 and order.limit_price == 100.0


def test_a_partly_filled_stop_only_covers_what_is_left():
    """A stop for five with four filled leaves one contract of cover."""
    ib = FakeIB(accounts=('DU1234567',), positions=[fake_position('ABCZ6', 5)])
    v = ib_venue(ib=ib, client_id=6001)
    v.stop('ABCZ6', Side.SELL, 5, 20000.0)
    ib.placed[-1].orderStatus.filled = 4.0
    ib.placed[-1].orderStatus.status = 'Submitted'
    cover = v.protection()['ABCZ6']
    assert cover['protected_qty'] == 1.0, 'remaining, not original'
    assert cover['exposed_qty'] == 4.0
    assert [p.symbol for p in v.unprotected_positions()] == ['ABCZ6']


def test_an_untouched_stop_still_covers_its_full_size():
    ib = FakeIB(accounts=('DU1234567',), positions=[fake_position('ABCZ6', 5)])
    v = ib_venue(ib=ib, client_id=6002)
    v.stop('ABCZ6', Side.SELL, 5, 20000.0)
    assert v.protection()['ABCZ6']['exposed_qty'] == 0.0


def test_the_in_flight_record_is_written_before_the_send():
    """A process killed between the send and the reply must not lose it."""
    state = Path(tempfile.mkdtemp()) / 'state.json'
    seen = {}

    class CheckingIB(FakeIB):
        def placeOrder(self, contract, order):
            # At this instant the broker has the order and we have not
            # returned. Whatever is on disk now is all a restart would get.
            seen['on_disk'] = json.loads(state.read_text())['submissions']
            return super().placeOrder(contract, order)

    v = ib_venue(
        ib=CheckingIB(accounts=('DU1234567',)), state_path=state, client_id=6003
    )
    v.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='k3'))
    assert len(seen['on_disk']) == 1, (
        'the record must be on disk before the request leaves'
    )
    assert seen['on_disk'][0]['state'] == 'in_flight'


def test_a_restored_in_flight_record_blocks_a_resend():
    state = Path(tempfile.mkdtemp()) / 'state.json'

    class CheckingIB(FakeIB):
        def placeOrder(self, contract, order):
            raise TimeoutError('died here')

    first = ib_venue(
        ib=CheckingIB(accounts=('DU1234567',)), state_path=state, client_id=6004
    )
    with pytest.raises(TransportError):
        first.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='k4'))

    ib = FakeIB(accounts=('DU1234567',))
    second = IbkrVenue(
        Env.PAPER,
        client_id=6005,
        port=4001,
        ib=ib,
        state_path=state,
        contract_factory=lambda s: FakeContract(s),
    ).connect()
    from anybroker import UnresolvedOrderError

    with pytest.raises(UnresolvedOrderError):
        second.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='k4'))
    assert ib.placed == []


def test_a_clean_failure_still_leaves_no_record_behind():
    """Persisting early must not leave a stale lock after a clean refusal."""
    state = Path(tempfile.mkdtemp()) / 'state.json'

    class Refusing(FakeIB):
        def placeOrder(self, contract, order):
            raise ValueError('bad contract')

    v = ib_venue(ib=Refusing(accounts=('DU1234567',)), state_path=state, client_id=6006)
    with pytest.raises(TransportError):
        v.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='k5'))
    assert v.submissions() == {}
    assert json.loads(state.read_text())['submissions'] == []


def test_a_refusal_crosses_the_library_boundary_as_a_refusal(monkeypatch):
    """tradovate-client raises OrderRejected; it must not arrive as transport.

    Driven through a stand-in module so it runs without the optional
    dependency installed, which is how the translation table is reached.
    """
    import sys
    import types

    class OrderRejected(Exception):
        def __init__(self, message, *, reason=None, text=None):
            super().__init__(message)
            self.reason = reason
            self.text = text

    stub = types.ModuleType('tradovate_client.errors')
    stub.OrderRejected = OrderRejected
    for name in (
        'AuthError',
        'ThrottledError',
        'NotFoundError',
        'ConfigError',
        'RequestError',
        'TransportError',
    ):
        setattr(stub, name, type(name, (Exception,), {}))
    # The parent package too: `import tradovate_client.errors` imports
    # tradovate_client first, and without it the venue takes its ImportError
    # branch and every failure becomes a TransportError.
    parent = sys.modules.get('tradovate_client') or types.ModuleType('tradovate_client')
    parent.errors = stub
    monkeypatch.setitem(sys.modules, 'tradovate_client', parent)
    monkeypatch.setitem(sys.modules, 'tradovate_client.errors', stub)

    class Refusing(FakeTradovateClient):
        def place(self, **kw):
            raise OrderRejected('refused', reason='MaxPosLimitReached')

    v = TradovateVenue(Env.PAPER, client=Refusing(), account='ACCOUNT_A')
    with pytest.raises(RejectedError) as caught:
        v.place(OrderSpec('ABCZ6', Side.BUY, 1))
    assert caught.value.detail == 'MaxPosLimitReached'


@pytest.mark.skipif(
    __import__('importlib.util', fromlist=['util']).find_spec('tradovate_client')
    is None,
    reason='tradovate-client is an optional extra and is not on PyPI yet',
)
def test_the_same_holds_against_the_real_library():
    from tradovate_client.errors import OrderRejected

    class Refusing(FakeTradovateClient):
        def place(self, **kw):
            raise OrderRejected('refused', reason='InvalidPrice')

    v = TradovateVenue(Env.PAPER, client=Refusing(), account='ACCOUNT_A')
    with pytest.raises(RejectedError) as caught:
        v.place(OrderSpec('ABCZ6', Side.BUY, 1))
    assert caught.value.detail == 'InvalidPrice'


def test_a_genuine_transport_failure_is_still_transport():
    from anybroker import TransportError as TE

    class Dying(FakeTradovateClient):
        def place(self, **kw):
            raise RuntimeError('socket went away')

    v = TradovateVenue(Env.PAPER, client=Dying(), account='ACCOUNT_A')
    with pytest.raises(TE):
        v.place(OrderSpec('ABCZ6', Side.BUY, 1))
