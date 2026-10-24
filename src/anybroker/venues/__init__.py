"""Venues. Each module imports its own driver lazily, so the package works
with none of them installed.
"""

from typing import Any

__all__ = ['AlpacaVenue', 'CcxtVenue', 'IbkrVenue', 'TradovateVenue']


def __getattr__(name) -> Any:
    if name == 'CcxtVenue':
        from .ccxt_venue import CcxtVenue

        return CcxtVenue
    if name == 'IbkrVenue':
        from .ibkr import IbkrVenue

        return IbkrVenue
    if name == 'TradovateVenue':
        from .tradovate import TradovateVenue

        return TradovateVenue
    if name == 'AlpacaVenue':
        from .alpaca import AlpacaVenue

        return AlpacaVenue
    raise AttributeError(name)
