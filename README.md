# anybroker

Python interface for orders, positions, accounts, and TradingView webhooks across
IBKR, Tradovate, Alpaca, and CCXT venues.

## Install

Python 3.10–3.13. Install the SDK for the venue you use:

```sh
pip install "anybroker[ibkr] @ git+https://github.com/QuantQJ/anybroker.git@v0.1.0"
pip install "anybroker[tradovate] @ git+https://github.com/QuantQJ/anybroker.git@v0.1.0"
pip install "anybroker[alpaca] @ git+https://github.com/QuantQJ/anybroker.git@v0.1.0"
pip install "anybroker[crypto] @ git+https://github.com/QuantQJ/anybroker.git@v0.1.0"
# All venue SDKs:
pip install "anybroker[all] @ git+https://github.com/QuantQJ/anybroker.git@v0.1.0"
```

The Tradovate extra installs `tradovate-client` from a pinned Git commit.
`ib_insync` depends on `eventkit`, which does not import under Python 3.14.

## Read an account

With IB Gateway running and API access enabled:

```python
from anybroker import Env, open_venue

with open_venue("ibkr", Env.PAPER, client_id=7788, port=4002) as broker:
    print(broker.get_account())
    print(broker.list_positions())
    print(broker.working_orders())
```

IBKR requires a `client_id` and `port`. Use a client ID that other sessions are
not using. Gateway defaults are 4002 for paper and 4001 for live; TWS defaults
are 7497 and 7496. Check your gateway configuration and account selection.

Other venue constructors:

```python
tradovate = open_venue(
    "tradovate", Env.PAPER, credential_path="/secure/key.json", account="DEMO1234"
)
alpaca = open_venue("alpaca", Env.PAPER, api_key="...", secret_key="...")
exchange = open_venue("krakenfutures", Env.PAPER, api_key="...", secret="...")
```

`Env` is required. A CCXT venue without sandbox support raises
`UnsupportedError` when asked for `Env.PAPER`.

## Orders

These calls submit orders when used with a connected broker:

```python
from anybroker import OrderSpec, OrderType, Side, TimeInForce

spec = OrderSpec(
    "AAPL",
    Side.BUY,
    1,
    OrderType.LIMIT,
    limit_price=200.0,
    time_in_force=TimeInForce.DAY,
    client_order_id="example-entry-001",
)
order = broker.place(spec)
broker.cancel(order.id)
```

The API includes `market`, `limit`, `stop`, `stop_limit`, `trailing_stop`,
`modify`, `cancel_all`, `flatten`, and `flatten_all`. Add `take_profit` and
`stop_loss` to an `OrderSpec` for a bracket. Symbols, quantities, and prices must
match the broker's requirements. Tradovate futures quantities are whole contracts.

Check `broker.capabilities` for order types, brackets, modification, and paper
support. CCXT capabilities come from the exchange's `has` table. Broker
acceptance also depends on the instrument, account permissions, and market rules.

## Venues

| Venue | Driver | Connection |
| --- | --- | --- |
| `ibkr` | `ib_insync` | TWS or IB Gateway |
| `tradovate` | `tradovate-client` | Tradovate REST API |
| `alpaca` | `alpaca-py` | Alpaca trading API |
| CCXT exchange ID | `ccxt` | Exchange API |

`available()` reports installed drivers. CCXT also has an Interactive Brokers
integration; anybroker's `ibkr` adapter uses the TWS/Gateway socket API.

## Duplicate submissions and recovery

`place()` tracks submissions using `client_order_id`, or a fingerprint of the
order when no ID is provided. Repeating a working order returns its known order;
repeating a finished order raises `DuplicateOrderError`. An ambiguous submission
raises and leaves the key unresolved until `resolve()` or `forget()` is called.
Check broker state before resolving it. Duplicate lookup may return cached state
when the broker cannot be reached.

`extra` fields are excluded from the fingerprint. Assign different
`client_order_id` values when those fields change the intended order. The default
registry is in memory; restarting a process loses that registry. IBKR supports
`state_path` for persistence and restores submissions as unresolved.

IBKR's `resync()` recovers bracket relationships from open orders;
`unprotected_positions()` reports positions without a working stop. These
checks report observed broker state and cannot guarantee future execution.

Tradovate bulk operations select an account before writing and check each
response for rejection. They stop at the first error. Earlier cancellations or
liquidations may already have succeeded; bulk operations are not transactions.

## TradingView webhooks

```sh
anyhook secret
ANYHOOK_SECRET=... ANYHOOK_VENUE=tradovate ANYHOOK_ENV=paper anyhook serve
```

Configure the venue's credentials and account for the server process. Send JSON
or text such as `buy 1 ABCZ6` to `/alert/<secret>`. The server authenticates the
shared secret, queues execution, and records events in SQLite. It uses the same
venue API and submission registry. Journaling is not a guarantee of durable
execution or deduplication across restarts.

## Release status and tests

`v0.1.0` is a GitHub prerelease. Tests exercise simulated broker responses and SDK
request models. This release has not been validated by placing orders in paper
or live broker accounts. Passing tests do not establish that the library is
bug-free.

```sh
pip install -e ".[dev]"
pytest -q
ruff check .
ruff format --check .
mypy src --ignore-missing-imports
```

CI runs tests on Python 3.10–3.13, lint, type checking, and package builds.
Tradier, TradeStation, and market-data streaming are not implemented.

## Licence

MIT. Not affiliated with any broker or exchange.
