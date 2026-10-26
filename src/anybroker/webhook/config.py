"""Configuration, with no unsafe default anywhere.

Two things this refuses to do: start without a shared secret, and reach a
live account without being told to in as many words.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import urllib.parse

from .alert import TRADINGVIEW_IPS
from ..errors import ConfigError as _BrokerConfigError

__all__ = ['Config', 'ConfigError', 'new_secret', 'LOOPBACK', 'DEFAULT_ALLOW_IPS']

#: The machine itself. Deliberately *not* in the default allowlist: a reverse
#: proxy on the same box connects from loopback, so allowing it would accept
#: whatever that proxy forwards, from anywhere, and the sender check would
#: silently stop meaning anything. Name it in ``allow_ips`` yourself if you
#: want it, or set ``trusted_proxies`` and let the real client be checked.
#: ``/healthz`` answers loopback regardless -- it reveals only that the port
#: is up, and takes no order.
LOOPBACK = ('127.0.0.1', '::1')

#: Who may POST, unless you say otherwise.
DEFAULT_ALLOW_IPS = TRADINGVIEW_IPS

#: A path token shorter than this is not worth having.
MIN_SECRET_LENGTH = 24


def _looks_like_path(text) -> bool:
    """Whether a value is plausibly a filesystem path rather than a secret.

    Strict on purpose. A leading slash alone is not enough: ``/aGVsbG8=`` is
    a base64 secret with a slash in front of it, and treating that as a path
    would publish it. So a directory component is required, and the base64
    and URL-safe-base64 markers disqualify it outright.
    """
    if not text or any(ch.isspace() for ch in text):
        return False
    if text.startswith(('http://', 'https://')):
        return False
    if any(ch in text for ch in '+=%'):
        return False
    if text.startswith(('~/', './', '../')):
        return True
    # Two separators means there is a directory, not just a prefixed token.
    return text.startswith(os.sep) and text.count(os.sep) >= 2


def _url_secrets(text) -> list:
    """Credentials hiding inside an otherwise public value.

    A base URL is safe to print until someone puts a password in its
    userinfo or an API key in its query string.
    """
    out: list = []
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return out
    if not parsed.scheme:
        return out
    if parsed.password:
        out.append(parsed.password)
    # Plenty of services put the key in the username half of the userinfo.
    if parsed.username and len(parsed.username) >= 4:
        out.append(parsed.username)
    if parsed.query:
        for _, values in urllib.parse.parse_qs(parsed.query).items():
            out.extend(v for v in values if len(v) >= 4)
    return out


class ConfigError(_BrokerConfigError):
    """The configuration is unsafe or incomplete. Nothing was started.

    A subclass of the package's own ``ConfigError``, so a caller who catches
    that catches a bad bridge configuration too.
    """


def new_secret() -> str:
    """A fresh URL-safe token, for the setup instructions to print."""
    return secrets.token_urlsafe(32)


class Config:
    """How the bridge is set up.

    ``env`` is the only way to reach a live account, and it has no default --
    the same rule anybroker follows, for the same reason.
    """

    def __init__(
        self,
        *,
        secret,
        venue,
        env,
        account=None,
        host='127.0.0.1',
        port=8080,
        path='/alert',
        default_qty=None,
        allow_ips=DEFAULT_ALLOW_IPS,
        allow_any_ip=False,
        trusted_proxies=(),
        forwarded_header='X-Forwarded-For',
        dry_run=False,
        journal=None,
        venue_options=None,
        request_budget=2.5,
    ) -> None:
        if not secret or len(str(secret)) < MIN_SECRET_LENGTH:
            raise ConfigError(
                'a shared secret of at least %d characters is required. '
                'TradingView cannot send custom headers, so the secret '
                'travels in the URL path or the payload and is the only thing '
                'between the open internet and your broker. Generate one with '
                '`python -m anybroker.webhook secret`.' % MIN_SECRET_LENGTH
            )
        if not venue:
            raise ConfigError('venue is required, e.g. "tradovate" or "ibkr"')
        if env not in ('paper', 'live'):
            raise ConfigError(
                'env must be "paper" or "live", spelled out. There is no '
                'default: a bridge that guesses this one routes real orders.'
            )
        self.secret = str(secret)
        self.venue = str(venue)
        self.env = env
        self.account = account
        self.host = host
        self.port = int(port)
        self.path = path if path.startswith('/') else '/' + path
        self.default_qty = default_qty
        self.allow_ips = tuple(allow_ips or ())
        self.allow_any_ip = bool(allow_any_ip)
        self.trusted_proxies = tuple(trusted_proxies or ())
        # One header, named, rather than a guess between two. Reading
        # whichever of several happens to be present lets a client pick the
        # one its proxy does not set.
        self.forwarded_header = str(forwarded_header or 'X-Forwarded-For')
        self.dry_run = bool(dry_run)
        self.journal = pathlib.Path(journal).expanduser() if journal else None
        self.venue_options = dict(venue_options or {})
        self.request_budget = float(request_budget)
        if self.allow_any_ip and self.env == 'live':
            raise ConfigError(
                'allow_any_ip with env="live" is refused. A live order '
                'endpoint open to every address is one leaked URL from being '
                'someone else\'s trading bot. Allowlist TradingView\'s '
                'addresses, or put this behind a proxy that does.'
            )

    #: Words that, appearing anywhere in an option name, make it a
    #: credential whatever else the name says. Checked first, so a key like
    #: ``secret_env`` or ``token_file`` cannot be read as public because it
    #: happens to end in a public word.
    ALWAYS_SENSITIVE = (
        'secret',
        'sec',
        'key',
        'pass',
        'pwd',
        'token',
        'auth',
        'sig',
        'cookie',
        'cred',
        'jwt',
        'bearer',
        'session',
        'nonce',
        'private',
    )

    #: Option names whose values are not secret. Everything else handed to a
    #: venue is treated as a credential.
    #:
    #: The other way round -- guessing which names look secret -- misses
    #: whatever the next venue decides to call its key. ``pwd``, ``auth``,
    #: ``jwt``, ``bearer``, ``signature`` and ``cookie`` all slip a
    #: substring test, and each of them is a credential.
    #:
    #: Matched as whole words, not substrings: ``port`` must not make
    #: ``passport`` public, nor ``url`` make ``api_key_url`` public.
    PUBLIC_OPTION_HINTS = (
        'path',
        'dir',
        'file',
        'account',
        'env',
        'host',
        'port',
        'url',
        'timeout',
        'symbol',
        'venue',
        'exchange',
        'market',
        'sandbox',
        'paper',
        'currency',
        'client_id',
        'clientid',
    )

    def sensitive_values(self) -> tuple:
        """Everything that must not appear in a journal or a reply.

        The webhook secret, and every value handed to the venue that is not
        obviously public. A broker exception has seen those values, and its
        text reaches both the journal and the status endpoint.

        Paths are public on purpose: ``credential_path`` names a file rather
        than holding one, and blanking it would hide the useful half of a
        "no credential file at ..." message.
        """
        found = [self.secret]

        def walk(value, name=''):
            if isinstance(value, dict):
                for key, inner in value.items():
                    walk(inner, str(key))
                return
            if isinstance(value, (list, tuple, set)):
                for inner in value:
                    walk(inner, name)
                return
            if isinstance(value, bool) or value is None:
                return
            lowered = name.lower()
            text = str(value)
            # A path-named option is public only if the value really is a
            # path. ``credential_path`` names a file and may be printed --
            # blanking it hides the useful half of "no file at ...". But
            # ``token_file`` holding "abc123" is holding a token, whatever
            # it is called.
            if self._names_a_path(lowered) and _looks_like_path(text):
                return
            if not self._is_public_name(lowered):
                if len(text) >= 4:
                    found.append(text)
                return
            # A public option can still carry a credential inside it: a URL
            # with userinfo or a query string is the usual way.
            found.extend(_url_secrets(text))
            return

        walk(self.venue_options)
        return tuple(dict.fromkeys(v for v in found if v))

    #: Name words that promise a filesystem location rather than a value.
    PATH_WORDS = ('path', 'dir', 'file', 'filename', 'folder')

    @classmethod
    def _names_a_path(cls, lowered) -> bool:
        words = [w for w in re.split(r'[^a-z0-9]+', lowered) if w]
        return any(w in cls.PATH_WORDS for w in words)

    @classmethod
    def _is_public_name(cls, lowered) -> bool:
        """Whether an option name may be printed, by whole-word match.

        A name carrying any of ``ALWAYS_SENSITIVE`` is never public, however
        it ends -- ``secret_env`` is not an env, and ``token_file`` is not a
        file as far as this is concerned.
        """
        if any(word in lowered for word in cls.ALWAYS_SENSITIVE):
            return False
        words = [w for w in re.split(r'[^a-z0-9]+', lowered) if w]
        return any(w in cls.PUBLIC_OPTION_HINTS for w in words)

    @property
    def is_live(self) -> bool:
        return self.env == 'live'

    @property
    def url_path(self) -> str:
        """The path TradingView should be given, secret included.

        The secret goes in the path because TradingView sends no headers. It
        may also be sent in the payload, which keeps it out of access logs.
        """
        return '%s/%s' % (self.path.rstrip('/'), self.secret)

    @classmethod
    def from_file(cls, path) -> Config:
        path = pathlib.Path(path).expanduser()
        if not path.exists():
            raise ConfigError('no config file at %s' % path)
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ConfigError(
                '%s is readable by others (mode %o); it holds the secret. '
                'Run: chmod 600 %s' % (path, mode, path)
            )
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            raise ConfigError('%s is not valid JSON: %s' % (path, exc)) from exc
        return cls(**data)

    @classmethod
    def from_env(cls, prefix='ANYHOOK_') -> Config:
        get = lambda name: os.environ.get(prefix + name)  # noqa: E731
        size = get('DEFAULT_QTY')
        return cls(
            secret=get('SECRET'),
            venue=get('VENUE'),
            env=get('ENV'),
            account=get('ACCOUNT'),
            host=get('HOST') or '127.0.0.1',
            port=get('PORT') or 8080,
            path=get('PATH') or '/alert',
            default_qty=float(size) if size else None,
            allow_any_ip=(get('ALLOW_ANY_IP') or '').lower() in ('1', 'true', 'yes'),
            dry_run=(get('DRY_RUN') or '').lower() in ('1', 'true', 'yes'),
            journal=get('JOURNAL'),
            trusted_proxies=tuple(
                p.strip()
                for p in (get('TRUSTED_PROXIES') or '').split(',')
                if p.strip()
            ),
            forwarded_header=get('FORWARDED_HEADER') or 'X-Forwarded-For',
        )

    def describe(self) -> str:
        """A startup banner that never prints the secret."""
        return '\n'.join(
            [
                'anyhook',
                '  venue        : %s (%s)%s'
                % (self.venue, self.env, '  DRY RUN' if self.dry_run else ''),
                '  account      : %s' % (self.account or 'resolved at connect'),
                '  listening    : http://%s:%d%s/<secret>'
                % (self.host, self.port, self.path.rstrip('/')),
                '  senders      : %s'
                % ('ANY ADDRESS' if self.allow_any_ip else ', '.join(self.allow_ips)),
                '  proxies      : %s'
                % (
                    '%s via %s'
                    % (', '.join(self.trusted_proxies), self.forwarded_header)
                    if self.trusted_proxies
                    else 'none; the peer is the client'
                ),
                '  default size : %s'
                % (self.default_qty if self.default_qty else 'required in the alert'),
                '  journal      : %s' % (self.journal or 'memory only'),
            ]
        )
