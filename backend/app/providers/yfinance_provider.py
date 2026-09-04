"""
yfinance-backed implementation of MarketDataProvider.

Field mapping is exactly what the live investigation confirmed (see
architecture.md's "External Dependency: Market Data Provider" section),
with a correction below for previous_close:

  last_price      <- fast_info.last_price      (fallback: info.regularMarketPrice)
  previous_close  <- fast_info.previous_close  (fallback: info.regularMarketPreviousClose)
  volume          <- fast_info.last_volume     (fallback: info.regularMarketVolume)
  provider_timestamp <- info.regularMarketTime (diagnostics only, per
                        architecture.md -- NEVER treated as authoritative
                        freshness or exchange trade time anywhere in this
                        codebase)

CRITICAL: fast_info.last_price / last_volume / previous_close are
snake_case Python @property attributes on yfinance's FastInfo class.
They are NOT valid keys for FastInfo's separate dict-style .get()/
.keys() interface, which uses different, camelCase key names
("lastPrice", "lastVolume", ...) internally mapped back to these same
properties. Calling fast_info.get("last_price") looks like it should
work (FastInfo imitates a dict) but silently returns None on every call,
because "last_price" is never a member of FastInfo.keys() -- this is
NOT a network failure or a missing-data case, it is asking for a key
that structurally does not exist under that name. This must be accessed
via getattr(fast_info, "last_price", None) (i.e., real attribute
access), not fast_info.get("last_price"). Confirmed directly against a
live FastInfo instance during debugging; see decisions.md for the
regression story.

PREVIOUS_CLOSE FALLBACK (added after a second, distinct bug): real Mac
runtime evidence (yfinance 1.7.0, live RELIANCE.NS) showed
fast_info.previous_close can legitimately return None even when
fast_info.last_price and fast_info.last_volume succeed -- this is
expected per fast_info.previous_close's own implementation, which
computes previous close from a separate 1-week pre/post-market history
fetch that can fail independently of the simpler last_price/last_volume
lookups. The SAME live evidence confirmed info["regularMarketPreviousClose"]
(and the equivalent info["previousClose"]) correctly held the real prior
session's close (1302.5) in that exact case. This is a genuine,
yfinance-supported field -- not a derived or fabricated value -- so it
is used as a fallback, mirroring the existing last_price/volume fallback
pattern rather than introducing a new mechanism.

We deliberately do NOT use history(interval="1m").Volume -- the live
test observed it return near-zero for almost every symbol/bar, and
architecture.md explicitly excludes it from the volume signal path.

This module is the ONLY place yfinance is imported. Every other module
depends on the MarketDataProvider/RawQuote abstraction in base.py.
"""
import logging
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
        (confirmed in the live test) is convenient for OHLCV history but
        does not expose fast_info.last_volume / previous_close cleanly
        per symbol -- those live on the Ticker object's fast_info/info,
        which is what the confirmed field mapping above depends on.
        Per-symbol yf.Ticker calls were also confirmed working
        individually in the live test (~0.9-1.7s each). For this first
        vertical slice with 5 symbols, sequential per-symbol calls are
        simple and fast enough; if this becomes a real bottleneck with a
        larger watchlist, that is a concrete, measurable problem to
        revisit -- not something to pre-optimize now.
        """
        results: list[RawQuote] = []
        for symbol in symbols:
            results.append(self._get_single_quote(symbol))
        return results

    def _get_single_quote(self, symbol: str) -> RawQuote:
        fetched_at = datetime.now(timezone.utc)
        try:
            ticker = yf.Ticker(symbol)

            last_price = None
            previous_close = None
            volume = None
            provider_timestamp = None
            fast_info_error = None
            info_error = None

            try:
                fi = ticker.fast_info
                # ATTRIBUTE access (getattr), NOT fi.get("last_price") --
                # see the module docstring for why .get() silently fails
                # here regardless of whether the underlying fetch
                # succeeded.
                last_price = self._safe_number(getattr(fi, "last_price", None))
                previous_close = self._safe_number(getattr(fi, "previous_close", None))
                vol = getattr(fi, "last_volume", None)
                if vol is not None:
                    volume = int(vol)
            except Exception as e:
                fast_info_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    "fast_info fetch failed for %s: %s", symbol, fast_info_error
                )
                # fall through to .info fallback below

            # Fallback path, and where we get provider_timestamp from.
            # Also the fallback for previous_close, which fast_info can
            # legitimately fail to populate even when last_price/volume
            # succeed -- see module docstring.
            try:
                info = ticker.info
                if info:
                    if last_price is None:
                        last_price = self._safe_number(info.get("regularMarketPrice"))
                    if previous_close is None:
                        previous_close = self._safe_number(
                            info.get("regularMarketPreviousClose")
                        )
                    if volume is None:
                        vol = info.get("regularMarketVolume")
                        if vol is not None:
                            volume = int(vol)
                    provider_timestamp = info.get("regularMarketTime")
            except Exception as e:
                info_error = f"{type(e).__name__}: {e}"
                logger.warning("info fetch failed for %s: %s", symbol, info_error)
                # info is a fallback; its absence alone isn't fatal

            if last_price is None:
                # Surface the REAL underlying failure reason instead of a
                # generic message -- this is what makes a genuine
                # provider outage distinguishable from "the field mapping
                # is wrong" during development, which is exactly the
                # class of bug this replaces.
                detail = fast_info_error or info_error or "no error captured"
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

            # NOTE: previous_close can still legitimately be None here
            # even though last_price succeeded (both fast_info and the
            # .info fallback failed to provide it). This is NOT
            # fabricated or defaulted to any value -- RawQuote.previous_close
            # is passed through exactly as found, including None. It is
            # MarketDataService's explicit job (not this provider's) to
            # decide what an unusable previous_close means for the
            # resulting snapshot -- see market_data_service.py. This
            # provider's only responsibility is honest reporting of what
            # the data source actually gave us.
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

    @staticmethod
    def _safe_number(value) -> float | None:
        """Reject NaN/inf/non-numeric at the provider boundary itself,
        so nothing downstream ever has to re-check this."""
        import math

        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        return f