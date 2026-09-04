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
from datetime import datetime, timezone

import yfinance as yf

from app.providers.base import MarketDataProvider, RawQuote

logger = logging.getLogger(__name__)


class YFinanceProvider(MarketDataProvider):
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
            ticker = yf.Ticker(symbol)

            last_price, volume, intraday_error = self._get_intraday_price_and_volume(ticker)

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
    ) -> tuple[float | None, int | None, str | None]:
        """
        Fetch today's 1-minute intraday bars and derive:
          - last_price: the latest bar with a valid (finite, positive) Close.
          - volume: the sum of every valid (finite, non-negative) Volume
            value across today's bars -- this reconstructs cumulative
            session volume from the intraday series, since we no longer
            rely on fast_info/info's own cumulative volume field for this.

        Returns (last_price, volume, error_message). Never raises;
        empty/malformed data results in (None, None, <reason>), which
        the caller treats as a failed fetch for this symbol -- never a
        fabricated price or volume.

        Conservative handling of malformed bars: a bar with a
        NaN/inf/non-positive Close is skipped when searching for the
        latest valid price (we do not assume the LAST row is
        automatically usable -- an in-progress or malformed final bar
        must not silently produce a bad price). A bar with a
        NaN/negative Volume is excluded from the sum rather than
        treated as zero or aborting the whole calculation -- one bad
        minute must not zero out or invalidate the entire session's
        volume figure.
        """
        try:
            bars = ticker.history(period="1d", interval="1m")
        except Exception as e:
            return None, None, f"history() fetch failed: {type(e).__name__}: {e}"

        if bars is None or bars.empty:
            return None, None, "history() returned no intraday bars"

        if "Close" not in bars.columns or "Volume" not in bars.columns:
            return None, None, "history() response missing Close/Volume columns"

        last_price = self._latest_valid_close(bars)
        if last_price is None:
            return None, None, "no bar had a valid (finite, positive) Close"

        volume = self._sum_valid_volume(bars)
        # Note: volume can legitimately be 0 (e.g., very start of
        # session with only one bar and no trades yet) -- 0 is a valid
        # sum, not a failure. Only last_price being unobtainable fails
        # the whole fetch, per the existing "missing/invalid PRICE
        # invalidates the update; volume degrades gracefully" rule
        # preserved from market_data_service.py's Invalid Data Rules.
        return last_price, volume, None

    @staticmethod
    def _latest_valid_close(bars) -> float | None:
        """Search from the most recent bar backwards for the latest
        bar with a finite, positive Close -- does not assume the very
        last row is automatically valid."""
        for close in reversed(bars["Close"].tolist()):
            try:
                f = float(close)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f) and f > 0:
                return f
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