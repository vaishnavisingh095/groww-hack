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


def meaningful_change(checkpoint_price=100.0, current_price=110.0):
    """10% price move -- well above the 2% threshold, price-only mode
    (volume signal correctly reported unavailable, never fabricated)."""
    return evaluate_change(checkpoint_price=checkpoint_price, current_price=current_price)


def unmeaningful_change(checkpoint_price=100.0, current_price=100.5):
    """0.5% price move -- below the 2% threshold."""
    return evaluate_change(checkpoint_price=checkpoint_price, current_price=current_price)


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


def test_acknowledge_active_is_a_no_op_when_no_events_exist(db):
    """Calling acknowledge on an instrument with no ChangeEvent history
    at all must not raise -- this is the normal case for most
    mark-as-seen calls (nothing meaningful happened since last time)."""
    ChangeEventService(db).acknowledge_active("user1", "inst123")
    assert db.change_events.count_documents({}) == 0
