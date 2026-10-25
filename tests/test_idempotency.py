"""Resubmitting the same order must not create a second one.

QJ's requirement: the same OrderSpec sent down to anybroker resolves to a
duplicate check or a read of the live order state, never a second request.
"""

import pytest

from anybroker import (
    DuplicateOrderError,
    Env,
    OrderSpec,
    OrderType,
    Side,
    TimeInForce,
    TransportError,
    UnresolvedOrderError,
)
from anybroker.spec import FINGERPRINT_FIELDS
from anybroker.venues.ccxt_venue import CcxtVenue
from anybroker.venues.ibkr import IbkrVenue
from fakes import FakeCcxt, FakeIB, fake_position

LIVE_ACCOUNTS = ('U1234567',)


def ib_venue(**kw):
    ib = kw.pop('ib', None) or FakeIB(accounts=('DU1234567',), **kw)
    return IbkrVenue(Env.PAPER, client_id=9001, port=4001, ib=ib).connect()


def ccxt_venue(**kw):
    return CcxtVenue('fake', Env.LIVE, exchange=FakeCcxt(**kw))


# ── the fingerprint ─────────────────────────────────────────────────────────


def test_the_same_instruction_has_the_same_fingerprint():
    a = OrderSpec('ABCZ6', Side.BUY, 2, OrderType.LIMIT, limit_price=20150.25)
    b = OrderSpec('ABCZ6', Side.BUY, 2, OrderType.LIMIT, limit_price=20150.25)
    assert a.fingerprint == b.fingerprint


def test_a_label_does_not_change_the_order():
    """label annotates; it must not make two identical orders look different."""
    a = OrderSpec('ABCZ6', Side.BUY, 2, label='rule-a')
    b = OrderSpec('ABCZ6', Side.BUY, 2, label='rule-b')
    assert a.fingerprint == b.fingerprint


@pytest.mark.parametrize(
    'change',
    [
        {'symbol': 'MESZ6'},
        {'side': Side.SELL},
        {'qty': 3},
        {'time_in_force': TimeInForce.GTC},
        {'take_profit': 20200.0},
        {'stop_loss': 20050.0},
        {'reduce_only': True},
    ],
)
def test_changing_anything_material_changes_the_fingerprint(change):
    base = {'symbol': 'ABCZ6', 'side': Side.BUY, 'qty': 2}
    a = OrderSpec(**base)
    b = OrderSpec(**{**base, **change})
    assert a.fingerprint != b.fingerprint, change


def test_two_as_an_int_and_two_as_a_float_are_the_same_order():
    assert (
        OrderSpec('ABCZ6', Side.BUY, 2).fingerprint
        == OrderSpec('ABCZ6', Side.BUY, 2.0).fingerprint
    )


def test_the_key_is_your_client_order_id_when_you_set_one():
    spec = OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='james:2026-10-24:entry')
    assert spec.idempotency_key == 'james:2026-10-24:entry'


def test_otherwise_the_key_is_the_fingerprint():
    spec = OrderSpec('ABCZ6', Side.BUY, 1)
    assert spec.idempotency_key == 'fp:' + spec.fingerprint


def test_the_fingerprint_covers_every_field_that_instructs():
    assert set(FINGERPRINT_FIELDS) == {
        'symbol',
        'side',
        'qty',
        'order_type',
        'limit_price',
        'stop_price',
        'trail_amount',
        'trail_percent',
        'time_in_force',
        'good_til',
        'take_profit',
        'stop_loss',
        'reduce_only',
    }
    assert 'label' not in FINGERPRINT_FIELDS
    assert 'extra' not in FINGERPRINT_FIELDS


# ── a working order is returned, not sent again ─────────────────────────────


def test_a_second_send_of_a_working_order_sends_nothing():
    v = ib_venue()
    first = v.market('ABCZ6', Side.BUY, 2)
    sent = len(v._ib.placed)
    second = v.market('ABCZ6', Side.BUY, 2)
    assert len(v._ib.placed) == sent, 'the second call must not reach the broker'
    assert second.id == first.id


def test_the_returned_order_is_read_from_the_venue_not_a_cache():
    """The second call must reflect the broker's current state."""
    v = ib_venue()
    order = v.market('ABCZ6', Side.BUY, 2)
    # the broker moves on: partially filled
    trade = v._ib.placed[-1]
    trade.orderStatus.status = 'Submitted'
    trade.orderStatus.filled = 1.0
    again = v.market('ABCZ6', Side.BUY, 2)
    assert again.id == order.id
    assert again.filled_qty == 1.0, 'it must re-read, not replay the first reply'


def test_it_works_on_a_ccxt_venue_too():
    v = ccxt_venue(orders=[])
    v.market('BTC/USD', Side.BUY, 0.5)
    sent = len(v.exchange.created)
    v.market('BTC/USD', Side.BUY, 0.5)
    assert len(v.exchange.created) == sent


# ── a finished order is ambiguous, so it raises ─────────────────────────────


def test_repeating_a_finished_order_raises_rather_than_guessing():
    v = ib_venue()
    v.market('ABCZ6', Side.BUY, 2)
    v._ib.placed[-1].orderStatus.status = 'Filled'
    with pytest.raises(DuplicateOrderError) as caught:
        v.market('ABCZ6', Side.BUY, 2)
    assert 'already sent' in str(caught.value)
    assert 'client_order_id' in str(caught.value)
    assert caught.value.key.startswith('fp:')


def test_allow_duplicate_is_the_explicit_way_through():
    v = ib_venue()
    v.market('ABCZ6', Side.BUY, 2)
    v._ib.placed[-1].orderStatus.status = 'Filled'
    sent = len(v._ib.placed)
    v.place(OrderSpec('ABCZ6', Side.BUY, 2), allow_duplicate=True)
    assert len(v._ib.placed) == sent + 1


def test_a_distinct_client_order_id_is_the_other_way_through():
    """What a daily rule should do: scope the id to the day."""
    v = ib_venue()
    v.place(OrderSpec('ABCZ6', Side.BUY, 2, client_order_id='long0558:2026-10-23'))
    v._ib.placed[-1].orderStatus.status = 'Filled'
    v.place(OrderSpec('ABCZ6', Side.BUY, 2, client_order_id='long0558:2026-10-24'))
    assert len(v._ib.placed) == 2


# ── an unanswered write is the dangerous one ─────────────────────────────────


class AmbiguousIB(FakeIB):
    """An IB whose placeOrder dies after the order may already be away."""

    def placeOrder(self, contract, order):
        raise TimeoutError('socket went quiet mid-write')


def test_an_ambiguous_failure_is_recorded_as_unresolved():
    v = IbkrVenue(
        Env.PAPER, client_id=9002, port=4001, ib=AmbiguousIB(accounts=('DU1',))
    ).connect()
    with pytest.raises(TransportError) as caught:
        v.market('ABCZ6', Side.BUY, 1)
    assert caught.value.ambiguous is True
    assert len(v.unresolved()) == 1
    assert v.unresolved()[0].state == 'unresolved'


def test_an_unresolved_order_is_never_quietly_resent():
    v = IbkrVenue(
        Env.PAPER, client_id=9003, port=4001, ib=AmbiguousIB(accounts=('DU1',))
    ).connect()
    with pytest.raises(TransportError):
        v.market('ABCZ6', Side.BUY, 1)
    with pytest.raises(UnresolvedOrderError) as caught:
        v.market('ABCZ6', Side.BUY, 1)
    assert 'may be working' in str(caught.value)
    assert 'resolve(' in str(caught.value) and 'forget(' in str(caught.value)


def test_resolve_clears_it_once_you_have_looked():
    v = IbkrVenue(
        Env.PAPER, client_id=9004, port=4001, ib=AmbiguousIB(accounts=('DU1',))
    ).connect()
    with pytest.raises(TransportError):
        v.market('ABCZ6', Side.BUY, 1)
    key = v.unresolved()[0].key
    v.resolve(key, order_id=777)
    assert v.unresolved() == []
    assert v.submissions()[key].order_id == '777'


def test_forget_is_for_when_nothing_was_accepted():
    v = IbkrVenue(
        Env.PAPER, client_id=9005, port=4001, ib=AmbiguousIB(accounts=('DU1',))
    ).connect()
    with pytest.raises(TransportError):
        v.market('ABCZ6', Side.BUY, 1)
    v.forget(v.unresolved()[0].key)
    assert v.unresolved() == [] and v.submissions() == {}


def test_a_plain_rejection_does_not_block_the_next_attempt():
    """A refused order is resolved -- it did not happen. Retrying is fine."""

    class RefusingIB(FakeIB):
        fail = True

        def placeOrder(self, contract, order):
            if self.fail:
                self.fail = False
                raise ValueError('bad contract')
            return super().placeOrder(contract, order)

    v = IbkrVenue(
        Env.PAPER, client_id=9006, port=4001, ib=RefusingIB(accounts=('DU1',))
    ).connect()
    with pytest.raises(TransportError) as caught:
        v.market('ABCZ6', Side.BUY, 1)
    assert caught.value.ambiguous is False, 'a bad argument never reached IB'
    assert 'before it was sent' in str(caught.value)
    assert v.submissions() == {}, 'a clean failure leaves no trace to block on'
    assert v.market('ABCZ6', Side.BUY, 1).id, 'and the retry goes through'


# ── flatten is held to the same rule ────────────────────────────────────────


def test_flattening_twice_does_not_sell_the_position_twice():
    v = ib_venue(positions=[fake_position('ABCZ6', 2)])
    v.flatten('ABCZ6')
    sent = len(v._ib.placed)
    v.flatten('ABCZ6')
    assert len(v._ib.placed) == sent, 'the second flatten must not reach IB'


# ── the registry itself ─────────────────────────────────────────────────────


def test_submissions_are_visible():
    v = ib_venue()
    v.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='abc'))
    assert set(v.submissions()) == {'abc'}
    assert v.submissions()['abc'].state == 'submitted'
    assert v.submissions()['abc'].symbol == 'ABCZ6'


def test_two_venues_do_not_share_a_registry():
    a, b = ib_venue(), ib_venue()
    a.market('ABCZ6', Side.BUY, 1)
    assert a.submissions() and not b.submissions()
