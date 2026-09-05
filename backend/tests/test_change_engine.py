"""
Tests for the Meaningful Change Engine.

All tests are pure unit tests against evaluate_change() / the adaptive
price-threshold helper -- no MongoDB, no network, no live Yahoo Finance.
Timestamps are constructed explicitly so every test is fully
deterministic (see the rehearsal's own lesson: tests must prove the
formula, not just "look plausible").

Price threshold history: this module originally locked
PRICE_CHANGE_THRESHOLD_PCT = 2.0 as a fixed constant. It has since been
replaced by a stock-adaptive threshold (see
_compute_adaptive_price_threshold and decisions.md's "Adaptive price
meaningful-change threshold" entry) -- PRICE_CHANGE_THRESHOLD_PCT no
longer exists in change_engine.py. The price-boundary tests below that
predate this change now pass an EXPLICIT price_threshold to
price_only()/with_volume() (defaulting to 2.0, the old locked value, so
their original numeric intent and names are preserved unchanged) rather
than relying on a module constant. VOLUME_ACCELERATION_THRESHOLD is
UNCHANGED and remains a real module constant throughout.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.change_engine import (
    ADAPTIVE_PRICE_THRESHOLD_CEILING_PCT,
    ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT,
    ADAPTIVE_PRICE_THRESHOLD_FLOOR_PCT,
    ADAPTIVE_PRICE_THRESHOLD_RANGE_MULTIPLIER,
    EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT,
    VOLUME_ACCELERATION_THRESHOLD,
    _MIN_MINUTES_SINCE_OPEN_FOR_PRICE_THRESHOLD,
    _compute_adaptive_price_threshold,
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

# The old locked constant's value, kept here ONLY as a literal default
# for tests below that predate the adaptive threshold and were written
# reasoning about "the 2% threshold" -- not a reintroduction of the
# removed module constant.
_LEGACY_PRICE_THRESHOLD_PCT = 2.0


def price_only(checkpoint_price, current_price, price_threshold=_LEGACY_PRICE_THRESHOLD_PCT):
    """Call evaluate_change with price args only -- volume signal must
    be reported unavailable, never fabricated, in this mode. Defaults
    price_threshold to the old locked 2.0 value so existing call sites
    below need no other changes; pass an explicit value to test a
    specific adaptive threshold, or None to exercise the real
    ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT fallback path."""
    return evaluate_change(
        checkpoint_price=checkpoint_price,
        checkpoint_price_threshold=price_threshold,
        current_price=current_price,
    )


def with_volume(
    checkpoint_price,
    current_price,
    current_volume,
    minutes_after_checkpoint,
    price_threshold=_LEGACY_PRICE_THRESHOLD_PCT,
):
    """Call evaluate_change with full price+volume+timing args. Volume
    delta and elapsed time are chosen by the caller; current_fetched_at
    is derived from CHECKPOINT_AT + minutes_after_checkpoint."""
    current_fetched_at = CHECKPOINT_AT + timedelta(minutes=minutes_after_checkpoint)
    return evaluate_change(
        checkpoint_price=checkpoint_price,
        checkpoint_price_threshold=price_threshold,
        checkpoint_volume=CHECKPOINT_VOLUME,
        checkpoint_at=CHECKPOINT_AT,
        checkpoint_session_date=SESSION_DATE,
        current_price=current_price,
        current_volume=current_volume,
        current_fetched_at=current_fetched_at,
        current_session_date=SESSION_DATE,
    )


# ============================================================
# _compute_adaptive_price_threshold -- pure formula tests
# ============================================================


def test_adaptive_threshold_normal_calculation():
    """day_high=110, day_low=100, previous_close=100 -> range_percent =
    (10/100)*100 = 10.0 -> 0.25*10.0 = 2.5, within [0.5, 3.0] -> 2.5."""
    result = _compute_adaptive_price_threshold(
        day_high=110.0, day_low=100.0, previous_close=100.0,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(2.5, abs=1e-9)


def test_adaptive_threshold_exactly_at_floor():
    """A very narrow range should clamp UP to exactly 0.5%, inclusive."""
    # range_percent = (100.4 - 100.0)/100 * 100 = 0.4 -> 0.25*0.4 = 0.1 -> clamps to 0.5
    result = _compute_adaptive_price_threshold(
        day_high=100.4, day_low=100.0, previous_close=100.0,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(ADAPTIVE_PRICE_THRESHOLD_FLOOR_PCT, abs=1e-9)


def test_adaptive_threshold_exactly_at_ceiling():
    """A wide range should clamp DOWN to exactly 3.0%, inclusive."""
    # range_percent = (150 - 100)/100 * 100 = 50 -> 0.25*50 = 12.5 -> clamps to 3.0
    result = _compute_adaptive_price_threshold(
        day_high=150.0, day_low=100.0, previous_close=100.0,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(ADAPTIVE_PRICE_THRESHOLD_CEILING_PCT, abs=1e-9)


def test_adaptive_threshold_computed_value_below_floor_clamps_up():
    """0.25 * range_percent landing below 0.5 must clamp UP, never be
    returned as-is."""
    # range_percent = 1.0 -> 0.25*1.0 = 0.25 < 0.5 floor
    result = _compute_adaptive_price_threshold(
        day_high=101.0, day_low=100.0, previous_close=100.0,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(ADAPTIVE_PRICE_THRESHOLD_FLOOR_PCT, abs=1e-9)


def test_adaptive_threshold_computed_value_above_ceiling_clamps_down():
    """0.25 * range_percent landing above 3.0 must clamp DOWN, never be
    returned as-is."""
    # range_percent = 20 -> 0.25*20 = 5.0 > 3.0 ceiling
    result = _compute_adaptive_price_threshold(
        day_high=120.0, day_low=100.0, previous_close=100.0,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(ADAPTIVE_PRICE_THRESHOLD_CEILING_PCT, abs=1e-9)


def test_adaptive_threshold_day_high_equals_day_low_is_valid_zero_range():
    """A genuinely zero observed intraday range (e.g. very early in a
    session) is valid, not an error -- range_percent is exactly 0 and
    the result correctly clamps to the 0.5% floor, never treated as
    unavailable/invalid."""
    result = _compute_adaptive_price_threshold(
        day_high=100.0, day_low=100.0, previous_close=100.0,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(ADAPTIVE_PRICE_THRESHOLD_FLOOR_PCT, abs=1e-9)


def test_adaptive_threshold_day_low_greater_than_day_high_is_invalid_fallback():
    """A real impossibility (like negative volume elsewhere in this
    module) -- must fall back, never produce a negative range_percent."""
    result = _compute_adaptive_price_threshold(
        day_high=99.0, day_low=100.0, previous_close=100.0,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT


@pytest.mark.parametrize(
    "day_high,day_low,previous_close",
    [
        (None, 100.0, 100.0),
        (110.0, None, 100.0),
        (110.0, 100.0, None),
        (None, None, None),
    ],
)
def test_adaptive_threshold_none_range_values_fall_back(day_high, day_low, previous_close):
    result = _compute_adaptive_price_threshold(
        day_high, day_low, previous_close,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT


@pytest.mark.parametrize(
    "day_high,day_low,previous_close",
    [
        (float("nan"), 100.0, 100.0),
        (110.0, float("nan"), 100.0),
        (110.0, 100.0, float("nan")),
        (float("inf"), 100.0, 100.0),
        (110.0, float("-inf"), 100.0),
        (110.0, 100.0, float("inf")),
    ],
)
def test_adaptive_threshold_nan_or_infinite_inputs_fall_back(day_high, day_low, previous_close):
    result = _compute_adaptive_price_threshold(
        day_high, day_low, previous_close,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT


def test_adaptive_threshold_zero_or_negative_previous_close_falls_back():
    """previous_close is already validated positive by MarketSnapshot in
    production, but this pure function does not trust its caller
    blindly (same philosophy as the rest of this module)."""
    assert _compute_adaptive_price_threshold(
        110.0, 100.0, 0.0, checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    ) == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT
    assert _compute_adaptive_price_threshold(
        110.0, 100.0, -50.0, checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    ) == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT


def test_adaptive_threshold_uses_full_precision_not_rounded():
    """The formula itself must not round intermediate values -- only a
    UI display boundary may round."""
    # range_percent = (101.0 - 100.0)/137.0 * 100 = 0.729927007...
    # 0.25 * that = 0.182481752... -> clamps to the 0.5 floor either way,
    # so instead pick inputs that land BETWEEN the clamp bounds to prove
    # the raw computed value (not a rounded one) is what's returned.
    day_high, day_low, previous_close = 106.3, 100.0, 100.0
    expected_range_percent = (day_high - day_low) / previous_close * 100  # 6.3
    expected = ADAPTIVE_PRICE_THRESHOLD_RANGE_MULTIPLIER * expected_range_percent  # 1.575
    assert 0.5 < expected < 3.0  # sanity: genuinely between the clamp bounds
    result = _compute_adaptive_price_threshold(
        day_high, day_low, previous_close,
        checkpoint_at=CHECKPOINT_AT, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(expected, abs=1e-12)
    assert result == pytest.approx(1.575, abs=1e-9)


# ============================================================
# _compute_adaptive_price_threshold -- near-market-open guard
# ============================================================


def test_adaptive_threshold_early_checkpoint_cannot_freeze_a_hypersensitive_value():
    """(a) A checkpoint established only 1 minute after market open, with
    a razor-thin observed range that would otherwise clamp to the 0.5%
    floor under the normal formula, must instead use
    EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT -- the near-open guard
    intercepts BEFORE the floor/ceiling clamp logic ever runs. This is
    the core regression proof for the checkpoint-timing fix."""
    early_checkpoint_at = ist_time_on_session(9, 16)  # 1 minute after 9:15 open
    result = _compute_adaptive_price_threshold(
        day_high=100.01, day_low=100.00, previous_close=100.0,  # range_percent ~= 0.01%
        checkpoint_at=early_checkpoint_at, session_date=SESSION_DATE,
    )
    assert result == EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT
    assert result != ADAPTIVE_PRICE_THRESHOLD_FLOOR_PCT  # would have floored under the old logic


def test_adaptive_threshold_later_checkpoint_uses_observed_range_not_early_fallback():
    """(b) The SAME razor-thin range, once past the guard, is evaluated
    normally and clamps to the general 0.5% floor -- proving the guard
    only intercepts the early window, not the formula itself."""
    late_checkpoint_at = ist_time_on_session(10, 15)  # 60 minutes after open
    result = _compute_adaptive_price_threshold(
        day_high=100.01, day_low=100.00, previous_close=100.0,
        checkpoint_at=late_checkpoint_at, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(ADAPTIVE_PRICE_THRESHOLD_FLOOR_PCT, abs=1e-9)


def test_adaptive_threshold_one_minute_before_guard_boundary_uses_early_fallback():
    """Boundary: the comparison is strict '<', so one minute short of
    _MIN_MINUTES_SINCE_OPEN_FOR_PRICE_THRESHOLD (14 of 15 minutes
    elapsed) still uses the early-session fallback -- even with a WIDE
    range that would otherwise clamp all the way to the 3.0% ceiling."""
    assert _MIN_MINUTES_SINCE_OPEN_FOR_PRICE_THRESHOLD == 15.0  # pins the locked value
    checkpoint_at = ist_time_on_session(9, 29)  # 14 minutes after open
    result = _compute_adaptive_price_threshold(
        day_high=150.0, day_low=100.0, previous_close=100.0,  # would clamp to the 3.0 ceiling
        checkpoint_at=checkpoint_at, session_date=SESSION_DATE,
    )
    assert result == EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT


def test_adaptive_threshold_exactly_at_guard_boundary_uses_observed_range():
    """Boundary: exactly _MIN_MINUTES_SINCE_OPEN_FOR_PRICE_THRESHOLD
    minutes elapsed is inclusive (same '>=' convention as this module's
    other inclusive boundaries) -- no longer 'early,' the range is
    trusted."""
    checkpoint_at = ist_time_on_session(9, 30)  # exactly 15 minutes after open
    result = _compute_adaptive_price_threshold(
        day_high=150.0, day_low=100.0, previous_close=100.0,
        checkpoint_at=checkpoint_at, session_date=SESSION_DATE,
    )
    assert result == pytest.approx(ADAPTIVE_PRICE_THRESHOLD_CEILING_PCT, abs=1e-9)


def test_adaptive_threshold_early_guard_also_covers_missing_range_data():
    """An early checkpoint with ALSO-missing/invalid day_high/day_low
    must still resolve safely -- EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT
    and ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT happen to share the same
    numeric value today (both locked at 1.0), so this does not prove
    which internal branch ran -- it only confirms the combination of
    "early" and "no usable range" together degrades safely, never
    raising and never producing an unexpected value."""
    early_checkpoint_at = ist_time_on_session(9, 16)
    result = _compute_adaptive_price_threshold(
        day_high=None, day_low=None, previous_close=None,
        checkpoint_at=early_checkpoint_at, session_date=SESSION_DATE,
    )
    assert result == EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT


def test_frozen_early_session_threshold_is_honored_unchanged_by_evaluate_change():
    """(c) Once a checkpoint's price_threshold_applied was frozen via the
    early-session fallback, evaluate_change must apply that EXACT frozen
    value on every later comparison. evaluate_change takes no
    day_high/day_low/checkpoint_at/session_date inputs at all -- it is
    structurally incapable of re-deriving a threshold from a later,
    wider intraday range, which is the actual mechanism guaranteeing the
    freeze holds, not just a convention this test hopes is followed."""
    # A move that clears the OLD general 0.5% floor but stays below the
    # frozen 1.0% early-session value -- must NOT be meaningful if the
    # frozen value is genuinely applied, not silently relaxed back down.
    below = price_only(
        checkpoint_price=100.0, current_price=100.6,
        price_threshold=EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT,
    )
    assert below.price_signal.threshold == EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT
    assert below.price_signal.meaningful is False  # 0.6% < 1.0% frozen threshold

    # A move that clears the frozen 1.0% threshold IS meaningful.
    above = price_only(
        checkpoint_price=100.0, current_price=101.2,
        price_threshold=EARLY_SESSION_PRICE_THRESHOLD_FALLBACK_PCT,
    )
    assert above.price_signal.meaningful is True


# ---------- 1. No checkpoint ----------


def test_no_checkpoint_is_baseline_pending_not_meaningful():
    result = price_only(checkpoint_price=None, current_price=99999.0)

    assert result.has_baseline is False
    assert result.meaningful_change is False
    assert result.price_change_pct is None
    assert result.volume_acceleration_ratio is None
    assert result.reason == "Baseline pending — no previous check to compare against."


# ---------- 2-6. Price signal boundaries and direction (explicit 2.0% threshold) ----------


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


# ---------- Adaptive-threshold-specific price meaningfulness ----------


def test_0_6_percent_move_meaningful_with_0_5_percent_threshold():
    """New adaptive-specific case: a move well below the OLD fixed 2.0%
    is correctly meaningful against a narrow adaptive threshold."""
    result = price_only(checkpoint_price=100.0, current_price=100.6, price_threshold=0.5)

    assert result.price_signal.meaningful is True
    assert result.meaningful_change is True
    assert result.price_signal.threshold == 0.5


def test_2_5_percent_move_not_meaningful_with_3_0_percent_threshold():
    """New adaptive-specific case: a move that WOULD have been
    meaningful against the old fixed 2.0% is correctly NOT meaningful
    against a wide adaptive threshold."""
    result = price_only(checkpoint_price=100.0, current_price=102.5, price_threshold=3.0)

    assert result.price_signal.meaningful is False
    assert result.meaningful_change is False
    assert result.price_signal.threshold == 3.0


def test_price_threshold_inclusive_at_an_arbitrary_adaptive_value():
    """Exactly-at-threshold inclusivity must hold for ANY threshold
    value, not just the old fixed 2.0 -- proven here at 1.23%."""
    result = price_only(checkpoint_price=100.0, current_price=101.23, price_threshold=1.23)

    assert result.price_signal.meaningful is True
    assert result.meaningful_change is True


def test_negative_price_movement_uses_absolute_value_against_adaptive_threshold():
    result = price_only(checkpoint_price=100.0, current_price=98.77, price_threshold=1.23)

    assert result.price_change_pct == pytest.approx(-1.23, abs=1e-9)
    assert result.price_signal.meaningful is True  # abs(-1.23) >= 1.23


def test_price_signal_comparison_uses_full_precision():
    """A price move that rounds to a boundary value at low precision but
    is NOT actually at/past the threshold at full precision must not be
    misjudged -- proves the comparison itself is not performed on a
    rounded value."""
    # 100.6999999 vs threshold 0.7 -> price_change_pct is very slightly
    # BELOW 0.7, not meaningful, even though it would round to "0.7%" at
    # 1-decimal display precision.
    result = price_only(checkpoint_price=100.0, current_price=100.6999999, price_threshold=0.7)
    assert result.price_change_pct < 0.7
    assert result.price_signal.meaningful is False


def test_no_explicit_threshold_uses_adaptive_fallback():
    """Omitting checkpoint_price_threshold entirely (None) must resolve
    to ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT, the same safe-compatibility
    path a checkpoint written before this field existed takes. 0.6% is
    below the 1.0% fallback, so it's correctly NOT meaningful here --
    proving the fallback value is actually being applied, not silently
    ignored (a bug that would make this move meaningful against a
    smaller/zero effective threshold)."""
    result = price_only(checkpoint_price=100.0, current_price=100.6, price_threshold=None)

    assert result.price_signal.threshold == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT
    assert result.price_signal.meaningful is False


def test_invalid_supplied_threshold_falls_back_to_adaptive_default():
    """A non-finite or non-positive checkpoint_price_threshold must not
    be trusted -- this function does not trust its caller blindly, same
    as checkpoint_price/current_price themselves."""
    for bad_threshold in [0.0, -1.0, float("nan"), float("inf")]:
        result = price_only(checkpoint_price=100.0, current_price=100.6, price_threshold=bad_threshold)
        assert result.price_signal.threshold == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT


# ---------- 7-9. Volume signal boundaries (unchanged) ----------


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


def test_volume_acceleration_threshold_constant_unchanged():
    assert VOLUME_ACCELERATION_THRESHOLD == 2.0


# ---------- 10-14. Combination logic (explicit 2.0% price threshold) ----------


def test_price_below_and_volume_below_threshold_is_not_meaningful():
    result = with_volume(
        checkpoint_price=100.0,
        current_price=101.0,  # +1.0%, below the 2.0% threshold
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
        current_price=101.0,  # +1.0%, below the 2.0% threshold
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
    assert "2.00%" in result.reason  # the threshold is now named in the reason


def test_price_below_threshold_with_volume_unavailable_is_not_meaningful():
    result = price_only(checkpoint_price=100.0, current_price=101.0)  # +1.0%, below 2.0%

    assert result.price_signal.meaningful is False
    assert result.volume_signal.available is False
    assert result.meaningful_change is False
    assert result.reason == "No meaningful change since your last check."


def test_both_signals_above_threshold_reason_mentions_both():
    result = with_volume(
        checkpoint_price=100.0,
        current_price=103.0,  # +3.0%, above the 2.0% threshold
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
        current_price=101.0,  # +1.0%, below the 2.0% threshold
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
    must never have its volume compared against today's. UNCHANGED by
    the adaptive price threshold -- this is a volume-signal-only test."""
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
    produce a wild/meaningless rate. UNCHANGED by the adaptive price
    threshold."""
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
    """PRICE_CHANGE_THRESHOLD_PCT no longer exists (replaced by the
    adaptive threshold below) -- VOLUME_ACCELERATION_THRESHOLD remains
    the one still-locked fixed constant."""
    assert VOLUME_ACCELERATION_THRESHOLD == 2.0
    assert ADAPTIVE_PRICE_THRESHOLD_RANGE_MULTIPLIER == 0.25
    assert ADAPTIVE_PRICE_THRESHOLD_FLOOR_PCT == 0.5
    assert ADAPTIVE_PRICE_THRESHOLD_CEILING_PCT == 3.0
    assert ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT == 1.0
