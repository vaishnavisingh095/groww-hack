"""
MarketDataProvider abstraction.

This is the ONLY interface the rest of the application depends on for
market data. No other module (routes, services, models) imports
yfinance directly — that import is confined to yfinance_provider.py.
This is what lets a future swap to a broker/paid provider (per
decisions.md) happen by writing one new class, not by touching every
caller.

RawQuote is intentionally a plain, minimal shape: it carries exactly
what a provider can tell us, with NOTHING computed (no percent_change,
no status, no freshness). Computing those from a RawQuote is the Market
Data Service's job (see market_data_service.py), not the provider's --
keeping the provider dumb is what makes it swappable.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class RawQuote:
    """
    Exactly what one provider call told us about one instrument, with no
    interpretation applied. `None` fields mean "the provider did not
    give us this," not "the value is zero."
    """

    symbol: str
    last_price: float | None
    previous_close: float | None
    volume: int | None
    provider_timestamp: int | None  # raw regularMarketTime, diagnostics only
    fetched_at: datetime  # OUR timestamp, set the instant we received this
    fetch_succeeded: bool
    error_message: str | None = None
    # The calendar date of the actual intraday bar last_price came from
    # (exchange-local, per the provider) -- NOT derived from fetched_at.
    # yfinance's history(period="1d") can return the most recent
    # COMPLETED session's bars when the market is closed, so "our fetch
    # time" and "the trading day this data belongs to" are not
    # guaranteed to be the same date. None only when no valid bar/price
    # was found (fetch_succeeded=False).
    session_date: date | None = None


class MarketDataProvider(ABC):
    """
    Abstract interface for fetching market quotes. Implementations decide
    HOW to get the data (yfinance, a broker API, ...); callers only ever
    see RawQuote objects.
    """

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> list[RawQuote]:
        """
        Fetch quotes for the given symbols in as few provider calls as
        possible (batching is a provider-level concern, not a caller
        concern). Must return exactly one RawQuote per input symbol,
        in any order, and must NEVER raise -- a failed fetch for a
        symbol is represented as fetch_succeeded=False, not an
        exception, so a single bad symbol or a provider outage can
        never crash a caller that didn't expect to catch anything.
        """
        raise NotImplementedError
