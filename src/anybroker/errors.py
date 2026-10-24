"""Exceptions. Every failure raises; nothing returns an error dictionary."""

__all__ = ['BrokerError', 'ConfigError', 'UnsupportedError', 'NotFoundError',
           'TransportError', 'AuthError', 'ThrottledError', 'RejectedError',
           'WrongDestinationError']


class BrokerError(Exception):
    """Base class."""

    def __init__(self, message, *, venue=None, detail=None):
        super().__init__(message)
        self.message = message
        self.venue = venue
        self.detail = detail

    def __str__(self):
        bits = [self.message]
        if self.venue:
            bits.append('venue=%s' % self.venue)
        if self.detail not in (None, '', {}):
            bits.append('detail=%r' % (self.detail,))
        return ' | '.join(bits)


class ConfigError(BrokerError):
    """Something the caller must supply is missing or wrong. Nothing was sent."""


class UnsupportedError(BrokerError):
    """This venue cannot do what was asked.

    Raised instead of silently substituting something else -- asking for a
    paper account at a venue that has none, or a trailing stop at a venue that
    does not offer one.
    """


class NotFoundError(BrokerError):
    """No such order, position, symbol or account."""


class AuthError(BrokerError):
    """Credentials rejected."""


class ThrottledError(BrokerError):
    """Rate limited."""

    def __init__(self, message, *, retry_after=None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class RejectedError(BrokerError):
    """The venue received the order and refused it."""


class WrongDestinationError(BrokerError):
    """The venue connected, but to the wrong kind of account.

    Raised when ``Env.PAPER`` was asked for and the broker handed back a live
    account, or the reverse. The connection is closed before this is raised,
    so nothing can be sent to the wrong book.

    This exists because a destination is usually a port or a host, and a port
    is one digit away from the other account. Checking the account after
    connecting is the only guard that does not depend on getting that digit
    right.
    """

    def __init__(self, message, *, asked=None, accounts=None, port=None, **kw):
        super().__init__(message, **kw)
        self.asked = asked
        self.accounts = list(accounts or [])
        self.port = port


class TransportError(BrokerError):
    """The request did not complete.

    ``ambiguous`` is True when the request may already have been accepted, so
    repeating it could duplicate the effect. For an order that means: read the
    order and position state before sending anything else.
    """

    def __init__(self, message, *, ambiguous=False, **kw):
        super().__init__(message, **kw)
        self.ambiguous = ambiguous
