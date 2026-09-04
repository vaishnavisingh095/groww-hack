"""
Meaningful Change Engine -- PRICE SIGNAL ONLY for this first vertical
slice, per explicit instruction. Volume acceleration, session-boundary
handling, and attention ranking are later phases and are NOT implemented
here.

Deterministic, explainable, no LLM -- exactly per architecture.md's
stated philosophy, just scoped down to one signal for this slice.
"""
from dataclasses import dataclass

PRICE_THRESHOLD_PCT = 2.0  # starting value, per plan.md/architecture.md - not scientifically derived


@dataclass
class ChangeResult:
    has_baseline: bool
    meaningful_change: bool
    percent_difference: float | None  # None only when has_baseline is False
    reason: str


def evaluate_price_change(checkpoint_price: float | None, current_price: float) -> ChangeResult:
    """
    Compare current_price against a checkpoint baseline, if one exists.

    Formula (exactly as specified): abs(current - checkpoint) / checkpoint.
    Threshold: >= 2% => meaningful_change = True.

    If checkpoint_price is None (no prior checkpoint for this
    instrument/user), this is a "first-time baseline" case, NOT a
    meaningful change -- per the explicit instruction, this must never
    be reported as a change.
    """
    if checkpoint_price is None:
        return ChangeResult(
            has_baseline=False,
            meaningful_change=False,
            percent_difference=None,
            reason="Baseline created — no previous check to compare against.",
        )

    if checkpoint_price <= 0:
        # Defensive: a checkpoint price of zero or negative should never
        # exist (Checkpoint's BaselineSnapshot requires last_price > 0),
        # but this function does not re-trust its caller blindly --
        # dividing by a non-positive checkpoint price is undefined, so
        # this is treated the same as "no usable baseline" rather than
        # raising ZeroDivisionError into a request path.
        return ChangeResult(
            has_baseline=False,
            meaningful_change=False,
            percent_difference=None,
            reason="Baseline created — no previous check to compare against.",
        )

    percent_difference = abs(current_price - checkpoint_price) / checkpoint_price * 100
    is_meaningful = (percent_difference / 100) >= (PRICE_THRESHOLD_PCT / 100)
    # equivalent to: percent_difference >= PRICE_THRESHOLD_PCT
    # written this way to mirror the exact formula given (fraction-based
    # threshold comparison), not to obscure it

    if is_meaningful:
        reason = f"Moved {percent_difference:.2f}% since you last checked."
    else:
        reason = "No meaningful change."

    return ChangeResult(
        has_baseline=True,
        meaningful_change=is_meaningful,
        percent_difference=percent_difference,
        reason=reason,
    )
