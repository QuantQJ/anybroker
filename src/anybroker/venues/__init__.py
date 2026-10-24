"""Venues. Each module imports its own driver lazily, so the package works
with none of them installed.
"""

__all__ = ['CcxtVenue', 'IbkrVenue']


def __getattr__(name):
    if name == 'CcxtVenue':
        from .ccxt_venue import CcxtVenue
        return CcxtVenue
    if name == 'IbkrVenue':
        from .ibkr import IbkrVenue
        return IbkrVenue
    raise AttributeError(name)
