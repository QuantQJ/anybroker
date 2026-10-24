"""IBKR, against a fake IB. No gateway, no account, no network."""

import pytest

from anybroker import (ConfigError, Env, NotFoundError, OrderSpec, OrderType,
                       Side, UnsupportedError)
from anybroker.venues.ibkr import GATEWAY_PORTS, TWS_PORTS, IbkrVenue
from fakes import FakeIB, fake_position


def venue(env=Env.PAPER, **kw):
    ib = kw.pop('ib', None) or FakeIB(**{k: kw.pop(k) for k in
                                        ('positions', 'accounts', 'qualify')
                                        if k in kw})
    v = IbkrVenue(env, client_id=kw.pop('client_id', 7788), ib=ib, **kw)
    v.connect()
    return v


# ── the client-id hazard ────────────────────────────────────────────────────

def test_client_id_is_required():
    """IB kicks off an existing session on a duplicate id, so no default."""
    with pytest.raises(ConfigError, match='client_id is required'):
        IbkrVenue(Env.PAPER)


def test_the_client_id_reaches_the_connection():
    v = venue(client_id=424242)
    assert v._ib.connect_args['clientId'] == 424242


# ── destination ─────────────────────────────────────────────────────────────

def test_paper_and_live_are_different_ports():
    assert venue(Env.PAPER).port == GATEWAY_PORTS[Env.PAPER] == 4001
    assert venue(Env.LIVE).port == GATEWAY_PORTS[Env.LIVE] == 4002


def test_tws_ports_are_available():
    assert venue(Env.LIVE, tws=True).port == TWS_PORTS[Env.LIVE] == 7496
    assert venue(Env.PAPER, tws=True).port == TWS_PORTS[Env.PAPER] == 7497


def test_an_explicit_port_wins():
    assert venue(Env.PAPER, port=4005).port == 4005


def test_env_is_required():
    with pytest.raises(TypeError):
        IbkrVenue(client_id=1)


# ── order translation ───────────────────────────────────────────────────────

def test_a_market_order_becomes_MKT():
    v = venue()
    v.market('MNQZ6', Side.BUY, 2)
    order = v._ib.placed[-1].order
    assert order.orderType == 'MKT' and order.action == 'BUY'
    assert order.totalQuantity == 2 and order.tif == 'DAY'


def test_a_limit_order_becomes_LMT_with_lmtPrice():
    v = venue()
    v.limit('MNQZ6', Side.BUY, 1, 20150.25)
    order = v._ib.placed[-1].order
    assert order.orderType == 'LMT' and order.lmtPrice == 20150.25


def test_a_stop_order_becomes_STP_with_auxPrice():
    v = venue()
    v.stop('MNQZ6', Side.SELL, 1, 20100.0)
    order = v._ib.placed[-1].order
    assert order.orderType == 'STP' and order.auxPrice == 20100.0


def test_a_stop_limit_carries_both_prices():
    v = venue()
    v.stop_limit('MNQZ6', Side.SELL, 1, 20095.0, 20100.0)
    order = v._ib.placed[-1].order
    assert order.orderType == 'STP LMT'
    assert order.lmtPrice == 20095.0 and order.auxPrice == 20100.0


def test_a_trailing_stop_by_amount_uses_auxPrice():
    v = venue()
    v.trailing_stop('MNQZ6', Side.SELL, 1, trail_amount=25.0)
    order = v._ib.placed[-1].order
    assert order.orderType == 'TRAIL' and order.auxPrice == 25.0


def test_a_trailing_stop_by_percent_uses_trailingPercent():
    v = venue()
    v.trailing_stop('MNQZ6', Side.SELL, 1, trail_percent=1.5)
    assert v._ib.placed[-1].order.trailingPercent == 1.5


def test_a_label_or_client_id_becomes_orderRef():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 1, client_order_id='rule-7'))
    assert v._ib.placed[-1].order.orderRef == 'rule-7'


def test_extra_fields_pass_straight_through():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 1, extra={'outsideRth': True}))
    assert v._ib.placed[-1].order.outsideRth is True


def test_reduce_only_is_refused_rather_than_ignored():
    v = venue()
    with pytest.raises(UnsupportedError, match='reduce-only'):
        v.place(OrderSpec('MNQZ6', Side.SELL, 1, reduce_only=True))


# ── brackets: nothing works at the exchange until the group is staged ──────

def test_a_bracket_stages_the_parent_held_and_releases_on_the_last_child():
    v = venue()
    result = v.place(OrderSpec('MNQZ6', Side.BUY, 2, take_profit=20200.0,
                               stop_loss=20050.0))
    placed = v._ib.placed
    assert len(placed) == 3
    parent, target, stop = [t.order for t in placed]
    assert parent.transmit is False, 'the parent must be held'
    assert target.transmit is False, 'the first child must not release it'
    assert stop.transmit is True, 'only the last child releases the group'
    assert len(result.children) == 2


def test_bracket_children_are_linked_to_the_parent_and_to_each_other():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 1, take_profit=20200.0,
                      stop_loss=20050.0))
    parent, target, stop = [t.order for t in v._ib.placed]
    assert target.parentId == parent.orderId
    assert stop.parentId == parent.orderId
    assert target.ocaGroup == stop.ocaGroup != ''
    assert target.ocaType == stop.ocaType == 1


def test_bracket_children_are_the_opposite_side_and_the_right_types():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 3, take_profit=20200.0,
                      stop_loss=20050.0))
    _, target, stop = [t.order for t in v._ib.placed]
    assert target.action == 'SELL' and target.orderType == 'LMT'
    assert target.lmtPrice == 20200.0 and target.totalQuantity == 3
    assert stop.action == 'SELL' and stop.orderType == 'STP'
    assert stop.auxPrice == 20050.0


def test_a_stop_only_bracket_releases_on_its_single_child():
    v = venue()
    v.place(OrderSpec('MNQZ6', Side.BUY, 1, stop_loss=20050.0))
    parent, stop = [t.order for t in v._ib.placed]
    assert parent.transmit is False and stop.transmit is True


# ── reads ───────────────────────────────────────────────────────────────────

def test_positions_are_normalised():
    v = venue(positions=[fake_position('MNQZ6', 2, 20100.0),
                         fake_position('MESZ6', -1, 5800.0)])
    held = {p.symbol: p for p in v.list_positions()}
    assert held['MNQZ6'].qty == 2 and held['MNQZ6'].side is Side.BUY
    assert held['MESZ6'].qty == -1 and held['MESZ6'].side is Side.SELL


def test_get_position_finds_one_and_returns_none_otherwise():
    v = venue(positions=[fake_position('MNQZ6', 2)])
    assert v.get_position('MNQZ6').qty == 2
    assert v.get_position('NOPE') is None


def test_the_account_is_normalised():
    account = venue().get_account()
    assert account.id == 'DU111' and account.equity == 50250.0
    assert account.cash == 50000.0 and account.open_pnl == 250.0


def test_several_accounts_must_be_disambiguated():
    v = venue(accounts=('DU1', 'DU2'))
    with pytest.raises(ConfigError, match='say which'):
        v.get_account()


def test_no_accounts_raises():
    v = venue(accounts=())
    with pytest.raises(NotFoundError, match='no managed accounts'):
        v.get_account()


# ── cancel and flatten ──────────────────────────────────────────────────────

def test_cancel_reaches_ib():
    v = venue()
    order = v.market('MNQZ6', Side.BUY, 1)
    v.cancel(order.id)
    assert v._ib.cancelled == [int(order.id)]


def test_cancelling_an_unknown_order_raises():
    with pytest.raises(NotFoundError, match='no IB order'):
        venue().cancel(999)


def test_flatten_sells_a_long_and_buys_a_short():
    v = venue(positions=[fake_position('MNQZ6', 2)])
    v.flatten('MNQZ6')
    order = v._ib.placed[-1].order
    assert order.action == 'SELL' and order.totalQuantity == 2
    assert order.orderType == 'MKT' and order.orderRef == 'flatten'

    v2 = venue(positions=[fake_position('MESZ6', -3)])
    v2.flatten('MESZ6')
    order2 = v2._ib.placed[-1].order
    assert order2.action == 'BUY' and order2.totalQuantity == 3


def test_flattening_nothing_raises():
    with pytest.raises(NotFoundError, match='no open position'):
        venue().flatten('MNQZ6')


def test_flatten_all_cancels_then_closes():
    v = venue(positions=[fake_position('MNQZ6', 1)])
    v.market('MNQZ6', Side.BUY, 1)
    result = v.flatten_all()
    assert result['flattened'] == ['MNQZ6']
    assert result['cancelled']


# ── contracts ───────────────────────────────────────────────────────────────

def test_an_ambiguous_symbol_is_refused_rather_than_guessed():
    from fakes import FakeContract
    v = venue(qualify=[FakeContract('MNQ', 'MNQZ6'), FakeContract('MNQ', 'MNQH7')])
    with pytest.raises(ConfigError, match='ambiguous'):
        v.market('MNQ', Side.BUY, 1)


def test_an_unknown_symbol_is_refused():
    v = venue(qualify=[])
    with pytest.raises(NotFoundError, match='could not qualify'):
        v.market('ZZZZ', Side.BUY, 1)


def test_a_contract_factory_overrides_resolution():
    from fakes import FakeContract
    v = venue(contract_factory=lambda s: FakeContract('X', 'MNQZ6'))
    v.market('whatever', Side.BUY, 1)
    assert v._ib.placed[-1].contract.localSymbol == 'MNQZ6'
