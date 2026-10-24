"""Interactive Brokers, through ib_insync.

ccxt has no IBKR adapter and never has, so this is the library's own.

Two things about IB that this wrapper exists to get right:

**Destination.** IB publishes four endpoints and they are ordinary TCP ports
on the same host, which is why a hardcoded port is such an easy mistake.
``Env.PAPER`` is the paper gateway and ``Env.LIVE`` is the live gateway;
nothing else in the class knows a port number.

**Client id.** IB disconnects an existing session when a second one connects
with the same client id. On QJ's fleet roughly seventy ids are already in use,
including the ids of the running desks, so ``client_id`` is a required
argument with no default. Guessing one here would knock a live trader offline.

Order type strings were read from the installed ib_insync, not assumed:
``MKT``, ``LMT``, ``STP``, ``STP LMT``, ``TRAIL``, ``TRAIL LIMIT``.
"""

from ..enums import Env, OrderType, Side, TimeInForce
from ..errors import (ConfigError, NotFoundError, RejectedError,
                      TransportError, UnsupportedError)
from ..models import Account, Order, Position, Quote
from ..venue import Capabilities, Venue

__all__ = ['IbkrVenue', 'GATEWAY_PORTS', 'IB_ORDER_TYPES', 'IB_TIF']

#: IB Gateway, paper and live. TWS listens on 7497 and 7496; pass ``port=``
#: to reach those instead.
GATEWAY_PORTS = {Env.PAPER: 4001, Env.LIVE: 4002}
TWS_PORTS = {Env.PAPER: 7497, Env.LIVE: 7496}

IB_ORDER_TYPES = {
    OrderType.MARKET: 'MKT',
    OrderType.LIMIT: 'LMT',
    OrderType.STOP: 'STP',
    OrderType.STOP_LIMIT: 'STP LMT',
    OrderType.TRAILING_STOP: 'TRAIL',
    OrderType.TRAILING_STOP_LIMIT: 'TRAIL LIMIT',
}

IB_TIF = {
    TimeInForce.DAY: 'DAY',
    TimeInForce.GTC: 'GTC',
    TimeInForce.IOC: 'IOC',
    TimeInForce.FOK: 'FOK',
    TimeInForce.GTD: 'GTD',
}

IB_SIDE = {Side.BUY: 'BUY', Side.SELL: 'SELL'}


class IbkrVenue(Venue):
    """IBKR. Requires ``ib_insync`` and a running gateway or TWS.

        venue = IbkrVenue(Env.PAPER, client_id=7788)
        with venue:
            venue.limit('MNQZ6', Side.BUY, 2, 20150.25)
    """

    name = 'ibkr'
    capabilities = Capabilities(
        order_types=frozenset(IB_ORDER_TYPES),
        supports_paper=True,
        supports_brackets=True,
        supports_modify=True,
        supports_reduce_only=False,
        supports_quotes=True,
        notes='parent/child brackets with a shared OCA group; '
              'reduce_only is not an IB concept')

    def __init__(self, env, *, client_id=None, host='127.0.0.1', port=None,
                 account=None, tws=False, timeout=20.0, ib=None,
                 contract_factory=None):
        super().__init__(env)
        if client_id is None and ib is None:
            raise ConfigError(
                'client_id is required. IB disconnects an existing session '
                'when a second one uses the same id, and this fleet already '
                'has ids in use for the running desks -- pick one nothing '
                'else uses.', venue=self.name)
        self.client_id = client_id
        self.host = host
        self.port = port if port is not None else (
            TWS_PORTS if tws else GATEWAY_PORTS)[self.env]
        self.account = account
        self.timeout = timeout
        self._ib = ib
        self._contract_factory = contract_factory

    # ── lifecycle ───────────────────────────────────────────────────────────
    def connect(self):
        if self._ib is not None and getattr(self._ib, 'isConnected', bool)():
            return self
        if self._ib is None:
            try:
                from ib_insync import IB
            except ImportError as exc:
                raise ConfigError(
                    'ib_insync is not installed; pip install "anybroker[ibkr]"',
                    venue=self.name) from exc
            self._ib = IB()
        try:
            self._ib.connect(self.host, self.port, clientId=self.client_id,
                             timeout=self.timeout)
        except Exception as exc:
            raise TransportError(
                'could not reach IB at %s:%s as client %s (%s)'
                % (self.host, self.port, self.client_id, exc),
                venue=self.name) from exc
        return self

    def close(self):
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:
                pass

    @property
    def ib(self):
        if self._ib is None:
            raise ConfigError('not connected; call connect() first',
                              venue=self.name)
        return self._ib

    # ── contracts ───────────────────────────────────────────────────────────
    def contract(self, symbol):
        """Resolve a symbol to an IB contract.

        A caller with its own contract rules passes ``contract_factory=``.
        Without one this asks IB to qualify the symbol and refuses an
        ambiguous answer rather than picking a leg.
        """
        if self._contract_factory is not None:
            return self._contract_factory(symbol)
        if not isinstance(symbol, str):
            return symbol                      # already a contract object
        from ib_insync import Contract
        candidate = Contract(symbol=symbol)
        found = self.ib.qualifyContracts(candidate)
        if not found:
            raise NotFoundError(
                'IB could not qualify %r; pass contract_factory= or hand in '
                'an ib_insync Contract' % symbol, venue=self.name)
        if len(found) > 1:
            raise ConfigError(
                '%r is ambiguous at IB (%d matches); be specific or pass '
                'contract_factory=' % (symbol, len(found)), venue=self.name)
        return found[0]

    # ── reads ───────────────────────────────────────────────────────────────
    def get_account(self):
        account_id = self.account or self._default_account()
        values = {}
        for row in self.ib.accountValues(account_id) or []:
            if row.currency in ('USD', 'BASE', ''):
                values[row.tag] = row.value
        return Account(
            id=account_id, venue=self.name,
            cash=_f(values.get('TotalCashValue')),
            equity=_f(values.get('NetLiquidation')),
            open_pnl=_f(values.get('UnrealizedPnL')),
            realized_pnl=_f(values.get('RealizedPnL')),
            raw=values)

    def _default_account(self):
        accounts = list(self.ib.managedAccounts() or [])
        if not accounts:
            raise NotFoundError('IB reported no managed accounts',
                                venue=self.name)
        if len(accounts) > 1:
            raise ConfigError(
                'this login has %d accounts (%s); say which with account='
                % (len(accounts), ', '.join(accounts)), venue=self.name)
        return accounts[0]

    def list_positions(self):
        out = []
        for p in self.ib.positions(self.account) if self.account else self.ib.positions():
            out.append(Position(
                symbol=_local(p.contract), venue=self.name,
                qty=float(p.position), avg_price=float(p.avgCost or 0.0),
                raw={'contract': p.contract, 'account': getattr(p, 'account', None)}))
        return out

    def list_orders(self):
        out = []
        for t in self.ib.trades():
            out.append(_order_of(t, self.name))
        return out

    def get_quote(self, symbol):
        contract = self.contract(symbol)
        ticker = self.ib.reqMktData(contract, '', True, False)
        self.ib.sleep(0.4)
        return Quote(symbol=_local(contract), venue=self.name,
                     bid=_none_if_nan(ticker.bid), ask=_none_if_nan(ticker.ask),
                     last=_none_if_nan(ticker.last), raw={'ticker': ticker})

    # ── writes ──────────────────────────────────────────────────────────────
    def place(self, spec):
        spec.validate()
        self.capabilities.require_order_type(spec.order_type, self.name)
        if spec.reduce_only:
            raise UnsupportedError(
                'IB has no reduce-only flag; size the closing order yourself',
                venue=self.name)
        contract = self.contract(spec.symbol)
        if spec.is_bracket:
            return self._place_bracket(spec, contract)
        trade = self._submit(self._ib_order(spec), contract)
        return _order_of(trade, self.name)

    def _place_bracket(self, spec, contract):
        """Parent plus children, held until the whole group is staged.

        The parent goes out with ``transmit=False`` so IB holds it, each child
        carries ``parentId`` and a shared OCA group so one filling cancels the
        other, and only the last child sets ``transmit=True``, which releases
        the group as one unit. Nothing is working at the exchange until every
        leg has been accepted.
        """
        self.capabilities.require_brackets(self.name)
        parent = self._ib_order(spec)
        parent.transmit = False
        parent_trade = self._submit(parent, contract)
        parent_id = parent_trade.order.orderId
        children = spec.child_specs()
        oca = 'anybroker-%s' % parent_id
        trades = []
        for index, child in enumerate(children):
            order = self._ib_order(child)
            order.parentId = parent_id
            order.ocaGroup = oca
            order.ocaType = 1                      # cancel the rest on fill
            order.transmit = index == len(children) - 1
            trades.append(self._submit(order, contract))
        result = _order_of(parent_trade, self.name)
        result.children = [_order_of(t, self.name) for t in trades]
        return result

    def _ib_order(self, spec):
        from ib_insync import Order as IbOrder
        order = IbOrder(
            action=IB_SIDE[spec.side],
            totalQuantity=float(spec.qty),
            orderType=IB_ORDER_TYPES[spec.order_type],
            tif=IB_TIF[spec.time_in_force],
            transmit=True)
        if spec.limit_price is not None:
            order.lmtPrice = float(spec.limit_price)
        if spec.stop_price is not None:
            order.auxPrice = float(spec.stop_price)
        if spec.trail_amount is not None:
            order.auxPrice = float(spec.trail_amount)
        if spec.trail_percent is not None:
            order.trailingPercent = float(spec.trail_percent)
        if spec.time_in_force is TimeInForce.GTD and spec.good_til:
            order.goodTillDate = spec.good_til
        if spec.client_order_id:
            order.orderRef = str(spec.client_order_id)
        elif spec.label:
            order.orderRef = str(spec.label)
        if self.account:
            order.account = self.account
        for key, value in (spec.extra or {}).items():
            setattr(order, key, value)
        return order

    def _submit(self, order, contract):
        try:
            return self.ib.placeOrder(contract, order)
        except Exception as exc:
            raise TransportError(
                'IB refused the submission: %s -- this order may or may not '
                'have been accepted. Read order and position state before '
                'sending anything else.' % exc, venue=self.name,
                ambiguous=True) from exc

    def cancel(self, order_id):
        for trade in self.ib.trades():
            if str(trade.order.orderId) == str(order_id):
                self.ib.cancelOrder(trade.order)
                return _order_of(trade, self.name)
        raise NotFoundError('no IB order %s in this session' % order_id,
                            venue=self.name)

    def modify(self, order_id, **changes):
        """IB modifies by re-placing the same orderId with new values."""
        for trade in self.ib.trades():
            if str(trade.order.orderId) != str(order_id):
                continue
            order = trade.order
            mapping = {'qty': 'totalQuantity', 'limit_price': 'lmtPrice',
                       'stop_price': 'auxPrice', 'trail_amount': 'auxPrice',
                       'trail_percent': 'trailingPercent'}
            for key, value in changes.items():
                field = mapping.get(key, key)
                if not hasattr(order, field):
                    raise ConfigError('IB orders have no %r' % key,
                                      venue=self.name)
                setattr(order, field, value)
            order.transmit = True
            return _order_of(self._submit(order, trade.contract), self.name)
        raise NotFoundError('no IB order %s in this session' % order_id,
                            venue=self.name)

    def flatten(self, symbol):
        """Close one symbol at market, sized from the open position."""
        position = self.get_position(symbol)
        if position is None or position.is_flat:
            raise NotFoundError('no open position in %s' % symbol,
                                venue=self.name)
        side = Side.SELL if position.qty > 0 else Side.BUY
        from .. spec import OrderSpec
        spec = OrderSpec(symbol, side, abs(position.qty), OrderType.MARKET,
                         time_in_force=TimeInForce.DAY, label='flatten')
        contract = position.raw.get('contract') or symbol
        trade = self._submit(self._ib_order(spec), self.contract(contract))
        return _order_of(trade, self.name)


# ── helpers ─────────────────────────────────────────────────────────────────

def _order_of(trade, venue):
    o, s = trade.order, trade.orderStatus
    reverse = {v: k for k, v in IB_ORDER_TYPES.items()}
    status = getattr(s, 'status', '') or ''
    order = Order(
        id=str(o.orderId), venue=venue, symbol=_local(trade.contract),
        side=Side.BUY if o.action == 'BUY' else Side.SELL,
        qty=float(o.totalQuantity or 0.0),
        order_type=reverse.get(o.orderType),
        status=status,
        filled_qty=float(getattr(s, 'filled', 0.0) or 0.0),
        avg_fill_price=float(getattr(s, 'avgFillPrice', 0.0) or 0.0),
        limit_price=_none_if_zero(getattr(o, 'lmtPrice', None)),
        stop_price=_none_if_zero(getattr(o, 'auxPrice', None)),
        client_order_id=getattr(o, 'orderRef', None) or None,
        raw={'order': o, 'status': s, 'contract': trade.contract})
    if status.lower() in ('rejected', 'inactive') and getattr(s, 'whyHeld', None):
        raise RejectedError('IB rejected order %s: %s'
                            % (o.orderId, s.whyHeld), venue=venue,
                            detail=order)
    return order


def _local(contract):
    return (getattr(contract, 'localSymbol', None)
            or getattr(contract, 'symbol', None) or str(contract))


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _none_if_zero(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value == 0.0 else value


def _none_if_nan(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else value
