"""Tradovate, through the ``tradovate-client`` library.

ccxt covers 104 exchanges and Tradovate is not one of them, so this is the
library's own venue, the same as IBKR.

It drives ``tradovate-client`` with plain strings -- ``demo``/``live``,
``Buy``/``Sell``, ``Market``, ``Day`` -- every one of which is a value from
Tradovate's own published enums. That keeps this module free of the other
library's types and lets the whole mapping be asserted against a fake client.

Two Tradovate facts worth knowing, both inherited from the client library:

**Paper is a different host.** ``demo.tradovateapi.com`` and
``live.tradovateapi.com`` take different credentials and issue different
tokens, so a venue built for paper has no route to the live book -- not
because a flag says so, but because it is a different server.

**Full authentication is throttled.** The client library renews a cached token
rather than re-authenticating, which is what keeps an unattended bot from
being locked out. Nothing here defeats that.
"""

from __future__ import annotations

from typing import Any

from ..enums import Env, OrderType, Side, TimeInForce
from ..errors import (
    INSTALL_HINT,
    BrokerError,
    ConfigError,
    NotFoundError,
    RejectedError,
    TransportError,
    UnsupportedError,
)
from ..models import Account, Order, Position
from ..venue import Capabilities, Venue

__all__ = ['TradovateVenue', 'TV_ENV', 'TV_SIDE', 'TV_ORDER_TYPES', 'TV_TIF']

#: Tradovate's two environments. Different hosts, different credentials.
TV_ENV = {Env.PAPER: 'demo', Env.LIVE: 'live'}

#: Values from Tradovate's published model docs (``Action``: Buy, Sell).
TV_SIDE = {Side.BUY: 'Buy', Side.SELL: 'Sell'}

#: Values from Tradovate's published ``OrderType``. Tradovate also has MIT and
#: QTS, which this library's neutral vocabulary has no word for yet; reach them
#: through ``extra``.
TV_ORDER_TYPES = {
    OrderType.MARKET: 'Market',
    OrderType.LIMIT: 'Limit',
    OrderType.STOP: 'Stop',
    OrderType.STOP_LIMIT: 'StopLimit',
    OrderType.TRAILING_STOP: 'TrailingStop',
    OrderType.TRAILING_STOP_LIMIT: 'TrailingStopLimit',
}

#: Values from Tradovate's published ``TimeInForce``.
TV_TIF = {
    TimeInForce.DAY: 'Day',
    TimeInForce.GTC: 'GTC',
    TimeInForce.IOC: 'IOC',
    TimeInForce.FOK: 'FOK',
    TimeInForce.GTD: 'GTD',
}


class TradovateVenue(Venue):
    """Tradovate futures.

        venue = TradovateVenue(Env.PAPER, credential_path='/secure/key.json')
        venue.limit('ABCZ6', Side.BUY, 2, 20150.25)

    Credentials are the client library's business: pass ``credential_path=``,
    ``credentials=``, or set ``TRADOVATE_CREDENTIALS``. There is no default
    path -- neither library opens a file you did not name.
    """

    name = 'tradovate'
    capabilities = Capabilities(
        order_types=frozenset(TV_ORDER_TYPES),
        # Only an absolute distance is wired. Tradovate's schema has
        # pegDifference, but what it means for a trailing order is not stated
        # in their published docs, so a percent trail is refused rather than
        # mapped onto a field whose behaviour was not verified.
        trailing_forms=frozenset({'amount'}),
        supports_paper=True,
        supports_brackets=True,
        supports_modify=True,
        supports_reduce_only=False,
        supports_quotes=False,
        notes='paper is a separate host with separate credentials; '
        'percent trails are not wired, only an absolute trail distance',
    )

    def __init__(
        self,
        env,
        *,
        client=None,
        account=None,
        credentials=None,
        credential_path=None,
        token_dir=None,
        timeout=20.0,
    ) -> None:
        super().__init__(env)
        self.account = account
        self._client = client
        self._client_kw = {
            'credentials': credentials,
            'credential_path': credential_path,
            'token_dir': token_dir,
            'timeout': timeout,
            'account': account,
        }

    # ── lifecycle ───────────────────────────────────────────────────────────
    def connect(self) -> Venue:
        """Build the client now, so a bad config fails here, not at the first order."""
        self._client = self.client
        return self

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from tradovate_client import TradovateClient
            except ImportError as exc:
                raise ConfigError(
                    'tradovate-client is not installed; '
                    + INSTALL_HINT % 'tradovate'
                    + ' (or pip install "git+https://github.com/QuantQJ/'
                    'tradovate-client.git")',
                    venue=self.name,
                ) from exc
            kw = {k: v for k, v in self._client_kw.items() if v is not None}
            self._client = TradovateClient(TV_ENV[self.env], **kw)
        return self._client

    # ── reads ───────────────────────────────────────────────────────────────
    def get_account(self) -> Account:
        snap = self._guard(self.client.snapshot)
        return Account(
            id=str(snap.account.name),
            venue=self.name,
            cash=_f(snap.cash.total_cash),
            equity=_f(snap.cash.total_cash),
            open_pnl=_f(snap.cash.open_pnl),
            realized_pnl=_f(snap.cash.realized_pnl),
            raw={'account': snap.account, 'cash': snap.cash},
        )

    def list_positions(self) -> list[Position]:
        """Positions with their contract names resolved.

        Uses the client's ``snapshot()``, which names only the contracts
        actually held rather than pulling Tradovate's whole contract list on
        every poll.
        """
        snap = self._guard(self.client.snapshot)
        return [
            Position(
                symbol=symbol,
                venue=self.name,
                qty=float(view.net_pos),
                avg_price=_f(view.net_price),
                raw={'contract_id': view.contract_id},
            )
            for symbol, view in snap.positions.items()
        ]

    def list_orders(self) -> list[Order]:
        return [_order_of(o, self.name) for o in self._guard(self.client.orders) or []]

    def get_order(self, order_id) -> Any:
        return _order_of(self._guard(self.client.order, order_id), self.name)

    def working_orders(self) -> list[Order]:
        return [
            _order_of(o, self.name)
            for o in self._guard(self.client.working_orders) or []
        ]

    # ── writes ──────────────────────────────────────────────────────────────
    def _place(self, spec) -> Order:
        spec.validate()
        self.capabilities.require_order_type(spec.order_type, self.name)
        if spec.reduce_only:
            raise UnsupportedError(
                'Tradovate has no reduce-only flag; size the closing order '
                'yourself, or use flatten()',
                venue=self.name,
            )
        self.capabilities.require_trailing_form(spec, self.name)
        if spec.is_bracket:
            return self._place_bracket(spec)
        reply = self._guard(self.client.place, **self._order_kw(spec))
        return _order_of(reply, self.name, spec)

    def _place_bracket(self, spec) -> Order:
        """Entry plus children, through Tradovate's own one-sends-other.

        Tradovate takes the children as part of the entry, so the group is one
        request and there is no window where a naked entry is working.
        """
        self.capabilities.require_brackets(self.name)
        legs = []
        for child in spec.child_specs():
            legs.append(
                self.client.bracket_leg(
                    TV_SIDE[child.side],
                    TV_ORDER_TYPES[child.order_type],
                    price=child.limit_price,
                    stop_price=child.stop_price,
                    time_in_force=TV_TIF[child.time_in_force],
                )
            )
        kw = self._order_kw(spec)
        kw['bracket1'] = legs[0]
        if len(legs) > 1:
            kw['bracket2'] = legs[1]
        reply = self._guard(self.client.place_oso, **kw)
        order = _order_of(reply, self.name, spec)
        order.children = [
            Order(
                id='',
                venue=self.name,
                symbol=spec.symbol,
                side=c.side,
                qty=c.qty,
                order_type=c.order_type,
                limit_price=c.limit_price,
                stop_price=c.stop_price,
                status='staged',
                raw={'leg': leg},
            )
            # strict: legs were built from these specs, so a length mismatch
            # would mean a bracket leg went missing. Fail loudly, not quietly.
            for c, leg in zip(spec.child_specs(), legs, strict=True)
        ]
        return order

    def _order_kw(self, spec) -> dict:
        kw = {
            'action': TV_SIDE[spec.side],
            'symbol': spec.symbol,
            'qty': int(spec.qty) if float(spec.qty).is_integer() else spec.qty,
            'order_type': TV_ORDER_TYPES[spec.order_type],
            'time_in_force': TV_TIF[spec.time_in_force],
        }
        if spec.limit_price is not None:
            kw['price'] = float(spec.limit_price)
        if spec.stop_price is not None:
            kw['stop_price'] = float(spec.stop_price)
        if spec.trail_amount is not None:
            kw['stop_price'] = float(spec.trail_amount)
        if spec.good_til:
            kw['expire_time'] = spec.good_til
        if spec.client_order_id:
            kw['client_order_id'] = str(spec.client_order_id)
        if spec.label and not spec.client_order_id:
            kw['text'] = str(spec.label)
        if self.account:
            kw['account'] = self.account
        kw.update(spec.extra or {})
        return kw

    def cancel(self, order_id) -> Any:
        reply = self._guard(self.client.cancel, order_id)
        return _order_of(reply or {'orderId': order_id}, self.name)

    def cancel_all(self) -> list:
        return list(self._guard(self.client.cancel_working) or [])

    def modify(self, order_id, **changes) -> Any:
        mapping = {
            'qty': 'qty',
            'limit_price': 'price',
            'stop_price': 'stop_price',
            'order_type': 'order_type',
            'time_in_force': 'time_in_force',
        }
        kw = {}
        for key, value in changes.items():
            if key not in mapping:
                raise ConfigError('Tradovate orders have no %r' % key, venue=self.name)
            if key == 'order_type':
                value = TV_ORDER_TYPES[OrderType(value)]
            if key == 'time_in_force':
                value = TV_TIF[TimeInForce(value)]
            kw[mapping[key]] = value
        reply = self._guard(self.client.modify, order_id, **kw)
        return _order_of(reply or {'orderId': order_id}, self.name)

    def flatten(self, symbol) -> Any:
        """Close one contract with Tradovate's own liquidate endpoint."""
        position = self.get_position(symbol)
        if position is None or position.is_flat:
            raise NotFoundError('no open position in %s' % symbol, venue=self.name)
        contract_id = (position.raw or {}).get('contract_id')
        if contract_id is None:
            raise NotFoundError('no contract id for %s' % symbol, venue=self.name)
        reply = self._guard(self.client.liquidate, contract_id)
        return _order_of(reply or {}, self.name)

    def flatten_all(self, *, cancel_first=True) -> dict:
        # account= is passed through, so flattening one account does not
        # cancel another account's working orders on the same login.
        result = self._guard(
            self.client.flatten_all, account=self.account, cancel_first=cancel_first
        )
        if isinstance(result, dict):
            return {
                'cancelled': list(result.get('cancelled') or []),
                'flattened': list(result.get('liquidated') or []),
            }
        return super().flatten_all(cancel_first=cancel_first)

    # ── error translation ───────────────────────────────────────────────────
    def _guard(self, fn, *args, **kw) -> Any:
        """Turn tradovate-client's exceptions into this library's."""
        try:
            return fn(*args, **kw)
        except Exception as exc:
            raise self._translate(exc) from exc

    def _translate(self, exc) -> BrokerError:
        from ..errors import AuthError, ThrottledError

        try:
            import tradovate_client.errors as E
        except ImportError:
            return TransportError(str(exc), venue=self.name)
        if isinstance(exc, getattr(E, 'AuthError', ())):
            return AuthError(
                'Tradovate rejected the credentials', venue=self.name, detail=str(exc)
            )
        if isinstance(exc, getattr(E, 'ThrottledError', ())):
            return ThrottledError(
                'Tradovate rate limited the request',
                retry_after=getattr(exc, 'retry_after', None),
                venue=self.name,
                detail=str(exc),
            )
        if isinstance(exc, getattr(E, 'NotFoundError', ())):
            return NotFoundError(str(exc), venue=self.name)
        if isinstance(exc, getattr(E, 'ConfigError', ())):
            return ConfigError(str(exc), venue=self.name)
        # Checked before RequestError, since OrderRejected is the more
        # specific answer: the venue received the order and refused it.
        if isinstance(exc, getattr(E, 'OrderRejected', ())):
            return RejectedError(
                'Tradovate refused the order: %s' % exc,
                venue=self.name,
                detail=getattr(exc, 'reason', None),
            )
        if isinstance(exc, getattr(E, 'RequestError', ())):
            return RejectedError(
                'Tradovate refused the request: %s' % exc, venue=self.name
            )
        if isinstance(exc, getattr(E, 'TransportError', ())):
            return TransportError(
                str(exc), venue=self.name, ambiguous=getattr(exc, 'ambiguous', False)
            )
        return TransportError('%s: %s' % (type(exc).__name__, exc), venue=self.name)


#: Tradovate reports an order's fate in ``failureReason`` on an HTTP 200, and
#: ``Success`` is one of its own documented values -- so the field being
#: present does not mean the order failed. Only another value does.
SUCCESS_REASON = 'Success'


def _order_of(reply, venue, spec=None) -> Order:
    """Normalise whatever tradovate-client handed back.

    Two shapes arrive here. A *read* returns tradovate-client's typed
    ``Order``, which already carries the id, status, side, quantity and
    prices. A *write* returns Tradovate's raw result dict, with an ``orderId``
    and possibly a ``failureReason``. Treating the typed object as a dict
    loses the id and the status, so a working order reads as finished.
    """
    if reply is None:
        return Order(id='', venue=venue)
    if not isinstance(reply, dict):
        return _from_typed(reply, venue, spec)
    return _from_result(reply, venue, spec)


def _from_typed(obj, venue, spec=None) -> Order:
    """Map tradovate-client's own ``Order`` onto this library's.

    Tradovate names the direction ``action`` and the size ``orderQty``, and
    its price fields are ``price`` and ``stopPrice``. Reading ``side`` and
    ``limit_price`` -- this library's names -- silently produced side=None and
    lost the size and prices, which is why both names are tried here and the
    raw payload is the last resort rather than the first.
    """

    def pick(*names, default=None):
        for name in names:
            value = getattr(obj, name, None)
            if value not in (None, ''):
                return value
        raw = getattr(obj, 'raw', None)
        if isinstance(raw, dict):
            for name in names:
                if raw.get(name) not in (None, ''):
                    return raw[name]
        return default

    side_value = str(pick('action', 'side', default='') or '').lower()
    kind = str(pick('order_type', 'orderType', default='') or '')
    reverse = {v: k for k, v in TV_ORDER_TYPES.items()}
    order = Order(
        id=str(pick('id', default='') or ''),
        venue=venue,
        symbol=str(pick('symbol', default='') or (spec.symbol if spec else '')),
        side=Side(side_value) if side_value in ('buy', 'sell') else None,
        qty=_f(pick('qty', 'orderQty', default=0)),
        order_type=reverse.get(kind),
        status=str(pick('status', 'ordStatus', default='') or ''),
        filled_qty=_f(pick('filled_qty', 'cumQty', default=0)),
        avg_fill_price=_f(pick('avg_fill_price', 'avgPx', default=0)),
        limit_price=pick('price', 'limit_price'),
        stop_price=pick('stop_price', 'stopPrice'),
        client_order_id=pick('client_order_id', 'clOrdId'),
        raw={'order': obj},
    )
    if order.side is None and spec is not None:
        order.side = spec.side
    if not order.qty and spec is not None:
        order.qty = float(spec.qty)
    return order


def _from_result(reply, venue, spec=None) -> Order:
    """Map Tradovate's raw order result, raising only on a real refusal."""
    reason = reply.get('failureReason')
    if reason not in (None, '', SUCCESS_REASON):
        raise RejectedError(
            'Tradovate refused the order: %s%s'
            % (
                reason,
                ' (%s)' % reply['failureText'] if reply.get('failureText') else '',
            ),
            venue=venue,
            detail=reply,
        )
    order = Order(
        id=str(reply.get('orderId') or reply.get('id') or ''),
        venue=venue,
        raw=reply,
    )
    if spec is not None:
        order.symbol = spec.symbol
        order.side = spec.side
        order.qty = float(spec.qty)
        order.order_type = spec.order_type
        order.limit_price = spec.limit_price
        order.stop_price = spec.stop_price
        order.client_order_id = spec.client_order_id
    for source in ('ordStatus', 'status'):
        if reply.get(source):
            order.status = str(reply[source])
    return order


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
