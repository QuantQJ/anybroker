"""Stable error codes.

Every exception carries one. A code never changes once published, so code that
checks ``exc.code`` keeps working, and anyone who does not read English can
map these to their own language once instead of parsing a sentence.

    try:
        venue.place(spec)
    except BrokerError as exc:
        if exc.code == codes.DUPLICATE_ORDER:
            ...

The message after the code is English and may be reworded. The code is the
contract.
"""

from __future__ import annotations

__all__ = ['ALL', 'describe']

# ── configuration: the caller has to supply or fix something ───────────────
CONFIG = 'CONFIG'
NO_CREDENTIALS = 'NO_CREDENTIALS'
CREDENTIALS_FILE_MISSING = 'CREDENTIALS_FILE_MISSING'
CREDENTIALS_FILE_INSECURE = 'CREDENTIALS_FILE_INSECURE'
CREDENTIALS_INCOMPLETE = 'CREDENTIALS_INCOMPLETE'
CLIENT_ID_REQUIRED = 'CLIENT_ID_REQUIRED'
PORT_REQUIRED = 'PORT_REQUIRED'
ACCOUNT_AMBIGUOUS = 'ACCOUNT_AMBIGUOUS'
SYMBOL_AMBIGUOUS = 'SYMBOL_AMBIGUOUS'
DRIVER_MISSING = 'DRIVER_MISSING'
VENUE_UNKNOWN = 'VENUE_UNKNOWN'
FIELD_UNKNOWN = 'FIELD_UNKNOWN'
SPEC_INVALID = 'SPEC_INVALID'
NOT_CONNECTED = 'NOT_CONNECTED'

# ── the venue cannot do what was asked ─────────────────────────────────────
UNSUPPORTED = 'UNSUPPORTED'
PAPER_UNAVAILABLE = 'PAPER_UNAVAILABLE'
ORDER_TYPE_UNSUPPORTED = 'ORDER_TYPE_UNSUPPORTED'
TRAILING_FORM_UNSUPPORTED = 'TRAILING_FORM_UNSUPPORTED'
BRACKET_UNSUPPORTED = 'BRACKET_UNSUPPORTED'
REDUCE_ONLY_UNSUPPORTED = 'REDUCE_ONLY_UNSUPPORTED'
TIME_IN_FORCE_UNSUPPORTED = 'TIME_IN_FORCE_UNSUPPORTED'
MODIFY_UNSUPPORTED = 'MODIFY_UNSUPPORTED'
POSITIONS_UNSUPPORTED = 'POSITIONS_UNSUPPORTED'

# ── not found ──────────────────────────────────────────────────────────────
NOT_FOUND = 'NOT_FOUND'
NO_ACCOUNT = 'NO_ACCOUNT'
NO_POSITION = 'NO_POSITION'
NO_ORDER = 'NO_ORDER'
NO_CONTRACT = 'NO_CONTRACT'
NO_SUBMISSION = 'NO_SUBMISSION'

# ── the order was not accepted ─────────────────────────────────────────────
REJECTED = 'REJECTED'
AUTH_REJECTED = 'AUTH_REJECTED'
THROTTLED = 'THROTTLED'
DUPLICATE_ORDER = 'DUPLICATE_ORDER'

# ── the state is unknown, which is the dangerous one ───────────────────────
UNRESOLVED_ORDER = 'UNRESOLVED_ORDER'
TRANSPORT = 'TRANSPORT'
TRANSPORT_AMBIGUOUS = 'TRANSPORT_AMBIGUOUS'
DISCONNECTED = 'DISCONNECTED'
WRONG_DESTINATION = 'WRONG_DESTINATION'

#: One short English line per code, for anyone building a translation table.
DESCRIPTIONS = {
    CONFIG: 'a setting is missing or wrong; nothing was sent',
    NO_CREDENTIALS: 'no credentials were supplied',
    CREDENTIALS_FILE_MISSING: 'the named credential file does not exist',
    CREDENTIALS_FILE_INSECURE: 'the credential file is readable by other users',
    CREDENTIALS_INCOMPLETE: 'the credentials are missing a required field',
    CLIENT_ID_REQUIRED: 'a client id must be chosen; reusing one disconnects that session',
    PORT_REQUIRED: 'the port must be given; installations disagree on which is live',
    ACCOUNT_AMBIGUOUS: 'the login has several accounts; say which',
    SYMBOL_AMBIGUOUS: 'the symbol matches more than one instrument',
    DRIVER_MISSING: 'the venue driver is not installed',
    VENUE_UNKNOWN: 'no such venue',
    FIELD_UNKNOWN: 'that field cannot be set or replaced on this venue',
    SPEC_INVALID: 'the order description is incomplete or contradictory',
    NOT_CONNECTED: 'connect() has not been called',
    UNSUPPORTED: 'the venue cannot do this',
    PAPER_UNAVAILABLE: 'the venue has no paper environment',
    ORDER_TYPE_UNSUPPORTED: 'the venue does not offer that order type',
    TRAILING_FORM_UNSUPPORTED: 'the venue does not take the trail in that form',
    BRACKET_UNSUPPORTED: 'the venue cannot attach a target or stop to an entry',
    REDUCE_ONLY_UNSUPPORTED: 'the venue has no reduce-only flag',
    TIME_IN_FORCE_UNSUPPORTED: 'the venue does not offer that time in force',
    MODIFY_UNSUPPORTED: 'the venue cannot modify an order; cancel and replace',
    POSITIONS_UNSUPPORTED: 'the venue does not report positions',
    NOT_FOUND: 'no such object',
    NO_ACCOUNT: 'no account was returned',
    NO_POSITION: 'there is no open position in that instrument',
    NO_ORDER: 'no such order',
    NO_CONTRACT: 'no such contract',
    NO_SUBMISSION: 'no submission with that key',
    REJECTED: 'the venue received the request and refused it',
    AUTH_REJECTED: 'the credentials were rejected',
    THROTTLED: 'rate limited',
    DUPLICATE_ORDER: 'this order was already sent',
    UNRESOLVED_ORDER: 'a previous send of this order was never answered; read the state',
    TRANSPORT: 'the request did not complete; nothing was sent',
    TRANSPORT_AMBIGUOUS: 'the request may or may not have been accepted; read the state',
    DISCONNECTED: 'the connection is down; nothing was sent',
    WRONG_DESTINATION: 'connected, but to the wrong kind of account',
}

#: Every published code.
ALL = frozenset(DESCRIPTIONS)


def describe(code) -> str:
    """One line for a code, or the code itself if it is not a published one."""
    return DESCRIPTIONS.get(str(code), str(code))
