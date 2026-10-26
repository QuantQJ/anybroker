"""Turning an alert into an order at a broker, through its parent package.

What this layer is for, beyond translation:

**A redelivered alert does not become a second position.** TradingView sends
no id of its own and can deliver the same alert more than once. Every order
carries an id derived from what the alert said, and anybroker refuses a
duplicate rather than filling it twice.

**Paper cannot quietly become live.** The venue is built for one environment
and verifies the account it was given after connecting, so a misconfigured
port or a wrong key fails at startup rather than at the first order.

**A refusal is recorded, not swallowed.** The journal holds why an alert did
nothing, which is the question a fill log cannot answer.
"""

from __future__ import annotations

from .alert import BUY_WORDS, redact_all

__all__ = ['Router', 'RouterError']


class RouterError(Exception):
    """The order could not be placed. ``code`` is for the journal."""

    def __init__(self, message, *, code='ROUTE_FAILED', ambiguous=False):
        super().__init__(message)
        self.message = message
        self.code = code
        #: True when the broker may have acted anyway. Then the position has
        #: to be read rather than assumed, and the alert is not resent.
        self.ambiguous = ambiguous


class Router:
    """Holds one connected venue and places orders on it.

    The venue is built lazily and kept, so the TLS handshake and the
    authentication happen once at startup rather than per alert -- which
    matters when TradingView cancels the request after three seconds.
    """

    def __init__(self, config, *, venue=None, journal=None) -> None:
        self.config = config
        self.journal = journal
        self._venue = venue
        #: The account the broker actually answered with, once connected.
        self.account = None

    # ── the venue ───────────────────────────────────────────────────────────

    @property
    def venue(self):
        if self._venue is None:
            self._venue = self._open()
        return self._venue

    def _open(self):
        from .. import Env, open_venue

        options = dict(self.config.venue_options)
        if self.config.account and 'account' not in options:
            options['account'] = self.config.account
        try:
            venue = open_venue(self.config.venue, Env(self.config.env), **options)
        except Exception as exc:
            raise RouterError(
                'could not open %s (%s): %s'
                % (self.config.venue, self.config.env, self._safe(exc)),
                code='VENUE_OPEN_FAILED',
            ) from exc
        return venue

    def connect(self):
        """Open the venue now, so a bad setup fails at startup.

        Deliberately eager: discovering a wrong port or a stale key when the
        first alert arrives means missing that trade and learning about it
        from a log.

        And deliberately a *read*, not just a handshake. Several venues build
        a client without talking to the broker -- the token is fetched at the
        first request -- so a venue that constructed cleanly proves only that
        the credentials parsed. The account is read back, which forces the
        authentication and confirms which account the orders would reach.
        """
        venue = self.venue
        try:
            connector = getattr(venue, 'connect', None)
            if callable(connector):
                connector()
        except Exception as exc:
            raise RouterError(
                'could not connect to %s: %s' % (self.config.venue, self._safe(exc)),
                code='VENUE_CONNECT_FAILED',
            ) from exc
        self.account = self._verify_account(venue)
        return venue

    def _verify_account(self, venue):
        """Read the account, so "connected" means the broker answered."""
        reader = getattr(venue, 'get_account', None)
        if not callable(reader):
            return None
        try:
            account = reader()
        except Exception as exc:
            raise self._translate(exc, 'ACCOUNT_READ_FAILED') from exc
        if account is None:
            raise RouterError(
                '%s connected but returned no account, so there is nothing to '
                'confirm the orders would reach' % self.config.venue,
                code='NO_ACCOUNT',
            )
        wanted = self.config.account
        got = str(getattr(account, 'id', '') or '')
        if wanted and str(wanted) not in (got, str(getattr(account, 'name', ''))):
            raise RouterError(
                'the broker answered with account %s, not the %s this bridge '
                'was configured for' % (got or '(unnamed)', wanted),
                code='WRONG_ACCOUNT',
            )
        return account

    def close(self) -> None:
        if self._venue is not None:
            closer = getattr(self._venue, 'close', None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

    # ── placing ─────────────────────────────────────────────────────────────

    def place(self, alert) -> dict:
        """Act on one alert. Returns a dict for the journal and the reply."""
        if self.config.dry_run:
            return {
                'outcome': 'dry_run',
                'detail': 'dry run: %r was not sent' % (alert,),
                'order_id': alert.order_id,
            }
        if alert.is_flatten:
            return self._flatten(alert)
        return self._enter(alert)

    def _flatten(self, alert) -> dict:
        try:
            order = self.venue.flatten(alert.symbol)
        except Exception as exc:
            raise self._translate(exc, 'FLATTEN_FAILED') from exc
        return {
            'outcome': 'placed',
            'detail': 'flattened %s' % alert.symbol,
            'broker_id': getattr(order, 'id', None),
            'order_id': alert.order_id,
        }

    def _enter(self, alert) -> dict:
        spec = self.to_spec(alert)
        # Asked *before* the send, because afterwards the two cases look
        # identical: anybroker returns the live order either way.
        already = self._already_sent(spec)
        try:
            order = self.venue.place(spec)
        except Exception as exc:
            raise self._translate(exc, 'PLACE_FAILED') from exc
        if already is not None:
            return {
                'outcome': 'duplicate',
                'detail': 'already working as %s; nothing was sent' % already,
                'broker_id': getattr(order, 'id', None) or already,
                'order_id': alert.order_id,
            }
        return {
            'outcome': 'placed',
            'detail': 'order %s' % (getattr(order, 'id', '') or 'accepted'),
            'broker_id': getattr(order, 'id', None),
            'order_id': alert.order_id,
        }

    def _already_sent(self, spec):
        """The broker id of an earlier send of this exact order, or None.

        A redelivered alert whose order is still working comes back from
        anybroker as that order, with nothing sent -- which is right, and
        indistinguishable from a fresh placement at the call site. Without
        this check the journal would count a redelivery as a second order.
        """
        try:
            previous = self.venue.submissions().get(spec.idempotency_key)
        except Exception:
            return None
        if previous is None:
            return None
        return previous.order_id or spec.idempotency_key

    def to_spec(self, alert):
        """Build the venue-neutral order. Nothing here is inferred.

        A price turns the order into a limit, a stop into a stop, both into a
        stop-limit. A ``stop`` sent alongside a ``take_profit`` is read as a
        bracket on a market entry, because that is what an alert carrying both
        means. No size, side or price is ever supplied by this function.
        """
        from .. import OrderSpec, OrderType, Side

        side = Side.BUY if alert.action in BUY_WORDS else Side.SELL
        bracket = alert.take_profit is not None
        if bracket:
            return OrderSpec(
                alert.symbol,
                side,
                alert.qty,
                OrderType.LIMIT if alert.price is not None else OrderType.MARKET,
                limit_price=alert.price,
                take_profit=alert.take_profit,
                stop_loss=alert.stop,
                client_order_id=alert.order_id,
            )
        if alert.price is not None and alert.stop is not None:
            kind = OrderType.STOP_LIMIT
        elif alert.stop is not None:
            kind = OrderType.STOP
        elif alert.price is not None:
            kind = OrderType.LIMIT
        else:
            kind = OrderType.MARKET
        return OrderSpec(
            alert.symbol,
            side,
            alert.qty,
            kind,
            limit_price=alert.price,
            stop_price=alert.stop,
            client_order_id=alert.order_id,
        )

    # ── failures ────────────────────────────────────────────────────────────

    def _translate(self, exc, default_code) -> RouterError:
        """Name the failure, and say whether the broker may have acted.

        The distinction that matters: a duplicate or a refusal is settled and
        safe to report; a timeout is not, and an alert whose fate is unknown
        must not be replayed.
        """
        name = type(exc).__name__
        ambiguous = bool(getattr(exc, 'ambiguous', False))
        codes = {
            'DuplicateOrderError': 'DUPLICATE',
            'UnresolvedOrderError': 'UNRESOLVED',
            'RejectedError': 'REJECTED',
            'UnsupportedError': 'UNSUPPORTED',
            'NotFoundError': 'NOT_FOUND',
            'ConfigError': 'BAD_ORDER',
            'AuthError': 'AUTH',
            'ThrottledError': 'THROTTLED',
            'DisconnectedError': 'DISCONNECTED',
            'WrongDestinationError': 'WRONG_DESTINATION',
        }
        code = codes.get(name, default_code)
        if name == 'UnresolvedOrderError':
            ambiguous = True
        return RouterError(
            redact_all(str(exc), self.config.sensitive_values()),
            code=code,
            ambiguous=ambiguous,
        )

    # ── what the desk is actually doing ─────────────────────────────────────

    def _safe(self, exc) -> str:
        """An exception as text, with nothing secret left in it.

        A broker exception has seen the credentials it was handed, and this
        string is returned over HTTP as well as journaled.
        """
        return '%s: %s' % (
            type(exc).__name__,
            redact_all(str(exc), self.config.sensitive_values())[:400],
        )

    def status(self) -> dict:
        """A snapshot for the status endpoint. Read-only."""
        out = {'venue': self.config.venue, 'env': self.config.env}
        try:
            out['positions'] = {
                p.symbol: p.qty for p in self.venue.list_positions() if not p.is_flat
            }
        except Exception as exc:
            out['positions_error'] = self._safe(exc)
        try:
            out['working_orders'] = len(self.venue.working_orders())
        except Exception as exc:
            out['orders_error'] = self._safe(exc)
        try:
            unprotected = self.venue.unprotected_positions()
            out['unprotected'] = [p.symbol for p in unprotected]
        except Exception as exc:
            out['unprotected_error'] = self._safe(exc)
        try:
            out['unresolved'] = [s.key for s in self.venue.unresolved()]
        except Exception:
            pass
        return out
