"""
MongoDB index setup.

Kept separate from the model definitions: pydantic models describe the
shape and validation of a document, but indexes are a storage/query
concern the models themselves shouldn't need to know about. This also
means indexes can be created once (e.g., at app startup) without
importing pymongo into every model file.

Each index below is tied to a concrete requirement from architecture.md,
not added speculatively:
"""
from pymongo.database import Database
from pymongo import ASCENDING


def ensure_indexes(db: Database) -> None:
    # Instrument: (symbol, exchange) uniquely identifies one tradeable
    # listing. architecture.md treats RELIANCE-on-NSE and RELIANCE-on-BSE
    # as distinct Instrument documents, so the uniqueness is on the pair,
    # not on symbol alone.
    db.instruments.create_index(
        [("symbol", ASCENDING), ("exchange", ASCENDING)],
        unique=True,
        name="uniq_symbol_exchange",
    )

    # Watchlist: one watchlist per user for the hackathon MVP
    # (plan.md CUT list: no multi-list support). Enforcing this as a
    # unique index means "one watchlist per user" is a database-level
    # guarantee, not just an application-level convention that a future
    # bug could silently violate.
    db.watchlists.create_index(
        [("user_id", ASCENDING)],
        unique=True,
        name="uniq_user_id",
    )

    # MarketSnapshot: one document per instrument, upserted every poll
    # cycle (architecture.md: "not an append-only history"). Unique index
    # on instrument_id is what makes the upsert-by-instrument pattern
    # safe — without it, nothing would prevent duplicate snapshot
    # documents for the same instrument from accumulating.
    db.market_snapshots.create_index(
        [("instrument_id", ASCENDING)],
        unique=True,
        name="uniq_instrument_id",
    )

    # Checkpoint: "one active checkpoint per (user, instrument) pair —
    # advancing the checkpoint replaces the previous one" (architecture.md).
    # Same reasoning as MarketSnapshot's index: this is what makes
    # "advancing a checkpoint" a safe upsert rather than an operation that
    # could accidentally create duplicates.
    db.checkpoints.create_index(
        [("user_id", ASCENDING), ("instrument_id", ASCENDING)],
        unique=True,
        name="uniq_user_instrument_checkpoint",
    )

    # ChangeEvent: queried constantly by (user_id, acknowledged) for the
    # Attention Engine's "active changes for this user" query (a later
    # phase, but the query shape is already fixed by architecture.md's
    # design). Not unique -- multiple change events legitimately exist
    # over time for the same user/instrument, tied to different
    # checkpoints.
    db.change_events.create_index(
        [("user_id", ASCENDING), ("acknowledged", ASCENDING)],
        name="user_acknowledged_lookup",
    )

    # ChangeEvent uniqueness invariant (ChangeEvent persistence
    # milestone): exactly one ChangeEvent per
    # (user_id, instrument_id, checkpoint_id) -- a given checkpoint
    # VERSION may be evaluated as "meaningfully changed" many times
    # (every GET/refresh while it stays the active checkpoint), but must
    # never produce more than one persisted event for it.
    # `acknowledged` is deliberately excluded from this identity: an
    # event transitions from unacknowledged to acknowledged in place, it
    # is never duplicated because of that transition. This is the real
    # source of truth for the invariant under concurrent requests --
    # ChangeEventService's find-before-insert check is only the
    # common-case fast path.
    db.change_events.create_index(
        [("user_id", ASCENDING), ("instrument_id", ASCENDING), ("checkpoint_id", ASCENDING)],
        unique=True,
        name="uniq_user_instrument_checkpoint_change_event",
    )
