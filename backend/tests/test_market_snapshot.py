import math
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.models.market_snapshot import MarketSnapshot, SnapshotStatus


def base_snapshot(**overrides):
    """A known-good snapshot; override individual fields per test."""
    defaults = dict(
        instrument_id="inst123",
        last_price=1326.4,
        previous_close=1302.6,
        percent_change=MarketSnapshot.compute_percent_change(1326.4, 1302.6),
        volume=9122871,
        session_date=date(2026, 9, 4),
        fetched_at=datetime.now(timezone.utc),
        provider_timestamp=1788509522,
        status=SnapshotStatus.OK,
    )
    defaults.update(overrides)
    return defaults


def test_valid_snapshot_is_accepted():
    snap = MarketSnapshot(**base_snapshot())
    assert snap.status == SnapshotStatus.OK


def test_percent_change_must_match_computed_value():
    """Directly tests the architecture.md rule: percent_change must be
    self-computed, never an unverified provider value passed through."""
    with pytest.raises(ValidationError, match="percent_change"):
        MarketSnapshot(**base_snapshot(percent_change=99.9))


def test_percent_change_helper_matches_validator_expectation():
    """The static helper and the model's own validator must agree,
    or the helper would be useless for constructing valid snapshots."""
    snap = MarketSnapshot(**base_snapshot())
    assert snap.percent_change == pytest.approx(1.827, abs=0.01)


def test_missing_last_price_is_rejected():
    data = base_snapshot()
    del data["last_price"]
    with pytest.raises(ValidationError):
        MarketSnapshot(**data)


def test_zero_last_price_is_rejected():
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(last_price=0))


def test_negative_last_price_is_rejected():
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(last_price=-10))


def test_nan_last_price_is_rejected():
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(last_price=math.nan))


def test_positive_infinity_last_price_is_rejected():
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(last_price=math.inf))


def test_negative_infinity_previous_close_is_rejected():
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(previous_close=-math.inf))


def test_zero_previous_close_is_rejected():
    """previous_close must be > 0 -- both because a real stock price
    cannot be zero, and because percent_change's own formula divides by
    it (a zero previous_close would make percent_change undefined)."""
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(previous_close=0))


def test_zero_volume_is_allowed():
    """Per decisions.md's Invalid Data Rules: zero volume was observed
    under normal conditions in the live test and must NOT be
    automatically treated as invalid."""
    data = base_snapshot(volume=0)
    data["status"] = SnapshotStatus.OK
    snap = MarketSnapshot(**data)
    assert snap.volume == 0


def test_negative_volume_is_rejected():
    """Per decisions.md: negative cumulative volume is a real
    impossibility and must always be rejected."""
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(volume=-100))


def test_missing_volume_is_rejected_at_schema_level():
    """Note: this tests schema-level requiredness. The architecture.md
    rule that a missing/invalid VOLUME should not discard an otherwise-
    valid PRICE is a Market Data Service assembly concern (Phase 3),
    where the service decides whether to construct a MarketSnapshot at
    all vs. degrade gracefully -- it is not something this model,
    which represents a single already-assembled snapshot, can express by
    itself. This is a deliberate scope boundary: see the design notes in
    the implementation report."""
    data = base_snapshot()
    del data["volume"]
    with pytest.raises(ValidationError):
        MarketSnapshot(**data)


def test_missing_session_date_is_rejected():
    data = base_snapshot()
    del data["session_date"]
    with pytest.raises(ValidationError):
        MarketSnapshot(**data)


def test_missing_fetched_at_is_rejected():
    data = base_snapshot()
    del data["fetched_at"]
    with pytest.raises(ValidationError):
        MarketSnapshot(**data)


def test_provider_timestamp_is_optional():
    data = base_snapshot()
    del data["provider_timestamp"]
    snap = MarketSnapshot(**data)
    assert snap.provider_timestamp is None


def test_all_four_status_values_are_accepted():
    for status in SnapshotStatus:
        snap = MarketSnapshot(**base_snapshot(status=status))
        assert snap.status == status


def test_invalid_status_string_is_rejected():
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(status="not_a_real_status"))


def test_missing_instrument_id_is_rejected():
    data = base_snapshot()
    del data["instrument_id"]
    with pytest.raises(ValidationError):
        MarketSnapshot(**data)


def test_empty_instrument_id_is_rejected():
    with pytest.raises(ValidationError):
        MarketSnapshot(**base_snapshot(instrument_id=""))
