"""
Tests for ChangeEventService -- persistence and lifecycle of ChangeEvent
documents.

Uses mongomock (see conftest.py's mock_db fixture docstring) and real
Checkpoint documents written through CheckpointService, so these tests
exercise the actual checkpoint_id identity contract, not a stand-in.
change_result objects are produced by the real evaluate_change() in
price-only mode (checkpoint_price/current_price), which keeps these
tests decoupled from volume/timing plumbing while still exercising a
genuine Change Engine output shape -- consistent with test_change_engine.py's
own price_only() helper.
"""
from datetime import date, datetime, timezone

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

from app.db.indexes import ensure_indexes
from app.models.market_snapshot import MarketSnapshot, SnapshotStatus
from app.services.change_engine import evaluate_change
from app.services.change_event_service import ChangeEventService
from app.services.checkpoint_service import CheckpointService


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["test_db"]
    ensure_indexes(database)
    yield database
    client.close()


def make_snapshot(**overrides) -> MarketSnapshot:
    last_price = overrides.get("last_price", 1326.4)
    previous_close = overrides.get("previous_close", 1302.6)
    defaults = dict(
        instrument_id="inst123",
        last_price=last_price,
        previous_close=previous_close,
        percent_change=MarketSnapshot.compute_percent_change(last_price, previous_close),
        volume=9122871,
        session_date=date(2026, 9, 4),
        fetched_at=datetime.now(timezone.utc),
        provider_timestamp=1788509522,
        status=SnapshotStatus.OK,
    )
    defaults.update(overrides)
    return MarketSnapshot(**defaults)


def meaningful_change(checkpoint_price=100.0, current_price=110.0, price_threshold=None):
    """10% price move -- well above any adaptive threshold in this
    module's tested range (0.5%-3.0%, or the 1.0% fallback when
    price_threshold is left as None), price-only mode (volume signal
    correctly reported unavailable, never fabricated)."""
    return evaluate_change(
        checkpoint_price=checkpoint_price,
        checkpoint_price_threshold=price_threshold,
        current_price=current_price,
    )


def unmeaningful_change(checkpoint_price=100.0, current_price=100.5, price_threshold=None):
    """0.5% price move -- below the 1.0% fallback threshold applied when
    price_threshold is left as None (the default here)."""
    return evaluate_change(
        checkpoint_price=checkpoint_price,
        checkpoint_price_threshold=price_threshold,
        current_price=current_price,
    )


# --- A: no checkpoint -> no ChangeEvent ------------------------------------


def test_no_checkpoint_creates_no_change_event(db):
    service = ChangeEventService(db)

    result = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=None,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(),
    )

    assert result is None
    assert db.change_events.count_documents({}) == 0


# --- B: explicit checkpoint + meaningful OK snapshot -> exactly one event --


def test_explicit_checkpoint_and_meaningful_ok_change_creates_one_event(db):
    checkpoint = CheckpointService(db).create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot()
    )
    service = ChangeEventService(db)

    event = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(),
    )

    assert event is not None
    assert event.checkpoint_id == checkpoint.id
    assert event.user_id == "user1"
    assert event.instrument_id == "inst123"
    assert event.acknowledged is False
    assert db.change_events.count_documents({}) == 1


def test_created_event_persists_the_checkpoints_own_price_threshold(db):
    """End-to-end: the checkpoint's OWN frozen price_threshold_applied
    (computed once at checkpoint creation from make_snapshot()'s
    day_high/day_low) must be what actually lands on the persisted
    ChangeEvent.signals -- not a fixed constant, not recomputed here."""
    snapshot = make_snapshot(day_high=110.0, day_low=100.0, previous_close=100.0)
    checkpoint = CheckpointService(db).create_checkpoint_from_snapshot(
        "user1", "inst123", snapshot
    )
    assert checkpoint.baseline_snapshot.price_threshold_applied == pytest.approx(2.5)

    change_result = evaluate_change(
        checkpoint_price=checkpoint.baseline_snapshot.last_price,
        checkpoint_price_threshold=checkpoint.baseline_snapshot.price_threshold_applied,
        current_price=checkpoint.baseline_snapshot.last_price * 1.03,  # +3% > 2.5% threshold
    )
    assert change_result.meaningful_change is True

    service = ChangeEventService(db)
    event = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=change_result,
    )

    assert event.signals.price_threshold_applied == pytest.approx(2.5)
    stored = db.change_events.find_one({"checkpoint_id": checkpoint.id})
    assert stored["signals"]["price_threshold_applied"] == pytest.approx(2.5)


def test_ok_snapshot_without_meaningful_change_creates_no_event(db):
    checkpoint = CheckpointService(db).create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot()
    )
    service = ChangeEventService(db)

    result = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=unmeaningful_change(),
    )

    assert result is None
    assert db.change_events.count_documents({}) == 0


# --- C & D: repeated calls reuse, never duplicate, never overwrite ---------


def test_repeated_call_for_same_checkpoint_reuses_not_duplicates(db):
    """Simulates repeated GET/refresh against the same checkpoint
    version -- must remain exactly one persisted event."""
    checkpoint = CheckpointService(db).create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot()
    )
    service = ChangeEventService(db)

    first = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(),
    )
    second = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(),
    )
    third = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(),
    )

    assert db.change_events.count_documents({}) == 1
    assert first.detected_at == second.detected_at == third.detected_at


def test_reused_event_preserves_original_detected_at_signals_and_reason(db):
    """A later re-evaluation of the SAME checkpoint version with a
    DIFFERENT (e.g. larger) price move must not overwrite the
    originally-detected values -- the first detection is what's real."""
    checkpoint = CheckpointService(db).create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot()
    )
    service = ChangeEventService(db)

    first_result = meaningful_change(checkpoint_price=100.0, current_price=105.0)  # 5%
    first = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=first_result,
    )

    later_result = meaningful_change(checkpoint_price=100.0, current_price=115.0)  # 15%
    second = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=later_result,
    )

    assert db.change_events.count_documents({}) == 1
    assert second.signals.price_change_pct == first.signals.price_change_pct == pytest.approx(5.0)
    assert second.reason == first.reason
    assert second.detected_at == first.detected_at


# --- I & J: stale / invalid / unavailable never create an event -----------


def test_stale_snapshot_with_meaningful_looking_change_creates_no_event(db):
    checkpoint = CheckpointService(db).create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot()
    )
    service = ChangeEventService(db)

    result = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.STALE,
        change_result=meaningful_change(),
    )

    assert result is None
    assert db.change_events.count_documents({}) == 0


@pytest.mark.parametrize("status", [SnapshotStatus.INVALID, SnapshotStatus.UNAVAILABLE])
def test_invalid_or_unavailable_snapshot_creates_no_event(db, status):
    checkpoint = CheckpointService(db).create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot()
    )
    service = ChangeEventService(db)

    result = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=status,
        change_result=meaningful_change(),
    )

    assert result is None
    assert db.change_events.count_documents({}) == 0


# --- G & H: new checkpoint version -> new, independent event --------------


def test_new_checkpoint_after_old_one_creates_a_new_independent_event(db):
    checkpoint_service = CheckpointService(db)
    event_service = ChangeEventService(db)

    checkpoint_1 = checkpoint_service.create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot(last_price=100.0)
    )
    event_1 = event_service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint_1,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(checkpoint_price=100.0, current_price=110.0),
    )
    assert event_1 is not None

    # User explicitly re-acknowledges -- a brand new checkpoint version.
    checkpoint_2 = checkpoint_service.create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot(last_price=110.0)
    )
    assert checkpoint_2.id != checkpoint_1.id

    event_2 = event_service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint_2,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(checkpoint_price=110.0, current_price=130.0),
    )

    assert event_2 is not None
    assert event_2.checkpoint_id == checkpoint_2.id
    assert event_2.checkpoint_id != event_1.checkpoint_id
    assert db.change_events.count_documents({}) == 2

    # The old event, tied to the old checkpoint id, is untouched.
    stored_old = db.change_events.find_one({"checkpoint_id": checkpoint_1.id})
    assert stored_old["checkpoint_id"] == checkpoint_1.id
    assert stored_old["acknowledged"] is False


# --- E: acknowledge_active ---------------------------------------------


def test_evaluation_against_a_superseded_checkpoint_creates_no_orphaned_event(db):
    """REGRESSION/L3: simulates the race where a GET's change evaluation
    was computed against checkpoint_1, but by the time it goes to
    persist the ChangeEvent, a concurrent explicit Mark as Seen has
    already advanced the checkpoint to checkpoint_2 (e.g. the GET's
    market-data fetch was in flight while the user clicked Mark as Seen
    on the same instrument). Persisting an event tied to the
    now-superseded checkpoint_1 would resurface, as a brand new
    unacknowledged attention item, a market state the user has already
    effectively acknowledged. This must be a no-op instead -- the
    current checkpoint (checkpoint_2) will be correctly (re-)evaluated
    on the next observation."""
    checkpoint_service = CheckpointService(db)
    event_service = ChangeEventService(db)

    checkpoint_1 = checkpoint_service.create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot(last_price=100.0)
    )
    # A concurrent explicit Mark as Seen already advanced the checkpoint
    # before this (stale) evaluation gets around to persisting its event.
    checkpoint_2 = checkpoint_service.create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot(last_price=105.0)
    )
    assert checkpoint_2.id != checkpoint_1.id

    result = event_service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint_1,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(checkpoint_price=100.0, current_price=110.0),
    )

    assert result is None
    assert db.change_events.count_documents({}) == 0


def test_acknowledge_active_marks_matching_events_and_leaves_other_instruments_alone(db):
    checkpoint_service = CheckpointService(db)
    event_service = ChangeEventService(db)

    checkpoint_a = checkpoint_service.create_checkpoint_from_snapshot(
        "user1", "inst-A", make_snapshot(instrument_id="inst-A", last_price=100.0)
    )
    checkpoint_b = checkpoint_service.create_checkpoint_from_snapshot(
        "user1", "inst-B", make_snapshot(instrument_id="inst-B", last_price=100.0)
    )
    event_service.get_or_create_active(
        user_id="user1",
        instrument_id="inst-A",
        checkpoint=checkpoint_a,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(),
    )
    event_service.get_or_create_active(
        user_id="user1",
        instrument_id="inst-B",
        checkpoint=checkpoint_b,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(),
    )

    event_service.acknowledge_active("user1", "inst-A")

    event_a = db.change_events.find_one({"instrument_id": "inst-A"})
    event_b = db.change_events.find_one({"instrument_id": "inst-B"})
    assert event_a["acknowledged"] is True
    assert event_b["acknowledged"] is False


def test_concurrent_insert_race_recovers_existing_event_instead_of_raising(db, monkeypatch):
    """REGRESSION/L1: simulates two concurrent requests both detecting
    the same meaningful change against the same checkpoint version. The
    'losing' request's insert_one hits the unique
    (user_id, instrument_id, checkpoint_id) index and raises
    DuplicateKeyError, exactly as real MongoDB would under genuine
    concurrency -- the service must recover by returning whatever the
    'winning' request actually persisted, never raise, and never leave
    more than one document behind."""
    checkpoint = CheckpointService(db).create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot()
    )
    service = ChangeEventService(db)

    real_insert_one = db.change_events.insert_one

    def racing_insert_one(doc):
        # Simulate the winning concurrent request's insert landing
        # first (using the real collection method, not a mock), then
        # this request's own attempt fails on the unique index, exactly
        # as pymongo itself would report it.
        real_insert_one(doc)
        raise DuplicateKeyError("simulated concurrent insert")

    monkeypatch.setattr(db.change_events, "insert_one", racing_insert_one)

    result = service.get_or_create_active(
        user_id="user1",
        instrument_id="inst123",
        checkpoint=checkpoint,
        snapshot_status=SnapshotStatus.OK,
        change_result=meaningful_change(),
    )

    assert result is not None
    assert result.checkpoint_id == checkpoint.id
    assert db.change_events.count_documents({}) == 1


def test_acknowledge_active_is_a_no_op_when_no_events_exist(db):
    """Calling acknowledge on an instrument with no ChangeEvent history
    at all must not raise -- this is the normal case for most
    mark-as-seen calls (nothing meaningful happened since last time)."""
    ChangeEventService(db).acknowledge_active("user1", "inst123")
    assert db.change_events.count_documents({}) == 0
