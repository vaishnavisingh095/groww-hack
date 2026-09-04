"""
Attention Engine — Phase 6.

Ranks a user's currently ACTIVE (acknowledged=False) ChangeEvents by how
far past its own threshold each one's strongest signal is, and generates
a deterministic, human-readable explanation for each. Per
architecture.md: "Nothing persisted -- fully derivable from active
ChangeEvents at any moment." Nothing here writes to MongoDB; it only
reads ChangeEvent (owned by ChangeEventService) and Instrument (owned by
the Watchlist/Instrument layer) documents that already exist.

Deliberately kept separate from change_engine.py (rule: "Keep Change
Detection separate from Attention Ranking... do not fold ranking logic
into the Change Engine or vice versa") -- this module answers "what
order to show changes in and why," never "whether something changed."
It reuses change_engine.py's own threshold constants rather than
duplicating them, so a ranking is always traceable back to the exact
same numbers the Change Engine used to decide the event was meaningful
in the first place.

One ChangeEvent produces exactly one ranked AttentionItem, even when
both signals are individually past threshold -- the score is the max of
whichever signals are available, not a separate item per signal.

The scoring/explanation functions below (`score_change_event`,
`_price_strength`, `_volume_strength`, `_attention_level`,
`_build_explanation`) are pure -- no I/O, no MongoDB, testable exactly
like change_engine.py's own functions. `AttentionEngine` is the thin,
DB-touching layer that fetches the active ChangeEvents, resolves each
one's symbol, and calls the pure functions -- mirroring how
CheckpointService/ChangeEventService are the DB-touching counterparts
to their own otherwise-pure logic.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from bson import ObjectId

from app.models.change_event import ChangeEvent
from app.services.change_engine import PRICE_CHANGE_THRESHOLD_PCT, VOLUME_ACCELERATION_THRESHOLD


class AttentionLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    WATCH = "watch"


@dataclass
class AttentionItem:
    instrument_id: str
    symbol: str
    checkpoint_id: str
    detected_at: datetime
    price_change_pct: float
    volume_acceleration_ratio: float | None
    volume_acceleration_available: bool
    attention_score: float
    attention_level: AttentionLevel
    explanation: str
    rank: int


def _price_strength(price_change_pct: float) -> float:
    """
    How far past PRICE_CHANGE_THRESHOLD_PCT the move is, as a multiple.
    Uses the ABSOLUTE value -- price_change_pct is signed (a drop is
    negative), but "how strong is this signal" is a magnitude question,
    exactly mirroring change_engine.py's own `abs(price_change_pct) >=
    PRICE_CHANGE_THRESHOLD_PCT` meaningfulness check. Normalizing on the
    signed value would rank a -6% move BELOW a +1% move under max(),
    which is not what "attention-worthy" means.
    """
    return abs(price_change_pct) / PRICE_CHANGE_THRESHOLD_PCT


def _volume_strength(volume_acceleration_ratio: float) -> float:
    """
    volume_acceleration_ratio is already a non-negative rate multiple
    (change_engine.py's monotonic-volume guard means it can never be
    negative when available), so no abs() is needed here -- unlike
    price, there is no sign to lose.
    """
    return volume_acceleration_ratio / VOLUME_ACCELERATION_THRESHOLD


def _attention_level(score: float) -> AttentionLevel:
    """
    Locked bands (inclusive lower bounds, matching change_engine.py's
    own inclusive `>=` threshold convention):
      HIGH   : score >= 2.0
      MEDIUM : 1.25 <= score < 2.0
      WATCH  : 1.0 <= score < 1.25

    A ChangeEvent only exists at all when at least one signal was
    already >= its own threshold at detection time (that is what
    "meaningful_change" meant) -- so score, which is the max of the
    same signals normalized by the same thresholds, can never legitimately
    be below 1.0 here. WATCH is therefore the true floor, not a
    catch-all for an unreachable case.
    """
    if score >= 2.0:
        return AttentionLevel.HIGH
    if score >= 1.25:
        return AttentionLevel.MEDIUM
    return AttentionLevel.WATCH


def _build_explanation(
    symbol: str,
    price_change_pct: float,
    volume_acceleration_ratio: float | None,
    volume_acceleration_available: bool,
) -> str:
    """
    Deterministic, template-built string from the numbers already on
    the ChangeEvent -- never the persisted ChangeEvent.reason (which is
    change_engine.py's own detection-time explanation, a distinct
    concern per this phase's explicit instruction), never an LLM call.

    Price clause always communicates the SIGNED movement (a drop reads
    as "-5.0%", not "5.0%"). Volume clause is appended only when
    available, using architecture.md's locked required wording
    ("...the rate observed before you last checked", never "x normal
    volume", since the signal is a same-session rate comparison, not a
    historical-normal baseline).
    """
    sign = "+" if price_change_pct >= 0 else ""
    explanation = f"{symbol} moved {sign}{price_change_pct:.1f}% since your last check."

    if volume_acceleration_available:
        explanation += (
            f" Trading volume accelerated to {volume_acceleration_ratio:.1f}× "
            "the rate observed before you last checked."
        )

    return explanation


def score_change_event(*, symbol: str, change_event: ChangeEvent) -> AttentionItem:
    """
    Score and explain a single ChangeEvent. Does not sort or rank
    against other events -- that is AttentionEngine's job, since ranking
    only makes sense across a whole active set, not for one event in
    isolation. `rank` is set to 0 here as a placeholder always
    overwritten by AttentionEngine.get_ranked_active_items.
    """
    signals = change_event.signals
    strengths = [_price_strength(signals.price_change_pct)]
    if signals.volume_acceleration_available:
        strengths.append(_volume_strength(signals.volume_acceleration_ratio))

    score = max(strengths)

    return AttentionItem(
        instrument_id=change_event.instrument_id,
        symbol=symbol,
        checkpoint_id=change_event.checkpoint_id,
        detected_at=change_event.detected_at,
        price_change_pct=signals.price_change_pct,
        volume_acceleration_ratio=signals.volume_acceleration_ratio,
        volume_acceleration_available=signals.volume_acceleration_available,
        attention_score=score,
        attention_level=_attention_level(score),
        explanation=_build_explanation(
            symbol,
            signals.price_change_pct,
            signals.volume_acceleration_ratio,
            signals.volume_acceleration_available,
        ),
        rank=0,
    )


class AttentionEngine:
    """
    Thin DB-touching layer: fetch this user's active ChangeEvents,
    resolve each one's instrument symbol, score/explain (pure functions
    above), and return them ranked highest-attention-first. Nothing is
    written to MongoDB and nothing computed here is persisted -- a fresh
    call always recomputes from the current active ChangeEvent set,
    consistent with architecture.md's "never the persisted source of
    truth" design for attention ranking.
    """

    def __init__(self, db):
        self._db = db

    def get_ranked_active_items(self, user_id: str) -> list[AttentionItem]:
        docs = self._db.change_events.find({"user_id": user_id, "acknowledged": False})

        items = []
        for doc in docs:
            doc.pop("_id", None)
            change_event = ChangeEvent(**doc)
            symbol = self._resolve_symbol(change_event.instrument_id)
            items.append(score_change_event(symbol=symbol, change_event=change_event))

        items.sort(key=lambda item: item.attention_score, reverse=True)
        for rank, item in enumerate(items, start=1):
            item.rank = rank

        return items

    def _resolve_symbol(self, instrument_id: str) -> str:
        """
        Per this phase's explicit instruction: resolve instrument_id to
        the existing Instrument.symbol rather than adding a symbol
        field to ChangeEvent. Raw dict access (not the Instrument
        pydantic model), matching how app/routes/watchlist.py already
        reads instrument documents for the same purpose.
        """
        instrument_doc = self._db.instruments.find_one({"_id": ObjectId(instrument_id)})
        return instrument_doc["symbol"]
