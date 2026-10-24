# anybroker

One interface over the brokers you actually trade.

```python
from anybroker import open_venue, Env, Side

ib = open_venue("ibkr", Env.PAPER, client_id=7788)
with ib:
    ib.limit("MNQZ6", Side.BUY, 2, 20150.25)
    print(ib.list_positions())

kr = open_venue("krakenfutures", Env.LIVE, api_key="...", secret="...")
kr.limit("BTC/USD:USD", Side.BUY, 1, 60000.0)
```

Both objects answer the same calls.

## What this is, and what it refuses to be

**It does not reimplement crypto.** `ccxt` already covers 104 exchanges with
every order type, unified symbols and rate limiting, maintained by a project
with tens of thousands of users. Coinbase, Kraken, Kraken Futures, OKX and the
rest come straight from ccxt through `CcxtVenue`.

**What ccxt has never had is the futures and retail brokers.** No Tradovate.
No IBKR. No Tradier, TradeStation or NinjaTrader. Those are this library's own
venues, and they are the reason it exists.

```
anybroker
├── ccxt            → 104 crypto exchanges, somebody else's problem
└── native venues   → IBKR, Tradovate  (Alpaca equities, Tradier next)
```

```python
tv = open_venue("tradovate", Env.PAPER, credential_path="/secure/key.json")
tv.limit("MNQZ6", Side.BUY, 2, 20150.25)
tv.place(OrderSpec("MNQZ6", Side.BUY, 2, take_profit=20200.0, stop_loss=20050.0))
```

Tradovate's paper environment is a different host with different credentials,
so a venue built for paper has no route to the live book — not because a flag
says so, but because it is a different server.

## Paper and live are different destinations

`env` is required everywhere and has no default. A venue built for paper has no
code path to the live account — for IBKR that is a different port, for a ccxt
exchange it is sandbox mode.

And when a venue has **no** paper environment, it says so:

```python
open_venue("someexchange", Env.PAPER)
# UnsupportedError: someexchange has no paper environment;
# pass Env.LIVE deliberately or use a different venue
```

That beats silently sending a test order to a live book.

## One order, described once

```python
from anybroker import OrderSpec, OrderType, Side, TimeInForce

spec = OrderSpec(
    "MNQZ6",
    Side.BUY,
    2,
    OrderType.LIMIT,
    limit_price=20150.25,
    time_in_force=TimeInForce.GTC,
)

venue.place(spec)
```

Validated before any venue sees it — a limit order without a price, a trailing
stop without a distance, or a market order carrying a price all raise
`ConfigError` and nothing is sent.

Convenience methods for the common cases:

```python
venue.market("MNQZ6", Side.BUY, 2)
venue.limit("MNQZ6", Side.BUY, 2, 20150.25)
venue.stop("MNQZ6", Side.SELL, 2, 20100.00)
venue.stop_limit("MNQZ6", Side.SELL, 2, 20095.00, 20100.00)
venue.trailing_stop("MNQZ6", Side.SELL, 2, trail_amount=25.0)
venue.trailing_stop("MNQZ6", Side.SELL, 2, trail_percent=1.5)
```

## Brackets

An entry with a target and a protective stop, as one order:

```python
venue.place(OrderSpec("MNQZ6", Side.BUY, 2, take_profit=20200.00, stop_loss=20050.00))
```

Both prices are yours. **This library never calculates a stop distance or a
target** — that is a trading decision, not a client's job.

On IBKR this uses the parent/child pattern properly: the entry goes out held,
each child carries the parent's id and a shared OCA group so one filling
cancels the other, and only the last child releases the group. Nothing is
working at the exchange until every leg has been accepted.

A venue that cannot express a bracket raises rather than sending a naked entry.

## Capabilities, asked not discovered

```python
venue.capabilities.supports_paper  # does it have a sandbox at all
venue.capabilities.order_types  # what it will actually accept
venue.capabilities.supports_brackets
venue.capabilities.supports_modify
```

For a ccxt venue these are read from the exchange's own `has` table, so an
order type the exchange cannot do is refused before it is built, not after it
is rejected.

## IBKR

```python
from anybroker import open_venue, Env

ib = open_venue("ibkr", Env.LIVE, client_id=7788, account="U1234567")
```

`client_id` is **required and has no default.** IB disconnects an existing
session when a second one connects with the same id, so a default here would
knock whatever is already running offline. Pick an id nothing else uses.

| | port |
| --- | --- |
| `Env.PAPER` | 4001 (gateway) or 7497 with `tws=True` |
| `Env.LIVE` | 4002 (gateway) or 7496 with `tws=True` |

Pass `port=` for anything else. Symbols are qualified through IB, and an
ambiguous one raises instead of a leg being picked for you — pass
`contract_factory=` or an `ib_insync` `Contract` if you have your own rules.

## Sending the same order twice

The same `OrderSpec` does not become two orders.

```python
spec = OrderSpec("MNQZ6", Side.BUY, 2, OrderType.LIMIT, limit_price=20150.25)

first = venue.place(spec)
second = venue.place(spec)  # nothing is sent
assert second.id == first.id
```

The second call reads the order back **from the venue**, not from a cache, so
what you get reflects the broker's current state. Three outcomes:

| the first one is | `place()` does |
| --- | --- |
| still working | returns it, sends nothing |
| finished | raises `DuplicateOrderError` |
| never answered | raises `UnresolvedOrderError` |

A finished order raises rather than guessing: a repeat is either a bug or a
genuine second entry, and only you know which. Say which you meant:

```python
venue.place(spec, allow_duplicate=True)  # I meant it
venue.place(OrderSpec(..., client_order_id="long0558:2026-10-24"))  # better
```

Identity comes from `spec.idempotency_key` — your `client_order_id` when you
set one, otherwise a digest of what the order instructs (symbol, side, size,
type, prices, bracket). `label` and `extra` are excluded, because they
describe an order rather than change it. **A rule that fires daily should set
its own id, scoped to the day.**

### The unanswered write

A write that fails ambiguously locks its key until you have looked:

```python
try:
    venue.place(spec)
except TransportError as exc:
    if exc.ambiguous:
        # may be working. Read before writing anything else.
        print(venue.unresolved())

venue.resolve(key, order_id=4242)  # found it
venue.forget(key)  # confirmed it never landed
```

A clean refusal — a bad symbol, an unsupported type — is *not* ambiguous and
leaves you free to try again immediately.

## Surviving a disconnect

IB drops its socket during the nightly reset. The venue expects that.

```python
ib = open_venue(
    "ibkr",
    Env.LIVE,
    client_id=7788,
    port=4002,
    state_path="~/.local/state/anybroker/ibkr.json",
)
```

**A read may reconnect and answer. A write never does.** Reconnecting tells
you nothing about an order that was in flight when the socket died, so a write
on a dead connection raises `DisconnectedError` and sends nothing:

> the IB connection to 127.0.0.1:4002 is down, so this order was not sent.
> Reconnect and read your order and position state before sending anything —
> an order that was in flight when the socket dropped is unresolved, not failed.

**Brackets survive the drop.** Every connect runs `resync()`, which rebuilds
the parent/child groups from IB's own open orders — the OCA group name
survives in IB's copy, which is what makes this possible:

```python
venue.resync()
# {'connected': True, 'brackets': 3, 'recovered': ['41'],
#  'dropped': [], 'orphans': ['55'], 'working': 7}

venue.brackets()  # parent id -> group
venue.unprotected_positions()  # the question actually worth asking
```

`unprotected_positions()` lists open positions with no working stop against
them. A bracket whose stop was cancelled during a reset window leaves the
position naked and nothing in IB tells you unless you ask. A target alone does
not count — only a stop does.

With `state_path` set, the registry is written to disk, so a process restarted
by launchd at 1:36am does not start blind. IB is still the authority: the file
is a hint that `resync()` checks, and any submission restored from it is
marked **unresolved**, because this process never saw the broker confirm it.

## Errors

Everything raises. Nothing returns a dictionary with an error key in it that a
caller could mistake for a result.

```python
from anybroker import (
    BrokerError,
    ConfigError,
    UnsupportedError,
    NotFoundError,
    AuthError,
    ThrottledError,
    RejectedError,
    TransportError,
)

try:
    venue.market("MNQZ6", Side.BUY, 1)
except TransportError as exc:
    if exc.ambiguous:
        # The order may already be working. Read before you write.
        print(venue.working_orders())
```

A write that fails ambiguously is never retried, because a timeout on an order
is not a rejection.

## Install

```sh
pip install anybroker                  # the interface, no dependencies
pip install "anybroker[crypto]"        # + ccxt
pip install "anybroker[ibkr]"          # + ib_insync
pip install "anybroker[all]"
```

```python
from anybroker import available

available()  # what you can open, given what is installed
```

## Tests

```sh
pip install -e ".[dev]"
pytest
```

Fake IB and fake ccxt. No network, no credentials, no gateway, no account.

## Not here yet

- Alpaca equities (ccxt's Alpaca is the crypto side only)
- Tradier, TradeStation
- Market data beyond a single quote

## Disclaimer

Not affiliated with any broker or exchange. Trading involves risk of loss.
This is software, not advice.

## Licence

MIT.
