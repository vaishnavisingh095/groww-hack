"""
Watchlist Service.

For this first vertical slice: a single hardcoded watchlist containing
the 5 specified instruments, shared by a single demo user. Full
multi-user watchlist CRUD (add/remove instrument via API) is explicitly
NOT part of this slice's stop condition -- the stop condition is the
OPEN -> SEE DATA -> MARK AS SEEN -> REFRESH -> SEE CHANGE loop, which
does not require add/remove endpoints to demonstrate end-to-end.

This is a deliberate, disclosed scope reduction from architecture.md's
full Watchlist Service (which does support add/remove) -- documented
explicitly in the implementation report rather than silently omitted.
"""
from datetime import datetime, timezone

from pymongo.database import Database

from app.models.instrument import Exchange, Instrument

DEMO_USER_ID = "demo-user"

SEED_INSTRUMENTS = [
    ("RELIANCE", Exchange.NSE),
    ("TCS", Exchange.NSE),
    ("HDFCBANK", Exchange.NSE),
    ("INFY", Exchange.NSE),
    ("ICICIBANK", Exchange.NSE),
]


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
