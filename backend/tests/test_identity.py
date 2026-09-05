"""
Unit tests for app/services/identity.py's pure token-generation logic.
End-to-end cookie-issuing/resolution behavior (via the real FastAPI
dependency, a real Request/Response, and real routes) is covered in
test_watchlist_api.py instead, since that needs the full app + TestClient
machinery already set up there.
"""
from app.services.identity import generate_owner_id


def test_generate_owner_id_returns_a_non_empty_string():
    owner_id = generate_owner_id()
    assert isinstance(owner_id, str)
    assert len(owner_id) > 0


def test_generate_owner_id_is_high_entropy_not_sequential_or_timestamp_based():
    """
    32 bytes (256 bits) url-safe-base64-encoded is expected to produce a
    string comfortably longer than any timestamp or small sequential
    counter could ever be -- a cheap, real signal (not a proof) that
    this isn't a predictable id scheme.
    """
    owner_id = generate_owner_id()
    assert len(owner_id) >= 40


def test_generate_owner_id_produces_distinct_values_each_call():
    ids = {generate_owner_id() for _ in range(200)}
    assert len(ids) == 200
