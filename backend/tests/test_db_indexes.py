"""
Tests for index creation and the uniqueness constraints they enforce.

See conftest.py's mock_db fixture docstring for why mongomock is used
here instead of a real MongoDB server.
"""
import pymongo
import pytest


def test_ensure_indexes_creates_all_expected_indexes(mock_db):
    """Verifies ensure_indexes() runs without error against a fresh
    database and creates the named indexes we depend on elsewhere."""
    index_names_by_collection = {
        "instruments": set(mock_db.instruments.index_information().keys()),
        "watchlists": set(mock_db.watchlists.index_information().keys()),
        "market_snapshots": set(mock_db.market_snapshots.index_information().keys()),
        "checkpoints": set(mock_db.checkpoints.index_information().keys()),
        "change_events": set(mock_db.change_events.index_information().keys()),
    }

    assert "uniq_symbol_exchange" in index_names_by_collection["instruments"]
    assert "uniq_user_id" in index_names_by_collection["watchlists"]
    assert "uniq_instrument_id" in index_names_by_collection["market_snapshots"]
    assert "uniq_user_instrument_checkpoint" in index_names_by_collection["checkpoints"]
    assert "user_acknowledged_lookup" in index_names_by_collection["change_events"]


def test_ensure_indexes_is_idempotent(mock_db):
    """Running index creation twice must not raise -- this is what makes
    it safe to call on every app startup (see main.py's lifespan)."""
    from app.db.indexes import ensure_indexes

    ensure_indexes(mock_db)  # second call, same db
    # If this didn't raise, idempotency holds.


def test_duplicate_symbol_exchange_pair_is_rejected(mock_db):
    """Directly exercises the uniqueness constraint the index exists to
    enforce -- inserting the same (symbol, exchange) twice must fail."""
    mock_db.instruments.insert_one({"symbol": "RELIANCE", "exchange": "NSE"})
    with pytest.raises(pymongo.errors.DuplicateKeyError):
        mock_db.instruments.insert_one({"symbol": "RELIANCE", "exchange": "NSE"})


def test_same_symbol_different_exchange_is_allowed(mock_db):
    """RELIANCE-on-NSE and RELIANCE-on-BSE are legitimately different
    Instrument documents per architecture.md -- this must NOT collide."""
    mock_db.instruments.insert_one({"symbol": "RELIANCE", "exchange": "NSE"})
    mock_db.instruments.insert_one({"symbol": "RELIANCE", "exchange": "BSE"})
    assert mock_db.instruments.count_documents({}) == 2


def test_duplicate_watchlist_for_same_user_is_rejected(mock_db):
    """Enforces plan.md's 'one watchlist per user' rule at the DB level."""
    mock_db.watchlists.insert_one({"user_id": "user123", "instrument_ids": []})
    with pytest.raises(pymongo.errors.DuplicateKeyError):
        mock_db.watchlists.insert_one({"user_id": "user123", "instrument_ids": ["x"]})


def test_duplicate_market_snapshot_for_same_instrument_is_rejected(mock_db):
    """Enforces the upsert-only design: a second insert for the same
    instrument_id must fail so that updates are forced to go through an
    upsert/replace, never an accidental duplicate insert."""
    mock_db.market_snapshots.insert_one({"instrument_id": "inst123", "last_price": 100})
    with pytest.raises(pymongo.errors.DuplicateKeyError):
        mock_db.market_snapshots.insert_one({"instrument_id": "inst123", "last_price": 105})


def test_market_snapshot_upsert_by_instrument_id_works(mock_db):
    """The intended real-world write pattern: update_one with upsert=True
    keyed on instrument_id, which the unique index makes safe."""
    mock_db.market_snapshots.update_one(
        {"instrument_id": "inst123"},
        {"$set": {"last_price": 100}},
        upsert=True,
    )
    mock_db.market_snapshots.update_one(
        {"instrument_id": "inst123"},
        {"$set": {"last_price": 105}},
        upsert=True,
    )
    assert mock_db.market_snapshots.count_documents({"instrument_id": "inst123"}) == 1
    doc = mock_db.market_snapshots.find_one({"instrument_id": "inst123"})
    assert doc["last_price"] == 105


def test_duplicate_checkpoint_for_same_user_instrument_pair_is_rejected(mock_db):
    mock_db.checkpoints.insert_one({"user_id": "u1", "instrument_id": "i1"})
    with pytest.raises(pymongo.errors.DuplicateKeyError):
        mock_db.checkpoints.insert_one({"user_id": "u1", "instrument_id": "i1"})


def test_checkpoint_for_different_instrument_same_user_is_allowed(mock_db):
    mock_db.checkpoints.insert_one({"user_id": "u1", "instrument_id": "i1"})
    mock_db.checkpoints.insert_one({"user_id": "u1", "instrument_id": "i2"})
    assert mock_db.checkpoints.count_documents({"user_id": "u1"}) == 2
