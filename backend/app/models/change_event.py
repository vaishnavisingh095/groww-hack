"""
ChangeEvent: a persisted record of a detected meaningful change.

Per architecture.md: created once per detected change per checkpoint
period (never recreated on every page refresh), and immutable once
created — only its `acknowledged` flag is ever updated.

`volume_acceleration_available` is explicit (not inferred from a null
ratio) per the same-session volume semantics correction: when a
checkpoint and the current snapshot belong to different trading
sessions, volume acceleration must not be computed at all, and the
schema should make "not computed" and "computed as some value" clearly
distinguishable rather than relying on null-checking conventions.

This model defines the shape only. The Meaningful Change Engine that
actually computes these signals is a later phase — Phase 1 only needs
the schema to exist and validate correctly.
"""
from datetime import datetime, timezone

from pydantic import BaseModel, Field, model_validator


class ChangeSignals(BaseModel):
    price_change_pct: float
    volume_acceleration_ratio: float | None = None
    volume_acceleration_available: bool

    @model_validator(mode="after")
    def ratio_presence_must_match_availability(self) -> "ChangeSignals":
        """
        Enforce the same-session invariant at the schema level: if the
        volume signal is marked unavailable, no ratio value may be
        attached (it would be data computed from mismatched sessions,
        which architecture.md explicitly forbids); if it IS marked
        available, a ratio value must actually be present.
        """
        if not self.volume_acceleration_available and self.volume_acceleration_ratio is not None:
            raise ValueError(
                "volume_acceleration_ratio must be null when "
                "volume_acceleration_available is false — a ratio must "
                "never be attached for a comparison across a session "
                "boundary"
            )
        if self.volume_acceleration_available and self.volume_acceleration_ratio is None:
            raise ValueError(
                "volume_acceleration_ratio must be present when "
                "volume_acceleration_available is true"
            )
        return self


class ChangeEvent(BaseModel):
    user_id: str = Field(..., min_length=1)
    instrument_id: str = Field(..., min_length=1)
    checkpoint_id: str = Field(..., min_length=1)
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    signals: ChangeSignals
    reason: str = Field(..., min_length=1)
    acknowledged: bool = False
