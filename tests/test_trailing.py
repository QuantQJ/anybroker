"""Trailing distances: an amount and a percent are different endpoints.

Of the 19 ccxt exchanges with any trailing support, 16 accept only one of the
two forms. Sending the wrong one is a rejection at the exchange, so each venue
declares which it takes and the wrong form is refused before anything is sent.
"""

from __future__ import annotations

import pytest

from anybroker import Env, OrderSpec, OrderType, Side, UnsupportedError
from anybroker.venue import TRAILING_FORMS, Capabilities
from anybroker.venues.alpaca import AlpacaVenue
from anybroker.venues.ccxt_venue import CcxtVenue
from anybroker.venues.ibkr import IbkrVenue
from anybroker.venues.tradovate import TradovateVenue
from fakes import FakeCcxt, FakeIB


def ccxt_venue(**has):
    return CcxtVenue('fake', Env.LIVE, exchange=FakeCcxt(has=has))


def test_the_two_forms():
    assert frozenset({'amount', 'percent'}) == TRAILING_FORMS


# ── what each venue declares ────────────────────────────────────────────────


def test_ibkr_takes_both():
    v = IbkrVenue(Env.PAPER, client_id=1, port=4001, ib=FakeIB(accounts=('DU1',)))
    assert v.capabilities.trailing_forms == frozenset({'amount', 'percent'})


def test_alpaca_takes_both():
    v = AlpacaVenue(Env.PAPER, api_key='k', secret_key='s')
    assert v.capabilities.trailing_forms == frozenset({'amount', 'percent'})


def test_tradovate_takes_an_amount_only():
    v = TradovateVenue(Env.PAPER, client=object())
    assert v.capabilities.trailing_forms == frozenset({'amount'})


# ── ccxt reads the two flags separately ─────────────────────────────────────


def test_a_percent_only_exchange_declares_percent_only():
    """Binance, OKX, Bitget, HTX and others are percent-only."""
    v = ccxt_venue(createTrailingAmountOrder=False, createTrailingPercentOrder=True)
    assert v.capabilities.trailing_forms == frozenset({'percent'})
    assert OrderType.TRAILING_STOP in v.capabilities.order_types


def test_an_amount_only_exchange_declares_amount_only():
    """Bybit, Bitfinex, BTSE and Deribit are amount-only."""
    v = ccxt_venue(createTrailingAmountOrder=True, createTrailingPercentOrder=False)
    assert v.capabilities.trailing_forms == frozenset({'amount'})


def test_an_exchange_with_both_declares_both():
    """Kraken, BingX and Woo take either."""
    v = ccxt_venue(createTrailingAmountOrder=True, createTrailingPercentOrder=True)
    assert v.capabilities.trailing_forms == frozenset({'amount', 'percent'})


def test_an_exchange_with_neither_has_no_trailing_type_at_all():
    v = ccxt_venue(createTrailingAmountOrder=False, createTrailingPercentOrder=False)
    assert v.capabilities.trailing_forms == frozenset()
    assert OrderType.TRAILING_STOP not in v.capabilities.order_types


# ── the wrong form is refused before it is sent ─────────────────────────────


def test_an_amount_sent_to_a_percent_only_exchange_is_refused():
    v = ccxt_venue(createTrailingAmountOrder=False, createTrailingPercentOrder=True)
    with pytest.raises(UnsupportedError) as caught:
        v.trailing_stop('BTC/USD', Side.SELL, 1, trail_amount=100.0)
    assert 'does not take a trailing amount' in str(caught.value)
    assert 'trail_percent=' in str(caught.value), 'it should say what to use'
    assert v.exchange.created == []


def test_a_percent_sent_to_an_amount_only_exchange_is_refused():
    v = ccxt_venue(createTrailingAmountOrder=True, createTrailingPercentOrder=False)
    with pytest.raises(UnsupportedError) as caught:
        v.trailing_stop('BTC/USD', Side.SELL, 1, trail_percent=1.5)
    assert 'does not take a trailing percent' in str(caught.value)
    assert 'trail_amount=' in str(caught.value)
    assert v.exchange.created == []


def test_the_right_form_goes_through():
    v = ccxt_venue(createTrailingAmountOrder=False, createTrailingPercentOrder=True)
    v.trailing_stop('BTC/USD', Side.SELL, 1, trail_percent=1.5)
    assert v.exchange.created[-1]['params']['trailingPercent'] == 1.5


def test_an_amount_goes_through_where_it_is_taken():
    v = ccxt_venue(createTrailingAmountOrder=True, createTrailingPercentOrder=False)
    v.trailing_stop('BTC/USD', Side.SELL, 1, trail_amount=100.0)
    assert v.exchange.created[-1]['params']['trailingAmount'] == 100.0


def test_a_non_trailing_order_is_unaffected():
    v = ccxt_venue(createTrailingAmountOrder=False, createTrailingPercentOrder=False)
    v.limit('BTC/USD', Side.BUY, 1, 60000.0)
    assert v.exchange.created[-1]['type'] == 'limit'


# ── the check itself ────────────────────────────────────────────────────────


def test_a_spec_with_no_trail_passes_any_venue():
    caps = Capabilities(trailing_forms=frozenset())
    caps.require_trailing_form(OrderSpec('X', Side.BUY, 1), 'nowhere')


def test_the_message_names_the_venue_and_what_it_does_take():
    caps = Capabilities(trailing_forms=frozenset({'percent'}))
    spec = OrderSpec('X', Side.SELL, 1, OrderType.TRAILING_STOP, trail_amount=5.0)
    with pytest.raises(UnsupportedError) as caught:
        caps.require_trailing_form(spec, 'somewhere')
    text = str(caught.value)
    assert 'somewhere' in text and 'it takes percent' in text
