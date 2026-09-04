"""
Tests for YFinanceProvider's field mapping and failure handling.

REVISED (intraday history for price/volume): a direct runtime diagnostic
confirmed fast_info.last_price/last_volume return the SAME value across
repeated reads seconds apart during live market hours, while
history(period="1d", interval="1m") returns real, changing intraday
bars. Price and volume are therefore now derived from intraday history
bars, not fast_info. previous_close is UNCHANGED in spirit -- it still
uses info.regularMarketPreviousClose as primary and
fast_info.previous_close as fallback (order swapped from the prior
revision now that info is the confirmed-reliable source and fast_info is
only needed as a fallback for this one field).

Uses fake Ticker/DataFrame objects (not real network calls) to test OUR
mapping logic deterministically -- consistent with "keep provider-facing
code separate from domain logic" and "testable without network access."

Real-network verification against actual yfinance happens separately via
manual end-to-end testing, not here.
"""
import math
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from app.providers.yfinance_provider import YFinanceProvider


# ---------- Fakes ----------


class FakeFastInfo:
    """
    Mimics real yfinance FastInfo's dual-interface behavior (retained
    from the prior revision since previous_close still falls back to
    fast_info.previous_close): snake_case attributes are the real data;
    a separate camelCase .get()/.keys() interface does NOT recognize the
    snake_case names.
    """

    _CAMEL_CASE_KEYS = {"previousClose": "previous_close"}

    def __init__(self, previous_close=None, raise_on_access=False):
        self._previous_close = previous_close
        self._raise_on_access = raise_on_access

    @property
    def previous_close(self):
        if self._raise_on_access:
            raise RuntimeError("simulated fetch failure")
        return self._previous_close

    def keys(self):
        return list(self._CAMEL_CASE_KEYS.keys())

    def get(self, key, default=None):
        if key in self._CAMEL_CASE_KEYS:
            return getattr(self, self._CAMEL_CASE_KEYS[key])
        return default


def make_intraday_bars(rows):
    """
    Build a fake intraday-history DataFrame in the same shape
    yf.Ticker.history(period="1d", interval="1m") returns: a
    DatetimeIndex and Open/High/Low/Close/Volume columns.

    `rows` is a list of (close, volume) tuples, oldest first, matching
    how real bars are ordered.
    """
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    index = pd.date_range("2026-09-04 09:15", periods=len(rows), freq="1min", tz="Asia/Kolkata")
    closes = [r[0] for r in rows]
    volumes = [r[1] for r in rows]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": volumes,
        },
        index=index,
    )


def make_fake_ticker(
    bars=None,
    history_raises=False,
    info=None,
    info_raises=False,
    fast_info_previous_close=None,
    fast_info_raises=False,
):
    fake = MagicMock()

    if history_raises:
        fake.history.side_effect = RuntimeError("history() network failure")
    else:
        fake.history.return_value = bars if bars is not None else make_intraday_bars([])

    if info_raises:
        type(fake).info = property(lambda self: (_ for _ in ()).throw(RuntimeError("info failure")))
    else:
        fake.info = info if info is not None else {}

    if fast_info_raises:
        type(fake).fast_info = property(lambda self: (_ for _ in ()).throw(RuntimeError("fast_info failure")))
    else:
        fake.fast_info = FakeFastInfo(previous_close=fast_info_previous_close)

    return fake


# ---------- 1. Valid intraday bars produce the latest price ----------


def test_valid_intraday_bars_produce_the_latest_close_as_last_price():
    bars = make_intraday_bars([(1300.0, 1000), (1310.0, 1500), (1322.0, 800)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1302.5})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is True
    assert q.last_price == 1322.0  # the LAST bar's Close, not an earlier one


def test_latest_price_skips_a_trailing_invalid_bar():
    """Conservative handling: an in-progress or malformed final bar
    (NaN/zero/negative Close) must not silently produce a bad price --
    the search must fall back to the most recent VALID bar instead."""
    bars = make_intraday_bars([(1300.0, 1000), (1310.0, 1500), (float("nan"), 800)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1302.5})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].last_price == 1310.0  # the last VALID close, not NaN


def test_latest_price_skips_a_zero_or_negative_trailing_close():
    bars = make_intraday_bars([(1300.0, 1000), (1310.0, 1500), (0.0, 800), (-5.0, 200)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1302.5})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].last_price == 1310.0


# ---------- 2. Cumulative volume is derived correctly ----------


def test_cumulative_volume_is_sum_of_all_valid_bar_volumes():
    bars = make_intraday_bars([(1300.0, 1000), (1305.0, 2500), (1310.0, 4000)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].volume == 1000 + 2500 + 4000


def test_volume_sum_excludes_invalid_bars_without_aborting():
    """One malformed bar's Volume must be excluded from the sum, not
    zero out or invalidate the entire session total."""
    bars = make_intraday_bars(
        [(1300.0, 1000), (1305.0, float("nan")), (1308.0, -50), (1310.0, 4000)]
    )
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    # NaN and negative volume bars excluded; only 1000 + 4000 counted.
    assert quotes[0].volume == 1000 + 4000
    # Price still comes from the last valid close, unaffected by the
    # excluded-volume bars.
    assert quotes[0].last_price == 1310.0


def test_zero_total_volume_is_a_valid_sum_not_a_failure():
    """Very start of session: a single bar with zero volume must be
    accepted as a legitimate (if uninteresting) volume figure, not
    treated as missing/invalid data."""
    bars = make_intraday_bars([(1300.0, 0)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].fetch_succeeded is True
    assert quotes[0].volume == 0


# ---------- 3. Empty intraday history ----------


def test_empty_intraday_history_produces_fetch_failed_not_exception():
    fake_ticker = make_fake_ticker(bars=make_intraday_bars([]))

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert q.last_price is None
    assert q.volume is None
    assert "no intraday bars" in q.error_message


def test_history_raising_produces_fetch_failed_with_real_error_message():
    fake_ticker = make_fake_ticker(history_raises=True)

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert "history() fetch failed" in q.error_message
    assert "network failure" in q.error_message


def test_all_bars_invalid_produces_fetch_failed_no_fabricated_price():
    """Every bar has an unusable Close -- must fail cleanly, never
    fabricate a price from garbage data."""
    bars = make_intraday_bars([(float("nan"), 100), (0.0, 200), (-1.0, 300)])
    fake_ticker = make_fake_ticker(bars=bars)

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert q.last_price is None
    assert "no bar had a valid" in q.error_message


# ---------- 4. Invalid price/volume (non-finite types) ----------


def test_infinite_close_is_rejected_as_invalid():
    bars = make_intraday_bars([(1300.0, 1000), (float("inf"), 500)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].last_price == 1300.0  # infinite bar skipped


def test_non_numeric_close_value_is_skipped_not_crashed_on():
    bars = make_intraday_bars([(1300.0, 1000), (1310.0, 500)])
    # Force the Close column to object dtype so a non-numeric value can
    # be assigned, simulating a malformed cell the provider must not
    # crash on -- pandas' default float64 column would reject a string
    # assignment outright, which the real (unofficial, undocumented)
    # provider response is not guaranteed to do.
    bars["Close"] = bars["Close"].astype(object)
    bars.loc[bars.index[-1], "Close"] = "not-a-number"

    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].last_price == 1300.0  # malformed cell skipped, not a crash


def test_missing_close_or_volume_columns_produces_fetch_failed():
    bars = pd.DataFrame({"Open": [100.0]}, index=pd.date_range("2026-09-04", periods=1, freq="1min"))
    fake_ticker = make_fake_ticker(bars=bars)

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert "missing Close/Volume" in q.error_message


# ---------- 5. previous_close fallback (retained from prior revision) ----------


def test_previous_close_from_info_regular_market_previous_close():
    bars = make_intraday_bars([(1322.0, 1000)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1302.5})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].previous_close == 1302.5


def test_previous_close_falls_back_to_fast_info_when_info_gives_none():
    bars = make_intraday_bars([(1322.0, 1000)])
    fake_ticker = make_fake_ticker(
        bars=bars,
        info={},  # no regularMarketPreviousClose key
        fast_info_previous_close=1302.5,
    )

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].previous_close == 1302.5


def test_previous_close_stays_none_when_no_source_has_it():
    """Never fabricated -- if neither info nor fast_info can supply it,
    RawQuote.previous_close must be None, and last_price/volume must
    still succeed independently."""
    bars = make_intraday_bars([(1322.0, 1000)])
    fake_ticker = make_fake_ticker(bars=bars, info={}, fast_info_previous_close=None)

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is True
    assert q.last_price == 1322.0
    assert q.previous_close is None


def test_previous_close_falls_back_when_info_access_raises():
    bars = make_intraday_bars([(1322.0, 1000)])
    fake_ticker = make_fake_ticker(bars=bars, info_raises=True, fast_info_previous_close=1302.5)

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    # info raised entirely (so provider_timestamp is also unavailable),
    # but previous_close still comes through via fast_info, and
    # price/volume (from history(), independent of info) are unaffected.
    assert quotes[0].previous_close == 1302.5
    assert quotes[0].last_price == 1322.0
    assert quotes[0].provider_timestamp is None


# ---------- 6. Provider failure (total outage) ----------


def test_ticker_construction_itself_raising_does_not_crash_get_quotes():
    with patch("app.providers.yfinance_provider.yf.Ticker", side_effect=RuntimeError("network down")):
        quotes = YFinanceProvider().get_quotes(["HDFCBANK.NS"])

    assert len(quotes) == 1
    assert quotes[0].fetch_succeeded is False
    assert "network down" in quotes[0].error_message


def test_total_failure_across_history_info_and_fast_info_is_reported_not_raised():
    fake_ticker = make_fake_ticker(history_raises=True, info_raises=True, fast_info_raises=True)

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert q.last_price is None
    assert q.previous_close is None
    assert q.error_message is not None


# ---------- Other invariants retained from prior revisions ----------


def test_provider_timestamp_never_used_as_price_or_volume():
    bars = make_intraday_bars([(100.0, 500)])
    fake_ticker = make_fake_ticker(
        bars=bars, info={"regularMarketPreviousClose": 99.0, "regularMarketTime": 1788509522}
    )

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["INFY.NS"])

    q = quotes[0]
    assert q.provider_timestamp == 1788509522
    assert q.last_price == 100.0
    assert q.volume == 500


def test_fetched_at_is_set_to_our_own_current_time():
    bars = make_intraday_bars([(100.0, 500)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 99.0})

    before = datetime.now(timezone.utc)
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])
    after = datetime.now(timezone.utc)

    assert before <= quotes[0].fetched_at <= after


def test_multiple_symbols_each_get_a_quote():
    bars = make_intraday_bars([(100.0, 500)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 99.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS", "TCS.NS", "INFY.NS"])

    assert len(quotes) == 3
    assert {q.symbol for q in quotes} == {"RELIANCE.NS", "TCS.NS", "INFY.NS"}