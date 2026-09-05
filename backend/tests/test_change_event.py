import pytest
from pydantic import ValidationError

from app.models.change_event import ChangeEvent, ChangeSignals


def base_change_event(**overrides):
    defaults = dict(
        user_id="user123",
        instrument_id="inst123",
        checkpoint_id="cp123",
        signals=ChangeSignals(
            price_change_pct=4.2,
            volume_acceleration_ratio=2.3,
            volume_acceleration_available=True,
        ),
        reason="RELIANCE moved 4.2%. Trading volume accelerated to 2.3x the rate observed before you last checked.",
    )
    defaults.update(overrides)
    return defaults


def test_valid_change_event_with_volume_signal_is_accepted():
    event = ChangeEvent(**base_change_event())
    assert event.signals.volume_acceleration_available is True
    assert event.signals.volume_acceleration_ratio == 2.3


def test_price_only_change_event_is_accepted():
    """Represents a same-session-boundary case: price signal present,
    volume signal explicitly unavailable."""
    event = ChangeEvent(
        **base_change_event(
            signals=ChangeSignals(
                price_change_pct=3.1,
                volume_acceleration_ratio=None,
                volume_acceleration_available=False,
            ),
            reason="TCS moved 3.1%.",
        )
    )
    assert event.signals.volume_acceleration_available is False
    assert event.signals.volume_acceleration_ratio is None


def test_ratio_present_but_marked_unavailable_is_rejected():
    """CRITICAL invariant test: a ratio must never be attached when the
    signal is marked unavailable -- this is exactly the same-session
    volume semantics correction from decisions.md. Enforced at the
    schema level so it cannot be silently violated by a future caller."""
    with pytest.raises(ValidationError, match="volume_acceleration_ratio"):
        ChangeSignals(
            price_change_pct=1.0,
            volume_acceleration_ratio=2.3,
            volume_acceleration_available=False,
        )


def test_available_but_missing_ratio_is_rejected():
    """The inverse invariant: if the signal IS marked available, a real
    ratio value must actually be present."""
    with pytest.raises(ValidationError, match="volume_acceleration_ratio"):
        ChangeSignals(
            price_change_pct=1.0,
            volume_acceleration_ratio=None,
            volume_acceleration_available=True,
        )


def test_missing_reason_is_rejected():
    data = base_change_event()
    del data["reason"]
    with pytest.raises(ValidationError):
        ChangeEvent(**data)


def test_empty_reason_is_rejected():
    with pytest.raises(ValidationError):
        ChangeEvent(**base_change_event(reason=""))


def test_acknowledged_defaults_to_false():
    event = ChangeEvent(**base_change_event())
    assert event.acknowledged is False


def test_missing_checkpoint_id_is_rejected():
    data = base_change_event()
    del data["checkpoint_id"]
    with pytest.raises(ValidationError):
        ChangeEvent(**data)


def test_price_threshold_applied_can_be_set_explicitly():
    event = ChangeEvent(
        **base_change_event(
            signals=ChangeSignals(
                price_change_pct=1.5,
                volume_acceleration_ratio=None,
                volume_acceleration_available=False,
                price_threshold_applied=1.23,
            )
        )
    )
    assert event.signals.price_threshold_applied == 1.23


def test_price_threshold_applied_defaults_to_none_for_backward_compatibility():
    """A ChangeEvent/ChangeSignals document written before this field
    existed must still construct without it -- the key is simply absent
    from the dict, not merely None, matching how a real legacy MongoDB
    document would look."""
    legacy_signals_data = {
        "price_change_pct": 1.5,
        "volume_acceleration_ratio": None,
        "volume_acceleration_available": False,
        # price_threshold_applied deliberately omitted.
    }
    signals = ChangeSignals(**legacy_signals_data)
    assert signals.price_threshold_applied is None

    event = ChangeEvent(**base_change_event(signals=signals))
    assert event.signals.price_threshold_applied is None
