"""
Checkpoint Service.

Owns reading/writing Checkpoint documents. Per explicit instruction for
this slice: checkpoints are created ONLY via an explicit "mark as seen"
action -- there is no implicit checkpoint-on-page-load logic here. (The
full design in architecture.md includes implicit checkpoint creation as
a secondary mechanism; this slice implements explicit-only, which is a
strict subset of the approved behavior, not a contradiction of it.)
"""
from datetime import datetime, timezone

from pymongo.database import Database

from app.models.checkpoint import BaselineSnapshot, Checkpoint, CheckpointSource
from app.models.market_snapshot import MarketSnapshot


class CheckpointService:
    def __init__(self, db: Database):
        self._db = db

    def get_checkpoint(self, user_id: str, instrument_id: str) -> Checkpoint | None:
        doc = self._db.checkpoints.find_one(
            {"user_id": user_id, "instrument_id": instrument_id}
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return Checkpoint(**doc)

    def create_checkpoint_from_snapshot(
        self, user_id: str, instrument_id: str, snapshot: MarketSnapshot
    ) -> Checkpoint:
        """
        Explicitly persist the CURRENT snapshot as the new checkpoint
        baseline. This is only ever called in direct response to a user
        action (the "mark as seen" endpoint) -- never automatically on a
        read/GET request, per the explicit instruction not to silently
        overwrite checkpoints just because the user opened the page.
        """
        checkpoint = Checkpoint(
            user_id=user_id,
            instrument_id=instrument_id,
            checkpoint_at=datetime.now(timezone.utc),
            session_date=snapshot.session_date,
            baseline_snapshot=BaselineSnapshot(
                last_price=snapshot.last_price,
                volume=snapshot.volume,
                percent_change=snapshot.percent_change,
            ),
            source=CheckpointSource.EXPLICIT,
        )

        # Upsert keyed on (user_id, instrument_id) -- the unique index
        # from Phase 1 (uniq_user_instrument_checkpoint) is what makes
        # this "replace the existing checkpoint" pattern safe rather
        # than risking a duplicate-key error or an accidental second
        # document.
        self._db.checkpoints.replace_one(
            {"user_id": user_id, "instrument_id": instrument_id},
            checkpoint.model_dump(mode="json"),
            upsert=True,
        )
        return checkpoint
