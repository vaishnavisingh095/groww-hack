"""
Checkpoint Service.

Owns reading/writing Checkpoint documents.

Per the checkpoint semantics contract, a Checkpoint represents the
user's ACKNOWLEDGED market state, and the only thing that may create or
advance one is an explicit user action ("mark as seen" -- single
instrument or whole watchlist), via create_checkpoint_from_snapshot.
Opening the app, rendering, polling, or refreshing (GET /watchlist)
must never create or advance a checkpoint -- see app/routes/watchlist.py.

ensure_initial_checkpoint (IMPLICIT baseline creation) is intentionally
retained below but is no longer called from any read path -- see its
own docstring for why it still exists unused.
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
        """
        return self._write_checkpoint(
            user_id, instrument_id, snapshot, CheckpointSource.EXPLICIT
        )

    def ensure_initial_checkpoint(
        self, user_id: str, instrument_id: str, snapshot: MarketSnapshot
    ) -> Checkpoint | None:
        """
        Establish an IMPLICIT baseline only if no checkpoint exists yet
        for this (user, instrument) pair.

        NOT CALLED from any production code path as of the checkpoint
        semantics fix (see decisions.md): GET /watchlist previously
        called this on first sight of an instrument, but doing so meant
        a mere page load/refresh could establish state that the very
        next read would then treat as an acknowledged comparison
        baseline -- opening the app is not acknowledgement. The method
        is kept, unused, rather than deleted: it is a correct, isolated,
        well-tested primitive (create-if-absent, never advances an
        existing checkpoint) with no incorrect behavior of its own -- the
        bug was calling it from a read path, not the method itself.
        Removing it would mean deleting tested behavior and the
        CheckpointSource.IMPLICIT distinction it exists to exercise,
        which is a separate cleanup decision from the read-path fix this
        change makes.

        Returns the newly created Checkpoint if one was created, or None
        if a checkpoint already existed (in which case nothing is
        touched -- this method NEVER advances or replaces an existing
        checkpoint).
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