"""
Checkpoint Service.

Owns reading/writing Checkpoint documents. Supports both advancement
mechanisms from architecture.md:
- EXPLICIT: the user's own "mark as seen" action (single instrument or
  whole watchlist) -- always advances/replaces the existing checkpoint.
- IMPLICIT: automatic initial baseline establishment the first time an
  instrument has a valid snapshot but no checkpoint yet for this user
  (architecture.md, hard question G: "creates an implicit checkpoint on
  first sight, which resolves this state on the instrument's next poll
  cycle"). This NEVER advances/replaces an existing checkpoint -- it
  only fires when none exists at all. This is what keeps GET /watchlist
  from silently moving a baseline the user has already seen.
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
        Explicitly ADVANCE (create or replace) the checkpoint using the
        given snapshot as the new baseline. This is the "mark as seen"
        primitive -- called directly in response to a user action (single
        instrument or as part of mark-all), and unconditionally replaces
        whatever checkpoint existed before, per architecture.md's "one
        active checkpoint per (user, instrument) pair -- advancing the
        checkpoint replaces the previous one."

        Never call this from a read path (e.g. GET /watchlist) -- doing
        so would silently move the user's baseline without their action.
        Use ensure_initial_checkpoint for the read-path/implicit case.
        """
        return self._write_checkpoint(
            user_id, instrument_id, snapshot, CheckpointSource.EXPLICIT
        )

    def ensure_initial_checkpoint(
        self, user_id: str, instrument_id: str, snapshot: MarketSnapshot
    ) -> Checkpoint | None:
        """
        Establish an IMPLICIT baseline only if no checkpoint exists yet
        for this (user, instrument) pair. Per architecture.md (hard
        question G): "if no Checkpoint exists ... the Checkpoint Service
        creates an implicit checkpoint on first sight, which resolves
        this state on the instrument's next poll cycle."

        Returns the newly created Checkpoint if one was created, or None
        if a checkpoint already existed (in which case nothing is
        touched -- this method NEVER advances or replaces an existing
        checkpoint, which is what distinguishes it from
        create_checkpoint_from_snapshot and is exactly what makes it
        safe to call from a read path like GET /watchlist).

        Because this checkpoint is created using the CURRENT request's
        snapshot, the comparison for THIS same request still correctly
        shows "no baseline" (the caller should compare against whatever
        get_checkpoint returned before this call, not after) -- the
        newly-created checkpoint only takes effect starting from the
        next request, matching "resolves this state on the instrument's
        next poll cycle" precisely.
        """
        existing = self.get_checkpoint(user_id, instrument_id)
        if existing is not None:
            return None  # never advance an existing checkpoint implicitly

        return self._write_checkpoint(
            user_id, instrument_id, snapshot, CheckpointSource.IMPLICIT
        )

    def _write_checkpoint(
        self,
        user_id: str,
        instrument_id: str,
        snapshot: MarketSnapshot,
        source: CheckpointSource,
    ) -> Checkpoint:
        """
        Shared write path for both explicit and implicit checkpoint
        creation -- the only difference between the two is the `source`
        tag and the caller's decision about WHEN to call this (always,
        for explicit; only-if-absent, for implicit). The actual
        persistence/frozen-copy logic is identical either way, so it is
        not duplicated between create_checkpoint_from_snapshot and
        ensure_initial_checkpoint.
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
            source=source,
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