"""
Watchlist Service.

Ownership model (Persistent Anonymous Watchlist milestone): the
Watchlist model -- defined since Phase 1 but never previously used, see
decisions.md -- is now the real per-owner membership record.
Instrument documents remain GLOBAL reference data, shared and reused
across every owner (a ticker's metadata isn't "owned" by anyone); only
WHICH instruments a given owner tracks is owner-scoped, via
Watchlist.instrument_ids. get_or_create_watchlist() and
get_watchlist_instruments() below are the two functions that make this
real; add_instrument() now also updates the calling owner's own
membership. No second ownership model was introduced -- this activates
the one that already existed.

`owner_id` here is always the value resolved by
app.services.identity.resolve_owner_id from the anonymous capability
cookie -- this module has no opinion about where it came from, exactly
like CheckpointService/ChangeEventService/AttentionEngine, which have
always taken user_id as a plain parameter.

DEMO_USER_ID below is now a LEGACY value only: it was the single
hardcoded user_id every request used before anonymous identity existed.
It is kept here, unused by any route, purely so the exact string is
still discoverable in code for anyone who needs to manually inspect
that pre-existing data later -- see decisions.md's "Legacy demo-user
data" entry. No code path writes new data under this value anymore, and
none of the existing data under it is touched, migrated, or deleted by
this milestone.
"""
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.models.instrument import Exchange, Instrument
from app.models.watchlist import Watchlist
from app.providers.base import MarketDataProvider
from app.services.market_data_service import MarketDataService

DEMO_USER_ID = "demo-user"

SEED_INSTRUMENTS = [
    ("RELIANCE", Exchange.NSE),
    ("TCS", Exchange.NSE),
    ("HDFCBANK", Exchange.NSE),
    ("INFY", Exchange.NSE),
    ("ICICIBANK", Exchange.NSE),
]


class ProviderResolutionError(Exception):
    """
    Raised when the market data provider cannot produce a valid current
    snapshot for a requested (symbol, exchange) pair -- the same "no
    usable current data" condition POST /watchlist/instruments/{id}/
    checkpoint already handles (by returning 503 and creating nothing),
    just checked here at add-time instead of checkpoint-time.
    """

    def __init__(self, symbol: str, exchange: str):
        self.symbol = symbol
        self.exchange = exchange
        super().__init__(f"Provider could not resolve {symbol} on {exchange}")


def ensure_seed_instruments(db: Database) -> list[dict]:
    """
    Idempotently ensure the 5 required instruments exist as Instrument
    documents, and return them (with their Mongo _id as instrument_id).

    Idempotent via the (symbol, exchange) unique index from Phase 1 --
    calling this repeatedly (e.g., on every app startup) never creates
    duplicates.
    """
    result = []
    for symbol, exchange in SEED_INSTRUMENTS:
        existing = db.instruments.find_one({"symbol": symbol, "exchange": exchange.value})
        if existing:
            result.append(existing)
            continue

        instrument = Instrument(symbol=symbol, exchange=exchange)
        doc = instrument.model_dump(mode="json")
        try:
            insert_result = db.instruments.insert_one(doc)
            doc["_id"] = insert_result.inserted_id
        except DuplicateKeyError:
            # Two brand-new owners' first-ever requests can both reach
            # this loop (via get_or_create_watchlist) for the same
            # not-yet-seeded symbol before either has inserted it -- the
            # unique index (uniq_symbol_exchange) is the real source of
            # truth here, same pattern as add_instrument's own recovery.
            # Reuse whatever the winning request actually persisted
            # rather than raising an unhandled 500 for the loser.
            existing = db.instruments.find_one(
                {"symbol": symbol, "exchange": exchange.value}
            )
            if existing is None:
                raise
            doc = existing
        result.append(doc)
    return result


def get_or_create_watchlist(db: Database, owner_id: str) -> Watchlist:
    """
    Idempotently ensure a Watchlist document exists for this owner,
    defaulting a brand-new owner's membership to the 5 EXISTING seed
    instruments (reusing their existing global Instrument documents via
    ensure_seed_instruments -- never creating new ones). Creates ONLY a
    Watchlist document -- never a Checkpoint, never a ChangeEvent, so a
    new owner's seeded instruments start exactly baseline_pending,
    identical to how the 5 seeds behaved for the original demo user
    before their first "mark as seen."
    """
    doc = db.watchlists.find_one({"user_id": owner_id})
    if doc is not None:
        doc.pop("_id", None)
        return Watchlist(**doc)

    seed_instruments = ensure_seed_instruments(db)
    watchlist = Watchlist(
        user_id=owner_id,
        instrument_ids=[str(inst["_id"]) for inst in seed_instruments],
    )
    try:
        db.watchlists.insert_one(watchlist.model_dump(mode="json"))
    except DuplicateKeyError:
        # Another concurrent request for this exact owner_id already
        # created the Watchlist first -- e.g. this app's own frontend
        # fires GET /watchlist and GET /watchlist/attention concurrently
        # on first load (see App.jsx's loadAll), and both could resolve
        # this same brand-new owner_id in the same instant. The unique
        # index on user_id (Phase 1) is the real source of truth here;
        # reuse whatever was actually persisted rather than raising or
        # creating a duplicate.
        existing = db.watchlists.find_one({"user_id": owner_id})
        if existing is not None:
            existing.pop("_id", None)
            return Watchlist(**existing)
        raise
    return watchlist


def get_watchlist_instruments(db: Database, owner_id: str) -> list[dict]:
    """
    THIS OWNER'S OWN tracked instruments, resolved via
    Watchlist.instrument_ids membership -- never "every Instrument
    document in the collection" (that was the pre-ownership behavior;
    see decisions.md's "Persistent anonymous watchlist" entry).
    Instruments themselves remain global/shared reference data; only
    membership is owner-scoped.
    """
    watchlist = get_or_create_watchlist(db, owner_id)
    if not watchlist.instrument_ids:
        return []

    object_ids = [ObjectId(iid) for iid in watchlist.instrument_ids]
    docs_by_id = {
        str(doc["_id"]): doc for doc in db.instruments.find({"_id": {"$in": object_ids}})
    }
    # Preserve the owner's own membership order (append order -- a
    # newly-added instrument shows up last) rather than whatever
    # incidental order MongoDB's $in returns. Skips an id defensively if
    # its Instrument document is ever missing, rather than crashing --
    # should not happen in practice since instruments are never deleted.
    return [docs_by_id[iid] for iid in watchlist.instrument_ids if iid in docs_by_id]


def _add_instrument_to_watchlist(db: Database, owner_id: str, instrument_id: str) -> None:
    """
    Ensures the owner's Watchlist exists, then idempotently adds one
    instrument id to it. $addToSet makes this safe to call for an
    instrument the owner already has -- never a duplicate entry in
    instrument_ids.
    """
    get_or_create_watchlist(db, owner_id)
    db.watchlists.update_one(
        {"user_id": owner_id},
        {
            "$addToSet": {"instrument_ids": instrument_id},
            "$set": {"updated_at": datetime.now(timezone.utc).isoformat()},
        },
    )


def add_instrument(
    db: Database, provider: MarketDataProvider, owner_id: str, instrument: Instrument
) -> tuple[dict, bool]:
    """
    Add an instrument to OWNER_ID's own watchlist membership.

    The global Instrument document is shared/reused exactly as before
    -- `created` still means "a new global Instrument document was
    created," unchanged in meaning from before ownership existed. What's
    new: a successful call also ensures instrument_id is present in
    THIS owner's own Watchlist.instrument_ids (via $addToSet, so calling
    this twice for the same owner+instrument is always safe). Never
    creates a duplicate global Instrument document in any case.

    Three cases, matching the product's explicit Duplicate Add rule:
    1. Instrument already exists globally AND is already in this
       owner's watchlist -- fully idempotent no-op as far as the global
       collection and provider are concerned (the $addToSet below is a
       no-op too); no provider call.
    2. Instrument already exists globally but is NOT yet in this
       owner's watchlist -- added to this owner's membership only, no
       provider call. An already-existing global instrument was already
       validated as resolvable when it was first created by whichever
       owner added it first; re-validating it for every subsequent
       owner would be redundant, not a genuine new check.
    3. Instrument does not exist globally at all -- validated via the
       provider (as before creating anything), created once, then added
       to this owner's membership.
    """
    existing = db.instruments.find_one(
        {"symbol": instrument.symbol, "exchange": instrument.exchange.value}
    )
    if existing is not None:
        _add_instrument_to_watchlist(db, owner_id, str(existing["_id"]))
        return existing, False

    ticker = yfinance_ticker_for(instrument.symbol, instrument.exchange.value)
    snapshots = MarketDataService(provider).fetch_snapshots({ticker: ticker})
    if not snapshots:
        raise ProviderResolutionError(instrument.symbol, instrument.exchange.value)

    doc = instrument.model_dump(mode="json")
    try:
        insert_result = db.instruments.insert_one(doc)
        doc["_id"] = insert_result.inserted_id
    except DuplicateKeyError:
        # Another owner's concurrent Add Stock for this exact
        # (symbol, exchange) pair won the race and already created the
        # global Instrument document -- the unique index
        # (uniq_symbol_exchange, Phase 1) is the real source of truth
        # here, same pattern as get_or_create_watchlist's own recovery.
        # This request still needs to add the now-existing instrument to
        # ITS OWN owner's membership rather than raising an unhandled
        # 500 and leaving this owner with no membership at all.
        existing = db.instruments.find_one(
            {"symbol": instrument.symbol, "exchange": instrument.exchange.value}
        )
        if existing is None:
            raise
        _add_instrument_to_watchlist(db, owner_id, str(existing["_id"]))
        return existing, False

    _add_instrument_to_watchlist(db, owner_id, str(doc["_id"]))
    return doc, True


def yfinance_ticker_for(symbol: str, exchange: str) -> str:
    """
    The one place that knows the yfinance ticker-suffix convention
    (confirmed in the live investigation: .NS for NSE, .BO for BSE).
    Lives here (not on the Instrument model) because it's specifically a
    yfinance-facing translation, not a general fact about the
    instrument -- a future non-yfinance provider might use a different
    convention entirely.
    """
    suffix = ".NS" if exchange == Exchange.NSE.value else ".BO"
    return f"{symbol}{suffix}"
