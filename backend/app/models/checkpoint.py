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

`id` is a durable, application-assigned identity for this specific
checkpoint VERSION -- deliberately not MongoDB's own `_id`. Checkpoints
are stored via replace_one keyed on (user_id, instrument_id), so Mongo's
`_id` is preserved unchanged across every advance and cannot distinguish
"the baseline before this mark-as-seen" from "the baseline after it."
`id` gets a fresh value on every write (see CheckpointService), so a
ChangeEvent.checkpoint_id can durably reference the exact checkpoint
version it was detected against, even after that (user, instrument)
pair's checkpoint is later replaced.
"""
from datetime import date, datetime, timezone
from enum import Enum
from uuid import uuid4

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
    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = Field(..., min_length=1)
    instrument_id: str = Field(..., min_length=1)
    checkpoint_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_date: date
    baseline_snapshot: BaselineSnapshot
    source: CheckpointSource
