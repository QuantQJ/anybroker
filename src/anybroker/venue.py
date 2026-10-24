"""The interface every venue implements, and what it declares it can do.

The verb names are the ones QJ's eight hand-written adapters already converged
on -- ``place``, ``flatten``, ``get_position``, ``list_positions``,
``get_account`` -- so an existing caller reads almost unchanged.
"""

from dataclasses import dataclass, field

from .enums import Env, OrderType
from .errors import UnsupportedError

__all__ = ['Capabilities', 'Venue']

ALL_ORDER_TYPES = frozenset(OrderType)


@dataclass(frozen=True)
class Capabilities:
    """What a venue can actually do.

    Declared rather than discovered at the point of failure, so a caller can
    ask before it tries. ``supports_paper`` is the important one: several real
    venues have no paper environment at all, and this library refuses to
    pretend otherwise.
    """

    order_types: frozenset = ALL_ORDER_TYPES
    supports_paper: bool = False
    supports_brackets: bool = False
    supports_modify: bool = False
    supports_reduce_only: bool = False
    supports_quotes: bool = False
    notes: str = ''

    def require_order_type(self, order_type, venue=None):
        if order_type not in self.order_types:
            raise UnsupportedError(
                '%s does not offer %s; it has %s'
                % (venue or 'this venue', OrderType(order_type).value,
                   ', '.join(sorted(t.value for t in self.order_types))),
                venue=venue)

    def require_env(self, env, venue=None):
        if Env(env) is Env.PAPER and not self.supports_paper:
            raise UnsupportedError(
                '%s has no paper environment; pass Env.LIVE deliberately or '
                'use a different venue' % (venue or 'this venue'), venue=venue)

    def require_brackets(self, venue=None):
        if not self.supports_brackets:
            raise UnsupportedError(
                '%s cannot attach a target or a stop to an entry; place the '
                'children yourself after the fill' % (venue or 'this venue'),
                venue=venue)


class Venue:
    """Base class. Subclasses implement whatever their broker supports.

    ``env`` is a constructor argument with no default. Paper and live are
    different destinations, so the choice belongs to the object rather than to
    each call -- a venue built for paper has no code path to the live account.
    """

    name = 'venue'
    capabilities = Capabilities()

    def __init__(self, env):
        self.env = Env(env)
        self.capabilities.require_env(self.env, self.name)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def connect(self):
        return self

    def close(self):
        pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False

    # ── reads ───────────────────────────────────────────────────────────────
    def get_account(self):
        raise NotImplementedError

    def list_positions(self):
        raise NotImplementedError

    def get_position(self, symbol):
        for pos in self.list_positions():
            if pos.symbol == symbol:
                return pos
        return None

    def list_orders(self):
        raise NotImplementedError

    def working_orders(self):
        return [o for o in self.list_orders() if o.is_working]

    def get_order(self, order_id):
        for order in self.list_orders():
            if str(order.id) == str(order_id):
                return order
        return None

    def get_quote(self, symbol):
        raise NotImplementedError

    # ── writes ──────────────────────────────────────────────────────────────
    def place(self, spec):
        raise NotImplementedError

    def cancel(self, order_id):
        raise NotImplementedError

    def cancel_all(self):
        """Cancel every working order. Returns the ids cancelled."""
        done = []
        for order in self.working_orders():
            self.cancel(order.id)
            done.append(order.id)
        return done

    def modify(self, order_id, **changes):
        raise UnsupportedError('%s cannot modify an order; cancel and replace'
                               % self.name, venue=self.name)

    def flatten(self, symbol):
        """Close one symbol at market."""
        raise NotImplementedError

    def flatten_all(self, *, cancel_first=True):
        """Cancel working orders then close every position.

        On a live venue this closes real positions.
        """
        result = {'cancelled': [], 'flattened': []}
        if cancel_first:
            result['cancelled'] = self.cancel_all()
        for pos in self.list_positions():
            if pos.is_flat:
                continue
            self.flatten(pos.symbol)
            result['flattened'].append(pos.symbol)
        return result

    # ── convenience, in terms of place() ────────────────────────────────────
    def market(self, symbol, side, qty, **kw):
        from .spec import OrderSpec
        from .enums import OrderType as T
        return self.place(OrderSpec(symbol, side, qty, T.MARKET, **kw))

    def limit(self, symbol, side, qty, limit_price, **kw):
        from .spec import OrderSpec
        from .enums import OrderType as T
        return self.place(OrderSpec(symbol, side, qty, T.LIMIT,
                                    limit_price=limit_price, **kw))

    def stop(self, symbol, side, qty, stop_price, **kw):
        from .spec import OrderSpec
        from .enums import OrderType as T
        return self.place(OrderSpec(symbol, side, qty, T.STOP,
                                    stop_price=stop_price, **kw))

    def stop_limit(self, symbol, side, qty, limit_price, stop_price, **kw):
        from .spec import OrderSpec
        from .enums import OrderType as T
        return self.place(OrderSpec(symbol, side, qty, T.STOP_LIMIT,
                                    limit_price=limit_price,
                                    stop_price=stop_price, **kw))

    def trailing_stop(self, symbol, side, qty, *, trail_amount=None,
                      trail_percent=None, **kw):
        from .spec import OrderSpec
        from .enums import OrderType as T
        return self.place(OrderSpec(symbol, side, qty, T.TRAILING_STOP,
                                    trail_amount=trail_amount,
                                    trail_percent=trail_percent, **kw))

    def __repr__(self):
        return '%s(env=%r)' % (type(self).__name__, self.env.value)
