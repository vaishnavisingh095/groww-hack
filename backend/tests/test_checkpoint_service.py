from datetime import date, datetime, timezone

import mongomock
import pytest

from app.db.indexes import ensure_indexes
from app.models.market_snapshot import MarketSnapshot, SnapshotStatus
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


def test_get_checkpoint_returns_none_when_none_exists(db):
    service = CheckpointService(db)
    result = service.get_checkpoint("user1", "inst123")
    assert result is None


def test_create_checkpoint_persists_to_mongodb(db):
    service = CheckpointService(db)
    snapshot = make_snapshot()

    checkpoint = service.create_checkpoint_from_snapshot("user1", "inst123", snapshot)

    assert checkpoint.baseline_snapshot.last_price == 1326.4
    # Confirm it actually landed in the database, not just returned
    doc = db.checkpoints.find_one({"user_id": "user1", "instrument_id": "inst123"})
    assert doc is not None
    assert doc["baseline_snapshot"]["last_price"] == 1326.4


def test_created_checkpoint_can_be_read_back(db):
    service = CheckpointService(db)
    snapshot = make_snapshot()
    service.create_checkpoint_from_snapshot("user1", "inst123", snapshot)

    fetched = service.get_checkpoint("user1", "inst123")

    assert fetched is not None
    assert fetched.baseline_snapshot.last_price == 1326.4
    assert fetched.user_id == "user1"
    assert fetched.instrument_id == "inst123"


def test_creating_checkpoint_twice_replaces_not_duplicates(db):
    """This is the core of 'mark as seen' -- advancing a checkpoint must
    replace the old baseline, never create a second document."""
    service = CheckpointService(db)

    first_snapshot = make_snapshot(last_price=1326.4)
    service.create_checkpoint_from_snapshot("user1", "inst123", first_snapshot)

    second_snapshot = make_snapshot(last_price=1358.4)
    service.create_checkpoint_from_snapshot("user1", "inst123", second_snapshot)

    count = db.checkpoints.count_documents({"user_id": "user1", "instrument_id": "inst123"})
    assert count == 1

    fetched = service.get_checkpoint("user1", "inst123")
    assert fetched.baseline_snapshot.last_price == 1358.4  # the NEW baseline


def test_checkpoints_for_different_instruments_are_independent(db):
    service = CheckpointService(db)
    service.create_checkpoint_from_snapshot(
        "user1", "inst-A", make_snapshot(instrument_id="inst-A", last_price=100.0)
    )
    service.create_checkpoint_from_snapshot(
        "user1", "inst-B", make_snapshot(instrument_id="inst-B", last_price=200.0)
    )

    a = service.get_checkpoint("user1", "inst-A")
    b = service.get_checkpoint("user1", "inst-B")

    assert a.baseline_snapshot.last_price == 100.0
    assert b.baseline_snapshot.last_price == 200.0


def test_checkpoint_session_date_is_copied_from_snapshot(db):
    service = CheckpointService(db)
    snapshot = make_snapshot(session_date=date(2026, 9, 4))

    checkpoint = service.create_checkpoint_from_snapshot("user1", "inst123", snapshot)

    assert checkpoint.session_date == date(2026, 9, 4)


def test_create_checkpoint_from_snapshot_always_produces_explicit_source(db):
    """create_checkpoint_from_snapshot is the explicit "mark as seen"
    primitive -- it always tags the result as explicit, regardless of
    what existed before (see ensure_initial_checkpoint for the implicit
    counterpart). This is a permanent fact about this method, not a
    scope limitation of an earlier explicit-only slice -- the system now
    supports both explicit and implicit checkpoints (see the tests
    below for the implicit side)."""
    service = CheckpointService(db)
    checkpoint = service.create_checkpoint_from_snapshot("user1", "inst123", make_snapshot())

    assert checkpoint.source.value == "explicit"


def test_ensure_initial_checkpoint_creates_implicit_baseline_when_none_exists(db):
    """Core of the implicit-checkpoint mechanism (architecture.md, hard
    question G): when no checkpoint exists yet, ensure_initial_checkpoint
    must create one, tagged as implicit."""
    service = CheckpointService(db)
    snapshot = make_snapshot(last_price=1322.0)

    result = service.ensure_initial_checkpoint("user1", "inst123", snapshot)

    assert result is not None
    assert result.source.value == "implicit"
    assert result.baseline_snapshot.last_price == 1322.0

    # Confirm it actually persisted, not just returned
    fetched = service.get_checkpoint("user1", "inst123")
    assert fetched is not None
    assert fetched.source.value == "implicit"


def test_ensure_initial_checkpoint_does_not_advance_existing_checkpoint(db):
    """CRITICAL invariant: ensure_initial_checkpoint must NEVER replace
    an existing checkpoint, regardless of how different the new
    snapshot's price is. This is what makes it safe to call from a read
    path (GET /watchlist) without silently moving a baseline the user
    has already explicitly set."""
    service = CheckpointService(db)

    # User already has an EXPLICIT checkpoint at 1300.0
    service.create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot(last_price=1300.0)
    )

    # A later GET-path call tries to "ensure" a baseline with a very
    # different price -- this must be a no-op.
    result = service.ensure_initial_checkpoint(
        "user1", "inst123", make_snapshot(last_price=9999.0)
    )

    assert result is None  # nothing was created/changed

    fetched = service.get_checkpoint("user1", "inst123")
    assert fetched.baseline_snapshot.last_price == 1300.0  # untouched
    assert fetched.source.value == "explicit"  # NOT overwritten to implicit


def test_ensure_initial_checkpoint_does_not_advance_existing_implicit_checkpoint():
    """Same invariant, but starting from an existing IMPLICIT checkpoint
    -- calling ensure_initial_checkpoint again (e.g., on a later GET)
    must still be a no-op, not a repeated re-creation."""
    client = mongomock.MongoClient()
    db = client["test_db_implicit_twice"]
    ensure_indexes(db)
    service = CheckpointService(db)

    first = service.ensure_initial_checkpoint(
        "user1", "inst123", make_snapshot(last_price=1300.0)
    )
    assert first is not None

    second = service.ensure_initial_checkpoint(
        "user1", "inst123", make_snapshot(last_price=1400.0)
    )
    assert second is None  # no-op, already exists

    fetched = service.get_checkpoint("user1", "inst123")
    assert fetched.baseline_snapshot.last_price == 1300.0  # still the first value
    client.close()


def test_explicit_checkpoint_always_advances_even_after_implicit_one_exists(db):
    """The other direction: an EXPLICIT "mark as seen" must always
    advance/replace, even if the existing checkpoint was implicit. This
    is the normal "user finally acts on the auto-created baseline" case."""
    service = CheckpointService(db)

    implicit = service.ensure_initial_checkpoint(
        "user1", "inst123", make_snapshot(last_price=1300.0)
    )
    assert implicit.source.value == "implicit"

    explicit = service.create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot(last_price=1350.0)
    )
    assert explicit.source.value == "explicit"

    fetched = service.get_checkpoint("user1", "inst123")
    assert fetched.baseline_snapshot.last_price == 1350.0
    assert fetched.source.value == "explicit"

    # Still exactly one document -- advancement replaces, never adds.
    assert db.checkpoints.count_documents({"user_id": "user1", "instrument_id": "inst123"}) == 1