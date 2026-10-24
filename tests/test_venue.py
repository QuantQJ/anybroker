def test_asking_a_venue_for_a_quote_it_does_not_serve_says_so():
    """A bare NotImplementedError with no message tells nobody anything.

    Tradovate's market data is a separate websocket subscription and Alpaca
    needs a data plan the trading key does not carry, so both declare
    supports_quotes=False -- and asking anyway should explain that.
    """
    import pytest

    from anybroker.enums import Env
    from anybroker.errors import UnsupportedError
    from anybroker.venues.tradovate import TradovateVenue

    venue = TradovateVenue(Env.PAPER)
    assert venue.capabilities.supports_quotes is False
    with pytest.raises(UnsupportedError) as caught:
        venue.get_quote('MNQZ6')
    assert 'supports_quotes' in str(caught.value)
    assert 'tradovate' in str(caught.value)
