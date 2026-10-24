# Changelog

## 0.1.0 — unreleased

First cut. One interface, two kinds of venue.

**The shape**

- `OrderSpec` — a venue-neutral order, validated before any venue sees it.
  Market, limit, stop, stop-limit, trailing stop, trailing stop-limit, and
  brackets. Nothing in it invents a price: a target or a stop is the caller's
  own number or it is absent.
- `Venue` — the interface, using the verbs QJ's eight hand-written adapters had
  already converged on: `place`, `flatten`, `get_position`, `list_positions`,
  `get_account`.
- `Capabilities` — what a venue declares it can do, asked before it is tried.
  `supports_paper` is the important one: a venue with no paper environment
  says so rather than sending the order to the live book.
- `Env` is required everywhere with no default.

**Venues**

- `CcxtVenue` — any of ccxt's 104 exchanges. Capabilities read from the
  exchange's own `has` table and `urls['test']`, not assumed. ccxt errors
  translated to this library's exceptions, with a timeout on a write marked
  ambiguous rather than retried.
- `IbkrVenue` — ccxt has never had IBKR. Full order set against the strings
  read from the installed `ib_insync` (`MKT`, `LMT`, `STP`, `STP LMT`,
  `TRAIL`, `TRAIL LIMIT`). Brackets use the parent/child pattern with a shared
  OCA group, parent held on `transmit=False` until the last child releases the
  group, so nothing is working at the exchange until every leg is accepted.
  `client_id` is required with no default, because IB disconnects an existing
  session when a second one reuses an id.

- `TradovateVenue` — the second native venue, through `tradovate-client`.
  Driven with plain strings (`demo`/`live`, `Buy`/`Sell`, `Market`, `Day`),
  every one a value from Tradovate's published enums, so the mapping is
  asserted against a fake client. Brackets go out as Tradovate's own
  one-sends-other, so there is no window where a naked entry is working. A
  percent trail is refused rather than guessed at.

**Idempotency and reconnection**

- The same `OrderSpec` submitted twice does not become two orders. `place()`
  is now a template on `Venue`, so every venue inherits it: a working order is
  returned after being re-read from the broker, a finished one raises
  `DuplicateOrderError`, and one that never got an answer raises
  `UnresolvedOrderError`. Identity is `spec.idempotency_key` — your
  `client_order_id`, else a digest of what the order instructs.
- A write that fails ambiguously locks its key until `resolve()` or `forget()`.
  A clean refusal does not: a bad symbol leaves you free to retry, which is
  why `_submit` distinguishes connection-shaped failures from bad arguments
  rather than calling everything ambiguous.
- `flatten()` goes through the same guard, so flattening a position twice does
  not sell it twice.
- IBKR: a read may reconnect and be asked again; a write never does, and it is
  checked before contract resolution, because resolving a symbol is a read and
  would otherwise repair the socket behind the caller's back.
- IBKR: `resync()` rebuilds bracket groups from IB's own open orders on every
  connect, reports what it recovered, dropped and could not place, and
  `unprotected_positions()` answers the question that matters after a drop —
  which open positions have no working stop. Optional `state_path` persists
  the registry so a restarted process is not blind; anything restored from it
  is marked unresolved, because this process never saw IB confirm it.

- `AlpacaVenue` -- the third native venue. ccxt's `alpaca` is Alpaca's crypto
  API; this is the stocks and options side, which nothing else covers. Built
  on `alpaca-py`, so the order types, times in force, order classes and
  request models come from the vendor's own package, read from the installed
  SDK rather than assumed.
  - Idempotency is Alpaca's as well as ours: `client_order_id` is passed
    through because Alpaca enforces uniqueness on it, and its duplicate
    rejection (code 40010001) becomes `DuplicateOrderError` carrying the order
    it already has, looked up by that id.
  - Brackets are one request -- `order_class` with `take_profit` and
    `stop_loss` on the entry. Two children is a bracket, one is a
    one-triggers-other.
  - Both trailing forms work, unlike Tradovate: `trail_price` and
    `trail_percent`.
  - A trailing stop-limit and good-til-date are refused with an explanation
    rather than substituted; `opg` and `cls` reach through `extra`.
- `Order.is_working` and `is_done` now compare on a flattened status, so a
  venue's spelling never decides whether an order counts as live:
  `partially_filled`, `PreSubmitted` and `open` all mean the same thing.

Not here yet: Tradier, TradeStation, market data beyond a single quote.
