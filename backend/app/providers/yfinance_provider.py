"""
yfinance-backed implementation of MarketDataProvider.

REVISED FIELD MAPPING (intraday-history-based, replacing fast_info for
price/volume) -- see the "INTRADAY HISTORY REVISION" note below for the
full evidence and reasoning:

  last_price      <- history(period="1d", interval="1m")'s latest valid Close
  volume          <- sum of history(period="1d", interval="1m")'s valid
                      per-minute Volume values for the current session
  previous_close  <- info.regularMarketPreviousClose (fallback: fast_info.previous_close)
  provider_timestamp <- info.regularMarketTime (diagnostics only, per
                        architecture.md -- NEVER treated as authoritative
                        freshness or exchange trade time anywhere in this
                        codebase)

CRITICAL (retained from the earlier fast_info investigation, for
historical context -- no longer load-bearing for price/volume since
those no longer come from fast_info): fast_info.last_price / last_volume
/ previous_close are snake_case Python @property attributes, not valid
keys for FastInfo's separate dict-style .get()/.keys() interface. See
decisions.md for the full regression story. previous_close STILL uses
fast_info.previous_close as a fallback below, so this distinction still
matters for that one field.

INTRADAY HISTORY REVISION (this revision): a direct runtime diagnostic
confirmed fast_info.last_price and fast_info.last_volume returned the
EXACT SAME values across three reads 10 seconds apart during live NSE
market hours, while history(period="1d", interval="1m") for the same
symbol at the same time returned real, distinct intraday bars with
changing prices and per-minute volumes. This means fast_info's
price/volume path (which internally derives from a DAILY-interval
history call, not live intraday data -- see the fast_info.last_price
source) does not refresh on a timescale usable for intraday
change-detection. Price and volume are therefore now sourced from
1-minute intraday bars instead.

NOTE ON A PRIOR, CONFLICTING OBSERVATION: architecture.md's original
live investigation stated that history(interval="1m")'s Volume field
returned near-zero for almost every bar, and explicitly excluded it from
the volume path for that reason. The diagnostic behind THIS revision
found the opposite -- real, non-zero, changing per-minute volumes. Both
observations are real evidence from real runs; they are not being
silently reconciled here. This discrepancy is documented in decisions.md
rather than assumed away. What changed in this revision is a direct
architectural instruction based on new, explicit runtime evidence
overriding the earlier documented exclusion for this specific field.

This module is the ONLY place yfinance is imported. Every other module
depends on the MarketDataProvider/RawQuote abstraction in base.py.
"""
import logging
import math
from datetime import date, datetime, timezone

import requests
import yfinance as yf
from requests.adapters import HTTPAdapter

from app.providers.base import MarketDataProvider, RawQuote

logger = logging.getLogger(__name__)

# Explicit, short, bounded timeout for EVERY yfinance network call this
# provider makes (ticker.history(), ticker.info, ticker.fast_info) --
# see _TimeoutEnforcingAdapter below for why a session-level override is
# the only mechanism that covers all three uniformly. yfinance's own
# internal defaults (10s for history(), 30s for info/fast_info's
# underlying YfData.get/get_raw_json) are otherwise inherited silently
# and are not coordinated across the up to 5 sequential per-symbol
# fetches get_quotes() below performs -- see decisions.md's "Provider
# network timeout" entry. 5s comfortably exceeds a healthy real
# yfinance call (typically well under 2s) while still bounding the
# worst-case total wait for a full watchlist refresh to a handful of
# seconds per symbol instead of tens of seconds.
_PROVIDER_REQUEST_TIMEOUT_SECONDS = 5


class _TimeoutEnforcingAdapter(HTTPAdapter):
    """
    requests.Session has no native "default timeout" concept -- every
    request needs an explicit timeout= kwarg per call, or it can block
    indefinitely (documented `requests` behavior, not a yfinance quirk).
    yfinance's own internal code always supplies ITS OWN timeout
    explicitly (_make_request's own 30s default for info/fast_info,
    history()'s own 10s default) but exposes no public parameter for a
    caller to override that default for .info/.fast_info specifically
    -- only history() has its own timeout= kwarg, and there is no
    equivalent for Ticker.info/Ticker.fast_info anywhere in the public
    API (confirmed by inspecting the installed yfinance==1.7.0 source:
    Quote._fetch_info/_fetch_additional_info call self._data.get/
    get_raw_json with no caller-supplied timeout at all).

    Overriding HTTPAdapter.send() -- the actual point where a prepared
    request is finally sent over the wire -- is the standard, documented
    `requests` pattern for enforcing a session-wide timeout ceiling
    regardless of what any caller passes in (see requests' own
    "Advanced Usage: Timeouts" documentation). This CLAMPS whatever
    timeout yfinance itself tries to use down to
    _PROVIDER_REQUEST_TIMEOUT_SECONDS, so one mechanism -- mounting this
    adapter on a session passed to yf.Ticker(symbol, session=...), which
    TickerBase.__init__'s own docstring documents as a supported
    "Custom requests session" -- covers ticker.history(), ticker.info,
    AND ticker.fast_info uniformly (all three route through the same
    underlying YfData instance built from that one session; confirmed
    by inspecting TickerBase.__init__ and FastInfo.__init__ in the
    installed source).
    """

    def send(self, request, **kwargs):
        kwargs["timeout"] = _PROVIDER_REQUEST_TIMEOUT_SECONDS
        return super().send(request, **kwargs)


class YFinanceProvider(MarketDataProvider):
    def __init__(self):
        # One shared, timeout-bounded session, built once and reused for
        # every get_quotes() call for this provider's lifetime -- this
        # matches the existing lifetime of the provider itself (a single
        # module-level `_provider` instance reused across requests, see
        # routes/watchlist.py), and keeps requests' own connection
        # pooling working across calls rather than paying a fresh
        # session's setup cost per symbol.
        self._session = requests.Session()
        timeout_adapter = _TimeoutEnforcingAdapter()
        self._session.mount("https://", timeout_adapter)
        self._session.mount("http://", timeout_adapter)

    def get_quotes(self, symbols: list[str]) -> list[RawQuote]:
        """
        Fetch one symbol at a time via yf.Ticker rather than yf.download's
        batch call.

        Why not batch here: yf.download's multi-symbol DataFrame shape
        is convenient for OHLCV history but per-symbol yf.Ticker calls
        were confirmed working individually and keep the per-symbol
        error handling (a single bad symbol must never affect another)
        simple. For this vertical slice with 5 symbols, sequential
        per-symbol calls remain simple and fast enough; if this becomes
        a real bottleneck with a larger watchlist, that is a concrete,
        measurable problem to revisit -- not something to pre-optimize
        now.
        """
        results: list[RawQuote] = []
        for symbol in symbols:
            results.append(self._get_single_quote(symbol))
        return results

    def _get_single_quote(self, symbol: str) -> RawQuote:
        fetched_at = datetime.now(timezone.utc)
        try:
            ticker = yf.Ticker(symbol, session=self._session)

            (
                last_price,
                volume,
                session_date,
                bar_timestamp,
                day_high,
                day_low,
                intraday_error,
            ) = self._get_intraday_price_and_volume(ticker)

            previous_close, previous_close_error = self._get_previous_close(ticker)

            provider_timestamp = self._get_provider_timestamp(ticker)

            if last_price is None:
                # Surface the REAL underlying failure reason instead of a
                # generic message -- this is what makes a genuine
                # provider outage distinguishable from a mapping bug
                # during development.
                detail = intraday_error or "no error captured"
                return RawQuote(
                    symbol=symbol,
                    last_price=None,
                    previous_close=None,
                    volume=None,
                    provider_timestamp=None,
                    fetched_at=fetched_at,
                    fetch_succeeded=False,
                    error_message=f"Provider returned no usable price ({detail})",
                )

            # previous_close can still legitimately be None here even
            # though last_price succeeded -- this is NOT fabricated or
            # defaulted. RawQuote.previous_close is passed through
            # exactly as found, including None. It is
            # MarketDataService's job (not this provider's) to decide
            # what an unusable previous_close means for the resulting
            # snapshot.
            return RawQuote(
                symbol=symbol,
                last_price=last_price,
                previous_close=previous_close,
                volume=volume,
                provider_timestamp=provider_timestamp,
                fetched_at=fetched_at,
                fetch_succeeded=True,
                session_date=session_date,
                bar_timestamp=bar_timestamp,
                day_high=day_high,
                day_low=day_low,
            )

        except Exception as e:
            # Catches anything unexpected (network error, library bug,
            # etc.) -- per base.py's contract, get_quotes must never
            # raise, so this is the last line of defense for a single
            # symbol's fetch.
            error_message = f"{type(e).__name__}: {e}"
            logger.warning("Unexpected failure fetching %s: %s", symbol, error_message)
            return RawQuote(
                symbol=symbol,
                last_price=None,
                previous_close=None,
                volume=None,
                provider_timestamp=None,
                fetched_at=fetched_at,
                fetch_succeeded=False,
                error_message=error_message,
            )

    def _get_intraday_price_and_volume(
        self, ticker: "yf.Ticker"
    ) -> tuple[
        float | None,
        int | None,
        date | None,
        datetime | None,
        float | None,
        float | None,
        str | None,
    ]:
        """
        Fetch the most recent 1-minute intraday bars and derive:
          - last_price: the latest bar with a valid (finite, positive) Close.
          - volume: the sum of every valid (finite, non-negative) Volume
            value across those bars -- this reconstructs cumulative
            session volume from the intraday series, since we no longer
            rely on fast_info/info's own cumulative volume field for this.
          - session_date: the exchange-local calendar date of that SAME
            bar (see _latest_valid_close) -- NOT derived from our own
            fetch clock. yfinance's history(period="1d") can return the
            most recently COMPLETED session's bars when the market is
            closed, so the bars' own date is not guaranteed to be
            "today."
          - bar_timestamp: the FULL tz-aware timestamp of that SAME bar
            (see _latest_valid_close) -- the actual market-observation
            time, preserved exactly as yfinance/pandas reports it
            (exchange-local, e.g. Asia/Kolkata for NSE), never converted
            to UTC or stripped of tzinfo. Distinct from fetched_at (our
            own clock) and provider_timestamp (unverified diagnostics)
            -- see decisions.md's "Market-bar timestamp propagation"
            entry. Purely informational: never used to compute
            freshness/status, which remains fetched_at's job alone.
          - day_high / day_low: the max/min of every valid (finite,
            positive) High/Low value across the SAME bars -- no separate
            provider call. Independently optional: a price/volume/
            session_date fetch can still succeed even when a valid
            day_high/day_low cannot be derived, since the adaptive price
            threshold this feeds has its own documented fallback for
            exactly that case.

        Returns (last_price, volume, session_date, bar_timestamp,
        day_high, day_low, error_message). Never raises; a failed PRICE
        fetch results in (None, None, None, None, None, None, <reason>),
        which the caller treats as a failed fetch for this symbol --
        never a fabricated price, volume, session date, timestamp, or
        range.

        Conservative handling of malformed bars: a bar with a
        NaN/inf/non-positive Close is skipped when searching for the
        latest valid price (we do not assume the LAST row is
        automatically usable -- an in-progress or malformed final bar
        must not silently produce a bad price). A bar with a
        NaN/negative Volume is excluded from the sum rather than
        treated as zero or aborting the whole calculation -- one bad
        minute must not zero out or invalidate the entire session's
        volume figure. The same conservative filtering applies to
        High/Low when computing day_high/day_low.
        """
        try:
            bars = ticker.history(period="1d", interval="1m")
        except Exception as e:
            return None, None, None, None, None, None, f"history() fetch failed: {type(e).__name__}: {e}"

        if bars is None or bars.empty:
            return None, None, None, None, None, None, "history() returned no intraday bars"

        if "Close" not in bars.columns or "Volume" not in bars.columns:
            return None, None, None, None, None, None, "history() response missing Close/Volume columns"

        latest_valid = self._latest_valid_close(bars)
        if latest_valid is None:
            return None, None, None, None, None, None, "no bar had a valid (finite, positive) Close"
        last_price, session_date, bar_timestamp = latest_valid

        volume = self._sum_valid_volume(bars)
        # Note: volume can legitimately be 0 (e.g., very start of
        # session with only one bar and no trades yet) -- 0 is a valid
        # sum, not a failure. Only last_price being unobtainable fails
        # the whole fetch, per the existing "missing/invalid PRICE
        # invalidates the update; volume degrades gracefully" rule
        # preserved from market_data_service.py's Invalid Data Rules.
        day_high, day_low = self._day_high_low(bars)
        return last_price, volume, session_date, bar_timestamp, day_high, day_low, None

    @staticmethod
    def _day_high_low(bars) -> tuple[float | None, float | None]:
        """Max/min of every valid (finite, positive) High/Low value
        across the given bars. Returns (None, None) if the columns are
        missing or no bar has a valid value -- never a fabricated range,
        and never raises (a missing/invalid range must not affect the
        price/volume fetch this is derived alongside)."""
        if "High" not in bars.columns or "Low" not in bars.columns:
            return None, None

        valid_highs = []
        for v in bars["High"].tolist():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f) and f > 0:
                valid_highs.append(f)

        valid_lows = []
        for v in bars["Low"].tolist():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f) and f > 0:
                valid_lows.append(f)

        if not valid_highs or not valid_lows:
            return None, None
        return max(valid_highs), min(valid_lows)

    @staticmethod
    def _latest_valid_close(bars) -> tuple[float, date, datetime] | None:
        """Search from the most recent bar backwards for the latest
        bar with a finite, positive Close -- does not assume the very
        last row is automatically valid.

        Returns (price, session_date, bar_timestamp) from that SAME bar,
        not independently-sourced values: session_date and bar_timestamp
        must always reflect the actual bar last_price came from, never a
        separate clock reading (that decoupling was the P1-1 bug --
        session_date used to come from our own fetched_at instead of the
        data itself). yfinance's intraday DatetimeIndex is already
        timezone-aware and localized to the exchange (Asia/Kolkata for
        NSE), so `.date()` on the bar's own timestamp gives the correct
        exchange-local trading-session date directly -- no manual UTC/IST
        conversion needed here.

        bar_timestamp is the FULL timestamp (not just the date),
        converted from pandas' Timestamp to a plain Python datetime via
        `.to_pydatetime()` -- this preserves the exact tzinfo pandas
        already attached (Asia/Kolkata), it does not convert to UTC or
        drop the offset. It is kept separate from session_date (which
        remains exactly as before) rather than derived from it later, so
        both are guaranteed to come from this SAME bar in one place.
        """
        closes = bars["Close"].tolist()
        timestamps = bars.index.tolist()
        for i in range(len(closes) - 1, -1, -1):
            try:
                f = float(closes[i])
            except (TypeError, ValueError):
                continue
            if math.isfinite(f) and f > 0:
                bar_timestamp = timestamps[i].to_pydatetime()
                return f, bar_timestamp.date(), bar_timestamp
        return None

    @staticmethod
    def _sum_valid_volume(bars) -> int:
        """Sum every finite, non-negative per-minute Volume value,
        skipping (not zeroing-out or aborting on) any malformed bar."""
        total = 0
        for vol in bars["Volume"].tolist():
            try:
                f = float(vol)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f) and f >= 0:
                total += int(f)
        return total

    def _get_previous_close(self, ticker: "yf.Ticker") -> tuple[float | None, str | None]:
        """
        previous_close continues to come from the source already
        confirmed reliable in the prior investigation:
        info.regularMarketPreviousClose, with fast_info.previous_close
        as a fallback (reversed priority from the earlier revision,
        since info.regularMarketPreviousClose was the field real Mac
        evidence confirmed as reliably populated; fast_info.previous_close
        was the field that was confirmed to unreliably return None).
        This path is intentionally UNCHANGED in spirit from the prior
        fix -- only the primary/fallback order is swapped to put the
        more reliable source first now that we no longer need fast_info
        for anything else.
        """
        previous_close = None
        error = None

        try:
            info = ticker.info
            if info:
                previous_close = self._safe_number(info.get("regularMarketPreviousClose"))
        except Exception as e:
            error = f"info fetch failed: {type(e).__name__}: {e}"

        if previous_close is None:
            try:
                fi = ticker.fast_info
                previous_close = self._safe_number(getattr(fi, "previous_close", None))
            except Exception as e:
                if error is None:
                    error = f"fast_info fetch failed: {type(e).__name__}: {e}"

        return previous_close, error

    @staticmethod
    def _get_provider_timestamp(ticker: "yf.Ticker") -> int | None:
        """Diagnostics-only field, unchanged from the prior revision --
        never authoritative for freshness, never used for price/volume."""
        try:
            info = ticker.info
            if info:
                return info.get("regularMarketTime")
        except Exception:
            pass
        return None

    @staticmethod
    def _safe_number(value) -> float | None:
        """Reject NaN/inf/non-numeric at the provider boundary itself,
        so nothing downstream ever has to re-check this."""
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        return f