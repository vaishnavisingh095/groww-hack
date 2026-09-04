"""
Tests for the price-only Meaningful Change Engine.

Per explicit instruction, this is where the CONTROLLED TEST BASELINE
lives (checkpoint=100, current=102.1 -> meaningful_change=True). This is
pure unit-test data -- literal Python floats passed to a pure function
-- not a hidden fake-data mode in the application itself. The actual
app/demo never runs this code path with anything but real fetched
prices; nothing here reads from or writes to the real market-data path.
"""
import pytest

from app.services.change_engine import PRICE_THRESHOLD_PCT, evaluate_price_change


def test_controlled_baseline_above_threshold_is_meaningful():
    """The exact scenario specified: checkpoint=100, current=102.1 ->
    2.1% move -> meaningful_change=True."""
    result = evaluate_price_change(checkpoint_price=100.0, current_price=102.1)

    assert result.has_baseline is True
    assert result.meaningful_change is True
    assert result.percent_difference == pytest.approx(2.1, abs=0.001)


def test_price_change_below_threshold_is_not_meaningful():
    # 100 -> 101.5 is a 1.5% move, below the 2% threshold
    result = evaluate_price_change(checkpoint_price=100.0, current_price=101.5)

    assert result.meaningful_change is False
    assert result.percent_difference == pytest.approx(1.5, abs=0.001)


def test_price_change_exactly_at_threshold_is_meaningful():
    """Boundary test: exactly 2.00% must count as meaningful (>=, not >)."""
    result = evaluate_price_change(checkpoint_price=100.0, current_price=102.0)

    assert result.percent_difference == pytest.approx(2.0, abs=0.0001)
    assert result.meaningful_change is True


def test_price_change_above_threshold_is_meaningful():
    result = evaluate_price_change(checkpoint_price=100.0, current_price=110.0)

    assert result.percent_difference == pytest.approx(10.0, abs=0.001)
    assert result.meaningful_change is True


def test_price_decrease_uses_absolute_value():
    """A 2.1% DROP must also count as meaningful -- the formula uses
    abs(), so direction doesn't matter, only magnitude."""
    result = evaluate_price_change(checkpoint_price=100.0, current_price=97.9)

    assert result.percent_difference == pytest.approx(2.1, abs=0.001)
    assert result.meaningful_change is True


def test_no_checkpoint_is_first_time_baseline_not_a_change():
    """Explicit requirement: no prior checkpoint must NEVER be reported
    as a meaningful change, regardless of the current price."""
    result = evaluate_price_change(checkpoint_price=None, current_price=99999.0)

    assert result.has_baseline is False
    assert result.meaningful_change is False
    assert result.percent_difference is None
    assert "Baseline created" in result.reason


def test_zero_percent_change_is_not_meaningful():
    result = evaluate_price_change(checkpoint_price=100.0, current_price=100.0)

    assert result.percent_difference == 0.0
    assert result.meaningful_change is False


def test_zero_checkpoint_price_is_treated_as_no_baseline():
    """Defensive: a checkpoint price of 0 (which should never legitimately
    exist) must not cause a division error -- treated as no-baseline."""
    result = evaluate_price_change(checkpoint_price=0.0, current_price=100.0)

    assert result.has_baseline is False
    assert result.meaningful_change is False


def test_negative_checkpoint_price_is_treated_as_no_baseline():
    result = evaluate_price_change(checkpoint_price=-10.0, current_price=100.0)

    assert result.has_baseline is False


def test_threshold_constant_is_two_percent():
    """Confirms the threshold constant matches the specified value --
    catches an accidental edit to the wrong number."""
    assert PRICE_THRESHOLD_PCT == 2.0


def test_reason_string_for_meaningful_change_contains_percentage():
    result = evaluate_price_change(checkpoint_price=100.0, current_price=102.41)
    assert "2.41" in result.reason


def test_reason_string_for_non_meaningful_change():
    result = evaluate_price_change(checkpoint_price=100.0, current_price=100.5)
    assert result.reason == "No meaningful change."
