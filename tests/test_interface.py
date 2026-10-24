"""Both venues answer the same calls -- that is the whole point."""

from anybroker import Env
from anybroker.venue import Venue
from anybroker.venues.ccxt_venue import CcxtVenue
from anybroker.venues.ibkr import IbkrVenue
from fakes import FakeCcxt, FakeIB

VERBS = (
    'get_account',
    'list_positions',
    'get_position',
    'list_orders',
    'get_order',
    'working_orders',
    'place',
    'cancel',
    'cancel_all',
    'flatten',
    'flatten_all',
    'market',
    'limit',
    'stop',
    'stop_limit',
    'trailing_stop',
    'connect',
    'close',
)


def test_every_venue_answers_the_same_verbs():
    for v in (
        IbkrVenue(Env.PAPER, client_id=1, port=4002, ib=FakeIB(accounts=('DU1',))),
        CcxtVenue('fake', Env.LIVE, exchange=FakeCcxt()),
    ):
        for verb in VERBS:
            assert callable(getattr(v, verb)), '%s lacks %s' % (v.name, verb)


def test_the_verbs_match_the_names_the_hand_written_adapters_used():
    """place_market and flatten were in all seven; the rest are the same shape."""
    for verb in ('get_account', 'list_positions', 'get_position', 'flatten'):
        assert hasattr(Venue, verb)


def test_a_venue_is_a_context_manager():
    ib = FakeIB(accounts=('DU1',))
    with IbkrVenue(Env.PAPER, client_id=1, port=4002, ib=ib) as v:
        assert v.env is Env.PAPER
        assert ib.connected
    assert not ib.connected


def test_every_venue_declares_its_capabilities():
    for v in (
        IbkrVenue(Env.PAPER, client_id=1, port=4002, ib=FakeIB(accounts=('DU1',))),
        CcxtVenue('fake', Env.LIVE, exchange=FakeCcxt()),
    ):
        caps = v.capabilities
        assert caps.order_types
        assert isinstance(caps.supports_paper, bool)
        assert isinstance(caps.notes, str)


def test_both_report_their_env_in_repr():
    assert 'paper' in repr(
        IbkrVenue(Env.PAPER, client_id=1, port=4002, ib=FakeIB(accounts=('DU1',)))
    )
    assert 'live' in repr(CcxtVenue('fake', Env.LIVE, exchange=FakeCcxt()))
