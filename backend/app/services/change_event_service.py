"""
ChangeEvent Service.

Owns persistence and lifecycle for ChangeEvent documents: creating one
when a meaningful change is detected against an explicit checkpoint, and
acknowledging (superseding) active ones when the user explicitly
acknowledges an instrument.

Per the approved ChangeEvent milestone decisions:

- A ChangeEvent may only be created when ALL of these hold: an explicit
  Checkpoint exists, the current snapshot's status is OK (not stale,
  invalid, or unavailable), and the Change Engine reported
  meaningful_change=True. A stale snapshot may still be DISPLAYED (see
  app/routes/watchlist.py) -- it simply may never create or reuse a
  persisted ChangeEvent.
- The business invariant is one ChangeEvent per
  (user_id, instrument_id, checkpoint_id) -- enforced both here (a
  find-before-insert check) and at the database level (a unique
  compound index, see app/db/indexes.py), which is the actual source of
  truth under concurrent requests. `acknowledged` is deliberately NOT
  part of this identity: an event transitions from unacknowledged to
  acknowledged in place, it is never duplicated because of that
  transition.
- Acknowledging active events is only ever triggered by an explicit
  "mark as seen" action (single instrument or mark-all) -- never by
  GET /watchlist, consistent with the checkpoint semantics contract
  that opening/rendering/refreshing is never acknowledgement.
"""
from pymongo.errors import DuplicateKeyError

from app.models.change_event import ChangeEvent, ChangeSignals
from app.models.checkpoint import Checkpoint
from app.models.market_snapshot import SnapshotStatus
from app.services.change_engine import ChangeResult


class ChangeEventService:
    def __init__(self, db):
        self._db = db

    def get_or_create_active(
        self,
        *,
        user_id: str,
        instrument_id: str,
        checkpoint: Checkpoint | None,
        snapshot_status: SnapshotStatus,
        change_result: ChangeResult,
    ) -> ChangeEvent | None:
        """
        Persist the ChangeEvent for the given (checkpoint, change_result)
        pair, or reuse the one that already exists for this exact
        checkpoint version -- never create a second one.

        Returns None (no-op, nothing persisted) unless ALL of:
        - checkpoint is not None (an explicit baseline exists)
        - snapshot_status == OK (never for stale/invalid/unavailable)
        - change_result.meaningful_change is True

        When an event already exists for
        (user_id, instrument_id, checkpoint.id), it is returned as-is --
        its original detected_at, signals, and reason are never
        overwritten by a later, redundant evaluation of the same
        checkpoint version (e.g. a repeated GET/refresh).
        """
        if checkpoint is None:
            return None
        if snapshot_status != SnapshotStatus.OK:
            return None
        if not change_result.meaningful_change:
            return None

        # Defends against a race between this evaluation and a
        # concurrent explicit Mark as Seen on the same instrument: the
        # `checkpoint` argument was read by the caller (e.g. GET
        # /watchlist) at the START of this request, but a Mark as Seen
        # request racing against it can have already advanced the
        # checkpoint (via CheckpointService's replace_one upsert) to a
        # newer version by the time we get here -- market-data fetches
        # take long enough (network round trip) for this window to be
        # real. Without this check, this evaluation -- computed against
        # an already-superseded checkpoint -- would persist a brand new
        # unacknowledged ChangeEvent for market state the user has
        # already effectively acknowledged. Re-reading the CURRENT
        # checkpoint right before the write (rather than trusting the
        # one passed in) shrinks that window to just this function's own
        # insert, without needing a lock or a cross-collection
        # transaction: the current checkpoint version will still be
        # correctly (re-)evaluated on the very next observation.
        current_checkpoint_doc = self._db.checkpoints.find_one(
            {"user_id": user_id, "instrument_id": instrument_id}
        )
        if current_checkpoint_doc is None or current_checkpoint_doc.get("id") != checkpoint.id:
            return None

        existing = self._find(user_id, instrument_id, checkpoint.id)
        if existing is not None:
            return existing

        event = ChangeEvent(
            user_id=user_id,
            instrument_id=instrument_id,
            checkpoint_id=checkpoint.id,
            signals=ChangeSignals(
                price_change_pct=change_result.price_change_pct,
                volume_acceleration_ratio=change_result.volume_acceleration_ratio,
                volume_acceleration_available=change_result.volume_signal.available,
            ),
            reason=change_result.reason,
        )
        try:
            self._db.change_events.insert_one(event.model_dump(mode="json"))
        except DuplicateKeyError:
            # Another concurrent request won the race and already
            # inserted the event for this exact checkpoint version --
            # the unique index (user_id, instrument_id, checkpoint_id)
            # is the real source of truth here, this find-before-insert
            # check is just the common-case fast path. Reuse whatever
            # was actually persisted rather than raising.
            existing = self._find(user_id, instrument_id, checkpoint.id)
            if existing is not None:
                return existing
            raise
        return event

    def acknowledge_active(self, user_id: str, instrument_id: str) -> None:
        """
        Mark every currently-active (acknowledged=False) ChangeEvent for
        this (user, instrument) as acknowledged. Deliberately not scoped
        to a specific checkpoint_id: at most one checkpoint is ever
        active per (user, instrument), so "supersede whatever is
        currently active for this instrument" is the correct and
        simplest statement of "the user has now seen the current state."

        Only ever call this from an explicit mark-as-seen action, after
        that instrument's new checkpoint has actually been written --
        never from GET /watchlist, and never before a checkpoint write
        that could still fail.
        """
        self._db.change_events.update_many(
            {"user_id": user_id, "instrument_id": instrument_id, "acknowledged": False},
            {"$set": {"acknowledged": True}},
        )

    def _find(self, user_id: str, instrument_id: str, checkpoint_id: str) -> ChangeEvent | None:
        doc = self._db.change_events.find_one(
            {
                "user_id": user_id,
                "instrument_id": instrument_id,
                "checkpoint_id": checkpoint_id,
            }
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return ChangeEvent(**doc)
