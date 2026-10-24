# anybroker

One interface over the brokers you actually trade.

```python
from anybroker import open_venue, Env, Side

ib = open_venue("ibkr", Env.PAPER, client_id=7788, port=4002)  # see below
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
└── native venues   → IBKR, Tradovate, Alpaca   (Tradier, TradeStation next)
```

```python
tv = open_venue("tradovate", Env.PAPER, credential_path="/secure/key.json")
tv.limit("MNQZ6", Side.BUY, 2, 20150.25)
tv.place(OrderSpec("MNQZ6", Side.BUY, 2, take_profit=20200.0, stop_loss=20050.0))
```

Tradovate's paper environment is a different host with different credentials,
so a venue built for paper has no route to the live book — not because a flag
says so, but because it is a different server.

```python
al = open_venue("alpaca", Env.PAPER, api_key="...", secret_key="...")
al.limit("AAPL", Side.BUY, 10, 227.50)
al.place(OrderSpec("AAPL", Side.BUY, 10, take_profit=240.0, stop_loss=220.0))
```

**Alpaca enforces idempotency itself.** `client_order_id` is unique server
side, so passing yours through means the broker's own guarantee backs up the
one in `place()` — and when Alpaca refuses a repeat (error code `40010001`),
you get the same `DuplicateOrderError`, carrying the order it already has.

Two things Alpaca cannot do, refused rather than substituted: a trailing
*stop-limit*, and good-til-date. It has `opg` and `cls` instead — at the open
and at the close — which this library has no neutral word for, so they go
through `extra`. Note that ccxt's `alpaca` is Alpaca's **crypto** API; this
venue is the stocks and options side, which ccxt has never covered.

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

For a ccxt venue these are read from the exchange's own `has` table — market
and limit included, rather than assumed — so an order type the exchange does
not advertise is refused before the order is built.

That is only as good as `has`. An exchange that under-reports will have a
capability refused that it could in fact do, and one that over-reports will
have it accepted and then rejected at the venue. Treat it as the exchange's
own claim, not as a guarantee.

## IBKR

```python
from anybroker import open_venue, Env

ib = open_venue("ibkr", Env.LIVE, client_id=7788, port=4001, account="U1234567")
```

`client_id` is **required and has no default.** IB disconnects an existing
session when a second one connects with the same id, so a default here would
knock whatever is already running offline. Pick an id nothing else uses.

`port` is **required** — there is no default and no `tws=` shortcut. IBKR's
documented defaults are:

| | IB Gateway | TWS |
| --- | --- | --- |
| live | 4001 | 7496 |
| paper | 4002 | 7497 |

**Installations vary** and a gateway can be configured either way round, so
check your own rather than trusting that table:

```sh
lsof -nP -iTCP -sTCP:LISTEN | grep -E '4001|4002|7496|7497'
```

The constants are available by name if yours is a default install:

```python
from anybroker.venues.ibkr import IBKR_DOCUMENTED_GATEWAY, IBKR_DOCUMENTED_TWS

open_venue("ibkr", Env.LIVE, client_id=7788, port=IBKR_DOCUMENTED_GATEWAY[Env.LIVE])
```

Because the port cannot be trusted, the venue reads the account back after
connecting and refuses a mismatch — which is the guard that does not depend on
getting it right. Symbols are qualified through IB, and an
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

The second call reads the order back **from the venue** rather than replaying
the first reply. One caveat worth stating: if the venue cannot be reached, or
returns nothing for that id, it falls back to the order recorded when it was
sent — so a network blip cannot turn a duplicate check into a second order,
but the state you get back is the last known one rather than guaranteed fresh.

Three outcomes:

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
type, prices, bracket). **A rule that fires daily should set its own id,
scoped to the day.**

`label` and `extra` are **not** part of that digest. `label` is pure
annotation, so that is safe. `extra` is not: it passes venue-specific fields
straight through and can change what the order does — Alpaca's `opg` and
`cls` timings go through it, for instance. So two orders differing only in
`extra` share a fingerprint and the second will be treated as a duplicate.
**If `extra` changes the order, give it its own `client_order_id`.**

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
    port=4002,  # a NON-default live gateway; check your own
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
pip install "git+https://github.com/QuantQJ/anybroker.git"

# with a venue's SDK
pip install "anybroker[crypto] @ git+https://github.com/QuantQJ/anybroker.git"
pip install "anybroker[ibkr] @ git+https://github.com/QuantQJ/anybroker.git"
pip install "anybroker[alpaca] @ git+https://github.com/QuantQJ/anybroker.git"
pip install "anybroker[tradovate] @ git+https://github.com/QuantQJ/anybroker.git"
pip install "anybroker[all] @ git+https://github.com/QuantQJ/anybroker.git"
```

`[all]` deliberately leaves out `tradovate-client`, because it is not on PyPI
yet — install it from source:

```sh
pip install "git+https://github.com/QuantQJ/tradovate-client.git"
```

```python
from anybroker import available

available()
# {'native': ['alpaca', 'ibkr'],
#  'native_missing': {'tradovate': 'pip install "anybroker[tradovate]"'},
#  'all_native': ['alpaca', 'ibkr', 'tradovate'],
#  'ccxt': [...]}
```

`available()` reports only the venues whose driver actually imports, so it is
a readiness check rather than a catalogue.

Python 3.10 through 3.13, which is what CI tests. `ccxt` and `alpaca-py` both
require 3.10, and `ib_insync` does not import on 3.14 — its `eventkit`
dependency fails there — so 3.10 to 3.13 is the real window.

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

- Tradier, TradeStation
- Market data beyond a single quote

## Disclaimer

Not affiliated with any broker or exchange. Trading involves risk of loss.
This is software, not advice.

## Licence

MIT.
