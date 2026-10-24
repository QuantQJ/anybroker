"""Open a venue by name.

    from anybroker import open_venue, Env, Side

    ib = open_venue('ibkr', Env.PAPER, client_id=7788)
    kr = open_venue('kraken', Env.LIVE, api_key=..., secret=...)

Both objects answer the same calls. ``open_venue`` resolves this library's own
venues first, then falls through to ccxt, so the crypto exchanges come from
ccxt without being named here one by one.
"""

from .enums import Env
from .errors import INSTALL_HINT
from .venue import Venue

__all__ = ['open_venue', 'available', 'NATIVE_VENUES', 'CCXT_ONLY_NOTE']

#: Venues this library implements itself, because ccxt has never had them.
NATIVE_VENUES = ('ibkr', 'tradovate', 'alpaca')

CCXT_ONLY_NOTE = (
    'ccxt covers crypto exchanges only. It has no Tradovate, IBKR, Tradier, '
    'TradeStation or NinjaTrader -- those are this library\'s own venues.'
)


def open_venue(name, env, **kw) -> "Venue":
    """Return a connected-on-demand venue. ``env`` is required."""
    key = str(name).strip().lower()
    env = Env(env)
    if key in ('ibkr', 'ib', 'interactivebrokers', 'interactive_brokers'):
        from .venues.ibkr import IbkrVenue

        return IbkrVenue(env, **kw)
    if key in ('tradovate', 'tv'):
        from .venues.tradovate import TradovateVenue

        return TradovateVenue(env, **kw)
    if key in ('alpaca', 'alpaca-equities'):
        # ccxt's `alpaca` is Alpaca's crypto API. This is the stocks and
        # options side, which ccxt has never covered.
        from .venues.alpaca import AlpacaVenue

        return AlpacaVenue(env, **kw)
    from .venues.ccxt_venue import CcxtVenue

    return CcxtVenue(key, env, **kw)


#: Each native venue and the import it needs.
NATIVE_DRIVERS = {
    'ibkr': ('ib_insync', 'anybroker[ibkr]'),
    'tradovate': ('tradovate_client', 'anybroker[tradovate]'),
    'alpaca': ('alpaca', 'anybroker[alpaca]'),
}


def available() -> dict:
    """What can actually be opened here, given what is installed.

    ``native`` lists only the venues whose driver imports. A venue whose
    driver is missing appears in ``missing`` with the extra that supplies it,
    so this is a readiness check rather than a catalogue.
    """
    import importlib.util

    native, missing = [], {}
    for name, (module, extra) in sorted(NATIVE_DRIVERS.items()):
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if found:
            native.append(name)
        else:
            missing[name] = INSTALL_HINT % extra.split('[')[-1].rstrip(']')
    out = {
        'native': native,
        'native_missing': missing,
        'all_native': sorted(NATIVE_DRIVERS),
        'ccxt': [],
        'note': CCXT_ONLY_NOTE,
    }
    try:
        import ccxt

        # getattr, not ccxt.exchanges: anything importable as "ccxt" lands
        # here, including a source checkout or a stray ccxt.py in the
        # working directory, and this is the function someone runs *because*
        # something is already confusing them.
        exchanges = getattr(ccxt, 'exchanges', None)
        if exchanges is None:
            out['missing_ccxt'] = (
                'something is importable as "ccxt" but it is not the ccxt '
                'package -- a source checkout or a ccxt.py in the working '
                'directory will do it. Installed at: %s'
                % (getattr(ccxt, '__file__', None) or '(namespace package)')
            )
        else:
            out['ccxt'] = sorted(exchanges)
    except ImportError:
        out['missing_ccxt'] = INSTALL_HINT % 'crypto'
    return out
