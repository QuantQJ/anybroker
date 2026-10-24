"""Interactive Brokers, through ib_insync.

ccxt has no IBKR adapter and never has, so this is the library's own.

Two things about IB that this wrapper exists to get right:

**Destination.** IB's endpoints are ordinary TCP ports on the same host, one
digit apart, and installations disagree about which is which. IBKR documents
Gateway live on 4001 and paper on 4002; QJ's Studio runs the *live* gateway on
4002, which is how the live book trades there. Because the vendor default and
a real installation disagree, this class does not map an environment to a port
at all -- ``port`` is required, the two known mappings are named constants you
can opt into, and the destination is then **verified after connecting** by
reading the account back. IB paper accounts carry a ``D`` prefix. Asking for
paper and receiving a live account closes the connection and raises; so does
the reverse.

**Client id.** IB disconnects an existing session when a second one connects
with the same client id. On QJ's fleet roughly seventy ids are already in use,
including the ids of the running desks, so ``client_id`` is a required
argument with no default. Guessing one here would knock a live trader offline.

Order type strings were read from the installed ib_insync, not assumed:
``MKT``, ``LMT``, ``STP``, ``STP LMT``, ``TRAIL``, ``TRAIL LIMIT``.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

from ..enums import Env, OrderType, Side, TimeInForce
from ..errors import (
    ConfigError,
    DisconnectedError,
    NotFoundError,
    RejectedError,
    TransportError,
    UnsupportedError,
    WrongDestinationError,
)
from ..models import Account, Order, Position, Quote
from ..venue import Capabilities, Venue

__all__ = [
    'BracketGroup',
    'IbkrVenue',
    'IBKR_DOCUMENTED_GATEWAY',
    'IBKR_DOCUMENTED_TWS',
    'PAPER_ACCOUNT_PREFIXES',
    'looks_like_paper',
    'IB_ORDER_TYPES',
    'IB_TIF',
]

#: IBKR's documented defaults. An installation may differ -- QJ's Studio runs
#: the live gateway on 4002 -- so these are offered by name, never applied
#: silently. Pass one explicitly: ``port=IBKR_DOCUMENTED_GATEWAY[Env.LIVE]``.
IBKR_DOCUMENTED_GATEWAY = {Env.LIVE: 4001, Env.PAPER: 4002}
IBKR_DOCUMENTED_TWS = {Env.LIVE: 7496, Env.PAPER: 7497}

#: Prefixes IB uses for simulated accounts: DU for an individual paper
#: account, DF for an advisor one. A live account has no D.
PAPER_ACCOUNT_PREFIXES = ('DU', 'DF')


def looks_like_paper(account) -> bool:
    """Whether an IB account id is a simulated one."""
    return str(account or '').upper().startswith(PAPER_ACCOUNT_PREFIXES)


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


@dataclass
class BracketGroup:
    """A parent order and the protective children that belong to it.

    ``oca_group`` is what IB uses to make the children cancel each other, so
    it is also how this registry is rebuilt after a reconnect: the group name
    survives in IB's own copy of the orders.
    """

    parent_id: str
    symbol: str
    oca_group: str = ''
    child_ids: list = field(default_factory=list)
    recovered: bool = False

    def as_dict(self) -> dict:
        return {
            'parent_id': self.parent_id,
            'symbol': self.symbol,
            'oca_group': self.oca_group,
            'child_ids': list(self.child_ids),
        }

    @classmethod
    def from_dict(cls, d) -> BracketGroup:
        return cls(
            parent_id=str(d.get('parent_id') or ''),
            symbol=str(d.get('symbol') or ''),
            oca_group=str(d.get('oca_group') or ''),
            child_ids=[str(c) for c in (d.get('child_ids') or [])],
        )


class IbkrVenue(Venue):
    """IBKR. Requires ``ib_insync`` and a running gateway or TWS.

        venue = IbkrVenue(Env.PAPER, client_id=7788, port=4001)
        with venue:
            venue.limit('MNQZ6', Side.BUY, 2, 20150.25)

    ``port`` is required and the environment is checked against the account IB
    hands back, so a port that points at the wrong book fails at connect time
    rather than at the first order.
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
        'reduce_only is not an IB concept',
    )

    def __init__(
        self,
        env,
        *,
        client_id=None,
        port=None,
        host='127.0.0.1',
        account=None,
        timeout=20.0,
        ib=None,
        contract_factory=None,
        verify_destination=True,
        auto_reconnect=True,
        state_path=None,
    ) -> None:
        super().__init__(env)
        if client_id is None and ib is None:
            raise ConfigError(
                'client_id is required. IB disconnects an existing session '
                'when a second one uses the same id, and this fleet already '
                'has ids in use for the running desks -- pick one nothing '
                'else uses.',
                venue=self.name,
            )
        if port is None:
            raise ConfigError(
                'port is required. IB installations disagree about which port '
                'is paper and which is live -- IBKR documents gateway live on '
                '%d and paper on %d, and real installations run them the '
                'other way round -- so this venue will not guess. Check which '
                'gateway is listening and pass it, or pass '
                'IBKR_DOCUMENTED_GATEWAY[%r] if yours is a default install. '
                'The account is verified after connecting either way.'
                % (
                    IBKR_DOCUMENTED_GATEWAY[Env.LIVE],
                    IBKR_DOCUMENTED_GATEWAY[Env.PAPER],
                    self.env,
                ),
                venue=self.name,
            )
        self.client_id = client_id
        self.host = host
        self.port = int(port)
        self.account = account
        self.timeout = timeout
        self.verify_destination = verify_destination
        self.auto_reconnect = auto_reconnect
        self.state_path = pathlib.Path(state_path).expanduser() if state_path else None
        self._ib = ib
        self._contract_factory = contract_factory
        self._brackets: dict[str, BracketGroup] = {}
        self._flatten_contract = None
        if self.state_path is not None:
            self._load_state()

    # ── lifecycle ───────────────────────────────────────────────────────────
    def connect(self) -> Venue:
        if self._ib is not None and getattr(self._ib, 'isConnected', bool)():
            return self
        if self._ib is None:
            try:
                from ib_insync import IB
            except ImportError as exc:
                raise ConfigError(
                    'ib_insync is not installed; pip install "anybroker[ibkr]"',
                    venue=self.name,
                ) from exc
            self._ib = IB()
        try:
            self._ib.connect(
                self.host, self.port, clientId=self.client_id, timeout=self.timeout
            )
        except Exception as exc:
            raise TransportError(
                'could not reach IB at %s:%s as client %s (%s)'
                % (self.host, self.port, self.client_id, exc),
                venue=self.name,
            ) from exc
        if self.verify_destination:
            self._verify_destination()
        self.resync()
        return self

    # ── connection state ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        if self._ib is None:
            return False
        try:
            return bool(self._ib.isConnected())
        except Exception:
            return False

    def _require_connection(self, *, for_write: bool) -> None:
        """Make sure there is a socket, without ever resending a write.

        A read can safely reconnect and be asked again. A write cannot: if the
        socket died while an order was in flight, that order is unresolved,
        and reconnecting tells you nothing about whether it was accepted.
        """
        if self.is_connected:
            return
        if for_write:
            raise DisconnectedError(
                'the IB connection to %s:%s is down, so this order was not '
                'sent. Reconnect and read your order and position state '
                'before sending anything -- an order that was in flight when '
                'the socket dropped is unresolved, not failed.'
                % (self.host, self.port),
                venue=self.name,
            )
        if not self.auto_reconnect:
            raise DisconnectedError(
                'the IB connection to %s:%s is down and auto_reconnect is off'
                % (self.host, self.port),
                venue=self.name,
            )
        self.connect()

    # ── bracket state across a reconnect ────────────────────────────────────

    def _after_place(self, spec, order) -> None:
        self._save_state()

    def brackets(self) -> dict:
        """The parent/child groups this venue knows about."""
        return dict(self._brackets)

    def resync(self) -> dict:
        """Rebuild bracket state from IB's own open orders.

        Called on every connect, so a reconnect after the nightly reset picks
        the groups back up rather than starting blind. IB is the authority
        here: anything the registry believed that IB does not report is
        dropped, and anything IB reports that the registry did not know is
        adopted and marked ``recovered``.

        Returns a summary, including ``orphans`` -- orders carrying an OCA
        group whose parent IB no longer reports. Those are not necessarily
        wrong (a parent that filled leaves its stop working) but they are the
        ones worth looking at.
        """
        if not self.is_connected:
            return {
                'connected': False,
                'brackets': 0,
                'orphans': [],
                'working': 0,
                'dropped': [],
            }
        trades = list(self._ib.trades())
        by_id = {str(t.order.orderId): t for t in trades}
        children: dict[str, list] = {}
        for t in trades:
            parent_id = str(getattr(t.order, 'parentId', 0) or '')
            if parent_id and parent_id != '0':
                children.setdefault(parent_id, []).append(t)

        rebuilt: dict[str, BracketGroup] = {}
        for parent_id, kids in children.items():
            parent = by_id.get(parent_id)
            symbol = (
                _local(parent.contract)
                if parent
                else (_local(kids[0].contract) if kids else '')
            )
            oca = next(
                (
                    getattr(k.order, 'ocaGroup', '')
                    for k in kids
                    if getattr(k.order, 'ocaGroup', '')
                ),
                '',
            )
            known = self._brackets.get(parent_id)
            rebuilt[parent_id] = BracketGroup(
                parent_id=parent_id,
                symbol=symbol,
                oca_group=oca,
                child_ids=[str(k.order.orderId) for k in kids],
                recovered=known is None,
            )

        dropped = [k for k in self._brackets if k not in rebuilt]
        orphans = [
            str(t.order.orderId)
            for t in trades
            if getattr(t.order, 'ocaGroup', '')
            and str(getattr(t.order, 'parentId', 0) or '0') not in by_id
        ]
        self._brackets = rebuilt
        self._save_state()
        return {
            'connected': True,
            'brackets': len(rebuilt),
            'recovered': sorted(k for k, g in rebuilt.items() if g.recovered),
            'dropped': sorted(dropped),
            'orphans': sorted(set(orphans)),
            'working': sum(1 for t in trades if _order_of(t, self.name).is_working),
        }

    def unprotected_positions(self) -> list:
        """Open positions with no working stop against them.

        The question worth asking after a socket drop. A bracket whose stop
        was cancelled during a reset window leaves the position naked, and
        nothing in IB tells you that unless you ask.
        """
        working = [o for o in self.list_orders() if o.is_working]
        protected = {
            o.symbol
            for o in working
            if o.order_type
            in (
                OrderType.STOP,
                OrderType.STOP_LIMIT,
                OrderType.TRAILING_STOP,
                OrderType.TRAILING_STOP_LIMIT,
            )
        }
        return [
            p
            for p in self.list_positions()
            if not p.is_flat and p.symbol not in protected
        ]

    # ── optional on-disk state ──────────────────────────────────────────────

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        payload = {
            'env': self.env.value,
            'port': self.port,
            'client_id': self.client_id,
            'brackets': [g.as_dict() for g in self._brackets.values()],
            'submissions': [
                {
                    'key': s.key,
                    'state': s.state,
                    'order_id': s.order_id,
                    'symbol': s.symbol,
                    'fingerprint': s.fingerprint,
                }
                for s in self._submissions.values()
            ],
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(payload, indent=1))
            tmp.replace(self.state_path)
        except OSError:
            pass  # state is a convenience; never fail a trade over it

    def _load_state(self) -> None:
        """Restore what a previous process knew. IB still gets the final say.

        A restored submission marked ``submitted`` is downgraded to
        ``unresolved`` on purpose: this process has not seen the broker
        confirm it, so it must be read rather than trusted.
        """
        if self.state_path is None:
            return
        try:
            payload = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        if payload.get('env') and payload['env'] != self.env.value:
            return
        for raw in payload.get('brackets') or []:
            group = BracketGroup.from_dict(raw)
            if group.parent_id:
                self._brackets[group.parent_id] = group
        from ..venue import Submission

        for raw in payload.get('submissions') or []:
            key = raw.get('key')
            if not key:
                continue
            self._submissions[key] = Submission(
                key=key,
                fingerprint=raw.get('fingerprint') or '',
                symbol=raw.get('symbol') or '',
                state='unresolved',
                order_id=raw.get('order_id') or '',
                detail='restored from %s; not confirmed by IB in this process'
                % self.state_path,
            )

    def _verify_destination(self) -> None:
        """Confirm the account IB handed back matches the environment asked for.

        This is the guard that does not depend on knowing which port is which.
        A paper account carries a D prefix; a live one does not. If the two
        disagree the connection is closed before anything can be sent.
        """
        try:
            accounts = [a for a in (self._ib.managedAccounts() or []) if a]
        except Exception as exc:
            self.close()
            raise TransportError(
                'connected to %s:%s but could not read the account back (%s); '
                'refusing to trade a destination that cannot be verified'
                % (self.host, self.port, exc),
                venue=self.name,
            ) from exc
        if not accounts:
            self.close()
            raise WrongDestinationError(
                'connected to %s:%s but IB reported no accounts, so the '
                'destination cannot be confirmed as %s'
                % (self.host, self.port, self.env.value),
                asked=self.env,
                accounts=accounts,
                port=self.port,
                venue=self.name,
            )
        paper = [a for a in accounts if looks_like_paper(a)]
        live = [a for a in accounts if not looks_like_paper(a)]
        if self.env is Env.PAPER and live:
            self.close()
            raise WrongDestinationError(
                'asked for PAPER and connected to %s:%s, but IB handed back '
                'live account(s) %s. Disconnected without sending anything. '
                'That port is the live gateway on this machine.'
                % (self.host, self.port, ', '.join(live)),
                asked=self.env,
                accounts=accounts,
                port=self.port,
                venue=self.name,
            )
        if self.env is Env.LIVE and not live:
            self.close()
            raise WrongDestinationError(
                'asked for LIVE and connected to %s:%s, but every account IB '
                'returned is a paper one (%s). Disconnected. Orders sent here '
                'would not have reached the market.'
                % (self.host, self.port, ', '.join(paper)),
                asked=self.env,
                accounts=accounts,
                port=self.port,
                venue=self.name,
            )
        if self.account and self.account not in accounts:
            self.close()
            raise WrongDestinationError(
                'account %s was requested but IB at %s:%s only offers %s'
                % (self.account, self.host, self.port, ', '.join(accounts)),
                asked=self.env,
                accounts=accounts,
                port=self.port,
                venue=self.name,
            )

    def close(self) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:
                pass

    @property
    def ib(self) -> Any:
        if self._ib is None:
            raise ConfigError('not connected; call connect() first', venue=self.name)
        self._require_connection(for_write=False)
        return self._ib

    # ── contracts ───────────────────────────────────────────────────────────
    def contract(self, symbol) -> Any:
        """Resolve a symbol to an IB contract.

        A contract handed in by ``flatten`` wins, so closing a position uses
        the very contract IB reported holding it rather than re-qualifying a
        name that might be ambiguous.

        A caller with its own contract rules passes ``contract_factory=``.
        Without one this asks IB to qualify the symbol and refuses an
        ambiguous answer rather than picking a leg.
        """
        if self._flatten_contract is not None:
            symbol = self._flatten_contract
        if self._contract_factory is not None:
            return self._contract_factory(symbol)
        if not isinstance(symbol, str):
            return symbol  # already a contract object
        from ib_insync import Contract

        candidate = Contract(symbol=symbol)
        found = self.ib.qualifyContracts(candidate)
        if not found:
            raise NotFoundError(
                'IB could not qualify %r; pass contract_factory= or hand in '
                'an ib_insync Contract' % symbol,
                venue=self.name,
            )
        if len(found) > 1:
            raise ConfigError(
                '%r is ambiguous at IB (%d matches); be specific or pass '
                'contract_factory=' % (symbol, len(found)),
                venue=self.name,
            )
        return found[0]

    # ── reads ───────────────────────────────────────────────────────────────
    def get_account(self) -> Account:
        account_id = self.account or self._default_account()
        values = {}
        for row in self.ib.accountValues(account_id) or []:
            if row.currency in ('USD', 'BASE', ''):
                values[row.tag] = row.value
        return Account(
            id=account_id,
            venue=self.name,
            cash=_f(values.get('TotalCashValue')),
            equity=_f(values.get('NetLiquidation')),
            open_pnl=_f(values.get('UnrealizedPnL')),
            realized_pnl=_f(values.get('RealizedPnL')),
            raw=values,
        )

    def _default_account(self) -> str:
        accounts = list(self.ib.managedAccounts() or [])
        if not accounts:
            raise NotFoundError('IB reported no managed accounts', venue=self.name)
        if len(accounts) > 1:
            raise ConfigError(
                'this login has %d accounts (%s); say which with account='
                % (len(accounts), ', '.join(accounts)),
                venue=self.name,
            )
        return accounts[0]

    def list_positions(self) -> list[Position]:
        out = []
        for p in (
            self.ib.positions(self.account) if self.account else self.ib.positions()
        ):
            out.append(
                Position(
                    symbol=_local(p.contract),
                    venue=self.name,
                    qty=float(p.position),
                    avg_price=float(p.avgCost or 0.0),
                    raw={
                        'contract': p.contract,
                        'account': getattr(p, 'account', None),
                    },
                )
            )
        return out

    def list_orders(self) -> list[Order]:
        out = []
        for t in self.ib.trades():
            out.append(_order_of(t, self.name))
        return out

    def get_quote(self, symbol) -> Quote:
        contract = self.contract(symbol)
        ticker = self.ib.reqMktData(contract, '', True, False)
        self.ib.sleep(0.4)
        return Quote(
            symbol=_local(contract),
            venue=self.name,
            bid=_none_if_nan(ticker.bid),
            ask=_none_if_nan(ticker.ask),
            last=_none_if_nan(ticker.last),
            raw={'ticker': ticker},
        )

    # ── writes ──────────────────────────────────────────────────────────────
    def _place(self, spec) -> Order:
        # Before anything else, including contract resolution: resolving a
        # symbol is a read, and a read is allowed to reconnect. That would
        # quietly repair the socket and let a write through that the caller
        # should have been told about.
        self._require_connection(for_write=True)
        spec.validate()
        self.capabilities.require_order_type(spec.order_type, self.name)
        if spec.reduce_only:
            raise UnsupportedError(
                'IB has no reduce-only flag; size the closing order yourself',
                venue=self.name,
            )
        contract = self.contract(spec.symbol)
        if spec.is_bracket:
            return self._place_bracket(spec, contract)
        trade = self._submit(self._ib_order(spec), contract)
        return _order_of(trade, self.name)

    def _place_bracket(self, spec, contract) -> Order:
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
            order.ocaType = 1  # cancel the rest on fill
            order.transmit = index == len(children) - 1
            trades.append(self._submit(order, contract))
        result = _order_of(parent_trade, self.name)
        result.children = [_order_of(t, self.name) for t in trades]
        self._brackets[str(parent_id)] = BracketGroup(
            parent_id=str(parent_id),
            symbol=_local(contract),
            oca_group=oca,
            child_ids=[str(t.order.orderId) for t in trades],
        )
        self._save_state()
        return result

    def _ib_order(self, spec) -> Any:
        from ib_insync import Order as IbOrder

        order = IbOrder(
            action=IB_SIDE[spec.side],
            totalQuantity=float(spec.qty),
            orderType=IB_ORDER_TYPES[spec.order_type],
            tif=IB_TIF[spec.time_in_force],
            transmit=True,
        )
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

    def _submit(self, order, contract) -> Any:
        self._require_connection(for_write=True)
        try:
            return self.ib.placeOrder(contract, order)
        except Exception as exc:
            raise self._submit_error(exc) from exc

    def _submit_error(self, exc) -> TransportError:
        """Decide whether a failed submission is ambiguous or merely refused.

        It matters: an ambiguous one locks the key until someone reads the
        broker's state, while a clean refusal leaves the caller free to try
        again. Calling everything ambiguous would make a typo in a symbol as
        obstructive as a half-sent order.

        Connection-shaped failures, and anything that left the socket down,
        are ambiguous. A bad argument is not -- it never reached IB.
        """
        connection_shaped = isinstance(
            exc, (TimeoutError, ConnectionError, OSError, EOFError)
        )
        if connection_shaped or not self.is_connected:
            return TransportError(
                'IB submission failed: %s -- this order may or may not have '
                'been accepted. Read order and position state before sending '
                'anything else.' % exc,
                venue=self.name,
                ambiguous=True,
            )
        return TransportError(
            'IB refused the submission before it was sent: %s' % exc,
            venue=self.name,
            ambiguous=False,
        )

    def cancel(self, order_id) -> Any:
        for trade in self.ib.trades():
            if str(trade.order.orderId) == str(order_id):
                self.ib.cancelOrder(trade.order)
                return _order_of(trade, self.name)
        raise NotFoundError(
            'no IB order %s in this session' % order_id, venue=self.name
        )

    def modify(self, order_id, **changes) -> Any:
        """IB modifies by re-placing the same orderId with new values."""
        for trade in self.ib.trades():
            if str(trade.order.orderId) != str(order_id):
                continue
            order = trade.order
            mapping = {
                'qty': 'totalQuantity',
                'limit_price': 'lmtPrice',
                'stop_price': 'auxPrice',
                'trail_amount': 'auxPrice',
                'trail_percent': 'trailingPercent',
            }
            for key, value in changes.items():
                field = mapping.get(key, key)
                if not hasattr(order, field):
                    raise ConfigError('IB orders have no %r' % key, venue=self.name)
                setattr(order, field, value)
            order.transmit = True
            return _order_of(self._submit(order, trade.contract), self.name)
        raise NotFoundError(
            'no IB order %s in this session' % order_id, venue=self.name
        )

    def flatten(self, symbol) -> Any:
        """Close one symbol at market, sized from the open position."""
        position = self.get_position(symbol)
        if position is None or position.is_flat:
            raise NotFoundError('no open position in %s' % symbol, venue=self.name)
        side = Side.SELL if position.qty > 0 else Side.BUY
        from ..spec import OrderSpec

        spec = OrderSpec(
            symbol,
            side,
            abs(position.qty),
            OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            label='flatten',
        )
        # Through place(), so flattening the same position twice is caught by
        # the duplicate guard instead of selling it twice.
        self._flatten_contract = position.raw.get('contract') or symbol
        try:
            return self.place(spec)
        finally:
            self._flatten_contract = None


# ── helpers ─────────────────────────────────────────────────────────────────


def _order_of(trade, venue) -> Order:
    o, s = trade.order, trade.orderStatus
    reverse = {v: k for k, v in IB_ORDER_TYPES.items()}
    status = getattr(s, 'status', '') or ''
    order = Order(
        id=str(o.orderId),
        venue=venue,
        symbol=_local(trade.contract),
        side=Side.BUY if o.action == 'BUY' else Side.SELL,
        qty=float(o.totalQuantity or 0.0),
        order_type=reverse.get(o.orderType),
        status=status,
        filled_qty=float(getattr(s, 'filled', 0.0) or 0.0),
        avg_fill_price=float(getattr(s, 'avgFillPrice', 0.0) or 0.0),
        limit_price=_none_if_zero(getattr(o, 'lmtPrice', None)),
        stop_price=_none_if_zero(getattr(o, 'auxPrice', None)),
        client_order_id=getattr(o, 'orderRef', None) or None,
        raw={'order': o, 'status': s, 'contract': trade.contract},
    )
    if status.lower() in ('rejected', 'inactive') and getattr(s, 'whyHeld', None):
        raise RejectedError(
            'IB rejected order %s: %s' % (o.orderId, s.whyHeld),
            venue=venue,
            detail=order,
        )
    return order


def _local(contract) -> str:
    return (
        getattr(contract, 'localSymbol', None)
        or getattr(contract, 'symbol', None)
        or str(contract)
    )


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _none_if_zero(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value == 0.0 else value


def _none_if_nan(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else value
