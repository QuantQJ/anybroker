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

Not here yet: Alpaca equities (ccxt's Alpaca is the crypto side only),
Tradier, TradeStation, market data beyond a single quote.
