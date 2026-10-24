"""The interface every venue implements, and what it declares it can do.

The verb names are the ones QJ's eight hand-written adapters already converged
on -- ``place``, ``flatten``, ``get_position``, ``list_positions``,
``get_account`` -- so an existing caller reads almost unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .enums import Env, OrderType, Side
from .errors import (
    ConfigError,
    DuplicateOrderError,
    NotFoundError,
    TransportError,
    UnresolvedOrderError,
    UnsupportedError,
)
from .models import Account, Order, Position, Quote

__all__ = [
    'Capabilities',
    'Venue',
    'Submission',
    'TRAILING_FORMS',
    'PROTECTIVE_ORDER_TYPES',
]

ALL_ORDER_TYPES = frozenset(OrderType)

#: Order types that can protect an open position. A limit order cannot: a
#: profit target is not a stop.
PROTECTIVE_ORDER_TYPES = frozenset(
    {
        OrderType.STOP,
        OrderType.STOP_LIMIT,
        OrderType.TRAILING_STOP,
        OrderType.TRAILING_STOP_LIMIT,
    }
)

#: How a trailing distance can be expressed. Most venues take one and not the
#: other -- of the 19 ccxt exchanges with any trailing support, 16 accept only
#: one form -- so this is declared per venue rather than assumed.
TRAILING_FORMS = frozenset({'amount', 'percent'})


@dataclass
class Submission:
    """One order this venue has sent, and what became of it.

    ``state`` is ``in_flight`` between the request leaving and an answer
    arriving, ``submitted`` once the venue acknowledged it, and ``unresolved``
    when the answer never came -- a timeout, or a socket that dropped
    mid-write. Unresolved is the dangerous one: the order may be working.
    Nothing is resent while a key stays that way.
    """

    key: str
    fingerprint: str
    symbol: str
    state: str = 'in_flight'
    order_id: str = ''
    order: Any = None
    detail: str = ''

    @property
    def is_unresolved(self) -> bool:
        return self.state == 'unresolved'


@dataclass(frozen=True)
class Capabilities:
    """What a venue can actually do.

    Declared rather than discovered at the point of failure, so a caller can
    ask before it tries. ``supports_paper`` is the important one: several real
    venues have no paper environment at all, and this library refuses to
    pretend otherwise.
    """

    order_types: frozenset = ALL_ORDER_TYPES
    trailing_forms: frozenset = TRAILING_FORMS
    supports_paper: bool = False
    supports_brackets: bool = False
    supports_modify: bool = False
    supports_reduce_only: bool = False
    supports_quotes: bool = False
    notes: str = ''

    def require_order_type(self, order_type, venue=None) -> None:
        if order_type not in self.order_types:
            raise UnsupportedError(
                '%s does not offer %s; it has %s'
                % (
                    venue or 'this venue',
                    OrderType(order_type).value,
                    ', '.join(sorted(t.value for t in self.order_types)),
                ),
                venue=venue,
            )

    def require_env(self, env, venue=None) -> None:
        if Env(env) is Env.PAPER and not self.supports_paper:
            raise UnsupportedError(
                '%s has no paper environment; pass Env.LIVE deliberately or '
                'use a different venue' % (venue or 'this venue'),
                venue=venue,
            )

    def require_trailing_form(self, spec, venue=None) -> None:
        """Check the trail was expressed in a form this venue accepts.

        An absolute distance and a percentage are different endpoints at most
        venues. Sending the wrong one is a rejection at the exchange, so it is
        caught here instead.
        """
        wanted = None
        if spec.trail_amount is not None:
            wanted = 'amount'
        elif spec.trail_percent is not None:
            wanted = 'percent'
        if wanted is None or wanted in self.trailing_forms:
            return
        other = {'amount': 'trail_percent=', 'percent': 'trail_amount='}[wanted]
        has = ', '.join(sorted(self.trailing_forms)) or 'neither'
        raise UnsupportedError(
            '%s does not take a trailing %s; it takes %s. Use %s.'
            % (venue or 'this venue', wanted, has, other),
            venue=venue,
        )

    def require_brackets(self, venue=None) -> None:
        if not self.supports_brackets:
            raise UnsupportedError(
                '%s cannot attach a target or a stop to an entry; place the '
                'children yourself after the fill' % (venue or 'this venue'),
                venue=venue,
            )


class Venue:
    """Base class. Subclasses implement whatever their broker supports.

    ``env`` is a constructor argument with no default. Paper and live are
    different destinations, so the choice belongs to the object rather than to
    each call -- a venue built for paper has no code path to the live account.
    """

    name = 'venue'
    capabilities = Capabilities()

    def __init__(self, env) -> None:
        self.env = Env(env)
        self.capabilities.require_env(self.env, self.name)
        self._submissions: dict[str, Submission] = {}

    # ── lifecycle ───────────────────────────────────────────────────────────
    def connect(self) -> Venue:
        return self

    def close(self) -> None:
        pass

    def __enter__(self) -> Venue:
        return self.connect()

    def __exit__(self, *exc) -> None:
        """Never suppresses. A failure inside the block is the caller's."""
        self.close()

    # ── reads ───────────────────────────────────────────────────────────────
    def get_account(self) -> Account:
        raise NotImplementedError

    def list_positions(self) -> list[Position]:
        raise NotImplementedError

    def get_position(self, symbol) -> Position | None:
        for pos in self.list_positions():
            if pos.symbol == symbol:
                return pos
        return None

    def list_orders(self) -> list[Order]:
        raise NotImplementedError

    def working_orders(self) -> list[Order]:
        return [o for o in self.list_orders() if o.is_working]

    def get_order(self, order_id) -> Any:
        for order in self.list_orders():
            if str(order.id) == str(order_id):
                return order
        return None

    def get_quote(self, symbol) -> Quote:
        raise NotImplementedError

    # ── writes ──────────────────────────────────────────────────────────────
    def place(self, spec, *, allow_duplicate: bool = False) -> Order:
        """Send an order, once.

        The same ``OrderSpec`` submitted twice does not become two orders:

        * still working -> this returns it, read from the venue rather than a
          cache, and sends nothing;
        * already finished -> ``DuplicateOrderError``, rather than guessing
          whether you meant a second entry. Give the new one its own
          ``client_order_id``, or pass ``allow_duplicate=True``;
        * never answered -> ``UnresolvedOrderError``, because resending could
          double the position and assuming failure could leave one unmanaged.

        Identity is ``spec.idempotency_key``: your ``client_order_id`` when
        set, otherwise a digest of the instruction. A rule that fires daily
        should set its own, scoped to the day -- which is what the live
        Tradday rules already do.
        """
        spec.validate()
        key = spec.idempotency_key
        if not allow_duplicate:
            existing = self._check_duplicate(spec)
            if existing is not None:
                return existing
        submission = self._record_in_flight(spec)
        try:
            order = self._place(spec)
        except TransportError as exc:
            if getattr(exc, 'ambiguous', False):
                submission.state = 'unresolved'
                submission.detail = str(exc)
            else:
                self._submissions.pop(key, None)
            # Persist on the failure path above all. An unresolved submission
            # is the one piece of state that must survive a restart, because
            # the order may be working -- saving only on success loses exactly
            # the case that matters.
            self._persist_submissions()
            raise
        except Exception:
            self._submissions.pop(key, None)
            self._persist_submissions()
            raise
        submission.state = 'submitted'
        submission.order_id = str(getattr(order, 'id', '') or '')
        submission.order = order
        self._persist_submissions()
        return order

    def _persist_submissions(self) -> None:
        """Hook for a venue that writes its submissions down. Default: nothing.

        Called on every transition -- submitted, unresolved, dropped, resolved
        and forgotten -- so no reachable state was never saved.
        """

    def _place(self, spec) -> Order:
        """Actually send it. Each venue implements this; callers use place()."""
        raise NotImplementedError

    # ── idempotency ─────────────────────────────────────────────────────────

    def _check_duplicate(self, spec) -> Any:
        """The live order if this spec is already working, else None."""
        key = spec.idempotency_key
        previous = self._submissions.get(key)
        if previous is None:
            return None
        if previous.is_unresolved:
            raise UnresolvedOrderError(
                'a previous submission of %s never got an answer from %s '
                '(%s). It may be working. Read the order state, then call '
                'resolve(key, order_id=...) or forget(key).'
                % (key, self.name, previous.detail or 'no detail'),
                key=key,
                venue=self.name,
            )
        live = self._live_order(previous)
        if live is not None and live.is_working:
            return live
        state = live.status if live is not None else 'unknown'
        raise DuplicateOrderError(
            'this exact order was already sent as %s (now %s). If you mean a '
            'second one, give it its own client_order_id or pass '
            'allow_duplicate=True.' % (previous.order_id or key, state or 'finished'),
            key=key,
            order=live or previous.order,
            venue=self.name,
        )

    def _live_order(self, submission) -> Any:
        """Read the venue's own state for a recorded submission."""
        if not submission.order_id:
            return submission.order
        try:
            found = self.get_order(submission.order_id)
        except Exception:
            return submission.order
        return found if found is not None else submission.order

    def _record_in_flight(self, spec) -> Submission:
        submission = Submission(
            key=spec.idempotency_key,
            fingerprint=spec.fingerprint,
            symbol=spec.symbol,
        )
        self._submissions[submission.key] = submission
        return submission

    def submissions(self) -> dict[str, Submission]:
        """Everything this venue has sent, by key."""
        return dict(self._submissions)

    def unresolved(self) -> list[Submission]:
        """Submissions whose fate is unknown. Should always be empty."""
        return [s for s in self._submissions.values() if s.is_unresolved]

    def resolve(self, key, *, order_id=None) -> Submission:
        """Record what you found out about an unresolved submission."""
        submission = self._submissions.get(key)
        if submission is None:
            raise NotFoundError(
                'no submission %r on %s' % (key, self.name), venue=self.name
            )
        if order_id is None:
            raise ConfigError(
                'say which order it became, or call forget()', venue=self.name
            )
        submission.state = 'submitted'
        submission.order_id = str(order_id)
        submission.order = None
        self._persist_submissions()
        return submission

    def forget(self, key) -> None:
        """Drop a submission, having confirmed the venue never accepted it."""
        self._submissions.pop(key, None)
        self._persist_submissions()

    def cancel(self, order_id) -> Any:
        raise NotImplementedError

    def cancel_all(self) -> list:
        """Cancel every working order. Returns the ids cancelled."""
        done = []
        for order in self.working_orders():
            self.cancel(order.id)
            done.append(order.id)
        return done

    def modify(self, order_id, **changes) -> Any:
        raise UnsupportedError(
            '%s cannot modify an order; cancel and replace' % self.name, venue=self.name
        )

    def flatten(self, symbol) -> Any:
        """Close one symbol at market."""
        raise NotImplementedError

    # ── protection ──────────────────────────────────────────────────────────

    def protection(self) -> dict:
        """How much of each open position has a working stop behind it.

        A long is protected by a **sell** stop and a short by a **buy** stop,
        so the side is checked and not merely the order type -- a buy stop on
        the same symbol as a long protects nothing. Quantity is checked too: a
        one-lot stop does not cover five lots.

        Returns, per open position, ``{'qty', 'side', 'protected_qty',
        'exposed_qty', 'orders'}``.
        """
        stops = [
            o
            for o in self.list_orders()
            if o.is_working and o.order_type in PROTECTIVE_ORDER_TYPES
        ]
        out = {}
        for position in self.list_positions():
            if position.is_flat:
                continue
            wanted = Side.SELL if position.qty > 0 else Side.BUY
            covering = [
                o for o in stops if o.symbol == position.symbol and o.side is wanted
            ]
            protected = sum(abs(float(o.qty or 0)) for o in covering)
            size = abs(float(position.qty))
            out[position.symbol] = {
                'qty': position.qty,
                'side': position.side,
                'protected_qty': protected,
                'exposed_qty': max(0.0, size - protected),
                'orders': [o.id for o in covering],
            }
        return out

    def unprotected_positions(self) -> list[Position]:
        """Open positions with no stop, or not enough stop, behind them.

        A position whose stop covers only part of it is listed: partly covered
        is not covered.
        """
        cover = self.protection()
        return [
            p
            for p in self.list_positions()
            if not p.is_flat and cover.get(p.symbol, {}).get('exposed_qty', 0) > 0
        ]

    def flatten_all(self, *, cancel_first=True) -> dict:
        """Cancel working orders then close every position.

        On a live venue this closes real positions.
        """
        result: dict[str, list] = {'cancelled': [], 'flattened': []}
        if cancel_first:
            result['cancelled'] = self.cancel_all()
        for pos in self.list_positions():
            if pos.is_flat:
                continue
            self.flatten(pos.symbol)
            result['flattened'].append(pos.symbol)
        return result

    # ── convenience, in terms of place() ────────────────────────────────────
    def market(self, symbol, side, qty, **kw) -> Any:
        from .enums import OrderType as T
        from .spec import OrderSpec

        return self.place(OrderSpec(symbol, side, qty, T.MARKET, **kw))

    def limit(self, symbol, side, qty, limit_price, **kw) -> Any:
        from .enums import OrderType as T
        from .spec import OrderSpec

        return self.place(
            OrderSpec(symbol, side, qty, T.LIMIT, limit_price=limit_price, **kw)
        )

    def stop(self, symbol, side, qty, stop_price, **kw) -> Any:
        from .enums import OrderType as T
        from .spec import OrderSpec

        return self.place(
            OrderSpec(symbol, side, qty, T.STOP, stop_price=stop_price, **kw)
        )

    def stop_limit(self, symbol, side, qty, limit_price, stop_price, **kw) -> Any:
        from .enums import OrderType as T
        from .spec import OrderSpec

        return self.place(
            OrderSpec(
                symbol,
                side,
                qty,
                T.STOP_LIMIT,
                limit_price=limit_price,
                stop_price=stop_price,
                **kw,
            )
        )

    def trailing_stop(
        self, symbol, side, qty, *, trail_amount=None, trail_percent=None, **kw
    ) -> Any:
        from .enums import OrderType as T
        from .spec import OrderSpec

        return self.place(
            OrderSpec(
                symbol,
                side,
                qty,
                T.TRAILING_STOP,
                trail_amount=trail_amount,
                trail_percent=trail_percent,
                **kw,
            )
        )

    def __repr__(self) -> str:
        return '%s(env=%r)' % (type(self).__name__, self.env.value)
