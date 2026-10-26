"""Turning a TradingView alert into an order, or refusing it.

TradingView's constraints shape all of this, and they are documented rather
than guessed:

* It POSTs with ``application/json`` when the alert message is valid JSON and
  ``text/plain`` otherwise.
* **It sends no custom headers.** So a shared secret has to travel in the URL
  path or in the payload; there is nowhere else to put it.
* It cancels the request after **3 seconds**.
* It sends from four fixed addresses.
* It accepts only ports 80 and 443, and no IPv6.

Nothing here invents a quantity, a price or a side. An alert that does not say
what to do is refused, because the alternative is guessing with money.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.parse

__all__ = [
    'Alert',
    'AlertError',
    'parse_alert',
    'redact_secret',
    'redact_all',
    'TRADINGVIEW_IPS',
]

#: TradingView's documented webhook senders. Allowlisted by default.
TRADINGVIEW_IPS = (
    '52.89.214.238',
    '34.212.75.30',
    '54.218.53.128',
    '52.32.178.7',
)

#: Words an alert may use for each side. Deliberately short: an alert that
#: says something else is refused rather than interpreted.
BUY_WORDS = frozenset({'buy', 'long', 'bull', 'enter_long', 'b'})
SELL_WORDS = frozenset({'sell', 'short', 'bear', 'enter_short', 's'})
FLAT_WORDS = frozenset({'flat', 'close', 'exit', 'flatten'})

_PLAIN = re.compile(
    r'^\s*(?P<action>[a-z_]+)\s+(?P<qty>[0-9]*\.?[0-9]+)\s+(?P<symbol>[A-Za-z0-9._/:-]+)'
    r'(?:\s+@\s*(?P<price>[0-9]*\.?[0-9]+))?\s*$',
    re.IGNORECASE,
)


class AlertError(Exception):
    """The alert cannot be turned into an order. Carries why, for the journal."""

    def __init__(self, message, *, code='BAD_ALERT'):
        super().__init__(message)
        self.message = message
        self.code = code


class Alert:
    """A validated alert: what to do, where, and how it identifies itself."""

    def __init__(
        self,
        *,
        action,
        symbol,
        qty=None,
        price=None,
        stop=None,
        take_profit=None,
        venue=None,
        account=None,
        order_id=None,
        raw=None,
    ):
        self.action = action
        self.symbol = symbol
        self.qty = qty
        self.price = price
        self.stop = stop
        self.take_profit = take_profit
        self.venue = venue
        self.account = account
        self.raw = raw or {}
        self.order_id = order_id or self.fingerprint()

    def fingerprint(self) -> str:
        """A stable id for this alert, so a redelivery is not a second order.

        TradingView sends no id of its own and can deliver the same alert more
        than once, so one is derived from what the alert says. An alert that
        wants to fire twice must carry its own ``id``.
        """
        parts = '|'.join(
            str(x)
            for x in (
                self.action,
                self.symbol,
                self.qty,
                self.price,
                self.stop,
                self.take_profit,
                self.venue,
                self.account,
            )
        )
        return 'tv:' + hashlib.blake2b(parts.encode(), digest_size=8).hexdigest()

    @property
    def is_flatten(self) -> bool:
        return self.action in FLAT_WORDS

    def __repr__(self) -> str:
        return 'Alert(%s %s %s on %s)' % (
            self.action,
            self.qty,
            self.symbol,
            self.venue or 'default',
        )


def parse_alert(body, *, default_venue=None, default_qty=None) -> Alert:
    """Parse a TradingView body into an ``Alert``, or raise ``AlertError``.

    Accepts JSON, which is what TradingView sends for a JSON alert message,
    and a short plain-text form for the rest::

        buy 1 ABCZ6
        sell 2 ABCZ6 @ 20150.25
        flatten 0 ABCZ6

    ``default_qty`` exists so an operator can configure a size rather than
    repeat it in every alert. There is no default side and no default symbol:
    guessing either of those places the wrong trade.
    """
    if isinstance(body, bytes):
        body = body.decode('utf-8', 'replace')
    body = (body or '').strip()
    if not body:
        raise AlertError('the alert body was empty', code='EMPTY')

    data = None
    if body[:1] in '{[':
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise AlertError(
                'the body looked like JSON but was not: %s' % exc, code='BAD_JSON'
            ) from exc
    if isinstance(data, dict):
        return _from_mapping(data, default_venue, default_qty)
    if data is not None:
        raise AlertError('a JSON alert must be an object', code='BAD_JSON')

    match = _PLAIN.match(body)
    if not match:
        raise AlertError(
            'could not read the alert. Send JSON, or text in the form '
            '"buy 1 ABCZ6" or "sell 2 ABCZ6 @ 20150.25".',
            code='UNREADABLE',
        )
    found = match.groupdict()
    return _from_mapping(
        {
            'action': found['action'],
            'qty': found['qty'],
            'symbol': found['symbol'],
            'price': found.get('price'),
        },
        default_venue,
        default_qty,
    )


def _from_mapping(data, default_venue, default_qty) -> Alert:
    lower = {str(k).lower(): v for k, v in data.items()}

    action = (
        str(
            _first(lower, 'action', 'side', 'order_action', 'strategy.order.action')
            or ''
        )
        .strip()
        .lower()
    )
    if not action:
        raise AlertError('the alert did not say buy, sell or flatten', code='NO_ACTION')
    if action not in BUY_WORDS | SELL_WORDS | FLAT_WORDS:
        raise AlertError(
            'unrecognised action %r. Use one of: %s'
            % (action, ', '.join(sorted(BUY_WORDS | SELL_WORDS | FLAT_WORDS))),
            code='BAD_ACTION',
        )

    symbol = _first(lower, 'symbol', 'ticker', 'instrument', 'contract')
    if not symbol:
        raise AlertError('the alert did not say which instrument', code='NO_SYMBOL')

    raw_qty = _first(
        lower, 'qty', 'quantity', 'contracts', 'size', 'strategy.order.contracts'
    )
    qty = _number(raw_qty, 'qty')
    if qty is None:
        qty = default_qty
    if action not in FLAT_WORDS:
        if qty is None:
            raise AlertError(
                'the alert did not say how many, and no default size is '
                'configured. This will not guess a size.',
                code='NO_QTY',
            )
        if qty <= 0:
            raise AlertError(
                'qty must be positive; the side says direction', code='BAD_QTY'
            )

    return Alert(
        action=action,
        symbol=str(symbol).strip(),
        qty=qty,
        price=_number(_first(lower, 'price', 'limit', 'limit_price'), 'price'),
        stop=_number(_first(lower, 'stop', 'stop_price', 'stop_loss', 'sl'), 'stop'),
        take_profit=_number(
            _first(lower, 'take_profit', 'target', 'tp', 'profit'), 'take_profit'
        ),
        venue=_text(_first(lower, 'venue', 'broker', 'exchange')) or default_venue,
        account=_text(_first(lower, 'account', 'account_id')),
        order_id=_text(_first(lower, 'id', 'order_id', 'client_order_id')),
        raw=data,
    )


def _first(mapping, *names):
    for name in names:
        if mapping.get(name) not in (None, ''):
            return mapping[name]
    return None


def _text(value):
    return None if value in (None, '') else str(value).strip()


def _number(value, field):
    if value in (None, ''):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AlertError(
            '%s is not a number: %r' % (field, value), code='BAD_NUMBER'
        ) from exc
    # NaN and Infinity reach here as floats and pass every comparison that
    # follows -- `float('nan') <= 0` is False, so a NaN quantity would clear
    # the "must be positive" check and go to a broker as a size. Python's
    # json module accepts the bare literals NaN and Infinity, so an alert
    # can simply say so.
    if not math.isfinite(number):
        raise AlertError(
            '%s must be a finite number, not %r' % (field, value),
            code='BAD_NUMBER',
        )
    return number


def redact_secret(body, secret) -> str:
    """The alert body with the shared secret taken out.

    The body is kept because it is the only way to see what TradingView
    actually sent when an alert is refused. But the secret may travel in the
    payload -- which is the recommended way, since it keeps it out of access
    logs -- and the journal is a file people attach to a bug report. So it
    comes out before anything is written down.
    """
    if not body or not secret:
        return body
    text = str(body)
    if text.lstrip()[:1] == '{':
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            kept = {k: v for k, v in data.items() if str(k).lower() != 'secret'}
            if len(kept) != len(data):
                # ensure_ascii=False, because the default escapes non-ASCII
                # to \uXXXX and the replace below would then miss it.
                text = json.dumps(kept, ensure_ascii=False)
    # Always, and last. Dropping the key is not enough: the same value can
    # sit in another field, in a nested object, or in free text, and any of
    # those would carry it into the journal.
    secret = str(secret)
    text = text.replace(secret, '<redacted>')
    # And the escaped forms, which are the same secret to anyone reading the
    # journal: a quote in the secret becomes \" inside JSON, and a non-ASCII
    # character becomes \uXXXX.
    for form in _escaped_forms(secret):
        text = text.replace(form, '<redacted>')
    return text


def _escaped_forms(secret) -> set:
    """The ways this secret can appear without appearing literally."""
    forms = {
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        urllib.parse.quote(secret, safe=''),
        urllib.parse.quote_plus(secret),
    }
    try:
        forms.add(secret.encode('unicode_escape').decode('ascii'))
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return {f for f in forms if f and f != secret}


def redact_all(body, secrets) -> str:
    """The body with every one of ``secrets`` taken out.

    The webhook secret is not the only thing worth hiding. A broker
    exception has seen the credentials it was given, and its text goes
    to the journal and to the status endpoint.
    """
    text = body
    for secret in secrets or ():
        if secret:
            text = redact_secret(text, secret)
    return text
