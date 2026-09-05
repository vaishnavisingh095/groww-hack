"""
Unit tests for app/services/identity.py's pure token-generation logic.
End-to-end cookie-issuing/resolution behavior (via the real FastAPI
dependency, a real Request/Response, and real routes) is covered in
test_watchlist_api.py instead, since that needs the full app + TestClient
machinery already set up there.
"""
from app.services.identity import OWNER_COOKIE_NAME, generate_owner_id, resolve_owner_id


class _FakeRequest:
    """Minimal stand-in exposing only what resolve_owner_id reads
    (.cookies) -- avoids constructing a real Starlette Request just to
    exercise this function's own logic in isolation."""

    def __init__(self, cookies: dict):
        self.cookies = cookies


class _FakeResponse:
    """Minimal stand-in recording set_cookie calls, so a test can
    assert whether a NEW cookie was (or wasn't) issued."""

    def __init__(self):
        self.set_cookie_calls = []

    def set_cookie(self, **kwargs):
        self.set_cookie_calls.append(kwargs)


def test_resolve_owner_id_trusts_any_nonempty_cookie_value_as_is():
    """resolve_owner_id imposes no length limit and no character
    allow-list -- values a real HTTP Cookie header cannot carry
    unencoded (raw whitespace, raw non-ASCII) are still exercised here
    directly against the function's own logic, since a real client
    would percent-encode them first and this function would see the
    already-decoded string either way. Each must be trusted as-is and
    must never trigger issuing a fresh cookie (that only happens when
    no cookie value is present at all)."""
    for value in ["   ", "x" * 10_000, "token-with-unicode-éü-and-spaces here"]:
        request = _FakeRequest({OWNER_COOKIE_NAME: value})
        response = _FakeResponse()

        result = resolve_owner_id(request, response)

        assert result == value
        assert response.set_cookie_calls == []


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
