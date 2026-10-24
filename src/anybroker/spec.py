"""One order, described once, for any venue.

An ``OrderSpec`` is what a strategy produces. It carries no venue dialect and
no venue defaults, so the same spec can be handed to IBKR, Tradovate or any
ccxt exchange and each one translates it at its own edge.

Nothing in here invents a price. ``take_profit`` and ``stop_loss`` are the
caller's own numbers or they are absent.
"""

from dataclasses import dataclass, field

from .enums import OrderType, Side, TimeInForce
from .errors import ConfigError

__all__ = ['OrderSpec', 'NEEDS_LIMIT', 'NEEDS_STOP', 'NEEDS_TRAIL']

#: Order types that cannot be sent without a limit price.
NEEDS_LIMIT = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT,
                         OrderType.TRAILING_STOP_LIMIT})

#: Order types that cannot be sent without a stop price.
NEEDS_STOP = frozenset({OrderType.STOP, OrderType.STOP_LIMIT})

#: Order types that need a trail distance, given as an amount or a percent.
NEEDS_TRAIL = frozenset({OrderType.TRAILING_STOP, OrderType.TRAILING_STOP_LIMIT})


@dataclass
class OrderSpec:
    """A venue-neutral order.

        OrderSpec('MNQZ6', Side.BUY, 2, OrderType.LIMIT, limit_price=20150.25)

    With ``take_profit`` or ``stop_loss`` set it becomes a bracket: the entry
    plus one or two protective children on the opposite side. Venues that
    cannot express that raise ``UnsupportedError`` rather than sending a naked
    entry.
    """

    symbol: str
    side: Side
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: 'float | None' = None
    stop_price: 'float | None' = None
    trail_amount: 'float | None' = None
    trail_percent: 'float | None' = None
    time_in_force: TimeInForce = TimeInForce.DAY
    good_til: 'str | None' = None
    take_profit: 'float | None' = None
    stop_loss: 'float | None' = None
    reduce_only: bool = False
    client_order_id: 'str | None' = None
    label: 'str | None' = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.side = Side(self.side)
        self.order_type = OrderType(self.order_type)
        self.time_in_force = TimeInForce(self.time_in_force)
        self.validate()

    def validate(self):
        if not self.symbol:
            raise ConfigError('symbol is required')
        if self.qty is None or float(self.qty) <= 0:
            raise ConfigError('qty must be positive; side says buy or sell')
        t = self.order_type
        if t in NEEDS_LIMIT and self.limit_price is None:
            raise ConfigError('%s needs limit_price' % t.value)
        if t in NEEDS_STOP and self.stop_price is None:
            raise ConfigError('%s needs stop_price' % t.value)
        if t in NEEDS_TRAIL and self.trail_amount is None and self.trail_percent is None:
            raise ConfigError('%s needs trail_amount or trail_percent' % t.value)
        if t is OrderType.MARKET and self.limit_price is not None:
            raise ConfigError('a market order carries no limit_price')
        if self.trail_amount is not None and self.trail_percent is not None:
            raise ConfigError('give trail_amount or trail_percent, not both')
        if self.time_in_force is TimeInForce.GTD and not self.good_til:
            raise ConfigError('good-til-date needs good_til')
        return self

    @property
    def is_bracket(self) -> bool:
        return self.take_profit is not None or self.stop_loss is not None

    @property
    def exit_side(self) -> Side:
        return self.side.opposite

    def child_specs(self):
        """The protective children this spec implies, on the opposite side."""
        out = []
        if self.take_profit is not None:
            out.append(OrderSpec(self.symbol, self.exit_side, self.qty,
                                 OrderType.LIMIT, limit_price=self.take_profit,
                                 time_in_force=self.time_in_force,
                                 reduce_only=True, label='take_profit'))
        if self.stop_loss is not None:
            out.append(OrderSpec(self.symbol, self.exit_side, self.qty,
                                 OrderType.STOP, stop_price=self.stop_loss,
                                 time_in_force=self.time_in_force,
                                 reduce_only=True, label='stop_loss'))
        return out
