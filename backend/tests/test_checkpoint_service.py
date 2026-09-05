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


def test_create_checkpoint_computes_and_persists_adaptive_price_threshold(db):
    """The adaptive price threshold is computed ONCE, from the exact
    snapshot establishing this checkpoint (day_high=110, day_low=100,
    previous_close=100 -> range_percent=10.0 -> 0.25*10.0=2.5), and
    persisted onto baseline_snapshot.price_threshold_applied."""
    service = CheckpointService(db)
    snapshot = make_snapshot(day_high=110.0, day_low=100.0, previous_close=100.0)

    checkpoint = service.create_checkpoint_from_snapshot("user1", "inst123", snapshot)

    assert checkpoint.baseline_snapshot.price_threshold_applied == pytest.approx(2.5)
    fetched = service.get_checkpoint("user1", "inst123")
    assert fetched.baseline_snapshot.price_threshold_applied == pytest.approx(2.5)


def test_create_checkpoint_with_missing_range_falls_back_to_default_threshold(db):
    """A snapshot with no day_high/day_low (the common case today, since
    not every provider path populates them) must not fail checkpoint
    creation -- falls back to ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT."""
    from app.services.change_engine import ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT

    service = CheckpointService(db)
    snapshot = make_snapshot()  # day_high/day_low default to None

    checkpoint = service.create_checkpoint_from_snapshot("user1", "inst123", snapshot)

    assert checkpoint.baseline_snapshot.price_threshold_applied == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT


def test_create_checkpoint_before_session_open_uses_early_session_fallback(db):
    """Wiring proof that _write_checkpoint actually passes its own
    checkpoint_at/session_date through to
    _compute_adaptive_price_threshold (not just day_high/day_low/
    previous_close as before). checkpoint_at is always real wall-clock
    "now" here (this test does not control it) -- so instead the
    snapshot's session_date is set to TOMORROW, meaning that session's
    market open is still in the future relative to "now," making
    elapsed-time-since-open negative and therefore always below the
    guard, deterministically, regardless of what real time the suite
    happens to run at. A WIDE range that would otherwise clamp to the
    3.0% ceiling must still resolve to the early-session fallback."""
    from datetime import timedelta

    from app.services.change_engine import EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT

    service = CheckpointService(db)
    future_session_date = date.today() + timedelta(days=1)
    snapshot = make_snapshot(
        day_high=150.0, day_low=100.0, previous_close=100.0,
        session_date=future_session_date,
    )

    checkpoint = service.create_checkpoint_from_snapshot("user1", "inst123", snapshot)

    assert (
        checkpoint.baseline_snapshot.price_threshold_applied
        == EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT
    )


def test_checkpoint_threshold_does_not_drift_when_replaced_with_wider_range(db):
    """CRITICAL invariant (explicit product requirement): re-establishing
    a checkpoint with a DIFFERENT observed range produces a NEW, distinct
    threshold on the NEW checkpoint version -- but the value is frozen
    per checkpoint VERSION, never silently recomputed in place. This
    proves the mechanism (computed fresh only at explicit
    create/replace time, not on any read), which is what
    'the threshold must not drift from a later, wider range' means: it
    only changes when the user explicitly re-acknowledges, exactly like
    baseline_snapshot.last_price itself already does."""
    service = CheckpointService(db)

    narrow_snapshot = make_snapshot(day_high=100.4, day_low=100.0, previous_close=100.0)
    first = service.create_checkpoint_from_snapshot("user1", "inst123", narrow_snapshot)
    assert first.baseline_snapshot.price_threshold_applied == pytest.approx(0.5)  # clamped to floor

    # The SAME (user, instrument) checkpoint is explicitly re-established
    # later in the day, once the observed range has widened considerably.
    wide_snapshot = make_snapshot(day_high=150.0, day_low=100.0, previous_close=100.0)
    second = service.create_checkpoint_from_snapshot("user1", "inst123", wide_snapshot)
    assert second.baseline_snapshot.price_threshold_applied == pytest.approx(3.0)  # clamped to ceiling

    # Only the latest (explicitly re-established) version is stored --
    # this is the SAME "advancing replaces, never adds" behavior already
    # exercised by test_creating_checkpoint_twice_replaces_not_duplicates,
    # now also proven for the threshold field specifically.
    fetched = service.get_checkpoint("user1", "inst123")
    assert fetched.baseline_snapshot.price_threshold_applied == pytest.approx(3.0)
    assert fetched.id == second.id


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


def test_advancing_a_checkpoint_assigns_a_new_id(db):
    """Regression for the ChangeEvent milestone: MongoDB's own `_id` is
    preserved unchanged across replace_one, so it cannot distinguish
    'the baseline before this mark-as-seen' from 'the baseline after
    it'. Checkpoint.id must therefore be a genuinely new value on every
    advance -- this is what lets a ChangeEvent durably reference the
    exact checkpoint version it was detected against."""
    service = CheckpointService(db)

    first = service.create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot(last_price=1300.0)
    )
    second = service.create_checkpoint_from_snapshot(
        "user1", "inst123", make_snapshot(last_price=1350.0)
    )

    assert first.id != second.id

    fetched = service.get_checkpoint("user1", "inst123")
    assert fetched.id == second.id  # the currently-stored checkpoint is the latest version


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