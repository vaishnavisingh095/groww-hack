"""
Instrument: static-ish reference data for a tradeable symbol.

Per architecture.md: "Instrument identity & metadata: our own Instrument
collection is the source of truth for what's trackable; the provider is
the source of truth for price/volume values only."
"""
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class Instrument(BaseModel):
    """
    One tradeable symbol on one exchange (e.g., RELIANCE on NSE).

    Note: the same company (e.g., RELIANCE) can appear as two distinct
    Instrument documents — one for NSE, one for BSE — since a single
    watchlist entry refers to one exchange's listing, not the company in
    the abstract. This mirrors how yfinance itself treats .NS and .BO as
    different tickers (see architecture.md's provider field mapping).
    """

    symbol: str = Field(..., min_length=1, max_length=32)
    exchange: Exchange
    company_name: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("symbol")
    @classmethod
    def symbol_must_be_meaningful(cls, v: str) -> str:
        """
        Reject whitespace-only or unnormalized symbols at the boundary.

        We uppercase and strip here because instrument symbols are
        conventionally uppercase (RELIANCE, not reliance), and treating
        "reliance" and "RELIANCE" as different instruments would silently
        create duplicate documents for the same real-world symbol.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError("symbol must not be empty or whitespace-only")
        return stripped.upper()
