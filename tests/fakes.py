"""Stand-ins for ib_insync and ccxt. No network, no credentials, no gateway."""

import types

# ── a fake ib_insync IB ─────────────────────────────────────────────────────


class FakeOrderStatus:
    def __init__(
        self, status='PreSubmitted', filled=0.0, avgFillPrice=0.0, whyHeld=None
    ):
        self.status = status
        self.filled = filled
        self.avgFillPrice = avgFillPrice
        self.whyHeld = whyHeld


class FakeTrade:
    def __init__(self, order, contract, status=None):
        self.order = order
        self.contract = contract
        self.orderStatus = status or FakeOrderStatus()


class FakeContract:
    def __init__(self, symbol, localSymbol=None):
        self.symbol = symbol
        self.localSymbol = localSymbol or symbol


class FakeIB:
    """Records what was placed. Mirrors just enough of ib_insync's surface."""

    def __init__(self, *, accounts=('DU111',), positions=(), qualify=None):
        self.connected = False
        self.connect_args = None
        self.placed = []
        self.cancelled = []
        self._accounts = list(accounts)
        self._positions = list(positions)
        self._qualify = qualify
        self._next_id = 1
        self._trades = []

    # lifecycle
    def connect(self, host, port, clientId=None, timeout=None):
        self.connected = True
        self.connect_args = {
            'host': host,
            'port': port,
            'clientId': clientId,
            'timeout': timeout,
        }

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def sleep(self, _seconds):
        pass

    # reads
    def managedAccounts(self):
        return list(self._accounts)

    def accountValues(self, account=None):
        def row(tag, value):
            return types.SimpleNamespace(
                tag=tag, value=value, currency='USD', account=account
            )

        return [
            row('TotalCashValue', '50000'),
            row('NetLiquidation', '50250'),
            row('UnrealizedPnL', '250'),
            row('RealizedPnL', '-10'),
        ]

    def positions(self, account=None):
        return list(self._positions)

    def trades(self):
        return list(self._trades)

    def qualifyContracts(self, contract):
        if self._qualify is not None:
            return list(self._qualify)
        return [FakeContract(contract.symbol)]

    def reqMktData(self, contract, *a, **kw):
        return types.SimpleNamespace(bid=100.0, ask=100.5, last=100.25)

    # writes
    def placeOrder(self, contract, order):
        if not getattr(order, 'orderId', 0):
            order.orderId = self._next_id
            self._next_id += 1
        trade = FakeTrade(order, contract)
        self.placed.append(trade)
        self._trades = [t for t in self._trades if t.order.orderId != order.orderId] + [
            trade
        ]
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)


def fake_position(symbol, qty, avg=0.0, account='DU111'):
    return types.SimpleNamespace(
        contract=FakeContract(symbol), position=qty, avgCost=avg, account=account
    )


# ── a fake ccxt exchange ────────────────────────────────────────────────────


class FakeCcxt:
    def __init__(
        self, exchange_id='fake', *, has=None, sandbox=True, positions=(), orders=()
    ):
        self.id = exchange_id
        # All 104 real ccxt exchanges report createOrder, and all report
        # createLimitOrder; 10 omit createMarketOrder. The fake mirrors that
        # so capability derivation is tested against a realistic table.
        self.has = {
            'createOrder': True,
            'createMarketOrder': True,
            'createLimitOrder': True,
            'fetchTicker': True,
            'fetchPositions': True,
            'createStopOrder': True,
            'editOrder': True,
        }
        if has is not None:
            self.has.update(has)
        self.urls = {'test': 'https://sandbox.example'} if sandbox else {}
        self.sandbox_mode = None
        self.created = []
        self.cancelled = []
        self._positions = list(positions)
        self._orders = list(orders)

    def set_sandbox_mode(self, on):
        if not self.urls.get('test'):
            raise RuntimeError('sandbox not supported')
        self.sandbox_mode = on

    def fetch_balance(self):
        return {'total': {'USD': 25000.0}, 'free': {'USD': 24000.0}}

    def fetch_positions(self):
        return list(self._positions)

    def fetch_open_orders(self, symbol=None):
        return list(self._orders)

    def fetch_order(self, order_id, symbol=None):
        for o in self._orders:
            if str(o.get('id')) == str(order_id):
                return o
        return None

    def fetch_ticker(self, symbol):
        return {'bid': 60000.0, 'ask': 60010.0, 'last': 60005.0}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        call = {
            'symbol': symbol,
            'type': type_,
            'side': side,
            'amount': amount,
            'price': price,
            'params': dict(params or {}),
        }
        self.created.append(call)
        return {
            'id': 'x%d' % len(self.created),
            'symbol': symbol,
            'type': type_,
            'side': side,
            'amount': amount,
            'price': price,
            'status': 'open',
            'filled': 0.0,
        }

    def cancel_order(self, order_id, symbol=None):
        self.cancelled.append(order_id)
        return {'id': order_id, 'status': 'canceled'}

    def edit_order(
        self, order_id, symbol=None, type_=None, side=None, amount=None, price=None
    ):
        return {
            'id': order_id,
            'symbol': symbol,
            'amount': amount,
            'price': price,
            'status': 'open',
        }
