"""
yfinance-backed implementation of MarketDataProvider.

Field mapping is exactly what the live investigation confirmed (see
architecture.md's "External Dependency: Market Data Provider" section):

  last_price      <- fast_info.last_price      (fallback: info.regularMarketPrice)
  previous_close  <- fast_info.previous_close
  volume          <- fast_info.last_volume     (fallback: info.regularMarketVolume)
  provider_timestamp <- info.regularMarketTime (diagnostics only, per
                        architecture.md -- NEVER treated as authoritative
                        freshness or exchange trade time anywhere in this
                        codebase)

We deliberately do NOT use history(interval="1m").Volume -- the live
test observed it return near-zero for almost every symbol/bar, and
architecture.md explicitly excludes it from the volume signal path.

This module is the ONLY place yfinance is imported. Every other module
depends on the MarketDataProvider/RawQuote abstraction in base.py.
"""
from datetime import datetime, timezone

import yfinance as yf

from app.providers.base import MarketDataProvider, RawQuote


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

            try:
                fi = ticker.fast_info
                last_price = self._safe_number(fi.get("last_price"))
                previous_close = self._safe_number(fi.get("previous_close"))
                vol = fi.get("last_volume")
                if vol is not None:
                    volume = int(vol)
            except Exception:
                pass  # fall through to .info fallback below

            # Fallback path, and where we get provider_timestamp from.
            try:
                info = ticker.info
                if info:
                    if last_price is None:
                        last_price = self._safe_number(info.get("regularMarketPrice"))
                    if volume is None:
                        vol = info.get("regularMarketVolume")
                        if vol is not None:
                            volume = int(vol)
                    provider_timestamp = info.get("regularMarketTime")
            except Exception:
                pass  # info is a fallback; its absence alone isn't fatal

            if last_price is None:
                return RawQuote(
                    symbol=symbol,
                    last_price=None,
                    previous_close=None,
                    volume=None,
                    provider_timestamp=None,
                    fetched_at=fetched_at,
                    fetch_succeeded=False,
                    error_message="Provider returned no usable price",
                )

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
            return RawQuote(
                symbol=symbol,
                last_price=None,
                previous_close=None,
                volume=None,
                provider_timestamp=None,
                fetched_at=fetched_at,
                fetch_succeeded=False,
                error_message=f"{type(e).__name__}: {e}",
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
