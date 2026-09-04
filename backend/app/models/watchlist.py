"""
Watchlist: one user's set of tracked instruments.

Per architecture.md: single watchlist per user for the hackathon MVP
(no multi-list support — see plan.md's explicit CUT list).
"""
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Watchlist(BaseModel):
    user_id: str = Field(..., min_length=1)
    instrument_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
