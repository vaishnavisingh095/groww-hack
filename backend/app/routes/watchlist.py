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
from app.services.change_engine import evaluate_change
from app.services.change_event_service import ChangeEventService
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

    Checkpoint handling on this read path (per the checkpoint semantics
    contract): opening the app, rendering, or refreshing is NEVER
    acknowledgement. This endpoint is READ-ONLY with respect to
    checkpoint state -- it never creates, replaces, or otherwise writes
    a Checkpoint document, implicitly or otherwise.
    - An EXISTING checkpoint is read for comparison only, never
      advanced or replaced here. This is what the explicit "mark as
      seen" endpoints are for.
    - If NO checkpoint exists yet for a (user, instrument) pair, the
      instrument reports has_baseline=False ("baseline pending") and no
      change comparison is run. No checkpoint is created to resolve
      this on a later request -- only an explicit acknowledgement
      (POST /watchlist/checkpoint or
      POST /watchlist/instruments/{id}/checkpoint) can do that.

    ChangeEvent persistence (this is the one write this endpoint DOES
    make): when an explicit checkpoint exists, the snapshot's status is
    OK, and the Change Engine reports a meaningful change, the
    corresponding ChangeEvent is persisted (or reused if one already
    exists for this exact checkpoint version) via ChangeEventService.
    This is intentionally still a GET-triggered write -- it persists a
    market-observed FACT ("this checkpoint's baseline was meaningfully
    exceeded"), not user acknowledgement, so it does not conflict with
    the checkpoint semantics contract above. A stale/invalid/unavailable
    snapshot, or the absence of a checkpoint, never creates one -- see
    ChangeEventService.get_or_create_active.
    """
    db = get_database()
    instruments = ensure_seed_instruments(db)
    checkpoint_service = CheckpointService(db)
    change_event_service = ChangeEventService(db)
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
            # invalid/unavailable snapshot must never create or advance
            # checkpoint state -- there is nothing here to checkpoint
            # against, and this read path never writes a checkpoint
            # regardless.
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
                        "price_change_pct": None,
                        "volume_acceleration_ratio": None,
                        "volume_signal_available": False,
                        "reason": "Data unavailable — cannot compare.",
                    },
                }
            )
            continue

        # Read-only: this endpoint never creates or advances a
        # checkpoint. If none exists, the comparison below correctly
        # falls back to the no-baseline branch.
        checkpoint = checkpoint_service.get_checkpoint(DEMO_USER_ID, instrument_id)

        if checkpoint is not None:
            change_result = evaluate_change(
                checkpoint_price=checkpoint.baseline_snapshot.last_price,
                checkpoint_volume=checkpoint.baseline_snapshot.volume,
                checkpoint_at=checkpoint.checkpoint_at,
                checkpoint_session_date=checkpoint.session_date,
                current_price=snapshot.last_price,
                current_volume=snapshot.volume,
                current_fetched_at=snapshot.fetched_at,
                current_session_date=snapshot.session_date,
            )
        else:
            # No checkpoint at all yet -- price-only call correctly
            # reports has_baseline=False without needing volume/timing
            # args that don't apply to a nonexistent baseline.
            change_result = evaluate_change(
                checkpoint_price=None, current_price=snapshot.last_price
            )

        # Persist/reuse the ChangeEvent for this checkpoint version, if
        # eligible. No-ops (returns None) unless checkpoint is explicit,
        # snapshot.status is OK, and change_result.meaningful_change is
        # True -- see ChangeEventService.get_or_create_active.
        change_event_service.get_or_create_active(
            user_id=DEMO_USER_ID,
            instrument_id=instrument_id,
            checkpoint=checkpoint,
            snapshot_status=snapshot.status,
            change_result=change_result,
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
                    "price_change_pct": (
                        round(change_result.price_change_pct, 4)
                        if change_result.price_change_pct is not None
                        else None
                    ),
                    "volume_acceleration_ratio": (
                        round(change_result.volume_acceleration_ratio, 4)
                        if change_result.volume_acceleration_ratio is not None
                        else None
                    ),
                    "volume_signal_available": change_result.volume_signal.available,
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

    Ordering note: the checkpoint write happens BEFORE the ChangeEvent
    acknowledgement below, and only unconditionally-successful code runs
    in between -- if the checkpoint write itself were ever to raise, the
    acknowledgement call is never reached, so a failed checkpoint
    advance can never falsely acknowledge an active ChangeEvent.
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
        # checkpoint from stale or absent data. Nothing is acknowledged
        # either, since we never reach the checkpoint write below.
        raise HTTPException(
            status_code=503,
            detail="Could not fetch current market data — checkpoint not saved.",
        )

    snapshot = snapshots[0]
    checkpoint_service = CheckpointService(db)
    checkpoint = checkpoint_service.create_checkpoint_from_snapshot(
        DEMO_USER_ID, instrument_id, snapshot
    )
    # The checkpoint write above succeeded -- this instrument's prior
    # baseline is now genuinely superseded, so its active ChangeEvent(s)
    # (if any) are acknowledged.
    ChangeEventService(db).acknowledge_active(DEMO_USER_ID, instrument_id)

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

    Ordering note: per instrument, the checkpoint write happens BEFORE
    that instrument's ChangeEvent acknowledgement, and a skipped
    instrument (no valid snapshot) never reaches either call -- so a
    skipped/failed instrument's active ChangeEvent(s), if any, are left
    untouched, never falsely acknowledged.
    """
    db = get_database()
    instruments = ensure_seed_instruments(db)
    market_service = MarketDataService(_provider)
    checkpoint_service = CheckpointService(db)
    change_event_service = ChangeEventService(db)

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
        # This instrument's checkpoint write succeeded -- acknowledge
        # its active ChangeEvent(s), if any. Instruments that hit the
        # `continue` above (no valid snapshot) never reach this line.
        change_event_service.acknowledge_active(DEMO_USER_ID, instrument_id)
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