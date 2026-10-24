"""What comes back. Each model keeps the venue's raw payload on ``.raw``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import OrderType, Side

__all__ = ['Account', 'Position', 'Order', 'Fill', 'Quote']


@dataclass
class Account:
    id: str
    venue: str
    cash: float = 0.0
    equity: float = 0.0
    open_pnl: float = 0.0
    realized_pnl: float = 0.0
    currency: str = 'USD'
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Position:
    symbol: str
    venue: str
    qty: float = 0.0
    avg_price: float = 0.0
    open_pnl: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def side(self) -> Any:
        if self.qty > 0:
            return Side.BUY
        if self.qty < 0:
            return Side.SELL
        return None


@dataclass
class Order:
    id: str
    venue: str
    symbol: str = ''
    side: Side | None = None
    qty: float = 0.0
    order_type: OrderType | None = None
    status: str = ''
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    limit_price: float | None = None
    stop_price: float | None = None
    client_order_id: str | None = None
    children: list = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_working(self) -> bool:
        return self.status.lower() in (
            'working',
            'open',
            'submitted',
            'presubmitted',
            'pendingsubmit',
            'pendingnew',
            'accepted',
            'new',
            'partiallyfilled',
        )

    @property
    def is_done(self) -> bool:
        return self.status.lower() in (
            'filled',
            'cancelled',
            'canceled',
            'rejected',
            'expired',
            'closed',
            'apicancelled',
            'inactive',
        )


@dataclass
class Fill:
    order_id: str
    venue: str
    symbol: str = ''
    qty: float = 0.0
    price: float = 0.0
    fee: float = 0.0
    at: str = ''
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Quote:
    symbol: str
    venue: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return self.last
        return (self.bid + self.ask) / 2.0
