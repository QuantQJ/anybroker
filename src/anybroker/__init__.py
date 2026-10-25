"""One interface over the brokers you actually trade.

    from anybroker import open_venue, Env, Side, OrderSpec

    ib = open_venue('ibkr', Env.PAPER, client_id=7788)
    with ib:
        ib.limit('ABCZ6', Side.BUY, 2, 20150.25)
        print(ib.list_positions())

The crypto exchanges come from ccxt, which already covers 104 of them with
every order type and somebody else maintaining it. What ccxt has never had is
Tradovate, IBKR, Tradier, TradeStation or NinjaTrader -- the futures and
retail brokers. Tradovate and IBKR are this library's own venues; the rest of
that list is still to come.

``env`` is required everywhere and has no default. Paper and live are
different destinations, and a venue built for one has no code path to the
other. A venue with no paper environment says so instead of quietly sending
the order to the live book.
"""

from .enums import Env, OrderType, Side, TimeInForce
from .errors import (
    AuthError,
    BrokerError,
    ConfigError,
    DisconnectedError,
    DuplicateOrderError,
    NotFoundError,
    RejectedError,
    ThrottledError,
    TransportError,
    UnresolvedOrderError,
    UnsupportedError,
    WrongDestinationError,
)
from .models import Account, Fill, Order, Position, Quote
from .registry import available, open_venue
from .spec import OrderSpec
from .venue import Capabilities, Submission, Venue

__version__ = '0.1.0'

__all__ = [
    'open_venue',
    'available',
    'Env',
    'Side',
    'OrderType',
    'TimeInForce',
    'OrderSpec',
    'Venue',
    'Capabilities',
    'Submission',
    'Account',
    'Position',
    'Order',
    'Fill',
    'Quote',
    'BrokerError',
    'ConfigError',
    'UnsupportedError',
    'NotFoundError',
    'AuthError',
    'ThrottledError',
    'RejectedError',
    'TransportError',
    'WrongDestinationError',
    'DuplicateOrderError',
    'UnresolvedOrderError',
    'DisconnectedError',
    '__version__',
]
