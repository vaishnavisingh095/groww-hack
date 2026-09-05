"""
Tests for the Attention Engine -- Phase 6.

Pure-function tests (score_change_event, and the threshold/explanation
logic it calls) require no MongoDB at all, mirroring
test_change_engine.py's own discipline -- a ChangeEvent is built
directly and scored in isolation. AttentionEngine class tests use
mongomock (see conftest.py's mock_db fixture docstring) to exercise the
real active-events query, acknowledged-exclusion, symbol resolution,
and cross-instrument ranking.
"""
from datetime import datetime, timezone

import mongomock
import pytest

from app.db.indexes import ensure_indexes
from app.models.change_event import ChangeEvent, ChangeSignals
from app.services.attention_engine import AttentionEngine, AttentionLevel, score_change_event


def make_signals(price_change_pct, ratio=None, available=False):
    return ChangeSignals(
        price_change_pct=price_change_pct,
        volume_acceleration_ratio=ratio,
        volume_acceleration_available=available,
    )


def make_change_event(**overrides) -> ChangeEvent:
    defaults = dict(
        user_id="user1",
        instrument_id="inst123",
        checkpoint_id="cp1",
        detected_at=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc),
        signals=make_signals(4.2),
        reason="irrelevant to the Attention Engine -- see decisions.md",
        acknowledged=False,
    )
    defaults.update(overrides)
    return ChangeEvent(**defaults)


# --- Pure scoring/explanation: price-only -----------------------------------


def test_price_only_positive_movement():
    event = make_change_event(signals=make_signals(4.0))
    item = score_change_event(symbol="RELIANCE", change_event=event)

    assert item.attention_score == pytest.approx(2.0)  # 4.0 / 2.0
    assert item.attention_level == AttentionLevel.HIGH
    assert item.explanation == "RELIANCE moved +4.0% since your last check."


def test_price_only_negative_movement():
    """Matches the task's own worked example exactly."""
    event = make_change_event(signals=make_signals(-5.0))
    item = score_change_event(symbol="INFY", change_event=event)

    assert item.attention_score == pytest.approx(2.5)  # abs(-5.0) / 2.0
    assert item.attention_level == AttentionLevel.HIGH
    assert item.explanation == "INFY moved -5.0% since your last check."


# --- Pure scoring/explanation: volume-only -----------------------------------


def test_volume_only_when_price_below_threshold():
    """Price didn't cross its own threshold, but volume did -- the
    ChangeEvent still exists (meaningful_change was true on volume
    alone), and attention_score must come from the volume signal."""
    event = make_change_event(signals=make_signals(0.5, ratio=3.0, available=True))
    item = score_change_event(symbol="TCS", change_event=event)

    assert item.attention_score == pytest.approx(1.5)  # 3.0 / 2.0
    assert item.attention_level == AttentionLevel.MEDIUM
    assert "TCS moved +0.5%" in item.explanation
    assert "3.0×" in item.explanation


# --- Pure scoring/explanation: both signals ----------------------------------


def test_both_price_and_volume_produce_one_item_scored_by_the_stronger_signal():
    """Matches the task's own worked example: +2.4% and 2.7x ->
    max(1.2, 1.35) = 1.35, ONE item, explanation mentions both signals."""
    event = make_change_event(signals=make_signals(2.4, ratio=2.7, available=True))
    item = score_change_event(symbol="HDFCBANK", change_event=event)

    assert item.attention_score == pytest.approx(1.35)
    assert item.attention_level == AttentionLevel.MEDIUM
    assert item.explanation == (
        "HDFCBANK moved +2.4% since your last check. Trading volume "
        "accelerated to 2.7× the rate observed before you last checked."
    )


# --- Available but sub-threshold volume (REGRESSION) -------------------------
#
# Bug found during manual browser QA: _build_explanation used to append the
# volume clause whenever volume_acceleration_available was True, with no
# check that the ratio actually met VOLUME_ACCELERATION_THRESHOLD -- an
# available-but-sub-threshold ratio (e.g. 0.0x, computed when checkpoint and
# current volume are equal) produced "Trading volume accelerated to 0.0x
# the rate observed before you last checked," describing a non-event as an
# acceleration. This must never contribute to the score (already correct,
# unchanged by this fix) OR to the explanation text (the actual bug).


def test_meaningful_price_with_zero_volume_ratio_has_no_acceleration_claim():
    """Exact bug scenario: a price-driven event where volume is available
    but computed as exactly 0.0x (no volume accumulated since checkpoint).
    The explanation must read as price-only -- no "Trading volume" clause,
    no mention of "0.0" or "accelerated" -- even though
    volume_acceleration_available is True."""
    event = make_change_event(signals=make_signals(4.2, ratio=0.0, available=True))
    item = score_change_event(symbol="RELIANCE", change_event=event)

    assert item.attention_score == pytest.approx(2.1)  # price alone: 4.2 / 2.0
    assert item.explanation == "RELIANCE moved +4.2% since your last check."
    assert "Trading volume" not in item.explanation
    assert "accelerated" not in item.explanation
    # The raw signal fields themselves are untouched by this fix -- only
    # the explanation TEXT changed, never the API/data fields.
    assert item.volume_acceleration_ratio == 0.0
    assert item.volume_acceleration_available is True


def test_meaningful_price_with_sub_threshold_volume_has_no_acceleration_claim():
    """Same gap, a non-zero but still below-threshold ratio (1.3x) -- not
    just the 0.0x edge case from the bug report."""
    event = make_change_event(signals=make_signals(3.0, ratio=1.3, available=True))
    item = score_change_event(symbol="TCS", change_event=event)

    assert item.attention_score == pytest.approx(1.5)  # price alone: 3.0 / 2.0
    assert item.explanation == "TCS moved +3.0% since your last check."
    assert "Trading volume" not in item.explanation


def test_meaningful_price_with_meaningful_volume_keeps_acceleration_claim():
    """Required regression: once the ratio actually meets
    VOLUME_ACCELERATION_THRESHOLD (inclusive), the volume clause must still
    appear -- this fix must not suppress a genuinely meaningful signal."""
    event = make_change_event(signals=make_signals(3.0, ratio=2.0, available=True))
    item = score_change_event(symbol="HDFCBANK", change_event=event)

    assert item.explanation == (
        "HDFCBANK moved +3.0% since your last check. Trading volume "
        "accelerated to 2.0× the rate observed before you last checked."
    )


# --- Unavailable volume -------------------------------------------------------


def test_unavailable_volume_never_contributes_to_score_or_explanation():
    event = make_change_event(signals=make_signals(3.0, ratio=None, available=False))
    item = score_change_event(symbol="ICICIBANK", change_event=event)

    assert item.attention_score == pytest.approx(1.5)  # price only
    assert "Trading volume" not in item.explanation
    assert item.volume_acceleration_ratio is None
    assert item.volume_acceleration_available is False


# --- Exact threshold boundaries -----------------------------------------------


def test_price_strength_exactly_at_threshold_is_1_0():
    event = make_change_event(signals=make_signals(2.0))  # == PRICE_CHANGE_THRESHOLD_PCT
    item = score_change_event(symbol="X", change_event=event)
    assert item.attention_score == pytest.approx(1.0)


def test_volume_strength_exactly_at_threshold_is_1_0():
    event = make_change_event(
        signals=make_signals(0.1, ratio=2.0, available=True)  # == VOLUME_ACCELERATION_THRESHOLD
    )
    item = score_change_event(symbol="X", change_event=event)
    assert item.attention_score == pytest.approx(1.0)


# --- HIGH / MEDIUM / WATCH boundaries -----------------------------------------


@pytest.mark.parametrize(
    "price_change_pct,expected_score,expected_level",
    [
        (2.0, 1.0, AttentionLevel.WATCH),  # exactly 1.0 -> WATCH floor
        (2.4999999, 1.24999995, AttentionLevel.WATCH),  # just below 1.25
        (2.5, 1.25, AttentionLevel.MEDIUM),  # exactly 1.25 -> MEDIUM
        (3.9999999, 1.99999995, AttentionLevel.MEDIUM),  # just below 2.0
        (4.0, 2.0, AttentionLevel.HIGH),  # exactly 2.0 -> HIGH
        (10.0, 5.0, AttentionLevel.HIGH),
    ],
)
def test_attention_level_boundaries(price_change_pct, expected_score, expected_level):
    event = make_change_event(signals=make_signals(price_change_pct))
    item = score_change_event(symbol="X", change_event=event)

    assert item.attention_score == pytest.approx(expected_score)
    assert item.attention_level == expected_level


# --- Determinism ---------------------------------------------------------------


def test_explanation_and_score_are_deterministic():
    event = make_change_event(signals=make_signals(2.4, ratio=2.7, available=True))
    first = score_change_event(symbol="HDFCBANK", change_event=event)
    second = score_change_event(symbol="HDFCBANK", change_event=event)

    assert first.explanation == second.explanation
    assert first.attention_score == second.attention_score
    assert first.attention_level == second.attention_level


# --- AttentionEngine (DB-touching) --------------------------------------------


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["test_db"]
    ensure_indexes(database)
    yield database
    client.close()


def insert_instrument(db, symbol: str) -> str:
    result = db.instruments.insert_one({"symbol": symbol, "exchange": "NSE"})
    return str(result.inserted_id)


def insert_change_event(
    db,
    *,
    user_id,
    instrument_id,
    checkpoint_id,
    price_change_pct,
    ratio=None,
    available=False,
    acknowledged=False,
):
    event = ChangeEvent(
        user_id=user_id,
        instrument_id=instrument_id,
        checkpoint_id=checkpoint_id,
        signals=make_signals(price_change_pct, ratio, available),
        reason="irrelevant to the Attention Engine",
        acknowledged=acknowledged,
    )
    db.change_events.insert_one(event.model_dump(mode="json"))


def test_symbol_resolution(db):
    instrument_id = insert_instrument(db, "RELIANCE")
    insert_change_event(
        db, user_id="user1", instrument_id=instrument_id, checkpoint_id="cp1", price_change_pct=4.0
    )

    items = AttentionEngine(db).get_ranked_active_items("user1")

    assert len(items) == 1
    assert items[0].symbol == "RELIANCE"
    assert items[0].instrument_id == instrument_id


def test_acknowledged_events_never_appear(db):
    instrument_id = insert_instrument(db, "RELIANCE")
    insert_change_event(
        db,
        user_id="user1",
        instrument_id=instrument_id,
        checkpoint_id="cp1",
        price_change_pct=4.0,
        acknowledged=True,
    )

    items = AttentionEngine(db).get_ranked_active_items("user1")

    assert items == []


def test_only_active_events_appear_when_mixed_with_acknowledged_ones(db):
    reliance_id = insert_instrument(db, "RELIANCE")
    insert_change_event(
        db,
        user_id="user1",
        instrument_id=reliance_id,
        checkpoint_id="cp-old",
        price_change_pct=4.0,
        acknowledged=True,
    )
    tcs_id = insert_instrument(db, "TCS")
    insert_change_event(
        db,
        user_id="user1",
        instrument_id=tcs_id,
        checkpoint_id="cp-new",
        price_change_pct=3.0,
        acknowledged=False,
    )

    items = AttentionEngine(db).get_ranked_active_items("user1")

    assert len(items) == 1
    assert items[0].symbol == "TCS"


def test_multiple_events_are_ranked_highest_score_first(db):
    reliance_id = insert_instrument(db, "RELIANCE")
    tcs_id = insert_instrument(db, "TCS")
    infy_id = insert_instrument(db, "INFY")

    insert_change_event(
        db, user_id="user1", instrument_id=reliance_id, checkpoint_id="cp1", price_change_pct=2.2
    )  # score 1.1 -> WATCH
    insert_change_event(
        db, user_id="user1", instrument_id=tcs_id, checkpoint_id="cp2", price_change_pct=8.0
    )  # score 4.0 -> HIGH
    insert_change_event(
        db, user_id="user1", instrument_id=infy_id, checkpoint_id="cp3", price_change_pct=3.0
    )  # score 1.5 -> MEDIUM

    items = AttentionEngine(db).get_ranked_active_items("user1")

    assert [item.symbol for item in items] == ["TCS", "INFY", "RELIANCE"]
    assert [item.rank for item in items] == [1, 2, 3]
    assert items[0].attention_level == AttentionLevel.HIGH
    assert items[1].attention_level == AttentionLevel.MEDIUM
    assert items[2].attention_level == AttentionLevel.WATCH


def test_different_users_events_do_not_mix(db):
    instrument_id = insert_instrument(db, "RELIANCE")
    insert_change_event(
        db, user_id="user1", instrument_id=instrument_id, checkpoint_id="cp1", price_change_pct=4.0
    )
    insert_change_event(
        db, user_id="user2", instrument_id=instrument_id, checkpoint_id="cp2", price_change_pct=4.0
    )

    items = AttentionEngine(db).get_ranked_active_items("user1")

    assert len(items) == 1


def test_no_active_events_returns_empty_list(db):
    assert AttentionEngine(db).get_ranked_active_items("user1") == []
