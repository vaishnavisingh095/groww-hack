"""
Watchlist routes.

Per explicit instruction: thin handlers only. All business logic
(fetching, assembling snapshots, computing change) lives in the
services/ layer and is imported here, not written inline.

Response shapes are our own domain JSON -- no yfinance field names,
objects, or shapes ever reach the frontend.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pymongo.database import Database

from app.db.connection import get_database
from app.models.market_snapshot import SnapshotStatus
from app.providers.yfinance_provider import YFinanceProvider
from app.services.change_engine import evaluate_price_change
from app.services.checkpoint_service import CheckpointService
from app.services.market_data_service import MarketDataService
from app.services.watchlist_service import (
    DEMO_USER_ID,
    ensure_seed_instruments,
    yfinance_ticker_for,
)

router = APIRouter()

# One shared provider instance -- yfinance.Ticker objects are cheap to
# create per-call, so there is no connection/session state worth pooling
# here (unlike the MongoDB client in db/connection.py, which genuinely
# manages a connection pool worth sharing).
_provider = YFinanceProvider()


def _status_label(status: SnapshotStatus, fetched_at: datetime) -> tuple[str, int]:
    """
    Compute the human-facing freshness label and raw age in seconds.
    ALWAYS derived from our own fetched_at -- never from provider_timestamp,
    per architecture.md and the explicit instruction repeated in this
    task.
    """
    age_seconds = int((datetime.now(timezone.utc) - fetched_at).total_seconds())
    if status == SnapshotStatus.OK:
        label = f"Updated {age_seconds}s ago"
    elif status == SnapshotStatus.STALE:
        minutes = age_seconds // 60
        label = f"Data delayed — {minutes}m ago" if minutes >= 1 else f"Data delayed — {age_seconds}s ago"
    else:  # UNAVAILABLE or INVALID
        minutes = age_seconds // 60
        label = f"Data unavailable — last update {minutes}m ago" if minutes >= 1 else "Data unavailable"
    return label, age_seconds


@router.get("/watchlist")
def get_watchlist() -> dict:
    """
    Fetch current market data for the demo watchlist, compare each
    instrument against its checkpoint (if any), and return clean domain
    JSON for the frontend.

    Checkpoint handling on this read path:
    - An EXISTING checkpoint is NEVER advanced or replaced here -- only
      read, for comparison. This is what "mark as seen" is for.
    - If NO checkpoint exists yet for a (user, instrument) pair, an
      IMPLICIT one is established from the current snapshot (per
      architecture.md, hard question G), so the instrument has a
      baseline for future comparisons. Per that same design note ("...
      resolves this state on the instrument's next poll cycle"), THIS
      request still reports has_baseline=False for that instrument --
      the comparison below is evaluated against what existed BEFORE the
      implicit checkpoint was created, not after. The next GET
      /watchlist call will see the checkpoint and compare against it
      normally.
    """
    db = get_database()
    instruments = ensure_seed_instruments(db)
    checkpoint_service = CheckpointService(db)
    market_service = MarketDataService(_provider)

    symbol_to_instrument_id = {
        yfinance_ticker_for(inst["symbol"], inst["exchange"]): str(inst["_id"])
        for inst in instruments
    }

    snapshots = market_service.fetch_snapshots(symbol_to_instrument_id)
    snapshots_by_instrument_id = {s.instrument_id: s for s in snapshots}

    results = []
    for inst in instruments:
        instrument_id = str(inst["_id"])
        snapshot = snapshots_by_instrument_id.get(instrument_id)

        if snapshot is None:
            # Provider gave us nothing usable this cycle for this
            # instrument. Per architecture.md's failure table: do not
            # fabricate data, report unavailable. (This slice has no
            # "last known good" persistence for snapshots yet -- see
            # Known Limitations in the implementation report.) An
            # invalid/unavailable snapshot must never become a baseline,
            # implicit or otherwise -- there is nothing here to
            # checkpoint against, so ensure_initial_checkpoint is
            # correctly never called in this branch.
            results.append(
                {
                    "instrument_id": instrument_id,
                    "symbol": inst["symbol"],
                    "exchange": inst["exchange"],
                    "price": None,
                    "percent_change": None,
                    "cumulative_volume": None,
                    "status": SnapshotStatus.UNAVAILABLE.value,
                    "freshness_label": "Data unavailable",
                    "data_age_seconds": None,
                    "change": {
                        "has_baseline": False,
                        "meaningful_change": False,
                        "percent_difference": None,
                        "reason": "Data unavailable — cannot compare.",
                    },
                }
            )
            continue

        # Read the EXISTING checkpoint (if any) BEFORE potentially
        # creating an implicit one -- the comparison below must reflect
        # what the user actually saw last time, not a baseline we are
        # establishing this very request.
        checkpoint = checkpoint_service.get_checkpoint(DEMO_USER_ID, instrument_id)
        checkpoint_price = checkpoint.baseline_snapshot.last_price if checkpoint else None
        change_result = evaluate_price_change(checkpoint_price, snapshot.last_price)

        if checkpoint is None:
            # No prior checkpoint existed -- establish the implicit
            # baseline now so the NEXT request has something to compare
            # against. This does not affect change_result above, which
            # was already computed against the absence of a checkpoint.
            checkpoint_service.ensure_initial_checkpoint(
                DEMO_USER_ID, instrument_id, snapshot
            )

        label, age_seconds = _status_label(snapshot.status, snapshot.fetched_at)

        results.append(
            {
                "instrument_id": instrument_id,
                "symbol": inst["symbol"],
                "exchange": inst["exchange"],
                "price": snapshot.last_price,
                "percent_change": round(snapshot.percent_change, 4),
                "cumulative_volume": snapshot.volume,
                "status": snapshot.status.value,
                "freshness_label": label,
                "data_age_seconds": age_seconds,
                "change": {
                    "has_baseline": change_result.has_baseline,
                    "meaningful_change": change_result.meaningful_change,
                    "percent_difference": (
                        round(change_result.percent_difference, 4)
                        if change_result.percent_difference is not None
                        else None
                    ),
                    "reason": change_result.reason,
                },
            }
        )

    return {"instruments": results}


@router.post("/watchlist/instruments/{instrument_id}/checkpoint")
def mark_as_seen(instrument_id: str) -> dict:
    """
    Explicit "mark as seen" action for ONE instrument. Fetches the
    CURRENT market data for this instrument and persists it as the new
    checkpoint baseline, unconditionally advancing/replacing whatever
    checkpoint existed before.

    Path matches architecture.md's documented API contract exactly:
    POST /watchlist/instruments/{id}/checkpoint.
    """
    db = get_database()
    instrument_doc = db.instruments.find_one({"_id": _to_object_id(instrument_id)})
    if instrument_doc is None:
        raise HTTPException(status_code=404, detail="Instrument not found")

    market_service = MarketDataService(_provider)
    ticker = yfinance_ticker_for(instrument_doc["symbol"], instrument_doc["exchange"])
    snapshots = market_service.fetch_snapshots({ticker: instrument_id})

    if not snapshots:
        # Missing/invalid/unavailable current data must never become a
        # valid baseline -- fail the request instead of fabricating a
        # checkpoint from stale or absent data.
        raise HTTPException(
            status_code=503,
            detail="Could not fetch current market data — checkpoint not saved.",
        )

    snapshot = snapshots[0]
    checkpoint_service = CheckpointService(db)
    checkpoint = checkpoint_service.create_checkpoint_from_snapshot(
        DEMO_USER_ID, instrument_id, snapshot
    )

    return {
        "instrument_id": instrument_id,
        "symbol": instrument_doc["symbol"],
        "checkpoint_price": checkpoint.baseline_snapshot.last_price,
        "checkpoint_at": checkpoint.checkpoint_at.isoformat(),
        "message": f"Baseline saved at ₹{checkpoint.baseline_snapshot.last_price:.2f}",
    }


@router.post("/watchlist/checkpoint")
def mark_all_as_seen() -> dict:
    """
    Explicit "mark all as seen" for the whole watchlist. Advances the
    checkpoint for every instrument that currently has a valid snapshot;
    instruments without valid current data are skipped safely (not
    fabricated, not treated as an error for the whole batch) and
    reported separately in the response so the caller can see exactly
    what happened.

    Path matches architecture.md's documented API contract exactly:
    POST /watchlist/checkpoint.
    """
    db = get_database()
    instruments = ensure_seed_instruments(db)
    market_service = MarketDataService(_provider)
    checkpoint_service = CheckpointService(db)

    symbol_to_instrument_id = {
        yfinance_ticker_for(inst["symbol"], inst["exchange"]): str(inst["_id"])
        for inst in instruments
    }
    snapshots = market_service.fetch_snapshots(symbol_to_instrument_id)
    snapshots_by_instrument_id = {s.instrument_id: s for s in snapshots}

    updated = []
    skipped = []
    for inst in instruments:
        instrument_id = str(inst["_id"])
        snapshot = snapshots_by_instrument_id.get(instrument_id)

        if snapshot is None:
            # No valid current snapshot for this instrument this cycle
            # -- skip it rather than fabricating a checkpoint or failing
            # the entire batch over one instrument's bad data.
            skipped.append({"instrument_id": instrument_id, "symbol": inst["symbol"]})
            continue

        checkpoint = checkpoint_service.create_checkpoint_from_snapshot(
            DEMO_USER_ID, instrument_id, snapshot
        )
        updated.append(
            {
                "instrument_id": instrument_id,
                "symbol": inst["symbol"],
                "checkpoint_price": checkpoint.baseline_snapshot.last_price,
            }
        )

    return {"updated": updated, "skipped": skipped}


def _to_object_id(instrument_id: str):
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        return ObjectId(instrument_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid instrument id")