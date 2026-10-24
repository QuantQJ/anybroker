"""Every crypto venue, through ccxt.

ccxt already covers 104 exchanges with every order type, unified symbols and
rate limiting, maintained by a project with tens of thousands of users. There
is no reason to hand-write Coinbase, Kraken, Kraken Futures or OKX again, so
this does not: it adapts ccxt to this library's interface and gets out of the
way.

What ccxt does not have, and never has, is Tradovate, IBKR, Tradier,
TradeStation or NinjaTrader. Those are the library's own venues.

Capabilities are read from the exchange's own ``has`` table rather than
assumed, so a venue that cannot do trailing stops says so before an order is
built. Paper is ccxt's sandbox mode where the exchange offers one.
"""

from __future__ import annotations

from typing import Any

from ..enums import Env, OrderType, Side, TimeInForce
from ..errors import (
    AuthError,
    BrokerError,
    ConfigError,
    NotFoundError,
    RejectedError,
    ThrottledError,
    TransportError,
    UnsupportedError,
)
from ..models import Account, Order, Position, Quote
from ..venue import Capabilities, Venue

__all__ = ['CcxtVenue', 'CCXT_ORDER_TYPES', 'CCXT_TIF']

CCXT_ORDER_TYPES = {
    OrderType.MARKET: 'market',
    OrderType.LIMIT: 'limit',
    OrderType.STOP: 'market',  # trigger price carries the stop
    OrderType.STOP_LIMIT: 'limit',
    OrderType.TRAILING_STOP: 'market',
    OrderType.TRAILING_STOP_LIMIT: 'limit',
}

CCXT_TIF = {
    TimeInForce.DAY: None,  # ccxt's default is good-til-cancel
    TimeInForce.GTC: 'GTC',
    TimeInForce.IOC: 'IOC',
    TimeInForce.FOK: 'FOK',
    TimeInForce.GTD: 'GTD',
}


def _capabilities_of(exchange) -> Capabilities:
    """Read what this exchange says it can do, from ccxt's own ``has``."""
    has = dict(getattr(exchange, 'has', {}) or {})
    types = {OrderType.MARKET, OrderType.LIMIT}
    if has.get('createStopOrder') or has.get('createTriggerOrder'):
        types |= {OrderType.STOP, OrderType.STOP_LIMIT}
    if has.get('createTrailingAmountOrder') or has.get('createTrailingPercentOrder'):
        types |= {OrderType.TRAILING_STOP, OrderType.TRAILING_STOP_LIMIT}
    sandbox = bool(getattr(exchange, 'urls', {}).get('test'))
    return Capabilities(
        order_types=frozenset(types),
        supports_paper=sandbox,
        supports_brackets=bool(has.get('createOrderWithTakeProfitAndStopLoss')),
        supports_modify=bool(has.get('editOrder')),
        supports_reduce_only=True,
        supports_quotes=bool(has.get('fetchTicker')),
        notes='capabilities read from ccxt has/urls for %s'
        % getattr(exchange, 'id', '?'),
    )


class CcxtVenue(Venue):
    """One ccxt exchange behind this library's interface.

        venue = CcxtVenue('kraken', Env.LIVE, api_key=..., secret=...)
        venue.limit('BTC/USD', Side.BUY, 0.01, 60000.0)

    Pass ``exchange=`` to hand in an already-configured ccxt instance.
    """

    def __init__(
        self,
        exchange_id,
        env,
        *,
        api_key=None,
        secret=None,
        password=None,
        exchange=None,
        options=None,
        default_type=None,
    ) -> None:
        self.exchange_id = (
            exchange_id
            if isinstance(exchange_id, str)
            else getattr(exchange_id, 'id', 'ccxt')
        )
        self.name = self.exchange_id
        if exchange is None:
            exchange = self._build(api_key, secret, password, options, default_type)
        self._exchange = exchange
        self.capabilities = _capabilities_of(exchange)
        super().__init__(env)
        if Env(env) is Env.PAPER:
            self._enable_sandbox()

    def _build(self, api_key, secret, password, options, default_type) -> Any:
        try:
            import ccxt
        except ImportError as exc:
            raise ConfigError(
                'ccxt is not installed; pip install "anybroker[crypto]"',
                venue=self.exchange_id,
            ) from exc
        if self.exchange_id not in ccxt.exchanges:
            raise ConfigError(
                'ccxt has no %r. It covers %d exchanges, all crypto -- for '
                'Tradovate or IBKR use this library\'s own venues.'
                % (self.exchange_id, len(ccxt.exchanges)),
                venue=self.exchange_id,
            )
        config: dict[str, Any] = {'enableRateLimit': True}
        if api_key:
            config['apiKey'] = api_key
        if secret:
            config['secret'] = secret
        if password:
            config['password'] = password
        merged = dict(options or {})
        if default_type:
            merged['defaultType'] = default_type
        if merged:
            config['options'] = merged
        return getattr(ccxt, self.exchange_id)(config)

    def _enable_sandbox(self) -> None:
        try:
            self._exchange.set_sandbox_mode(True)
        except Exception as exc:
            raise UnsupportedError(
                '%s has no sandbox in ccxt (%s); pass Env.LIVE deliberately'
                % (self.exchange_id, exc),
                venue=self.name,
            ) from exc

    @property
    def exchange(self) -> Any:
        return self._exchange

    # ── reads ───────────────────────────────────────────────────────────────
    def get_account(self) -> Account:
        data = self._call(self._exchange.fetch_balance)
        total = (data or {}).get('total', {}) or {}
        free = (data or {}).get('free', {}) or {}
        currency = next(
            (c for c in ('USD', 'USDT', 'USDC') if c in total), next(iter(total), 'USD')
        )
        return Account(
            id=self.exchange_id,
            venue=self.name,
            cash=_f(free.get(currency)),
            equity=_f(total.get(currency)),
            currency=currency,
            raw=data or {},
        )

    def list_positions(self) -> list[Position]:
        if not getattr(self._exchange, 'has', {}).get('fetchPositions'):
            raise UnsupportedError(
                '%s does not report positions' % self.name, venue=self.name
            )
        out = []
        for p in self._call(self._exchange.fetch_positions) or []:
            qty = _f(
                p.get('contracts')
                if p.get('contracts') is not None
                else p.get('amount')
            )
            if (p.get('side') or '').lower() == 'short' and qty > 0:
                qty = -qty
            out.append(
                Position(
                    symbol=p.get('symbol') or '',
                    venue=self.name,
                    qty=qty,
                    avg_price=_f(p.get('entryPrice')),
                    open_pnl=_f(p.get('unrealizedPnl')),
                    raw=p,
                )
            )
        return out

    def list_orders(self, symbol=None) -> list[Order]:
        rows = self._call(self._exchange.fetch_open_orders, symbol)
        return [_order_of(r, self.name) for r in rows or []]

    def get_order(self, order_id, symbol=None) -> Any:
        row = self._call(self._exchange.fetch_order, order_id, symbol)
        if not row:
            raise NotFoundError('no order %s' % order_id, venue=self.name)
        return _order_of(row, self.name)

    def get_quote(self, symbol) -> Quote:
        t = self._call(self._exchange.fetch_ticker, symbol) or {}
        return Quote(
            symbol=symbol,
            venue=self.name,
            bid=t.get('bid'),
            ask=t.get('ask'),
            last=t.get('last'),
            raw=t,
        )

    # ── writes ──────────────────────────────────────────────────────────────
    def place(self, spec) -> Any:
        spec.validate()
        self.capabilities.require_order_type(spec.order_type, self.name)
        if spec.is_bracket:
            self.capabilities.require_brackets(self.name)
        params = dict(spec.extra or {})
        if spec.stop_price is not None:
            params['triggerPrice'] = float(spec.stop_price)
        if spec.trail_amount is not None:
            params['trailingAmount'] = float(spec.trail_amount)
        if spec.trail_percent is not None:
            params['trailingPercent'] = float(spec.trail_percent)
        tif = CCXT_TIF[spec.time_in_force]
        if tif:
            params['timeInForce'] = tif
        if spec.reduce_only:
            params['reduceOnly'] = True
        if spec.client_order_id:
            params['clientOrderId'] = str(spec.client_order_id)
        if spec.take_profit is not None:
            params['takeProfit'] = {'triggerPrice': float(spec.take_profit)}
        if spec.stop_loss is not None:
            params['stopLoss'] = {'triggerPrice': float(spec.stop_loss)}
        row = self._call(
            self._exchange.create_order,
            spec.symbol,
            CCXT_ORDER_TYPES[spec.order_type],
            spec.side.value,
            float(spec.qty),
            float(spec.limit_price) if spec.limit_price is not None else None,
            params,
        )
        return _order_of(row or {}, self.name)

    def cancel(self, order_id, symbol=None) -> Any:
        row = self._call(self._exchange.cancel_order, order_id, symbol)
        return _order_of(row or {'id': order_id}, self.name)

    def modify(self, order_id, **changes) -> Any:
        if not self.capabilities.supports_modify:
            return super().modify(order_id, **changes)
        row = self._call(
            self._exchange.edit_order,
            order_id,
            changes.get('symbol'),
            changes.get('order_type'),
            changes.get('side'),
            changes.get('qty'),
            changes.get('limit_price'),
        )
        return _order_of(row or {}, self.name)

    def flatten(self, symbol) -> Any:
        position = self.get_position(symbol)
        if position is None or position.is_flat:
            raise NotFoundError('no open position in %s' % symbol, venue=self.name)
        from ..spec import OrderSpec

        side = Side.SELL if position.qty > 0 else Side.BUY
        return self.place(
            OrderSpec(
                symbol,
                side,
                abs(position.qty),
                OrderType.MARKET,
                reduce_only=True,
                label='flatten',
            )
        )

    # ── error translation ───────────────────────────────────────────────────
    def _call(self, fn, *args) -> Any:
        try:
            return fn(*args)
        except Exception as exc:
            raise self._translate(exc) from exc

    def _translate(self, exc) -> BrokerError:
        try:
            import ccxt
        except ImportError:
            return TransportError(str(exc), venue=self.name)
        name = type(exc).__name__
        if isinstance(exc, getattr(ccxt, 'AuthenticationError', ())):
            return AuthError(
                '%s rejected the credentials' % self.name,
                venue=self.name,
                detail=str(exc),
            )
        if isinstance(exc, getattr(ccxt, 'RateLimitExceeded', ())):
            return ThrottledError(
                '%s rate limited the request' % self.name,
                venue=self.name,
                detail=str(exc),
            )
        if isinstance(exc, getattr(ccxt, 'OrderNotFound', ())):
            return NotFoundError(str(exc), venue=self.name)
        if isinstance(exc, getattr(ccxt, 'NotSupported', ())):
            return UnsupportedError(str(exc), venue=self.name)
        if isinstance(
            exc,
            (getattr(ccxt, 'InvalidOrder', ()), getattr(ccxt, 'InsufficientFunds', ())),
        ):
            return RejectedError(
                '%s refused the order: %s' % (self.name, exc), venue=self.name
            )
        if isinstance(exc, getattr(ccxt, 'RequestTimeout', ())):
            return TransportError(
                '%s timed out (%s) -- the request may or may not have been '
                'accepted. Read order state before sending anything else.'
                % (self.name, name),
                venue=self.name,
                ambiguous=True,
            )
        if isinstance(exc, getattr(ccxt, 'NetworkError', ())):
            return TransportError('%s: %s' % (name, exc), venue=self.name)
        return TransportError('%s: %s' % (name, exc), venue=self.name)


def _order_of(row, venue) -> Order:
    row = row or {}
    side = (row.get('side') or '').lower()
    reverse = {'market': OrderType.MARKET, 'limit': OrderType.LIMIT}
    return Order(
        id=str(row.get('id') or ''),
        venue=venue,
        symbol=row.get('symbol') or '',
        side=Side(side) if side in ('buy', 'sell') else None,
        qty=_f(row.get('amount')),
        order_type=reverse.get(row.get('type')),
        status=row.get('status') or '',
        filled_qty=_f(row.get('filled')),
        avg_fill_price=_f(row.get('average')),
        limit_price=row.get('price'),
        stop_price=row.get('triggerPrice') or row.get('stopPrice'),
        client_order_id=row.get('clientOrderId'),
        raw=row,
    )


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
