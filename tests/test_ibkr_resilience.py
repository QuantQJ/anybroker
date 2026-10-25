"""Surviving IB's reset window without losing the brackets.

QJ's requirement, and his own logs from this morning are the motivation: eight
"Connectivity between IBKR and Trader Workstation has been lost" messages
between 06:35 and 06:36 UTC, then a restore, with two jobs dying and being
restarted in between. A wrapper that keeps bracket state only in memory loses
the link between a parent and its protective children exactly then.
"""

import json

import pytest

from anybroker import (
    DisconnectedError,
    Env,
    OrderSpec,
    OrderType,
    Side,
)
from anybroker.venues.ibkr import BracketGroup, IbkrVenue
from fakes import FakeContract, FakeIB, FakeOrderStatus, FakeTrade, fake_position


def venue(**kw):
    ib = kw.pop('ib', None) or FakeIB(
        accounts=('DU1234567',), **{k: kw.pop(k) for k in ('positions',) if k in kw}
    )
    return IbkrVenue(Env.PAPER, client_id=8001, port=4001, ib=ib, **kw).connect()


def drop(v):
    """Simulate IB pulling the socket out from under us."""
    v._ib.connected = False


# ── connection state ────────────────────────────────────────────────────────


def test_a_connected_venue_knows_it():
    assert venue().is_connected is True


def test_a_dropped_socket_is_noticed():
    v = venue()
    drop(v)
    assert v.is_connected is False


def test_a_write_on_a_dead_socket_is_refused_and_says_what_to_do():
    """The key rule: never send, never retry, tell the caller to go and read."""
    v = venue()
    drop(v)
    with pytest.raises(DisconnectedError) as caught:
        v.market('ABCZ6', Side.BUY, 1)
    text = str(caught.value)
    assert 'was not sent' in text
    assert 'unresolved, not failed' in text
    assert v._ib.placed == [], 'nothing may reach the broker'


def test_a_read_reconnects_and_answers():
    v = venue()
    drop(v)
    assert v.get_account().id == 'DU1234567'
    assert v.is_connected is True


def test_a_read_refuses_instead_of_reconnecting_when_told_to():
    ib = FakeIB(accounts=('DU1234567',))
    v = IbkrVenue(
        Env.PAPER, client_id=8002, port=4001, ib=ib, auto_reconnect=False
    ).connect()
    ib.connected = False
    with pytest.raises(DisconnectedError, match='auto_reconnect is off'):
        v.get_account()


def test_a_write_is_never_auto_reconnected_even_when_reads_are():
    """Reconnecting tells you nothing about an order that was in flight."""
    v = venue()
    drop(v)
    with pytest.raises(DisconnectedError):
        v.market('ABCZ6', Side.BUY, 1)
    assert v.is_connected is False, 'a write must not reconnect behind your back'


# ── bracket state ───────────────────────────────────────────────────────────


def test_placing_a_bracket_records_the_group():
    v = venue()
    v.place(OrderSpec('ABCZ6', Side.BUY, 2, take_profit=20200.0, stop_loss=20050.0))
    groups = v.brackets()
    assert len(groups) == 1
    group = next(iter(groups.values()))
    assert group.symbol == 'ABCZ6'
    assert len(group.child_ids) == 2
    assert group.oca_group.startswith('anybroker-')


def test_resync_rebuilds_the_group_from_ibs_own_orders():
    """The registry is wiped, as a fresh process would be. IB still knows."""
    v = venue()
    v.place(OrderSpec('ABCZ6', Side.BUY, 2, take_profit=20200.0, stop_loss=20050.0))
    expected = next(iter(v.brackets().values()))
    v._brackets = {}
    report = v.resync()
    assert report['brackets'] == 1
    rebuilt = next(iter(v.brackets().values()))
    assert rebuilt.parent_id == expected.parent_id
    assert sorted(rebuilt.child_ids) == sorted(expected.child_ids)
    assert rebuilt.oca_group == expected.oca_group
    assert rebuilt.recovered is True, 'it should say it found this, not placed it'


def test_connecting_resyncs():
    ib = FakeIB(accounts=('DU1234567',))
    v = IbkrVenue(Env.PAPER, client_id=8003, port=4001, ib=ib).connect()
    v.place(OrderSpec('ABCZ6', Side.BUY, 1, stop_loss=20050.0))
    drop(v)
    v._brackets = {}
    v.connect()
    assert len(v.brackets()) == 1, 'reconnecting must pick the group back up'


def test_resync_drops_what_ib_no_longer_reports():
    """IB is the authority. A group it has forgotten is gone."""
    v = venue()
    v._brackets['999'] = BracketGroup(parent_id='999', symbol='GHOST')
    report = v.resync()
    assert '999' in report['dropped']
    assert '999' not in v.brackets()


def test_resync_reports_an_orphan_whose_parent_ib_no_longer_has():
    v = venue()
    order = type('O', (), {})()
    order.orderId = 55
    order.parentId = 999
    order.ocaGroup = 'anybroker-999'
    order.action = 'SELL'
    order.totalQuantity = 1
    order.orderType = 'STP'
    order.lmtPrice = 0.0
    order.auxPrice = 20050.0
    order.orderRef = ''
    v._ib._trades = [
        FakeTrade(order, FakeContract('ABCZ6'), FakeOrderStatus('Submitted'))
    ]
    report = v.resync()
    assert '55' in report['orphans']


def test_resync_on_a_dead_socket_says_so_rather_than_lying():
    v = venue()
    drop(v)
    report = v.resync()
    assert report['connected'] is False and report['brackets'] == 0


# ── the question worth asking after a drop ──────────────────────────────────


def test_a_position_with_a_working_stop_is_protected():
    v = venue(positions=[fake_position('ABCZ6', 2)])
    v.stop('ABCZ6', Side.SELL, 2, 20050.0)
    assert v.unprotected_positions() == []


def test_a_position_with_no_stop_is_reported():
    v = venue(positions=[fake_position('ABCZ6', 2)])
    assert [p.symbol for p in v.unprotected_positions()] == ['ABCZ6']


def test_a_target_alone_does_not_count_as_protection():
    """A limit above the market is not a stop. Only a stop is."""
    v = venue(positions=[fake_position('ABCZ6', 2)])
    v.limit('ABCZ6', Side.SELL, 2, 20200.0)
    assert [p.symbol for p in v.unprotected_positions()] == ['ABCZ6']


@pytest.mark.parametrize(
    'order_type,kw',
    [
        (OrderType.STOP, {'stop_price': 20050.0}),
        (OrderType.STOP_LIMIT, {'stop_price': 20050.0, 'limit_price': 20045.0}),
        (OrderType.TRAILING_STOP, {'trail_amount': 25.0}),
    ],
)
def test_every_kind_of_stop_counts_as_protection(order_type, kw):
    v = venue(positions=[fake_position('ABCZ6', 2)])
    v.place(OrderSpec('ABCZ6', Side.SELL, 2, order_type, **kw))
    assert v.unprotected_positions() == []


def test_a_flat_position_is_not_unprotected():
    v = venue(positions=[fake_position('ABCZ6', 0)])
    assert v.unprotected_positions() == []


# ── optional on-disk state, for a process restart ───────────────────────────


def test_bracket_state_survives_a_process_restart(tmp_path):
    path = tmp_path / 'state.json'
    ib = FakeIB(accounts=('DU1234567',))
    first = IbkrVenue(
        Env.PAPER, client_id=8010, port=4001, ib=ib, state_path=path
    ).connect()
    first.place(OrderSpec('ABCZ6', Side.BUY, 1, stop_loss=20050.0))
    assert path.exists()

    # a new process, a new venue object, the same file
    second = IbkrVenue(
        Env.PAPER,
        client_id=8011,
        port=4001,
        ib=FakeIB(accounts=('DU1234567',)),
        state_path=path,
    )
    assert len(second.brackets()) == 1, 'it must not start blind'


def test_a_restored_submission_is_treated_as_unresolved(tmp_path):
    """This process has not seen IB confirm it, so it must be read, not trusted."""
    path = tmp_path / 'state.json'
    ib = FakeIB(accounts=('DU1234567',))
    first = IbkrVenue(
        Env.PAPER, client_id=8012, port=4001, ib=ib, state_path=path
    ).connect()
    first.place(OrderSpec('ABCZ6', Side.BUY, 1, client_order_id='k1'))
    assert first.submissions()['k1'].state == 'submitted'

    second = IbkrVenue(
        Env.PAPER,
        client_id=8013,
        port=4001,
        ib=FakeIB(accounts=('DU1234567',)),
        state_path=path,
    )
    assert second.submissions()['k1'].state == 'unresolved'
    assert 'not confirmed by IB in this process' in second.submissions()['k1'].detail


def test_state_from_a_different_environment_is_ignored(tmp_path):
    """Paper state must never be loaded into a live venue."""
    path = tmp_path / 'state.json'
    path.write_text(
        json.dumps(
            {
                'env': 'paper',
                'brackets': [{'parent_id': '1', 'symbol': 'ABCZ6'}],
                'submissions': [],
            }
        )
    )
    live = IbkrVenue(
        Env.LIVE,
        client_id=8014,
        port=4002,
        ib=FakeIB(accounts=('U1234567',)),
        state_path=path,
    )
    assert live.brackets() == {}


def test_an_unreadable_state_file_is_not_fatal(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{not json')
    v = IbkrVenue(
        Env.PAPER,
        client_id=8015,
        port=4001,
        ib=FakeIB(accounts=('DU1234567',)),
        state_path=path,
    )
    assert v.brackets() == {}


def test_no_state_path_means_no_file_and_no_complaint():
    v = venue()
    v.place(OrderSpec('ABCZ6', Side.BUY, 1, stop_loss=20050.0))
    assert v.state_path is None and len(v.brackets()) == 1
