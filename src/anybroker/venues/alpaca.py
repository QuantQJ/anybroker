"""Alpaca equities and options, through the ``alpaca-py`` SDK.

ccxt has an ``alpaca`` entry, but it is Alpaca's **crypto** API. The stocks and
options side has no coverage anywhere, which is why this is the library's
third native venue.

Everything in this module was read from the installed SDK rather than assumed:
the order types, the times in force, the order classes, the request models,
the client's own ``paper`` flag and the error codes. If Alpaca changes a value
the test that asserts the whole set fails, rather than an order being
rejected.

Three things make Alpaca the best-behaved venue here:

**It enforces idempotency itself.** ``client_order_id`` is unique server side
-- a repeat comes back as error ``40010001``, "client_order_id must be
unique". This venue passes the key through, so the broker's own guarantee
backs up the one in ``Venue.place``, and a rejection is turned into
``DuplicateOrderError`` carrying the order Alpaca already has.

**Brackets are one request.** ``order_class`` plus ``take_profit`` and
``stop_loss`` on the entry. No parent/child staging and no OCA bookkeeping.

**Both trailing forms work** -- ``trail_price`` for an absolute distance and
``trail_percent`` for a proportional one.

Two things it cannot do, which are refused rather than substituted: a
trailing *stop-limit*, which is not in its order types, and good-til-date,
which is not in its times in force. Alpaca has ``opg`` and ``cls`` instead --
at the open and at the close -- which this library has no neutral word for;
reach them through ``extra``.
"""

from __future__ import annotations

from typing import Any

from ..enums import Env, OrderType, Side, TimeInForce
from ..errors import (
    INSTALL_HINT,
    AuthError,
    BrokerError,
    ConfigError,
    DuplicateOrderError,
    NotFoundError,
    RejectedError,
    ThrottledError,
    TransportError,
    UnsupportedError,
)
from ..models import Account, Order, Position
from ..venue import Capabilities, Venue

__all__ = ['AlpacaVenue', 'ALPACA_ORDER_TYPES', 'ALPACA_TIF', 'DUPLICATE_CLIENT_ID']

#: Alpaca's own ``OrderType``. Note the absence of a trailing stop-limit.
ALPACA_ORDER_TYPES = {
    OrderType.MARKET: 'market',
    OrderType.LIMIT: 'limit',
    OrderType.STOP: 'stop',
    OrderType.STOP_LIMIT: 'stop_limit',
    OrderType.TRAILING_STOP: 'trailing_stop',
}

#: Alpaca's own ``TimeInForce``. It has no GTD; it has ``opg`` and ``cls``,
#: which this library has no neutral word for.
ALPACA_TIF = {
    TimeInForce.DAY: 'day',
    TimeInForce.GTC: 'gtc',
    TimeInForce.IOC: 'ioc',
    TimeInForce.FOK: 'fok',
}

ALPACA_SIDE = {Side.BUY: 'buy', Side.SELL: 'sell'}

#: Alpaca's error code for a reused ``client_order_id``. Read from a real
#: APIError payload: {"code":40010001,"message":"client_order_id must be unique"}
DUPLICATE_CLIENT_ID = 40010001


class AlpacaVenue(Venue):
    """Alpaca stocks and options.

        venue = AlpacaVenue(Env.PAPER, api_key='...', secret_key='...')
        venue.limit('AAPL', Side.BUY, 10, 227.50)

    Paper and live are the SDK's own ``paper`` flag, which selects a different
    host. There is no default credential path: pass the keys, name a file, or
    set ``APCA_API_KEY_ID`` and ``APCA_API_SECRET_KEY``.
    """

    name = 'alpaca'
    capabilities = Capabilities(
        order_types=frozenset(ALPACA_ORDER_TYPES),
        # trail_price and trail_percent are both on Alpaca's trailing request.
        trailing_forms=frozenset({'amount', 'percent'}),
        supports_paper=True,
        supports_brackets=True,
        supports_modify=True,
        supports_reduce_only=False,
        supports_quotes=False,
        notes=(
            'no trailing stop-limit and no GTD; opg/cls reachable through '
            'extra. Quotes come from the separate market-data client. '
            'client_order_id is enforced by Alpaca itself.'
        ),
    )

    KEY_VAR = 'APCA_API_KEY_ID'
    SECRET_VAR = 'APCA_API_SECRET_KEY'

    def __init__(
        self,
        env,
        *,
        api_key=None,
        secret_key=None,
        credential_path=None,
        client=None,
        url_override=None,
    ) -> None:
        super().__init__(env)
        self._client = client
        self._api_key = api_key
        self._secret_key = secret_key
        self._credential_path = credential_path
        self._url_override = url_override

    # ── lifecycle ───────────────────────────────────────────────────────────

    def connect(self) -> Venue:
        """Build the client now, so bad keys fail here and not at the first order."""
        self._client = self.client
        return self

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from alpaca.trading.client import TradingClient
            except ImportError as exc:
                raise ConfigError(
                    'alpaca-py is not installed; ' + INSTALL_HINT % 'alpaca',
                    venue=self.name,
                ) from exc
            key, secret = self._resolve_keys()
            # `paper` is the SDK's own flag and it selects a different host, so
            # a venue built for paper has no route to the live account.
            self._client = TradingClient(
                api_key=key,
                secret_key=secret,
                paper=self.env is Env.PAPER,
                url_override=self._url_override,
            )
        return self._client

    def _resolve_keys(self) -> tuple:
        """Keys from the constructor, a named file, or the SDK's env vars.

        No default path: nothing opens a credential file the caller did not
        name.
        """
        if self._api_key and self._secret_key:
            return self._api_key, self._secret_key
        if self._credential_path:
            import json
            import pathlib

            path = pathlib.Path(self._credential_path).expanduser()
            if not path.exists():
                raise ConfigError('no credential file at %s' % path, venue=self.name)
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise ConfigError(
                    '%s is readable by others (mode %o); run: chmod 600 %s'
                    % (path, mode, path),
                    venue=self.name,
                )
            try:
                data = json.loads(path.read_text())
            except ValueError as exc:
                raise ConfigError(
                    '%s is not valid JSON: %s' % (path, exc), venue=self.name
                ) from exc
            key = data.get('ALPACA_API_KEY') or data.get(self.KEY_VAR)
            secret = data.get('ALPACA_API_SECRET') or data.get(self.SECRET_VAR)
            if not (key and secret):
                raise ConfigError(
                    '%s has no %s / %s' % (path, self.KEY_VAR, self.SECRET_VAR),
                    venue=self.name,
                )
            return key, secret
        import os

        key = os.environ.get(self.KEY_VAR) or os.environ.get('ALPACA_API_KEY')
        secret = os.environ.get(self.SECRET_VAR) or os.environ.get('ALPACA_API_SECRET')
        if key and secret:
            return key, secret
        raise ConfigError(
            'no Alpaca keys were given. Use one of:\n'
            '  AlpacaVenue(env, api_key=..., secret_key=...)\n'
            '  AlpacaVenue(env, credential_path="/your/key.json")\n'
            '  %s and %s in the environment' % (self.KEY_VAR, self.SECRET_VAR),
            venue=self.name,
        )

    # ── reads ───────────────────────────────────────────────────────────────

    def get_account(self) -> Account:
        acct = self._guard(self.client.get_account)
        return Account(
            id=str(getattr(acct, 'account_number', '') or getattr(acct, 'id', '')),
            venue=self.name,
            cash=_f(getattr(acct, 'cash', 0)),
            equity=_f(getattr(acct, 'equity', 0)),
            open_pnl=_f(getattr(acct, 'equity', 0))
            - _f(getattr(acct, 'last_equity', 0)),
            currency=str(getattr(acct, 'currency', 'USD') or 'USD'),
            raw={'account': acct},
        )

    def list_positions(self) -> list[Position]:
        out = []
        for p in self._guard(self.client.get_all_positions) or []:
            qty = _f(getattr(p, 'qty', 0))
            if str(getattr(p, 'side', '')).lower().endswith('short') and qty > 0:
                qty = -qty
            out.append(
                Position(
                    symbol=str(getattr(p, 'symbol', '')),
                    venue=self.name,
                    qty=qty,
                    avg_price=_f(getattr(p, 'avg_entry_price', 0)),
                    open_pnl=_f(getattr(p, 'unrealized_pl', 0)),
                    raw={'position': p},
                )
            )
        return out

    def list_orders(self) -> list[Order]:
        return [
            _order_of(o, self.name) for o in self._guard(self.client.get_orders) or []
        ]

    def get_order(self, order_id) -> Any:
        try:
            return _order_of(
                self._guard(self.client.get_order_by_id, order_id), self.name
            )
        except NotFoundError:
            return None

    def get_order_by_client_id(self, client_order_id) -> Any:
        """Alpaca can look an order up by the id you gave it.

        Which is what makes its server-side idempotency usable: after a
        duplicate rejection, this finds the order that already exists.
        """
        try:
            return _order_of(
                self._guard(self.client.get_order_by_client_id, str(client_order_id)),
                self.name,
            )
        except NotFoundError:
            return None

    # ── writes ──────────────────────────────────────────────────────────────

    def _place(self, spec) -> Order:
        spec.validate()
        self.capabilities.require_order_type(spec.order_type, self.name)
        self.capabilities.require_trailing_form(spec, self.name)
        if spec.time_in_force is TimeInForce.GTD:
            raise UnsupportedError(
                'Alpaca has no good-til-date. Its times in force are %s, plus '
                'opg and cls, which have no neutral word here -- pass '
                "extra={'time_in_force': 'opg'} if you want one."
                % ', '.join(sorted(ALPACA_TIF.values())),
                venue=self.name,
            )
        if spec.reduce_only:
            raise UnsupportedError(
                'Alpaca has no reduce-only flag. It has position_intent '
                '(buy_to_close / sell_to_close) -- pass it through extra= -- '
                'or use flatten(), which lets Alpaca size the close itself.',
                venue=self.name,
            )
        request = self._build_request(spec)
        try:
            placed = self.client.submit_order(order_data=request)
        except Exception as exc:
            raise self._submit_error(exc, spec) from exc
        return _order_of(placed, self.name)

    def _build_request(self, spec) -> Any:
        """Turn an OrderSpec into the SDK's own request object.

        Using the SDK's models rather than a dict means Alpaca's own
        validation runs before anything leaves the machine.
        """
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLimitOrderRequest,
            StopLossRequest,
            StopOrderRequest,
            TakeProfitRequest,
            TrailingStopOrderRequest,
        )

        common: dict[str, Any] = {
            'symbol': spec.symbol,
            'qty': float(spec.qty),
            'side': ALPACA_SIDE[spec.side],
            'time_in_force': ALPACA_TIF[spec.time_in_force],
        }
        if spec.client_order_id:
            # Passed through on purpose: Alpaca enforces uniqueness on it, so
            # the broker's own guarantee backs up Venue.place().
            common['client_order_id'] = str(spec.client_order_id)

        if spec.take_profit is not None:
            common['take_profit'] = TakeProfitRequest(
                limit_price=float(spec.take_profit)
            )
        if spec.stop_loss is not None:
            common['stop_loss'] = StopLossRequest(stop_price=float(spec.stop_loss))
        if spec.is_bracket:
            # Two children is a bracket; one is a one-triggers-other. Alpaca is
            # the authority -- a wrong pairing comes back as RejectedError.
            both = spec.take_profit is not None and spec.stop_loss is not None
            common['order_class'] = OrderClass.BRACKET if both else OrderClass.OTO

        common.update(spec.extra or {})

        kind = spec.order_type
        if kind is OrderType.MARKET:
            return MarketOrderRequest(**common)
        if kind is OrderType.LIMIT:
            return LimitOrderRequest(limit_price=float(spec.limit_price), **common)
        if kind is OrderType.STOP:
            return StopOrderRequest(stop_price=float(spec.stop_price), **common)
        if kind is OrderType.STOP_LIMIT:
            return StopLimitOrderRequest(
                limit_price=float(spec.limit_price),
                stop_price=float(spec.stop_price),
                **common,
            )
        if kind is OrderType.TRAILING_STOP:
            if spec.trail_amount is not None:
                return TrailingStopOrderRequest(
                    trail_price=float(spec.trail_amount), **common
                )
            return TrailingStopOrderRequest(
                trail_percent=float(spec.trail_percent), **common
            )
        raise UnsupportedError(
            '%s is not an Alpaca order type' % kind.value, venue=self.name
        )

    def cancel(self, order_id) -> Any:
        self._guard(self.client.cancel_order_by_id, order_id)
        return Order(id=str(order_id), venue=self.name, status='pending_cancel')

    def cancel_all(self) -> list:
        """Alpaca cancels every open order in one call."""
        responses = self._guard(self.client.cancel_orders) or []
        return [str(getattr(r, 'id', '') or '') for r in responses]

    def modify(self, order_id, **changes) -> Any:
        # Arguments are checked before the SDK is imported, so a bad field name
        # tells you what is wrong even where alpaca-py is not installed.
        mapping = {
            'qty': 'qty',
            'limit_price': 'limit_price',
            'stop_price': 'stop_price',
            'trail_amount': 'trail',
            'trail_percent': 'trail',
            'client_order_id': 'client_order_id',
        }
        kw: dict[str, Any] = {}
        for key, value in changes.items():
            if key == 'time_in_force':
                kw['time_in_force'] = ALPACA_TIF[TimeInForce(value)]
                continue
            if key not in mapping:
                raise ConfigError(
                    'an Alpaca order cannot have %r replaced; it takes %s'
                    % (key, ', '.join(sorted({*mapping, 'time_in_force'}))),
                    venue=self.name,
                )
            kw[mapping[key]] = value
        from alpaca.trading.requests import ReplaceOrderRequest

        replaced = self._guard(
            self.client.replace_order_by_id, order_id, ReplaceOrderRequest(**kw)
        )
        return _order_of(replaced, self.name)

    def flatten(self, symbol) -> Any:
        """Close one symbol, letting Alpaca size it from the open position."""
        position = self.get_position(symbol)
        if position is None or position.is_flat:
            raise NotFoundError('no open position in %s' % symbol, venue=self.name)
        closed = self._guard(self.client.close_position, symbol)
        return (
            _order_of(closed, self.name)
            if closed is not None
            else Order(id='', venue=self.name, symbol=symbol, status='accepted')
        )

    def flatten_all(self, *, cancel_first: bool = True) -> dict:
        """Alpaca closes everything and optionally cancels in one call."""
        responses = self._guard(self.client.close_all_positions, cancel_first) or []
        return {
            'cancelled': ['all'] if cancel_first else [],
            'flattened': [str(getattr(r, 'symbol', '') or '') for r in responses],
        }

    # ── errors ──────────────────────────────────────────────────────────────

    def _submit_error(self, exc, spec) -> BrokerError:
        """Classify a failed submission, honouring Alpaca's own duplicate check.

        Alpaca rejects a reused ``client_order_id`` with code 40010001. That is
        the broker telling us what ``Venue.place`` would have: the order
        already exists. It is turned into ``DuplicateOrderError`` carrying the
        order Alpaca already has, so a caller sees the same thing either way.
        """
        if getattr(exc, 'code', None) == DUPLICATE_CLIENT_ID:
            existing = None
            if spec.client_order_id:
                try:
                    existing = self.get_order_by_client_id(spec.client_order_id)
                except BrokerError:
                    existing = None
            return DuplicateOrderError(
                'Alpaca already has an order with client_order_id %r, and '
                'enforces that itself. Give the new one its own id.'
                % spec.client_order_id,
                key=spec.idempotency_key,
                order=existing,
                venue=self.name,
            )
        return self._translate(exc)

    def _guard(self, fn, *args, **kw) -> Any:
        try:
            return fn(*args, **kw)
        except Exception as exc:
            raise self._translate(exc) from exc

    def _translate(self, exc) -> BrokerError:
        status = getattr(exc, 'status_code', None)
        detail = getattr(exc, 'message', None) or str(exc)
        if status in (401, 403):
            return AuthError(
                'Alpaca rejected the credentials', venue=self.name, detail=detail
            )
        if status == 404:
            return NotFoundError(detail, venue=self.name)
        if status == 429:
            return ThrottledError(
                'Alpaca rate limited the request', venue=self.name, detail=detail
            )
        if status is not None and 400 <= status < 500:
            return RejectedError(
                'Alpaca refused the request: %s' % detail, venue=self.name
            )
        if status is not None and status >= 500:
            return TransportError(
                'Alpaca server error (%s): %s -- if this was a write, the '
                'order may or may not have been accepted. Read order state '
                'before sending anything else.' % (status, detail),
                venue=self.name,
                ambiguous=True,
            )
        if isinstance(exc, (TimeoutError, ConnectionError, OSError, EOFError)):
            return TransportError(
                'Alpaca request failed: %s -- this may or may not have been '
                'accepted. Read order state before sending anything else.' % detail,
                venue=self.name,
                ambiguous=True,
            )
        if type(exc).__name__ == 'APIError':
            return RejectedError(
                'Alpaca refused the request: %s' % detail, venue=self.name
            )
        return TransportError('%s: %s' % (type(exc).__name__, detail), venue=self.name)


def _order_of(raw, venue) -> Order:
    if raw is None:
        return Order(id='', venue=venue)
    side = str(getattr(raw, 'side', '') or '').lower()
    kind = str(getattr(raw, 'order_type', '') or getattr(raw, 'type', '') or '').lower()
    reverse = {v: k for k, v in ALPACA_ORDER_TYPES.items()}
    order = Order(
        id=str(getattr(raw, 'id', '') or ''),
        venue=venue,
        symbol=str(getattr(raw, 'symbol', '') or ''),
        side=Side(side) if side in ('buy', 'sell') else None,
        qty=_f(getattr(raw, 'qty', 0)),
        order_type=reverse.get(kind),
        status=str(getattr(raw, 'status', '') or ''),
        filled_qty=_f(getattr(raw, 'filled_qty', 0)),
        avg_fill_price=_f(getattr(raw, 'filled_avg_price', 0)),
        limit_price=_opt(getattr(raw, 'limit_price', None)),
        stop_price=_opt(getattr(raw, 'stop_price', None)),
        client_order_id=getattr(raw, 'client_order_id', None) or None,
        raw={'order': raw},
    )
    legs = getattr(raw, 'legs', None) or []
    order.children = [_order_of(leg, venue) for leg in legs]
    return order


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
