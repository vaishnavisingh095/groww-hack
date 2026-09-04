"""
Meaningful Change Engine — Phase 5.

Deterministic, explainable, no LLM. Combines two independent signals:

1. Price movement: abs(price_change_pct) >= PRICE_CHANGE_THRESHOLD_PCT
2. Volume-rate acceleration (same trading session only):
   volume_acceleration_ratio >= VOLUME_ACCELERATION_THRESHOLD

Formulas are exactly as specified in architecture.md's "Meaningful
Change Engine — Design" section (price formula) and its "REQUIRED
CORRECTION — Same-Session Volume Semantics" / rate-based volume
definition (volume formula) -- this module implements that already-
approved design, it does not introduce a new one.

Both signals are evaluated against the FROZEN checkpoint baseline
(Checkpoint.baseline_snapshot), never against a live/mutable
MarketSnapshot document, per architecture.md's frozen-copy rationale.
This module does not read or write Checkpoint/MarketSnapshot documents
itself -- it is a pure function of the values passed in, so it can be
tested without MongoDB or network access.
"""
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

PRICE_CHANGE_THRESHOLD_PCT = 2.0
VOLUME_ACCELERATION_THRESHOLD = 2.0

# NSE/BSE market open, per architecture.md's confirmed constant.
_IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN_TIME_IST = time(9, 15)

# Near-market-open guard (architecture.md: "treat
# minutes_since_open_to_checkpoint below a fixed floor ... as
# insufficient data"). A few minutes, as the design note allows,
# expressed as a named constant rather than a bare number.
_MIN_MINUTES_SINCE_OPEN_FOR_RATE = 2.0


@dataclass
class PriceSignal:
    available: bool
    price_change_pct: float | None
    meaningful: bool


@dataclass
class VolumeSignal:
    available: bool
    volume_acceleration_ratio: float | None
    meaningful: bool
    unavailable_reason: str | None = None


@dataclass
class ChangeResult:
    has_baseline: bool
    meaningful_change: bool
    price_change_pct: float | None
    volume_acceleration_ratio: float | None
    price_signal: PriceSignal
    volume_signal: VolumeSignal
    reason: str


def _market_open_at(session_date: date) -> datetime:
    """The exact UTC instant of NSE market open (9:15 AM IST) on the
    given trading-session date."""
    local_open = datetime.combine(session_date, _MARKET_OPEN_TIME_IST, tzinfo=_IST)
    return local_open.astimezone(timezone.utc)


def _is_finite_positive(value: float | None) -> bool:
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def _is_finite(value: float | None) -> bool:
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _evaluate_price_signal(checkpoint_price: float, current_price: float) -> PriceSignal:
    """
    price_change_pct = ((current_price - checkpoint_price) / checkpoint_price) * 100
    Threshold check uses the ABSOLUTE value; the signed value is kept
    for the explanation/direction.
    """
    price_change_pct = (current_price - checkpoint_price) / checkpoint_price * 100
    meaningful = abs(price_change_pct) >= PRICE_CHANGE_THRESHOLD_PCT
    return PriceSignal(available=True, price_change_pct=price_change_pct, meaningful=meaningful)


def _evaluate_volume_signal(
    *,
    checkpoint_volume: int,
    checkpoint_at: datetime,
    checkpoint_session_date: date,
    current_volume: int,
    current_fetched_at: datetime,
    current_session_date: date,
) -> VolumeSignal:
    """
    Same-session, rate-based volume acceleration, exactly per
    architecture.md:

        minutes_since_open_to_checkpoint = checkpoint_at - market_open_time
        minutes_since_checkpoint         = now - checkpoint_at

        rate_before = checkpoint_volume / minutes_since_open_to_checkpoint
        rate_after  = (current_volume - checkpoint_volume) / minutes_since_checkpoint

        volume_acceleration_ratio = rate_after / rate_before

    Returns available=False (never a fabricated ratio) whenever any
    precondition for a defensible rate calculation is not met.
    """
    # Same-session requirement (architecture.md's "REQUIRED CORRECTION").
    if checkpoint_session_date != current_session_date:
        return VolumeSignal(
            available=False,
            volume_acceleration_ratio=None,
            meaningful=False,
            unavailable_reason="checkpoint is from a different trading session",
        )

    # Basic sanity on the raw values themselves -- never divide using a
    # negative/non-finite volume or a current volume that has somehow
    # decreased (cumulative volume cannot legitimately shrink within a
    # session; a decrease indicates bad/stale data, not real trading).
    if checkpoint_volume < 0 or current_volume < 0:
        return VolumeSignal(
            available=False,
            volume_acceleration_ratio=None,
            meaningful=False,
            unavailable_reason="negative volume value",
        )
    if current_volume < checkpoint_volume:
        return VolumeSignal(
            available=False,
            volume_acceleration_ratio=None,
            meaningful=False,
            unavailable_reason="current volume is lower than checkpoint volume (non-monotonic, treated as bad data)",
        )

    market_open_at = _market_open_at(checkpoint_session_date)
    minutes_since_open_to_checkpoint = (checkpoint_at - market_open_at).total_seconds() / 60.0
    minutes_since_checkpoint = (current_fetched_at - checkpoint_at).total_seconds() / 60.0

    # Near-market-open guard (architecture.md, explicit requirement):
    # a checkpoint too close to (or before/at) market open makes
    # rate_before undefined or wildly unstable. Also guards the
    # symmetric case of a current fetch occurring at/before the
    # checkpoint itself (minutes_since_checkpoint <= 0), which would
    # otherwise divide by zero or produce a negative elapsed time.
    if minutes_since_open_to_checkpoint < _MIN_MINUTES_SINCE_OPEN_FOR_RATE:
        return VolumeSignal(
            available=False,
            volume_acceleration_ratio=None,
            meaningful=False,
            unavailable_reason="checkpoint too close to market open for a defensible rate",
        )
    if minutes_since_checkpoint <= 0:
        return VolumeSignal(
            available=False,
            volume_acceleration_ratio=None,
            meaningful=False,
            unavailable_reason="no elapsed time since checkpoint",
        )

    rate_before = checkpoint_volume / minutes_since_open_to_checkpoint
    if rate_before <= 0:
        # A zero baseline rate (e.g., checkpoint_volume == 0) makes the
        # ratio undefined -- never divide by zero, never fabricate an
        # "infinite" acceleration.
        return VolumeSignal(
            available=False,
            volume_acceleration_ratio=None,
            meaningful=False,
            unavailable_reason="baseline volume rate is zero or non-positive",
        )

    rate_after = (current_volume - checkpoint_volume) / minutes_since_checkpoint
    volume_acceleration_ratio = rate_after / rate_before

    if not math.isfinite(volume_acceleration_ratio):
        return VolumeSignal(
            available=False,
            volume_acceleration_ratio=None,
            meaningful=False,
            unavailable_reason="computed ratio is not finite",
        )

    meaningful = volume_acceleration_ratio >= VOLUME_ACCELERATION_THRESHOLD
    return VolumeSignal(
        available=True, volume_acceleration_ratio=volume_acceleration_ratio, meaningful=meaningful
    )


def _build_reason(
    has_baseline: bool,
    price_signal: PriceSignal | None,
    volume_signal: VolumeSignal | None,
) -> str:
    if not has_baseline:
        return "Baseline pending — no previous check to compare against."

    price_meaningful = price_signal is not None and price_signal.meaningful
    volume_meaningful = volume_signal is not None and volume_signal.meaningful

    if not price_meaningful and not volume_meaningful:
        return "No meaningful change since your last check."

    if price_meaningful and volume_meaningful:
        price_pct = price_signal.price_change_pct
        sign = "+" if price_pct >= 0 else ""
        ratio = volume_signal.volume_acceleration_ratio
        return (
            f"Price moved {sign}{price_pct:.1f}% and trading activity "
            f"accelerated to {ratio:.1f}× its baseline rate."
        )

    if price_meaningful:
        pct = price_signal.price_change_pct
        sign = "+" if pct >= 0 else ""
        return f"Price moved {sign}{pct:.1f}% since your last check."

    ratio = volume_signal.volume_acceleration_ratio
    return f"Trading activity accelerated to {ratio:.1f}× its baseline rate."


def evaluate_change(
    *,
    checkpoint_price: float | None,
    checkpoint_volume: int | None = None,
    checkpoint_at: datetime | None = None,
    checkpoint_session_date: date | None = None,
    current_price: float,
    current_volume: int | None = None,
    current_fetched_at: datetime | None = None,
    current_session_date: date | None = None,
) -> ChangeResult:
    """
    Evaluate meaningful change against a frozen checkpoint baseline.

    Price-only usage remains supported: if only checkpoint_price and
    current_price are given (volume/timestamp args left as None), the
    volume signal is simply unavailable and the result is price-only --
    this keeps the function usable wherever a caller genuinely only has
    price data, without requiring volume/timing information it doesn't
    have, while never fabricating a volume signal from missing inputs.

    Never mutates any input; checkpoint values are read, never written,
    consistent with the checkpoint being a frozen, immutable baseline.
    """
    if not _is_finite_positive(checkpoint_price):
        # No checkpoint, or an invalid/non-positive checkpoint price
        # (which should never legitimately exist -- BaselineSnapshot
        # requires last_price > 0 -- but this function does not trust
        # its caller blindly). Treated identically to "no baseline":
        # never a meaningful change, never a fabricated comparison.
        return ChangeResult(
            has_baseline=False,
            meaningful_change=False,
            price_change_pct=None,
            volume_acceleration_ratio=None,
            price_signal=PriceSignal(available=False, price_change_pct=None, meaningful=False),
            volume_signal=VolumeSignal(
                available=False,
                volume_acceleration_ratio=None,
                meaningful=False,
                unavailable_reason="no baseline",
            ),
            reason=_build_reason(False, None, None),
        )

    if not _is_finite(current_price) or current_price <= 0:
        # Defensive: an invalid current price must never produce a
        # false meaningful signal. Treated as "no usable comparison,"
        # not as "no baseline" (a real baseline exists) -- surfaced as
        # not meaningful with no fabricated percentage.
        return ChangeResult(
            has_baseline=True,
            meaningful_change=False,
            price_change_pct=None,
            volume_acceleration_ratio=None,
            price_signal=PriceSignal(available=False, price_change_pct=None, meaningful=False),
            volume_signal=VolumeSignal(
                available=False,
                volume_acceleration_ratio=None,
                meaningful=False,
                unavailable_reason="invalid current price",
            ),
            reason="No meaningful change since your last check.",
        )

    price_signal = _evaluate_price_signal(checkpoint_price, current_price)

    volume_inputs_present = (
        checkpoint_volume is not None
        and checkpoint_at is not None
        and checkpoint_session_date is not None
        and current_volume is not None
        and current_fetched_at is not None
        and current_session_date is not None
    )

    if volume_inputs_present:
        volume_signal = _evaluate_volume_signal(
            checkpoint_volume=checkpoint_volume,
            checkpoint_at=checkpoint_at,
            checkpoint_session_date=checkpoint_session_date,
            current_volume=current_volume,
            current_fetched_at=current_fetched_at,
            current_session_date=current_session_date,
        )
    else:
        volume_signal = VolumeSignal(
            available=False,
            volume_acceleration_ratio=None,
            meaningful=False,
            unavailable_reason="insufficient data provided for volume-rate calculation",
        )

    meaningful_change = price_signal.meaningful or volume_signal.meaningful
    reason = _build_reason(True, price_signal, volume_signal)

    return ChangeResult(
        has_baseline=True,
        meaningful_change=meaningful_change,
        price_change_pct=price_signal.price_change_pct,
        volume_acceleration_ratio=volume_signal.volume_acceleration_ratio,
        price_signal=price_signal,
        volume_signal=volume_signal,
        reason=reason,
    )