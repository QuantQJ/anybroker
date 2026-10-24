# Trading futures with this thing

The practical stuff — how to set it up, and the handful of broker behaviours
worth knowing before you leave a bot running unattended.

If you read two sections, read **client ids** and **which port is live**.

---

## Start here

```sh
pip install "anybroker[ibkr]"        # Interactive Brokers
pip install "anybroker[tradovate]"   # Tradovate
pip install "anybroker[alpaca]"      # Alpaca stocks
pip install "anybroker[crypto]"      # everything crypto, via ccxt
```

A first script that can't place anything:

```python
from anybroker import open_venue, Env

ib = open_venue("ibkr", Env.PAPER, client_id=8765, port=4001)
with ib:
    print(ib.get_account())
    print(ib.list_positions())
    print(ib.working_orders())
```

Then orders:

```python
from anybroker import Side, OrderSpec

ib.limit("MNQZ6", Side.BUY, 1, 20150.25)
ib.stop("MNQZ6", Side.SELL, 1, 20100.00)
ib.trailing_stop("MNQZ6", Side.SELL, 1, trail_amount=25.0)

# an entry with a target and a stop, as one order
ib.place(OrderSpec("MNQZ6", Side.BUY, 1, take_profit=20200.00, stop_loss=20100.00))
```

Both prices are yours. This library never calculates a stop distance or a
target — that's a trading decision, not a client's job.

The same five calls work on Tradovate, Alpaca and any ccxt exchange. Point
them at a different venue and nothing else changes.

---

## Client ids

Interactive Brokers identifies each connection by a `client_id`. **Connect with
an id another session is already using and IB disconnects that session.** No
warning on either side.

So `client_id` here is required with no default. Pick something nothing else
uses — a date works well — and keep a note of what runs on what:

```python
ib = open_venue("ibkr", Env.LIVE, client_id=20261024, port=4002)
```

Check what's already taken in your own code before you choose:

```sh
grep -rhoE "(clientId|client_id|IB_CID)[^0-9]{1,6}[0-9]+" --include='*.py' . \
  | grep -oE '[0-9]+$' | sort -n -u
```

---

## Which port is live

IBKR's documented defaults:

| | live | paper |
| --- | --- | --- |
| IB Gateway | 4001 | 4002 |
| TWS | 7496 | 7497 |

**Installations vary** — a gateway can be configured either way round, so the
table above is a starting point, not a fact about your machine. Check:

```sh
lsof -nP -iTCP -sTCP:LISTEN | grep -E '4001|4002|7496|7497'
```

Because this matters, `port` is required and the library verifies the
destination *after* connecting: it reads the account back and checks it against
the environment you asked for. IB paper accounts are prefixed `D` (`DU…`,
`DF…`), live accounts aren't.

```python
open_venue("ibkr", Env.PAPER, client_id=1, port=4002).connect()
# WrongDestinationError: asked for PAPER and connected to 127.0.0.1:4002,
# but IB handed back live account(s) U1234567. Disconnected without
# sending anything.
```

It hangs up before anything can be sent. That check is the only guard that
doesn't depend on knowing which port is which, which is why it's there.

---

## The nightly reset

IB resets around 01:00 local and the socket goes with it. Three habits:

**Run under a supervisor.** launchd with `KeepAlive`, systemd, whatever you
like. Your process will go down at some point; what matters is that it comes
back.

**Never retry a write across a disconnect.** A timeout on an order isn't a
rejection — the order may already be working. Resending doubles the position;
assuming it failed leaves one unmanaged. The library won't guess:

```python
try:
    venue.place(spec)
except TransportError as exc:
    if exc.ambiguous:
        print(venue.unresolved())  # read before you write again
```

Reads are safe to repeat, and the venue reconnects for those on its own.

**Rebuild bracket state from the broker, not from memory.** `resync()` runs on
every connect and rediscovers which stop belongs to which entry from IB's own
open orders:

```python
venue.resync()
# {'connected': True, 'brackets': 3, 'recovered': ['41'],
#  'dropped': [], 'orphans': ['55'], 'working': 7}
```

Then the question that actually matters:

```python
venue.unprotected_positions()  # open positions with no working stop
```

A profit target doesn't count as protection — only a stop does.

Pass `state_path=` and the registry is written to disk, so a process restarted
at 01:36 doesn't start blind. The broker stays the authority; the file is a
hint that `resync()` checks.

---

## Paper and live are different servers

Not a checkbox:

- **Tradovate** — `demo.tradovateapi.com` and `live.tradovateapi.com`, separate
  credentials and separate tokens. A demo token can't reach the live account.
- **Alpaca** — separate hosts *and* separate keys. Paper key ids start `PK`,
  live ones `AK`, and neither works against the other host.
- **IBKR** — different ports on the same machine, which is why it's the one to
  be careful with.

Here, the environment is a property of the venue object with no default
anywhere, so a venue built for paper has no code path to the live account.

One thing to watch in your own config: if a file holds a base URL or an
endpoint, **read it**. A key file saying `https://api.alpaca.markets` is
pointed at live regardless of what the surrounding code is called.

---

## Know your actual costs

Get commission from your own fills rather than a rate card — the two often
differ, and on a strategy with a small edge that difference is the strategy.

On MNQ at $2.00 a point, four sides of commission is single-digit dollars. If
your average trade is under a couple of points, that number decides whether
you're profitable.

Measure your crossing cost separately — the gap between the price you wanted
and the price you got. It's usually larger than commission.

---

## Scope your order ids to the day

The same order sent twice is usually a bug and occasionally a real second
entry. Only you know which, so say so:

```python
venue.place(OrderSpec("MNQZ6", Side.BUY, 1, client_order_id="long0558:2026-10-24"))
```

With an id scoped like that, today's entry and yesterday's are different
orders, and a script that fires twice in one day sends once. Without one the
library falls back to a digest of the order itself, which catches a repeat
inside a session but can't tell today from tomorrow.

Alpaca enforces this server-side — it rejects a reused `client_order_id`
outright, which is the behaviour every broker should have.

---

## Brackets go out atomically

The gap between being in a position and having a working stop is the most
expensive moment in automated trading. Don't build a system that has one.

Each venue gets there differently; this library hides the difference:

- **IBKR** — the parent goes out held, each child carries the parent's id and a
  shared OCA group, and only the last child transmits, releasing the group at
  once. Nothing is live at the exchange until every leg is accepted.
- **Tradovate** — one-sends-other; the children travel with the entry in a
  single request.
- **Alpaca** — `order_class="bracket"`, with the target and stop on the entry.

All three from the same call:

```python
venue.place(OrderSpec(symbol, Side.BUY, qty, take_profit=..., stop_loss=...))
```

A venue that can't express a bracket raises rather than sending a naked entry.

---

## Smaller things

**Use full contract symbols.** `MNQZ6`, not `MNQ`. Or pass your own
`contract_factory`. An ambiguous symbol raises rather than having a leg picked
for you.

**Watch your gateway version.** IB desupports builds on a schedule and warns
about it in a startup log line that's easy to miss.

**`ib_insync` doesn't import on Python 3.14** — its `eventkit` dependency fails
there. Stay on 3.13 or below for the `[ibkr]` extra; the other venues are
unaffected.

**Check whether a cluster of results is one result.** A broad sweep of timed
entries and exits will often return many combinations that are really the same
trade with the clock nudged a minute. Group them before you believe the count.

**Log what you refused, not just what you filled.** A desk that records why it
*didn't* trade can be measured. One that only records fills can't.

---

## What the library guarantees

- **Nothing invents a number.** No default stop, no default target, no assumed
  tick size. If you didn't supply it and the venue didn't publish it, you get
  an error rather than a guess.
- **Failures raise.** No function returns a dict with an error key that you
  might mistake for a result.
- **What a venue can't do is refused, not substituted.** Ask Alpaca for a
  trailing stop-limit and you get an explanation, not something adjacent.
- **Every enum value is traceable** to the broker's own published material,
  with a test asserting the whole set — so a vendor change breaks the build
  instead of an order.

---

## Not advice

This is plumbing. It executes what you tell it, faithfully, including mistakes.
Trading futures involves risk of loss and leverage makes that worse.
