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

    # ChangeEvent: not unique (multiple change events can exist over
    # time for the same user/instrument, tied to different checkpoints),
    # but queried constantly by (user_id, acknowledged) for the Attention
    # Engine's "active changes for this user" query (a later phase, but
    # the query shape is already fixed by architecture.md's design).
    db.change_events.create_index(
        [("user_id", ASCENDING), ("acknowledged", ASCENDING)],
        name="user_acknowledged_lookup",
    )
