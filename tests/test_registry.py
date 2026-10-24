"""Opening a venue by name, and what the library says it covers."""

import sys
import types

import pytest

from anybroker import ConfigError, Env, available, open_venue
from anybroker.registry import CCXT_ONLY_NOTE, NATIVE_VENUES


def test_ibkr_resolves_to_the_native_venue():
    from anybroker.venues.ibkr import IbkrVenue

    v = open_venue('ibkr', Env.PAPER, client_id=4242, port=4002)
    assert isinstance(v, IbkrVenue) and v.env is Env.PAPER


@pytest.mark.parametrize(
    'alias', ['ibkr', 'IBKR', 'ib', 'interactivebrokers', 'interactive_brokers']
)
def test_the_ibkr_aliases_all_work(alias):
    assert open_venue(alias, Env.PAPER, client_id=1, port=4002).name == 'ibkr'


def test_ibkr_is_native_because_ccxt_has_never_had_it():
    assert 'ibkr' in NATIVE_VENUES
    assert 'IBKR' in CCXT_ONLY_NOTE or 'IBKR' in CCXT_ONLY_NOTE.upper()


def test_an_unknown_name_falls_through_to_ccxt(monkeypatch):
    """With ccxt present, an unknown name is reported as unknown to ccxt."""
    fake = types.ModuleType('ccxt')
    fake.exchanges = ['kraken', 'okx']
    monkeypatch.setitem(sys.modules, 'ccxt', fake)
    with pytest.raises(ConfigError, match='ccxt has no'):
        open_venue('definitely-not-an-exchange', Env.LIVE)


def test_tradovate_resolves_to_the_native_venue():
    """ccxt has never had Tradovate, so it must not fall through to ccxt."""
    from anybroker.venues.tradovate import TradovateVenue

    v = open_venue('tradovate', Env.PAPER, client=object())
    assert isinstance(v, TradovateVenue) and v.env is Env.PAPER
    assert open_venue('tv', Env.LIVE, client=object()).name == 'tradovate'


def test_the_venues_ccxt_has_never_had_are_native():
    assert set(NATIVE_VENUES) == {'ibkr', 'tradovate', 'alpaca'}


def test_alpaca_resolves_to_the_equities_venue():
    """ccxt's `alpaca` is the crypto API; this is the stocks side."""
    from anybroker.venues.alpaca import AlpacaVenue

    v = open_venue('alpaca', Env.PAPER, api_key='k', secret_key='s')
    assert isinstance(v, AlpacaVenue)


def test_the_message_names_the_native_venues_so_you_know_where_to_look(monkeypatch):
    fake = types.ModuleType('ccxt')
    fake.exchanges = ['kraken']
    monkeypatch.setitem(sys.modules, 'ccxt', fake)
    with pytest.raises(ConfigError) as caught:
        open_venue('tradestation', Env.LIVE)
    assert 'IBKR' in str(caught.value) or 'own venues' in str(caught.value)


def test_without_ccxt_installed_it_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, 'ccxt', None)
    with pytest.raises(ConfigError, match='ccxt is not installed'):
        open_venue('kraken', Env.LIVE)


def test_env_is_required_to_open_anything():
    with pytest.raises(TypeError):
        open_venue('ibkr')


def test_available_reports_only_the_drivers_that_import():
    """A readiness check, not a catalogue: a venue with no driver is not listed."""
    report = available()
    assert set(report['all_native']) == set(NATIVE_VENUES)
    assert set(report['native']).isdisjoint(report['native_missing'])
    assert set(report['native']) | set(report['native_missing']) == set(NATIVE_VENUES)
    for hint in report['native_missing'].values():
        assert 'pip install' in hint


def test_a_missing_driver_is_reported_with_how_to_get_it(monkeypatch):
    import importlib.util
    import sys

    real = importlib.util.find_spec

    def nothing_installed(name, *a, **kw):
        if name in ('ib_insync', 'tradovate_client', 'alpaca'):
            return None
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib.util, 'find_spec', nothing_installed)
    monkeypatch.setitem(sys.modules, 'ccxt', None)
    report = available()
    assert report['native'] == []
    assert set(report['native_missing']) == set(NATIVE_VENUES)
    assert 'pip install' in report['missing_ccxt']
