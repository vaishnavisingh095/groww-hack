"""
Checkpoint: "what the market looked like the last time this user checked
this instrument."

Per architecture.md: the baseline values are a FROZEN COPY of a
MarketSnapshot at checkpoint time, not a reference to a MarketSnapshot
document — because MarketSnapshot is upserted (overwritten) every poll
cycle, a reference would silently point at data that no longer represents
what it represented at checkpoint time.

`session_date` is copied from the baseline snapshot at checkpoint
creation time, and is required later for the same-session volume
acceleration rule (a Change Engine concern, not implemented in Phase 1).
"""
from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class CheckpointSource(str, Enum):
    EXPLICIT = "explicit"
    IMPLICIT = "implicit"


class BaselineSnapshot(BaseModel):
    """The frozen values copied from a MarketSnapshot at checkpoint time."""

    last_price: float = Field(..., gt=0)
    volume: int = Field(..., ge=0)
    percent_change: float


class Checkpoint(BaseModel):
    user_id: str = Field(..., min_length=1)
    instrument_id: str = Field(..., min_length=1)
    checkpoint_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_date: date
    baseline_snapshot: BaselineSnapshot
    source: CheckpointSource
