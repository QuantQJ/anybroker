# Contributing

Thanks for looking.

## Running the tests

```sh
python -m pip install -e ".[dev]"
pytest -q
```

The suite needs **no network, no credentials and no broker account**. Every
venue is exercised against a fake. If a test ever needs a live connection to
pass, it is the wrong test.

## What CI checks

Four jobs, all of which must be green:

| job | what it does |
| --- | --- |
| `test` | the suite on Python 3.9 through 3.13 |
| `types` | `mypy src` — the package ships `py.typed`, so the annotations are a promise |
| `lint` | `ruff check` and `ruff format --check` |
| `build` | the wheel builds and `twine check` passes |

## House rules

**Nothing invents a number.** No default stop distance, no default target, no
assumed tick size, no made-up threshold. If a value is not supplied by the
caller or published by the venue, the code asks or raises. This is a broker
client, not a strategy.

**Every enum value is traceable to the venue's own documentation.** When you
add one, say in a comment where it came from, and add it to the test that
asserts the whole set. A vendor changing a value should surface as a failing
test, not a rejected order.

**Failures raise.** No function returns a dictionary with an error key in it.
That mistake places orders.

**A write is never retried blindly.** A timeout on an order is not a
rejection — the order may already be working. Retries are allowed only when
the failure proves nothing was sent, and otherwise the caller gets an error
marked ambiguous so it knows to read state before writing again.

**Paper and live are different objects, not a flag.** If a venue has no paper
environment, say so in its capabilities and raise when one is asked for.

## Adding a venue

1. Subclass `Venue`, declare `Capabilities` honestly, and translate
   `OrderSpec` at your own edge.
2. Refuse what the venue cannot do rather than substituting something near it.
3. Write a fake client in `tests/` and assert the exact payload your venue
   sends. No network.
