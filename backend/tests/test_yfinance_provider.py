"""
Tests for YFinanceProvider's field mapping and failure handling.

Uses a fake Ticker (not real network calls) to test OUR mapping logic
deterministically -- this is exactly what "keep provider-facing code
separate from domain logic" and "testable without network access" mean
in practice: we can verify our own field-extraction logic is correct
without depending on live Yahoo data being in any particular state.

IMPORTANT: the fake fast_info object below is deliberately built to
mimic REAL yfinance FastInfo behavior, not a convenient plain dict. A
previous version of this test file used a plain dict for fast_info,
which supports both attribute-style .get() and item access -- that
fake accidentally masked a real bug where the provider used
fast_info.get("last_price") instead of attribute access
(getattr(fast_info, "last_price")). Real FastInfo's .get() uses a
DIFFERENT, camelCase key namespace ("lastPrice") than its snake_case
attributes ("last_price"), so fast_info.get("last_price") silently
returns None on every real call, always -- a bug this old test suite
could not have caught. See yfinance_provider.py's module docstring and
decisions.md for the full story. FakeFastInfo below is built to make
that exact distinction visible: it raises AttributeError for anything
accessed except real attributes, and its .get() only recognizes the
real camelCase key names, exactly like the real class.

Real-network verification against actual yfinance happens separately in
the manual end-to-end run (see implementation report), not here.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.providers.yfinance_provider import YFinanceProvider


class FakeFastInfo:
    """
    Mimics real yfinance FastInfo's actual dual-interface behavior:
    - Real attributes (last_price, last_volume, previous_close) are
      snake_case and accessed via normal attribute access.
    - A SEPARATE dict-like interface (.get(), .keys()) uses different,
      camelCase key names and does NOT recognize the snake_case
      attribute names as valid keys.

    This is what makes fast_info.get("last_price") a real bug: it looks
    up "last_price" in the camelCase key namespace, where it does not
    exist, and returns the default (None) -- exactly what real FastInfo
    does, confirmed directly against yfinance 1.7.0 during debugging.
    """

    _CAMEL_CASE_KEYS = {
        "lastPrice": "last_price",
        "lastVolume": "last_volume",
        "previousClose": "previous_close",
    }

    def __init__(self, last_price=None, last_volume=None, previous_close=None, raise_on_access=False):
        self._last_price = last_price
        self._last_volume = last_volume
        self._previous_close = previous_close
        self._raise_on_access = raise_on_access

    @property
    def last_price(self):
        if self._raise_on_access:
            raise RuntimeError("simulated fetch failure")
        return self._last_price

    @property
    def last_volume(self):
        if self._raise_on_access:
            raise RuntimeError("simulated fetch failure")
        return self._last_volume

    @property
    def previous_close(self):
        if self._raise_on_access:
            raise RuntimeError("simulated fetch failure")
        return self._previous_close

    def keys(self):
        return list(self._CAMEL_CASE_KEYS.keys())

    def get(self, key, default=None):
        # Real FastInfo.get() only recognizes camelCase keys -- snake_case
        # attribute names like "last_price" are NOT valid keys here, so
        # this correctly returns `default` for them, exactly like the
        # real bug that was found.
        if key in self._CAMEL_CASE_KEYS:
            return getattr(self, self._CAMEL_CASE_KEYS[key])
        return default


def _make_fake_ticker(
    fast_info_kwargs=None,
    info=None,
    raise_on_fast_info=False,
    raise_on_info=False,
):
    fake = MagicMock()
    if raise_on_fast_info:
        type(fake).fast_info = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    else:
        fake.fast_info = FakeFastInfo(**(fast_info_kwargs or {}))
    if raise_on_info:
        type(fake).info = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    else:
        fake.info = info or {}
    return fake


def test_maps_fast_info_fields_correctly():
    """Confirms the primary field mapping: fast_info.last_price,
    fast_info.previous_close, fast_info.last_volume -- via real
    attribute access, matching actual FastInfo behavior."""
    fake_ticker = _make_fake_ticker(
        fast_info_kwargs=dict(last_price=1326.4, previous_close=1302.6, last_volume=9122871)
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
    regularMarketVolume / regularMarketPreviousClose when fast_info
    raises entirely."""
    fake_ticker = _make_fake_ticker(
        raise_on_fast_info=True,
        info={
            "regularMarketPrice": 2312.8,
            "regularMarketPreviousClose": 2320.0,
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
    assert q.previous_close == 2320.0
    assert q.volume == 1722049
    assert q.provider_timestamp == 1788509522


def test_previous_close_falls_back_to_info_when_fast_info_gives_none():
    """
    REGRESSION TEST for the actual real-world failure: real Mac runtime
    evidence (yfinance 1.7.0, live RELIANCE.NS) showed fast_info.last_price
    and fast_info.last_volume succeeding while fast_info.previous_close
    returned None -- NOT an exception, a legitimate None value, since
    fast_info.previous_close performs a separate, independently-fragile
    history lookup internally. The same live evidence confirmed
    info["regularMarketPreviousClose"] correctly held the real value
    (1302.5) in that exact scenario.

    This must produce a usable quote with a real previous_close, not a
    quote with previous_close stuck at None despite a working fallback
    being available.
    """
    fake_ticker = _make_fake_ticker(
        # fast_info succeeds for price/volume but returns None for
        # previous_close specifically -- not raising, just None.
        fast_info_kwargs=dict(last_price=1322.0, last_volume=13022095, previous_close=None),
        info={
            "regularMarketPreviousClose": 1302.5,
        },
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is True
    assert q.last_price == 1322.0
    assert q.volume == 13022095
    assert q.previous_close == 1302.5  # filled from the .info fallback


def test_previous_close_stays_none_when_no_source_has_it():
    """
    The other side of the same fix: when NEITHER fast_info NOR info can
    supply previous_close, it must stay None -- never fabricated,
    defaulted, or guessed. RawQuote.previous_close=None in this case is
    the honest, correct signal; it is MarketDataService's job (not this
    provider's) to decide a snapshot cannot be assembled without it.
    """
    fake_ticker = _make_fake_ticker(
        fast_info_kwargs=dict(last_price=1322.0, last_volume=13022095, previous_close=None),
        info={},  # no regularMarketPreviousClose key at all
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    # last_price/volume still succeed independently -- this provider
    # reports what it actually has, honestly, per symbol/field.
    assert q.fetch_succeeded is True
    assert q.last_price == 1322.0
    assert q.volume == 13022095
    assert q.previous_close is None  # NOT fabricated


def test_provider_timestamp_never_used_as_price_or_volume():
    """Sanity check that provider_timestamp is captured for diagnostics
    ONLY and never leaks into price/volume fields."""
    fake_ticker = _make_fake_ticker(
        fast_info_kwargs=dict(last_price=100.0, previous_close=99.0, last_volume=500),
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
        fast_info_kwargs=dict(last_price=math.nan, previous_close=100.0, last_volume=500)
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
        fast_info_kwargs=dict(last_price=100.0, previous_close=99.0, last_volume=500)
    )
    before = datetime.now(timezone.utc)
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS"])
    after = datetime.now(timezone.utc)

    assert before <= quotes[0].fetched_at <= after


def test_multiple_symbols_each_get_a_quote():
    fake_ticker = _make_fake_ticker(
        fast_info_kwargs=dict(last_price=100.0, previous_close=99.0, last_volume=500)
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS", "TCS.NS", "INFY.NS"])

    assert len(quotes) == 3
    assert {q.symbol for q in quotes} == {"RELIANCE.NS", "TCS.NS", "INFY.NS"}


def test_get_style_access_on_snake_case_keys_would_have_masked_this_bug():
    """
    REGRESSION TEST for the actual root cause: reproduces the exact
    failure mode found in production (Mac test succeeded via attribute
    access; the deployed app returned "unavailable" for all instruments
    because it used fast_info.get("last_price") instead of
    fast_info.last_price).

    Confirms FakeFastInfo.get() -- built to mirror real FastInfo's
    behavior -- returns None for snake_case keys, proving that if the
    provider regresses back to dict-style .get() access, this test
    will catch it by asserting the CORRECT (attribute-based) result.
    """
    fake_fast_info = FakeFastInfo(last_price=1329.6, last_volume=11676605, previous_close=1300.0)

    # Sanity-check the fake itself mirrors the real bug surface:
    # .get() with the snake_case name must NOT find the value...
    assert fake_fast_info.get("last_price") is None
    # ...but the real camelCase key does...
    assert fake_fast_info.get("lastPrice") == 1329.6
    # ...and real attribute access always works, regardless of .get():
    assert fake_fast_info.last_price == 1329.6

    # Now confirm the ACTUAL PROVIDER gets the right answer, proving it
    # uses attribute access and not fast_info.get(snake_case_key):
    fake_ticker = MagicMock()
    fake_ticker.fast_info = fake_fast_info
    fake_ticker.info = {}

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS"])

    assert quotes[0].fetch_succeeded is True
    assert quotes[0].last_price == 1329.6
    assert quotes[0].volume == 11676605


def test_fast_info_access_failure_surfaces_real_error_message():
    """
    Per explicit instruction: when fast_info access fails, the real
    underlying exception must be captured in error_message, not
    silently converted to a generic "unavailable" with no diagnostic
    detail.
    """
    fake_ticker = _make_fake_ticker(
        fast_info_kwargs=dict(raise_on_access=True),
        info={},  # fallback also gives nothing, so failure surfaces
    )
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        provider = YFinanceProvider()
        quotes = provider.get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert "simulated fetch failure" in q.error_message