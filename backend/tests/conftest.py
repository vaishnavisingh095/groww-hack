"""
Shared test fixtures.

Uses mongomock instead of a real MongoDB server. This is a deliberate,
disclosed choice: no MongoDB server binary is available in this
environment to run integration tests against a real instance. mongomock
implements pymongo's actual API surface (including index creation and
uniqueness enforcement), so it genuinely exercises our index
definitions, unique-constraint behavior, and query shapes -- it does not
fake success. What it does NOT verify is real MongoDB-server-specific
behavior (replication, real network failures, exact server-version
quirks) -- that gap should be closed with a real MongoDB instance before
this is trusted for anything beyond the hackathon, and is called out
explicitly in the implementation report rather than left implicit.
"""
import mongomock
import pytest

from app.db.indexes import ensure_indexes


@pytest.fixture
def mock_db():
    client = mongomock.MongoClient()
    db = client["test_groww_watchlist"]
    ensure_indexes(db)
    yield db
    client.close()
