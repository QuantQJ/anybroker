"""The neutral vocabulary.

Every venue speaks its own dialect -- IB says ``MKT``, ccxt says ``market``,
Tradovate says ``Market``. A caller should never have to know which. These are
the words this library uses; each venue translates them at its own edge.
"""

from enum import Enum

__all__ = ['Env', 'Side', 'OrderType', 'TimeInForce']


class Env(str, Enum):
    """Paper or live. Required everywhere, with no default anywhere.

    Not every venue has a paper environment. ``Capabilities.supports_paper``
    says whether one does, and asking for paper where none exists raises
    rather than quietly sending the order to the live book.
    """

    PAPER = 'paper'
    LIVE = 'live'

    @property
    def is_live(self) -> bool:
        return self is Env.LIVE


class Side(str, Enum):
    BUY = 'buy'
    SELL = 'sell'

    @property
    def opposite(self) -> 'Side':
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = 'market'
    LIMIT = 'limit'
    STOP = 'stop'
    STOP_LIMIT = 'stop_limit'
    TRAILING_STOP = 'trailing_stop'
    TRAILING_STOP_LIMIT = 'trailing_stop_limit'


class TimeInForce(str, Enum):
    DAY = 'day'
    GTC = 'gtc'
    IOC = 'ioc'
    FOK = 'fok'
    GTD = 'gtd'
