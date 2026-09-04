from datetime import date

import pytest
from pydantic import ValidationError

from app.models.checkpoint import Checkpoint, CheckpointSource, BaselineSnapshot


def base_checkpoint(**overrides):
    defaults = dict(
        user_id="user123",
        instrument_id="inst123",
        session_date=date(2026, 9, 4),
        baseline_snapshot=BaselineSnapshot(
            last_price=1326.4, volume=9122871, percent_change=1.83
        ),
        source=CheckpointSource.EXPLICIT,
    )
    defaults.update(overrides)
    return defaults


def test_valid_checkpoint_is_accepted():
    cp = Checkpoint(**base_checkpoint())
    assert cp.source == CheckpointSource.EXPLICIT
    assert cp.baseline_snapshot.last_price == 1326.4


def test_implicit_source_is_accepted():
    cp = Checkpoint(**base_checkpoint(source=CheckpointSource.IMPLICIT))
    assert cp.source == CheckpointSource.IMPLICIT


def test_invalid_source_value_is_rejected():
    with pytest.raises(ValidationError):
        Checkpoint(**base_checkpoint(source="magic"))


def test_missing_user_id_is_rejected():
    data = base_checkpoint()
    del data["user_id"]
    with pytest.raises(ValidationError):
        Checkpoint(**data)


def test_missing_baseline_snapshot_is_rejected():
    data = base_checkpoint()
    del data["baseline_snapshot"]
    with pytest.raises(ValidationError):
        Checkpoint(**data)


def test_baseline_snapshot_zero_price_is_rejected():
    """The baseline itself must be a plausible price -- a checkpoint
    frozen from an invalid snapshot should never be constructable."""
    with pytest.raises(ValidationError):
        BaselineSnapshot(last_price=0, volume=100, percent_change=0)


def test_baseline_snapshot_negative_volume_is_rejected():
    with pytest.raises(ValidationError):
        BaselineSnapshot(last_price=100, volume=-5, percent_change=0)


def test_baseline_snapshot_zero_volume_is_allowed():
    """Consistent with MarketSnapshot: zero volume is not automatically
    invalid."""
    baseline = BaselineSnapshot(last_price=100, volume=0, percent_change=0)
    assert baseline.volume == 0


def test_checkpoint_at_defaults_to_now():
    cp = Checkpoint(**base_checkpoint())
    assert cp.checkpoint_at is not None


def test_missing_session_date_is_rejected():
    data = base_checkpoint()
    del data["session_date"]
    with pytest.raises(ValidationError):
        Checkpoint(**data)
