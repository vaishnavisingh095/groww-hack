"""
Tests for YFinanceProvider's field mapping and failure handling.

Uses a fake Ticker (not real network calls) to test OUR mapping logic
deterministically -- this is exactly what "keep provider-facing code
separate from domain logic" and "testable without network access" mean
in practice: we can verify our own field-extraction logic is correct
without depending on live Yahoo data being in any particular state.

Real-network verification against actual yfinance happens separately in
the manual end-to-end run (see implementation report), not here.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.providers.yfinance_provider import YFinanceProvider


def _make_fake_ticker(fast_info=None, info=None, raise_on_fast_info=False, raise_on_info=False):
    fake = MagicMock()
    if raise_on_fast_info:
        type(fake).fast_info = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    else:
        fake.fast_info = fast_info or {}
    if raise_on_info:
        type(fake).info = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    else:
        fake.info = info or {}
    return fake


def test_maps_fast_info_fields_correctly():
    """Confirms the primary field mapping: fast_info.last_price,
    fast_info.previous_close, fast_info.last_volume."""
    fake_ticker = _make_fake_ticker(
        fast_info={"last_price": 1326.4, "previous_close": 1302.6, "last_volume": 9122871}
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS"])

    assert len(quotes) == 1
    q = quotes[0]
    assert q.fetch_succeeded is True
    assert q.last_price == 1326.4
    assert q.previous_close == 1302.6
    assert q.volume == 9122871


def test_falls_back_to_info_fields_when_fast_info_fails():
    """Confirms the documented fallback: info.regularMarketPrice /
    regularMarketVolume when fast_info raises."""
    fake_ticker = _make_fake_ticker(
        raise_on_fast_info=True,
        info={
            "regularMarketPrice": 2312.8,
            "regularMarketVolume": 1722049,
            "regularMarketTime": 1788509522,
        },
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["TCS.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is True
    assert q.last_price == 2312.8
    assert q.volume == 1722049
    assert q.provider_timestamp == 1788509522
    # previous_close has no .info fallback in our mapping -- confirms it
    # stays None rather than being silently guessed at
    assert q.previous_close is None


def test_provider_timestamp_never_used_as_price_or_volume():
    """Sanity check that provider_timestamp is captured for diagnostics
    ONLY and never leaks into price/volume fields."""
    fake_ticker = _make_fake_ticker(
        fast_info={"last_price": 100.0, "previous_close": 99.0, "last_volume": 500},
        info={"regularMarketTime": 1788509522},
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["INFY.NS"])

    q = quotes[0]
    assert q.provider_timestamp == 1788509522
    assert q.last_price == 100.0  # not overwritten by provider_timestamp
    assert q.volume == 500


def test_no_usable_price_returns_fetch_failed_not_exception():
    """When both fast_info and info fail to give a price, get_quotes
    must return fetch_succeeded=False, never raise."""
    fake_ticker = _make_fake_ticker(raise_on_fast_info=True, raise_on_info=True)
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["ICICIBANK.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert q.last_price is None
    assert q.error_message is not None


def test_ticker_construction_itself_raising_does_not_crash_get_quotes():
    """Simulates a total provider outage at the lowest level -- even if
    yf.Ticker() itself raises, get_quotes must not propagate the
    exception (per base.py's contract)."""
    with patch("app.providers.yfinance_provider.yf.Ticker", side_effect=RuntimeError("network down")):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["HDFCBANK.NS"])

    assert len(quotes) == 1
    assert quotes[0].fetch_succeeded is False
    assert "network down" in quotes[0].error_message


def test_nan_price_from_provider_is_rejected_at_boundary():
    """NaN/inf values from a provider must be filtered out by
    _safe_number, not passed through as a 'valid' price."""
    import math

    fake_ticker = _make_fake_ticker(
        fast_info={"last_price": math.nan, "previous_close": 100.0, "last_volume": 500}
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS"])

    # last_price is None (NaN filtered), and .info fallback (empty mock)
    # gives nothing either -> overall fetch should report no usable price
    q = quotes[0]
    assert q.last_price is None
    assert q.fetch_succeeded is False


def test_fetched_at_is_set_to_our_own_current_time():
    """Confirms fetched_at is OUR timestamp (set at call time), not
    derived from anything the provider returns."""
    fake_ticker = _make_fake_ticker(
        fast_info={"last_price": 100.0, "previous_close": 99.0, "last_volume": 500}
    )
    before = datetime.now(timezone.utc)
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS"])
    after = datetime.now(timezone.utc)

    assert before <= quotes[0].fetched_at <= after


def test_multiple_symbols_each_get_a_quote():
    fake_ticker = _make_fake_ticker(
        fast_info={"last_price": 100.0, "previous_close": 99.0, "last_volume": 500}
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS", "TCS.NS", "INFY.NS"])

    assert len(quotes) == 3
    assert {q.symbol for q in quotes} == {"RELIANCE.NS", "TCS.NS", "INFY.NS"}
