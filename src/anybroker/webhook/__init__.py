"""TradingView alerts to any broker.

    python -m anybroker.webhook secret                 # make one
    ANYHOOK_SECRET=... ANYHOOK_VENUE=tradovate \
    ANYHOOK_ENV=paper python -m anybroker.webhook serve

Point a TradingView alert at ``http://your-host/alert/<secret>`` and send
either JSON or a line like ``buy 1 ABCZ6``.

Routing is done by anybroker itself, so the same alert reaches Tradovate,
IBKR, Alpaca or any ccxt exchange.

Three things this will not do: start without a shared secret, reach a live
account without being told to in those words, or guess a size or a side that
the alert did not state.
"""

from .alert import (
    TRADINGVIEW_IPS,
    Alert,
    AlertError,
    parse_alert,
    redact_secret,
)
from .config import Config, ConfigError, new_secret
from .journal import Journal


__all__ = [
    'Alert',
    'AlertError',
    'parse_alert',
    'TRADINGVIEW_IPS',
    'redact_secret',
    'Config',
    'ConfigError',
    'new_secret',
    'Journal',
]
