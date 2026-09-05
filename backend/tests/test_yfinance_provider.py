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
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

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


# ---------- 1b. session_date comes from the bar, never from our own clock ----------


def test_session_date_comes_from_the_bar_not_from_fetched_at():
    """REGRESSION for the P1-1 session_date bug: session_date must
    reflect the actual intraday bar's own (exchange-local) date, never
    our own fetch clock. make_intraday_bars fixes its index at
    2026-09-04 regardless of when this test actually runs -- if
    session_date were still (incorrectly) derived from fetched_at
    (today, whatever "today" is when the suite runs), this assertion
    would fail on any day other than 2026-09-04."""
    bars = make_intraday_bars([(1300.0, 1000), (1310.0, 1500), (1322.0, 800)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1302.5})

    before = datetime.now(timezone.utc)
    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])
    after = datetime.now(timezone.utc)

    q = quotes[0]
    assert q.session_date == date(2026, 9, 4)  # the bars' own date
    # fetched_at is untouched by this fix -- still our own real clock
    # reading, independent of session_date entirely.
    assert before <= q.fetched_at <= after


def test_session_date_reflects_the_same_bar_used_for_last_price_across_a_day_boundary():
    """session_date and last_price must always come from the SAME bar --
    when trailing bars on a later calendar day are all invalid and the
    search falls back to a valid bar on an EARLIER day, session_date
    must reflect that earlier day, not the later (invalid) one. This is
    the scenario a provider returning the most recently completed
    session's bars (market closed, no new valid bars yet today) would
    produce."""
    earlier_day_index = pd.date_range(
        "2026-09-04 15:28", periods=2, freq="1min", tz="Asia/Kolkata"
    )
    later_day_index = pd.date_range(
        "2026-09-05 09:15", periods=2, freq="1min", tz="Asia/Kolkata"
    )
    index = earlier_day_index.append(later_day_index)
    bars = pd.DataFrame(
        {
            "Open": [1300.0, 1310.0, float("nan"), float("nan")],
            "High": [1300.0, 1310.0, float("nan"), float("nan")],
            "Low": [1300.0, 1310.0, float("nan"), float("nan")],
            "Close": [1300.0, 1310.0, float("nan"), float("nan")],  # last two bars invalid
            "Volume": [1000, 1500, 0, 0],
        },
        index=index,
    )
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.last_price == 1310.0  # the last VALID close (earlier day's second bar)
    assert q.session_date == date(2026, 9, 4)  # the SAME bar's date, not 2026-09-05


# ---------- 1c. bar_timestamp preserves the exact bar's own timestamp ----------


def test_bar_timestamp_is_the_full_tz_aware_timestamp_of_the_last_price_bar():
    """(Focused regression, market-bar timestamp propagation milestone.)
    The FULL timestamp (not just the date) of the SAME bar last_price
    came from must be preserved -- exchange-local (Asia/Kolkata for
    NSE), tzinfo included, never converted to UTC or stripped."""
    bars = make_intraday_bars([(1300.0, 1000), (1310.0, 1500), (1322.0, 800)])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1302.5})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    # make_intraday_bars starts its index at 2026-09-04 09:15 IST with
    # 1-minute bars -- the third (last, latest-valid) bar is 09:17 IST.
    assert q.bar_timestamp == datetime(2026, 9, 4, 9, 17, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert q.bar_timestamp.tzinfo is not None  # never a naive datetime
    assert q.bar_timestamp.utcoffset() == timedelta(hours=5, minutes=30)  # IST, preserved as-is


def test_bar_timestamp_reflects_the_same_bar_used_for_last_price_across_a_day_boundary():
    """bar_timestamp (like session_date) must come from the SAME bar as
    last_price -- when trailing bars on a later calendar day are all
    invalid and the search falls back to a valid bar on an earlier day,
    bar_timestamp must reflect THAT earlier bar, not a later one."""
    earlier_day_index = pd.date_range(
        "2026-09-04 15:28", periods=2, freq="1min", tz="Asia/Kolkata"
    )
    later_day_index = pd.date_range(
        "2026-09-05 09:15", periods=2, freq="1min", tz="Asia/Kolkata"
    )
    index = earlier_day_index.append(later_day_index)
    bars = pd.DataFrame(
        {
            "Open": [1300.0, 1310.0, float("nan"), float("nan")],
            "High": [1300.0, 1310.0, float("nan"), float("nan")],
            "Low": [1300.0, 1310.0, float("nan"), float("nan")],
            "Close": [1300.0, 1310.0, float("nan"), float("nan")],  # last two bars invalid
            "Volume": [1000, 1500, 0, 0],
        },
        index=index,
    )
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.last_price == 1310.0  # the last VALID close (earlier day's second bar)
    assert q.bar_timestamp == datetime(2026, 9, 4, 15, 29, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_bar_timestamp_is_none_when_no_valid_bar_found():
    """No usable price -> no bar_timestamp either, same degrade-together
    behavior as session_date."""
    bars = make_intraday_bars([])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1302.5})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert q.bar_timestamp is None


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


# ---------- 2b. day_high/day_low derivation (adaptive price threshold) ----------
#
# Derived from the SAME intraday-history DataFrame already fetched for
# price/volume above -- no separate network request. These tests build
# their own DataFrame directly (rather than via make_intraday_bars,
# which sets High/Low == Close for every row, since that's irrelevant
# to price/volume derivation) so High/Low can genuinely differ from
# Close and from each other.


def _make_bars_with_range(rows):
    """rows: list of (close, volume, high, low) tuples, oldest first."""
    index = pd.date_range("2026-09-04 09:15", periods=len(rows), freq="1min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "Open": [r[0] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[0] for r in rows],
            "Volume": [r[1] for r in rows],
        },
        index=index,
    )


def test_day_high_low_derived_from_max_min_across_all_bars():
    bars = _make_bars_with_range(
        [
            (1300.0, 1000, 1302.0, 1298.0),
            (1310.0, 1500, 1315.0, 1305.0),  # highest High
            (1305.0, 800, 1308.0, 1290.0),  # lowest Low
        ]
    )
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].day_high == 1315.0
    assert quotes[0].day_low == 1290.0


def test_day_high_low_excludes_invalid_bars_without_aborting():
    """A NaN/non-positive High or Low in one bar must be excluded from
    the max/min, never abort or corrupt the whole calculation -- mirrors
    the existing volume-sum exclusion behavior."""
    bars = _make_bars_with_range(
        [
            (1300.0, 1000, 1302.0, 1298.0),
            (1310.0, 1500, float("nan"), 1305.0),  # invalid High excluded
            (1305.0, 800, 1308.0, -5.0),  # invalid (negative) Low excluded
        ]
    )
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].day_high == 1308.0  # max of the two VALID highs (1302.0, 1308.0)
    assert quotes[0].day_low == 1298.0  # min of the two VALID lows (1298.0, 1305.0)


def test_day_high_low_missing_columns_does_not_fail_the_quote():
    """Independently optional: a price/volume/session_date fetch can
    still succeed even when day_high/day_low cannot be derived at all
    (e.g. missing High/Low columns)."""
    bars = make_intraday_bars([(1300.0, 1000), (1310.0, 1500)])
    bars = bars.drop(columns=["High", "Low"])
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].fetch_succeeded is True  # price/volume unaffected
    assert quotes[0].last_price == 1310.0
    assert quotes[0].day_high is None
    assert quotes[0].day_low is None


def test_day_high_low_all_bars_invalid_produces_none_not_a_fabricated_range():
    bars = _make_bars_with_range(
        [
            (1300.0, 1000, float("nan"), float("nan")),
            (1310.0, 1500, -1.0, -1.0),
        ]
    )
    fake_ticker = make_fake_ticker(bars=bars, info={"regularMarketPreviousClose": 1290.0})

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    assert quotes[0].fetch_succeeded is True  # price/volume still fine
    assert quotes[0].day_high is None
    assert quotes[0].day_low is None


# ---------- 3. Empty intraday history ----------


def test_empty_intraday_history_produces_fetch_failed_not_exception():
    fake_ticker = make_fake_ticker(bars=make_intraday_bars([]))

    with patch("app.providers.yfinance_provider.yf.Ticker", return_value=fake_ticker):
        quotes = YFinanceProvider().get_quotes(["RELIANCE.NS"])

    q = quotes[0]
    assert q.fetch_succeeded is False
    assert q.last_price is None
    assert q.volume is None
    assert q.session_date is None
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
    assert q.session_date is None
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