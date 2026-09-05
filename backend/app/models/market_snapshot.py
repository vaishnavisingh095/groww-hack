"""
MarketSnapshot: the latest known market data for one instrument.

Per architecture.md (as corrected by the live yfinance investigation):
- `fetched_at` (our own timestamp) is the SOLE authoritative timestamp
  for freshness/staleness. It is never derived from the provider.
- `provider_timestamp` is stored for diagnostics only. Its semantic
  meaning is not independently verified and it must never be used to
  compute status or be shown to the user as an exchange trade time.
- `percent_change` is always computed by us from last_price and
  previous_close — never taken from an unverified provider field.
- `session_date` records which trading day this snapshot's cumulative
  volume belongs to, required later for same-session volume-acceleration
  checks (Phase 1 only stores the field; the Change Engine that reads it
  is a later phase).

This module defines the shape and boundary validation only. It does NOT
implement provider fetching, freshness-status computation over time, or
the invalid-data classification rules — those require a "now" and a
provider client respectively, and belong to Phase 3 (Market Data
Service), not Phase 1 (data model).
"""
from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class SnapshotStatus(str, Enum):
    OK = "ok"
    STALE = "stale"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class MarketSnapshot(BaseModel):
    instrument_id: str = Field(..., min_length=1)

    # Price fields. last_price is required for any snapshot claiming to
    # carry real data; previous_close is required to self-compute
    # percent_change (architecture.md: never depend on an unverified
    # provider percent field).
    last_price: float = Field(..., gt=0)
    previous_close: float = Field(..., gt=0)
    percent_change: float

    # Volume: per architecture.md's Invalid Data Rules, volume must be
    # >= 0 (a real impossibility check, not an assumption) but zero is
    # explicitly allowed — the live test observed zero volume under
    # normal conditions, so it is not treated as invalid here.
    volume: int = Field(..., ge=0)

    session_date: date

    # Today's intraday high/low, feeding the adaptive price
    # meaningful-change threshold (see change_engine.py). Nullable and
    # deliberately unvalidated beyond typing here -- a missing or
    # invalid range must degrade gracefully (the threshold computation
    # has its own documented fallback) rather than invalidating an
    # otherwise-valid snapshot, matching how `volume` already degrades.
    day_high: float | None = None
    day_low: float | None = None

    fetched_at: datetime
    provider_timestamp: int | None = Field(
        default=None,
        description=(
            "Raw value from the provider's regularMarketTime field, "
            "stored for diagnostics only. NOT authoritative for "
            "freshness. NOT verified to represent exchange trade time."
        ),
    )

    status: SnapshotStatus

    @field_validator("last_price", "previous_close")
    @classmethod
    def must_be_finite(cls, v: float) -> float:
        """
        Reject NaN/inf explicitly.

        Pydantic's `gt=0` constraint does NOT reject NaN (NaN fails every
        ordering comparison, including `> 0`, so it would actually already
        raise -- but infinity DOES pass `> 0` silently, which is the real
        gap). This mirrors the rehearsal's own lesson: a numeric type
        constraint alone does not exclude non-finite values.
        """
        import math

        if not math.isfinite(v):
            raise ValueError("must be a finite number (no NaN or infinity)")
        return v

    @model_validator(mode="after")
    def percent_change_must_match_computation(self) -> "MarketSnapshot":
        """
        Defensive check: percent_change must actually equal the value
        computed from last_price and previous_close, per architecture.md
        ("percent_change is computed by us... never depend on an
        unverified provider percent-change field").

        This does not recompute percent_change silently — it validates
        that whoever constructed this snapshot already did the
        computation correctly, catching a caller bug (e.g., accidentally
        passing through a provider-supplied percent field) at the
        boundary rather than letting a wrong number reach storage.
        """
        expected = (self.last_price - self.previous_close) / self.previous_close * 100
        if abs(self.percent_change - expected) > 0.01:
            raise ValueError(
                f"percent_change ({self.percent_change}) does not match "
                f"computed value ({expected:.4f}) from last_price and "
                f"previous_close — percent_change must be self-computed, "
                f"never taken directly from the provider"
            )
        return self

    @staticmethod
    def compute_percent_change(last_price: float, previous_close: float) -> float:
        """
        Shared computation so callers don't hand-roll this formula and
        risk it drifting from the validator's own expectation above.
        """
        return (last_price - previous_close) / previous_close * 100
