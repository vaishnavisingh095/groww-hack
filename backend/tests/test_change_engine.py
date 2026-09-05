"""
Tests for the Phase 5 Meaningful Change Engine.

All tests are pure unit tests against evaluate_change() -- no MongoDB,
no network, no live Yahoo Finance. Timestamps are constructed explicitly
so every test is fully deterministic (see the rehearsal's own lesson:
tests must prove the formula, not just "look plausible").
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.change_engine import (
    PRICE_CHANGE_THRESHOLD_PCT,
    VOLUME_ACCELERATION_THRESHOLD,
    evaluate_change,
)

IST = ZoneInfo("Asia/Kolkata")
SESSION_DATE = date(2026, 9, 4)


def ist_time_on_session(hour: int, minute: int) -> datetime:
    """Build a UTC-aware datetime for a given IST wall-clock time on
    SESSION_DATE, so tests can express 'checkpoint at 10:00 AM IST' etc.
    directly and readably."""
    local = datetime.combine(SESSION_DATE, time(hour, minute), tzinfo=IST)
    return local.astimezone(timezone.utc)


# Convenience: a checkpoint at 10:15 AM IST (60 real minutes after the
# 9:15 AM open) with a round baseline volume, so rate_before is a clean
# number (1000 shares/minute) for hand-checkable test math.
CHECKPOINT_AT = ist_time_on_session(10, 15)
CHECKPOINT_VOLUME = 60_000  # 60 minutes since open -> rate_before = 1000/min


def price_only(checkpoint_price, current_price):
    """Call evaluate_change with price args only -- volume signal must
    be reported unavailable, never fabricated, in this mode."""
    return evaluate_change(checkpoint_price=checkpoint_price, current_price=current_price)


def with_volume(checkpoint_price, current_price, current_volume, minutes_after_checkpoint):
    """Call evaluate_change with full price+volume+timing args. Volume
    delta and elapsed time are chosen by the caller; current_fetched_at
    is derived from CHECKPOINT_AT + minutes_after_checkpoint."""
    current_fetched_at = CHECKPOINT_AT + timedelta(minutes=minutes_after_checkpoint)
    return evaluate_change(
        checkpoint_price=checkpoint_price,
        checkpoint_volume=CHECKPOINT_VOLUME,
        checkpoint_at=CHECKPOINT_AT,
        checkpoint_session_date=SESSION_DATE,
        current_price=current_price,
        current_volume=current_volume,
        current_fetched_at=current_fetched_at,
        current_session_date=SESSION_DATE,
    )


# ---------- 1. No checkpoint ----------


def test_no_checkpoint_is_baseline_pending_not_meaningful():
    result = price_only(checkpoint_price=None, current_price=99999.0)

    assert result.has_baseline is False
    assert result.meaningful_change is False
    assert result.price_change_pct is None
    assert result.volume_acceleration_ratio is None
    assert result.reason == "Baseline pending — no previous check to compare against."


# ---------- 2-6. Price signal boundaries and direction ----------


def test_price_plus_1_9_percent_is_not_meaningful():
    result = price_only(checkpoint_price=100.0, current_price=101.9)

    assert result.price_signal.meaningful is False
    assert result.meaningful_change is False
    assert result.price_change_pct == pytest.approx(1.9, abs=0.001)


def test_price_plus_2_0_percent_is_meaningful_inclusive_boundary():
    """2.0% is the threshold; inclusive (>=), so exactly 2.0 must count."""
    result = price_only(checkpoint_price=100.0, current_price=102.0)

    assert result.price_signal.meaningful is True
    assert result.meaningful_change is True
    assert result.price_change_pct == pytest.approx(2.0, abs=0.0001)


def test_price_minus_2_0_percent_is_meaningful_inclusive_boundary():
    result = price_only(checkpoint_price=100.0, current_price=98.0)

    assert result.price_signal.meaningful is True
    assert result.meaningful_change is True
    assert result.price_change_pct == pytest.approx(-2.0, abs=0.0001)


def test_price_plus_2_4_percent_meaningful_with_positive_explanation():
    result = price_only(checkpoint_price=100.0, current_price=102.4)

    assert result.meaningful_change is True
    assert "+2.4%" in result.reason
    assert result.price_change_pct == pytest.approx(2.4, abs=0.001)


def test_price_minus_2_4_percent_meaningful_with_negative_explanation():
    result = price_only(checkpoint_price=100.0, current_price=97.6)

    assert result.meaningful_change is True
    assert "-2.4%" in result.reason
    assert result.price_change_pct == pytest.approx(-2.4, abs=0.001)


# ---------- 7-9. Volume signal boundaries ----------


def test_volume_acceleration_1_9x_is_not_meaningful():
    # rate_before = 1000/min. For a 1.9x ratio over 30 minutes elapsed:
    # rate_after = 1.9 * 1000 = 1900/min -> delta volume = 1900 * 30 = 57000
    result = with_volume(
        checkpoint_price=100.0,
        current_price=100.0,  # price flat, isolate the volume signal
        current_volume=CHECKPOINT_VOLUME + 57_000,
        minutes_after_checkpoint=30,
    )

    assert result.volume_signal.available is True
    assert result.volume_signal.meaningful is False
    assert result.volume_signal.volume_acceleration_ratio == pytest.approx(1.9, abs=0.01)
    assert result.meaningful_change is False


def test_volume_acceleration_2_0x_is_meaningful_inclusive_boundary():
    # rate_after = 2.0 * 1000 = 2000/min over 30 min -> delta = 60000
    result = with_volume(
        checkpoint_price=100.0,
        current_price=100.0,
        current_volume=CHECKPOINT_VOLUME + 60_000,
        minutes_after_checkpoint=30,
    )

    assert result.volume_signal.available is True
    assert result.volume_signal.volume_acceleration_ratio == pytest.approx(2.0, abs=0.01)
    assert result.volume_signal.meaningful is True
    assert result.meaningful_change is True


def test_volume_acceleration_above_2x_is_meaningful():
    # rate_after = 3.0 * 1000 = 3000/min over 30 min -> delta = 90000
    result = with_volume(
        checkpoint_price=100.0,
        current_price=100.0,
        current_volume=CHECKPOINT_VOLUME + 90_000,
        minutes_after_checkpoint=30,
    )

    assert result.volume_signal.volume_acceleration_ratio == pytest.approx(3.0, abs=0.01)
    assert result.volume_signal.meaningful is True
    assert result.meaningful_change is True


# ---------- 10-14. Combination logic ----------


def test_price_below_and_volume_below_threshold_is_not_meaningful():
    result = with_volume(
        checkpoint_price=100.0,
        current_price=101.0,  # +1.0%, below threshold
        current_volume=CHECKPOINT_VOLUME + 30_000,  # 1.0x, below threshold
        minutes_after_checkpoint=30,
    )

    assert result.price_signal.meaningful is False
    assert result.volume_signal.meaningful is False
    assert result.meaningful_change is False
    assert result.reason == "No meaningful change since your last check."


def test_price_below_and_volume_above_threshold_is_meaningful():
    result = with_volume(
        checkpoint_price=100.0,
        current_price=101.0,  # +1.0%, below threshold
        current_volume=CHECKPOINT_VOLUME + 90_000,  # 3.0x, above threshold
        minutes_after_checkpoint=30,
    )

    assert result.price_signal.meaningful is False
    assert result.volume_signal.meaningful is True
    assert result.meaningful_change is True
    assert "accelerated" in result.reason
    assert "%" not in result.reason  # price-only phrasing must not leak in


def test_price_above_threshold_with_volume_unavailable_is_still_meaningful():
    """Price signal alone must be sufficient -- an unavailable volume
    signal must never suppress an otherwise-meaningful price signal."""
    result = price_only(checkpoint_price=100.0, current_price=103.0)

    assert result.price_signal.meaningful is True
    assert result.volume_signal.available is False
    assert result.meaningful_change is True
    assert "3.0%" in result.reason


def test_price_below_threshold_with_volume_unavailable_is_not_meaningful():
    result = price_only(checkpoint_price=100.0, current_price=101.0)

    assert result.price_signal.meaningful is False
    assert result.volume_signal.available is False
    assert result.meaningful_change is False
    assert result.reason == "No meaningful change since your last check."


def test_both_signals_above_threshold_reason_mentions_both():
    result = with_volume(
        checkpoint_price=100.0,
        current_price=103.0,  # +3.0%, above threshold
        current_volume=CHECKPOINT_VOLUME + 90_000,  # 3.0x, above threshold
        minutes_after_checkpoint=30,
    )

    assert result.price_signal.meaningful is True
    assert result.volume_signal.meaningful is True
    assert result.meaningful_change is True
    assert "3.0%" in result.reason
    assert "accelerated" in result.reason
    assert "and" in result.reason  # combined into one sentence


def test_price_exactly_at_threshold_with_volume_below_threshold_is_meaningful():
    """E5: price exactly at its inclusive boundary (+2.0%) combined with
    a volume signal below its own threshold (1.5x) -- the price signal
    alone must still be sufficient."""
    result = with_volume(
        checkpoint_price=100.0,
        current_price=102.0,  # exactly +2.0%
        current_volume=CHECKPOINT_VOLUME + 45_000,  # exactly 1.5x, below threshold
        minutes_after_checkpoint=30,
    )

    assert result.price_signal.meaningful is True
    assert result.volume_signal.volume_acceleration_ratio == pytest.approx(1.5, abs=0.01)
    assert result.volume_signal.meaningful is False
    assert result.meaningful_change is True


def test_volume_exactly_at_threshold_with_price_below_threshold_is_meaningful():
    """E6: volume exactly at its inclusive boundary (2.0x) combined with
    a price signal below its own threshold (+1.0%) -- the volume signal
    alone must still be sufficient."""
    result = with_volume(
        checkpoint_price=100.0,
        current_price=101.0,  # +1.0%, below threshold
        current_volume=CHECKPOINT_VOLUME + 60_000,  # exactly 2.0x
        minutes_after_checkpoint=30,
    )

    assert result.price_signal.meaningful is False
    assert result.volume_signal.volume_acceleration_ratio == pytest.approx(2.0, abs=0.01)
    assert result.volume_signal.meaningful is True
    assert result.meaningful_change is True


def test_both_signals_exactly_at_their_thresholds_is_meaningful():
    """E7: price and volume both sitting exactly on their own inclusive
    boundary at once -- both must be independently reported meaningful,
    and the combined reason must mention both."""
    result = with_volume(
        checkpoint_price=100.0,
        current_price=102.0,  # exactly +2.0%
        current_volume=CHECKPOINT_VOLUME + 60_000,  # exactly 2.0x
        minutes_after_checkpoint=30,
    )

    assert result.price_signal.meaningful is True
    assert result.volume_signal.volume_acceleration_ratio == pytest.approx(2.0, abs=0.01)
    assert result.volume_signal.meaningful is True
    assert result.meaningful_change is True
    assert "2.0%" in result.reason
    assert "accelerated" in result.reason
    assert "and" in result.reason


# ---------- 15-16. Invalid/zero baseline and market values ----------


def test_zero_checkpoint_price_is_treated_as_no_baseline():
    result = price_only(checkpoint_price=0.0, current_price=100.0)

    assert result.has_baseline is False
    assert result.meaningful_change is False


def test_negative_checkpoint_price_is_treated_as_no_baseline():
    result = price_only(checkpoint_price=-10.0, current_price=100.0)

    assert result.has_baseline is False


def test_nan_checkpoint_price_is_treated_as_no_baseline():
    result = price_only(checkpoint_price=float("nan"), current_price=100.0)

    assert result.has_baseline is False


def test_infinite_checkpoint_price_is_treated_as_no_baseline():
    result = price_only(checkpoint_price=float("inf"), current_price=100.0)

    assert result.has_baseline is False


def test_zero_current_price_never_produces_false_meaningful_signal():
    result = price_only(checkpoint_price=100.0, current_price=0.0)

    assert result.has_baseline is True  # a real baseline existed
    assert result.meaningful_change is False  # but never fabricate a signal from bad current data
    assert result.price_signal.available is False


def test_negative_current_price_never_produces_false_meaningful_signal():
    result = price_only(checkpoint_price=100.0, current_price=-50.0)

    assert result.meaningful_change is False


def test_nan_current_price_never_produces_false_meaningful_signal():
    result = price_only(checkpoint_price=100.0, current_price=float("nan"))

    assert result.meaningful_change is False


def test_negative_checkpoint_volume_makes_volume_signal_unavailable_not_fabricated():
    result = evaluate_change(
        checkpoint_price=100.0,
        checkpoint_volume=-100,
        checkpoint_at=CHECKPOINT_AT,
        checkpoint_session_date=SESSION_DATE,
        current_price=100.0,
        current_volume=1000,
        current_fetched_at=CHECKPOINT_AT,
        current_session_date=SESSION_DATE,
    )

    assert result.volume_signal.available is False
    assert result.meaningful_change is False


def test_current_volume_lower_than_checkpoint_volume_is_treated_as_bad_data():
    """Cumulative volume cannot legitimately decrease within a session;
    this must never be silently used to compute a negative/nonsensical
    rate."""
    result = evaluate_change(
        checkpoint_price=100.0,
        checkpoint_volume=CHECKPOINT_VOLUME,
        checkpoint_at=CHECKPOINT_AT,
        checkpoint_session_date=SESSION_DATE,
        current_price=100.0,
        current_volume=CHECKPOINT_VOLUME - 1000,  # decreased -- impossible
        current_fetched_at=CHECKPOINT_AT + timedelta(minutes=10),
        current_session_date=SESSION_DATE,
    )

    assert result.volume_signal.available is False
    assert result.meaningful_change is False


def test_cross_session_volume_is_unavailable_never_computed():
    """Session-boundary rule: a checkpoint from a different trading day
    must never have its volume compared against today's."""
    prior_session = date(2026, 9, 3)
    prior_local = datetime.combine(prior_session, time(10, 15), tzinfo=IST)
    prior_checkpoint_at = prior_local.astimezone(timezone.utc)

    result = evaluate_change(
        checkpoint_price=100.0,
        checkpoint_volume=CHECKPOINT_VOLUME,
        checkpoint_at=prior_checkpoint_at,
        checkpoint_session_date=prior_session,
        current_price=100.0,
        current_volume=CHECKPOINT_VOLUME + 1_000_000,  # huge delta, would look "meaningful"
        current_fetched_at=CHECKPOINT_AT,
        current_session_date=SESSION_DATE,  # different day
    )

    assert result.volume_signal.available is False
    assert result.volume_signal.unavailable_reason == "checkpoint is from a different trading session"
    # Price is still evaluated normally even though volume is suppressed.
    assert result.price_signal.available is True


def test_checkpoint_too_close_to_market_open_is_unavailable():
    """Near-market-open guard: a checkpoint 1 minute after open must not
    produce a wild/meaningless rate."""
    checkpoint_near_open = ist_time_on_session(9, 16)  # 1 minute after 9:15 open

    result = evaluate_change(
        checkpoint_price=100.0,
        checkpoint_volume=1000,
        checkpoint_at=checkpoint_near_open,
        checkpoint_session_date=SESSION_DATE,
        current_price=100.0,
        current_volume=50_000,
        current_fetched_at=checkpoint_near_open + timedelta(minutes=30),
        current_session_date=SESSION_DATE,
    )

    assert result.volume_signal.available is False
    assert "market open" in result.volume_signal.unavailable_reason


def test_zero_baseline_volume_makes_rate_unavailable_never_divides_by_zero():
    """checkpoint_volume == 0 makes rate_before == 0 -- must never
    attempt to divide by it."""
    result = evaluate_change(
        checkpoint_price=100.0,
        checkpoint_volume=0,
        checkpoint_at=CHECKPOINT_AT,
        checkpoint_session_date=SESSION_DATE,
        current_price=100.0,
        current_volume=1000,
        current_fetched_at=CHECKPOINT_AT + timedelta(minutes=30),
        current_session_date=SESSION_DATE,
    )

    assert result.volume_signal.available is False


def test_current_fetched_at_before_checkpoint_is_unavailable_never_negative_elapsed():
    """Defensive: if current_fetched_at is somehow before checkpoint_at
    (clock skew, bad data), never compute a negative-elapsed-time rate."""
    result = evaluate_change(
        checkpoint_price=100.0,
        checkpoint_volume=CHECKPOINT_VOLUME,
        checkpoint_at=CHECKPOINT_AT,
        checkpoint_session_date=SESSION_DATE,
        current_price=100.0,
        current_volume=CHECKPOINT_VOLUME + 1000,
        current_fetched_at=CHECKPOINT_AT - timedelta(minutes=5),  # BEFORE checkpoint
        current_session_date=SESSION_DATE,
    )

    assert result.volume_signal.available is False


# ---------- 17-18. Determinism and immutability ----------


def test_repeated_evaluation_against_same_checkpoint_is_deterministic():
    args = dict(checkpoint_price=100.0, current_price=102.5)

    result1 = price_only(**args)
    result2 = price_only(**args)
    result3 = price_only(**args)

    assert result1 == result2 == result3


def test_frozen_checkpoint_values_are_not_mutated_by_evaluation():
    """The engine must never write back to or alter the checkpoint
    values it's given -- it's a pure read/compare function."""
    checkpoint_price = 100.0
    checkpoint_volume = CHECKPOINT_VOLUME
    checkpoint_at = CHECKPOINT_AT
    checkpoint_session_date = SESSION_DATE

    with_volume(
        checkpoint_price=checkpoint_price,
        current_price=110.0,
        current_volume=CHECKPOINT_VOLUME + 200_000,
        minutes_after_checkpoint=30,
    )

    # Original local variables are primitives/immutable types in Python,
    # so mutation is structurally impossible here -- but this test
    # documents the invariant explicitly and would catch a future
    # refactor that (for example) passed a mutable object by reference
    # and mutated it in place.
    assert checkpoint_price == 100.0
    assert checkpoint_volume == CHECKPOINT_VOLUME
    assert checkpoint_at == CHECKPOINT_AT
    assert checkpoint_session_date == SESSION_DATE


# ---------- Threshold constants sanity ----------


def test_threshold_constants_match_locked_product_decision():
    assert PRICE_CHANGE_THRESHOLD_PCT == 2.0
    assert VOLUME_ACCELERATION_THRESHOLD == 2.0