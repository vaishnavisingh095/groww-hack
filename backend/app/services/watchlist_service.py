"""
Watchlist Service.

For this first vertical slice: a single hardcoded watchlist containing
the 5 specified instruments, shared by a single demo user. Full
multi-user watchlist CRUD is still not implemented -- there is still no
real per-user Watchlist.instrument_ids membership and no multi-tenancy
(see decisions.md) -- but adding a new trackable instrument (Add Stock)
is now supported: add_instrument() below, plus
get_watchlist_instruments() so a newly-added instrument actually shows
up through the normal GET /watchlist path rather than only existing in
Mongo unseen.

This is still a deliberate, disclosed scope reduction from
architecture.md's full Watchlist Service (which also describes remove
and per-user lists) -- documented explicitly, not silently omitted.
"""
from datetime import datetime, timezone

from pymongo.database import Database

from app.models.instrument import Exchange, Instrument
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
        insert_result = db.instruments.insert_one(doc)
        doc["_id"] = insert_result.inserted_id
        result.append(doc)
    return result


def get_watchlist_instruments(db: Database) -> list[dict]:
    """
    Every instrument currently trackable by the demo user -- the 5
    original seeds plus anything added via add_instrument() below.

    Per the current single-demo-user architecture, "the watchlist" is
    simply every Instrument document that exists -- there is no real
    per-user Watchlist.instrument_ids membership in use (see
    decisions.md), and this deliberately does not introduce one now.
    ensure_seed_instruments still runs first so the 5 original seeds
    remain guaranteed present (unchanged, idempotent behavior) before
    the full collection is read; this is what fixes the previous gap
    where GET /watchlist only ever resolved the 5 hardcoded seeds by
    name and could never see a newly-added instrument.
    """
    ensure_seed_instruments(db)
    return list(db.instruments.find({}))


def add_instrument(
    db: Database, provider: MarketDataProvider, instrument: Instrument
) -> tuple[dict, bool]:
    """
    Add a new trackable instrument. Creates ONLY an Instrument document
    -- never a Checkpoint, never a ChangeEvent. A newly added instrument
    is baseline_pending on the very next GET /watchlist for exactly the
    same reason any seed instrument was before its first "mark as seen":
    CheckpointService.get_checkpoint returns None for it, so
    evaluate_change reports has_baseline=False and
    ChangeEventService.get_or_create_active is a no-op. No special
    casing for "newly added" is needed anywhere else in the app.

    Idempotent: an already-tracked (symbol, exchange) pair returns the
    existing document with created=False, the same
    find-before-insert pattern ensure_seed_instruments already uses --
    never a duplicate, never an error.

    Before creating anything, confirms the provider can actually produce
    a valid current MarketSnapshot for this (symbol, exchange) pair, via
    the exact same MarketDataService/YFinanceProvider path used
    everywhere else in the app (never a second, ad hoc "can I resolve
    this symbol" check). Raises ProviderResolutionError -- creating
    nothing -- if it cannot; the caller (the route) maps that to an
    HTTP error.
    """
    existing = db.instruments.find_one(
        {"symbol": instrument.symbol, "exchange": instrument.exchange.value}
    )
    if existing is not None:
        return existing, False

    ticker = yfinance_ticker_for(instrument.symbol, instrument.exchange.value)
    snapshots = MarketDataService(provider).fetch_snapshots({ticker: ticker})
    if not snapshots:
        raise ProviderResolutionError(instrument.symbol, instrument.exchange.value)

    doc = instrument.model_dump(mode="json")
    insert_result = db.instruments.insert_one(doc)
    doc["_id"] = insert_result.inserted_id
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
