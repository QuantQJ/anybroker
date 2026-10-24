"""Open a venue by name.

    from anybroker import open_venue, Env, Side

    ib = open_venue('ibkr', Env.PAPER, client_id=7788)
    kr = open_venue('kraken', Env.LIVE, api_key=..., secret=...)

Both objects answer the same calls. ``open_venue`` resolves this library's own
venues first, then falls through to ccxt, so the crypto exchanges come from
ccxt without being named here one by one.
"""

from .enums import Env
from .errors import ConfigError

__all__ = ['open_venue', 'available', 'NATIVE_VENUES', 'CCXT_ONLY_NOTE']

#: Venues this library implements itself, because ccxt has never had them.
NATIVE_VENUES = ('ibkr', 'tradovate')

CCXT_ONLY_NOTE = (
    'ccxt covers crypto exchanges only. It has no Tradovate, IBKR, Tradier, '
    'TradeStation or NinjaTrader -- those are this library\'s own venues.')


def open_venue(name, env, **kw):
    """Return a connected-on-demand venue. ``env`` is required."""
    key = str(name).strip().lower()
    env = Env(env)
    if key in ('ibkr', 'ib', 'interactivebrokers', 'interactive_brokers'):
        from .venues.ibkr import IbkrVenue
        return IbkrVenue(env, **kw)
    if key in ('tradovate', 'tv'):
        from .venues.tradovate import TradovateVenue
        return TradovateVenue(env, **kw)
    from .venues.ccxt_venue import CcxtVenue
    return CcxtVenue(key, env, **kw)


def available():
    """What can be opened here, given what is installed."""
    out = {'native': list(NATIVE_VENUES), 'ccxt': [], 'note': CCXT_ONLY_NOTE}
    try:
        import ccxt
        out['ccxt'] = sorted(ccxt.exchanges)
    except ImportError:
        out['ccxt_error'] = 'ccxt is not installed; pip install "anybroker[crypto]"'
    try:
        import ib_insync          # noqa: F401
    except ImportError:
        out['ibkr_error'] = 'ib_insync is not installed; pip install "anybroker[ibkr]"'
    return out
