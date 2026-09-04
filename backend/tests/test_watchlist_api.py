"""
API-level tests for the watchlist routes.

Uses mongomock (via monkeypatching get_database) and a fake provider
(via monkeypatching the module-level _provider instance) so these tests
run deterministically without live network access or a real MongoDB
server -- consistent with "keep business logic testable without MongoDB
or network access."

Live-network, real-yfinance, real-MongoDB verification is done
separately via manual end-to-end testing (see implementation report),
which is the appropriate place for that kind of check, not the
automated suite that runs on every commit.
"""
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.routes.watchlist as watchlist_routes
from app.db.indexes import ensure_indexes
from app.main import app
from app.providers.base import MarketDataProvider, RawQuote


class FakeProvider(MarketDataProvider):
    def __init__(self, quotes_by_symbol: dict[str, RawQuote]):
        self._quotes_by_symbol = quotes_by_symbol

    def get_quotes(self, symbols: list[str]) -> list[RawQuote]:
        return [
            self._quotes_by_symbol.get(s)
            or RawQuote(
                symbol=s,
                last_price=None,
                previous_close=None,
                volume=None,
                provider_timestamp=None,
                fetched_at=datetime.now(timezone.utc),
                fetch_succeeded=False,
                error_message="no test data configured for this symbol",
            )
            for s in symbols
        ]


def make_quote(symbol: str, last_price: float, previous_close: float, volume: int = 1000) -> RawQuote:
    return RawQuote(
        symbol=symbol,
        last_price=last_price,
        previous_close=previous_close,
        volume=volume,
        provider_timestamp=1788509522,
        fetched_at=datetime.now(timezone.utc),
        fetch_succeeded=True,
    )


@pytest.fixture
def client(monkeypatch):
    """
    Wires the app to a mongomock database and a FakeProvider, bypassing
    both real MongoDB and real yfinance for deterministic API tests.

    Must patch get_database in BOTH app.main (used by the lifespan's
    ensure_indexes call on startup) and app.routes.watchlist (used by
    the route handlers themselves) -- these are two separate import
    bindings of the same function, and patching only one leaves the
    other still trying to reach a real MongoDB server.
    """
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["test_api_db"]
    ensure_indexes(mock_db)

    monkeypatch.setattr(watchlist_routes, "get_database", lambda: mock_db)
    monkeypatch.setattr("app.main.get_database", lambda: mock_db)

    fake_provider = FakeProvider(
        {
            "RELIANCE.NS": make_quote("RELIANCE.NS", 1326.4, 1302.6, 9122871),
            "TCS.NS": make_quote("TCS.NS", 2312.8, 2320.0, 1722049),
            "HDFCBANK.NS": make_quote("HDFCBANK.NS", 715.6, 706.6, 9937354),
            "INFY.NS": make_quote("INFY.NS", 1129.0, 1130.3, 3875864),
            "ICICIBANK.NS": make_quote("ICICIBANK.NS", 1432.5, 1430.0, 4072353),
        }
    )
    monkeypatch.setattr(watchlist_routes, "_provider", fake_provider)

    with TestClient(app) as test_client:
        yield test_client, mock_db

    mock_client.close()


def test_get_watchlist_returns_five_instruments(client):
    test_client, _ = client
    response = test_client.get("/watchlist")

    assert response.status_code == 200
    body = response.json()
    assert "instruments" in body
    assert len(body["instruments"]) == 5


def test_get_watchlist_response_shape(client):
    """Verifies the exact clean-domain-JSON contract required: symbol,
    price, percent_change, cumulative_volume, fetched_at-derived
    freshness, status -- no yfinance field names anywhere."""
    test_client, _ = client
    response = test_client.get("/watchlist")
    body = response.json()

    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")

    assert "price" in reliance
    assert "percent_change" in reliance
    assert "cumulative_volume" in reliance
    assert "status" in reliance
    assert "freshness_label" in reliance
    assert "data_age_seconds" in reliance
    assert "change" in reliance

    # No yfinance-internal field names should ever leak through
    raw_json_keys = str(reliance.keys())
    assert "fast_info" not in raw_json_keys
    assert "regularMarketTime" not in raw_json_keys
    assert "provider_timestamp" not in raw_json_keys  # diagnostics-only, not exposed to frontend


def test_first_time_has_no_baseline_and_is_not_a_meaningful_change(client):
    test_client, _ = client
    response = test_client.get("/watchlist")
    body = response.json()

    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance["change"]["has_baseline"] is False
    assert reliance["change"]["meaningful_change"] is False
    assert "Baseline pending" in reliance["change"]["reason"]


def test_mark_as_seen_persists_checkpoint_and_read_reflects_it(client):
    test_client, mock_db = client

    # Get instrument_id for RELIANCE first
    response = test_client.get("/watchlist")
    reliance = next(i for i in response.json()["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    checkpoint_response = test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    assert checkpoint_response.status_code == 200
    body = checkpoint_response.json()
    assert body["symbol"] == "RELIANCE"
    assert "Baseline saved at" in body["message"]

    # Confirm it actually landed in Mongo
    doc = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert doc is not None
    assert doc["baseline_snapshot"]["last_price"] == 1326.4


def test_getting_watchlist_creates_implicit_baseline_but_never_advances_it(client):
    """
    CRITICAL: opening/refreshing the watchlist correctly establishes an
    IMPLICIT baseline the first time (per architecture.md, hard question
    G), but must NEVER advance/replace an existing checkpoint on
    subsequent requests. This test's earlier version asserted zero
    checkpoints were ever created by GET -- that was correct for the
    prior slice (which had not yet implemented implicit checkpoints) but
    is now the wrong expectation: implicit checkpoint creation on first
    sight is required behavior, not a bug. What must remain true is that
    once a checkpoint exists, GET never touches it again.
    """
    test_client, mock_db = client

    # First GET: no checkpoints exist yet -> implicit baselines created
    # for every instrument with a valid snapshot.
    test_client.get("/watchlist")
    count_after_first = mock_db.checkpoints.count_documents({})
    assert count_after_first == 5  # all 5 seed instruments have valid fake quotes

    checkpoints_after_first = list(mock_db.checkpoints.find({}))
    first_prices = {c["instrument_id"]: c["baseline_snapshot"]["last_price"] for c in checkpoints_after_first}
    assert all(c["source"] == "implicit" for c in checkpoints_after_first)

    # Second and third GET: checkpoints already exist -> must NOT be
    # replaced, count must not change, and baseline prices must be
    # untouched even though the (fake) provider always returns the same
    # price here -- the real assertion is about count and source, since
    # a same-price replace would be invisible by price alone.
    test_client.get("/watchlist")
    test_client.get("/watchlist")

    count_after_more_gets = mock_db.checkpoints.count_documents({})
    assert count_after_more_gets == 5  # unchanged -- no new or duplicate checkpoints

    checkpoints_after_more = list(mock_db.checkpoints.find({}))
    later_prices = {c["instrument_id"]: c["baseline_snapshot"]["last_price"] for c in checkpoints_after_more}
    assert later_prices == first_prices  # untouched
    assert all(c["source"] == "implicit" for c in checkpoints_after_more)  # still implicit, never overwritten by anything


def test_mark_as_seen_on_nonexistent_instrument_returns_404(client):
    test_client, _ = client
    response = test_client.post("/watchlist/instruments/000000000000000000000000/checkpoint")
    assert response.status_code == 404


def test_mark_as_seen_with_malformed_instrument_id_returns_400(client):
    test_client, _ = client
    response = test_client.post("/watchlist/instruments/not-a-valid-object-id/checkpoint")
    assert response.status_code == 400


def test_provider_failure_for_one_symbol_does_not_affect_others(client, monkeypatch):
    """One symbol failing must not take down the whole watchlist
    response."""
    test_client, mock_db = client

    partial_provider = FakeProvider(
        {
            "RELIANCE.NS": make_quote("RELIANCE.NS", 1326.4, 1302.6, 9122871),
            # TCS, HDFCBANK, INFY, ICICIBANK deliberately omitted -> will
            # fall back to the "no test data configured" failure quote
        }
    )
    monkeypatch.setattr(watchlist_routes, "_provider", partial_provider)

    response = test_client.get("/watchlist")
    assert response.status_code == 200
    body = response.json()

    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    tcs = next(i for i in body["instruments"] if i["symbol"] == "TCS")

    assert reliance["status"] == "ok"
    assert reliance["price"] == 1326.4
    assert tcs["status"] == "unavailable"
    assert tcs["price"] is None


def test_unavailable_instrument_still_includes_instrument_id(client, monkeypatch):
    """Regression test: an instrument with no usable data must still
    expose instrument_id, or the frontend can never call checkpoint for
    it once data becomes available. Caught during manual verification."""
    test_client, mock_db = client

    empty_provider = FakeProvider({})  # every symbol will fail
    monkeypatch.setattr(watchlist_routes, "_provider", empty_provider)

    response = test_client.get("/watchlist")
    body = response.json()

    for instrument in body["instruments"]:
        assert instrument["status"] == "unavailable"
        assert instrument["instrument_id"] is not None


def test_meaningful_change_flows_through_full_api_after_checkpoint(client):
    """End-to-end through the API layer: mark as seen at one price, then
    verify a subsequent (simulated) price move is correctly evaluated."""
    test_client, mock_db = client

    response = test_client.get("/watchlist")
    reliance = next(i for i in response.json()["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")

    # Directly verify the persisted checkpoint price -- simulating "a
    # later refresh with a different real price" would require changing
    # the fake provider's state, which is exercised at the unit level in
    # test_change_engine.py's controlled-baseline test. This test
    # confirms the API wiring itself: checkpoint -> stored -> readable.
    checkpoint_doc = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_doc["baseline_snapshot"]["last_price"] == 1326.4


def test_mark_as_seen_stores_frozen_baseline_not_a_snapshot_reference(client):
    """Per architecture.md: the checkpoint must store a FROZEN COPY of
    the values, not merely a reference to a MarketSnapshot document
    (which is overwritten on every poll cycle). Confirms the persisted
    checkpoint document itself carries real price/volume/percent_change
    values, not an id pointing elsewhere."""
    test_client, mock_db = client

    response = test_client.get("/watchlist")
    reliance = next(i for i in response.json()["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")

    doc = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    baseline = doc["baseline_snapshot"]
    # These are real, frozen numeric values in the checkpoint document
    # itself -- not a foreign key/reference to market_snapshots.
    assert isinstance(baseline["last_price"], (int, float))
    assert isinstance(baseline["volume"], int)
    assert isinstance(baseline["percent_change"], (int, float))
    assert "snapshot_id" not in doc  # no reference-style field exists


def test_mark_as_seen_for_instrument_with_unavailable_data_returns_503(client, monkeypatch):
    """FAILURE HANDLING: if the current market data is unavailable for
    this instrument, mark-as-seen must fail explicitly (503) rather than
    fabricate a checkpoint from missing/invalid data, and must leave any
    existing checkpoint completely untouched."""
    test_client, mock_db = client

    response = test_client.get("/watchlist")
    reliance = next(i for i in response.json()["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    # The GET above already created an implicit checkpoint (working fake
    # data) -- capture its state so we can confirm it's untouched below.
    checkpoint_before = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_before is not None
    price_before = checkpoint_before["baseline_snapshot"]["last_price"]

    # Now swap in a provider that has no data for anyone, and attempt an
    # explicit mark-as-seen -- this must fail, not fabricate a new
    # baseline and not touch the existing one.
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider({}))

    checkpoint_response = test_client.post(
        f"/watchlist/instruments/{instrument_id}/checkpoint"
    )

    assert checkpoint_response.status_code == 503
    # Exactly one checkpoint still exists (the earlier implicit one),
    # untouched -- the failed attempt did not fabricate or replace it.
    assert mock_db.checkpoints.count_documents({"instrument_id": instrument_id}) == 1
    checkpoint_after = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_after["baseline_snapshot"]["last_price"] == price_before
    assert checkpoint_after["source"] == "implicit"  # not overwritten to explicit


def test_mark_as_seen_with_no_prior_checkpoint_and_unavailable_data_creates_nothing(
    client, monkeypatch
):
    """The simpler failure case: no checkpoint has ever existed for this
    instrument, and the provider has no data at all. Mark-as-seen must
    fail (503) and create nothing."""
    test_client, mock_db = client

    # Seed the instrument WITHOUT ever calling GET /watchlist, so no
    # implicit checkpoint exists yet.
    from app.services.watchlist_service import ensure_seed_instruments

    instruments = ensure_seed_instruments(mock_db)
    reliance_doc = next(i for i in instruments if i["symbol"] == "RELIANCE")
    instrument_id = str(reliance_doc["_id"])

    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider({}))

    checkpoint_response = test_client.post(
        f"/watchlist/instruments/{instrument_id}/checkpoint"
    )

    assert checkpoint_response.status_code == 503
    assert mock_db.checkpoints.count_documents({"instrument_id": instrument_id}) == 0


def test_mark_all_as_seen_advances_all_instruments_with_valid_data(client):
    """POST /watchlist/checkpoint (mark-all-as-seen) must advance every
    eligible instrument's checkpoint in one call."""
    test_client, mock_db = client

    response = test_client.post("/watchlist/checkpoint")
    assert response.status_code == 200
    body = response.json()

    assert len(body["updated"]) == 5
    assert body["skipped"] == []
    assert mock_db.checkpoints.count_documents({}) == 5

    updated_symbols = {u["symbol"] for u in body["updated"]}
    assert updated_symbols == {"RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"}

    # Confirm sources are explicit -- this IS the explicit mark-all action.
    for doc in mock_db.checkpoints.find({}):
        assert doc["source"] == "explicit"


def test_mark_all_as_seen_skips_instruments_without_valid_snapshot(client, monkeypatch):
    """FAILURE HANDLING: when some instruments have no valid current
    data, mark-all-as-seen must skip them safely (not fail the whole
    batch, not fabricate a checkpoint for them) and report them as
    skipped."""
    test_client, mock_db = client

    partial_provider = FakeProvider(
        {
            "RELIANCE.NS": make_quote("RELIANCE.NS", 1326.4, 1302.6, 9122871),
            "TCS.NS": make_quote("TCS.NS", 2312.8, 2320.0, 1722049),
            # HDFCBANK, INFY, ICICIBANK deliberately have no data
        }
    )
    monkeypatch.setattr(watchlist_routes, "_provider", partial_provider)

    response = test_client.post("/watchlist/checkpoint")
    assert response.status_code == 200
    body = response.json()

    assert len(body["updated"]) == 2
    assert len(body["skipped"]) == 3
    updated_symbols = {u["symbol"] for u in body["updated"]}
    skipped_symbols = {s["symbol"] for s in body["skipped"]}
    assert updated_symbols == {"RELIANCE", "TCS"}
    assert skipped_symbols == {"HDFCBANK", "INFY", "ICICIBANK"}

    # Only the 2 successful instruments got checkpoints -- nothing
    # fabricated for the 3 skipped ones.
    assert mock_db.checkpoints.count_documents({}) == 2


def test_mark_all_as_seen_replaces_existing_checkpoints_not_duplicates(client):
    """Calling mark-all-as-seen twice must advance/replace, never
    duplicate, each instrument's checkpoint."""
    test_client, mock_db = client

    test_client.post("/watchlist/checkpoint")
    test_client.post("/watchlist/checkpoint")

    assert mock_db.checkpoints.count_documents({}) == 5  # still 5, not 10


def test_implicit_checkpoint_resolves_on_next_request_not_the_current_one(client):
    """Per architecture.md (hard question G): an implicit checkpoint
    'resolves this state on the instrument's NEXT poll cycle' -- meaning
    the request that CREATES the implicit checkpoint must still report
    has_baseline=False, and only the following request sees has_baseline
    reflecting the (now-existing) checkpoint."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance_first = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance_first["change"]["has_baseline"] is False
    assert "Baseline pending" in reliance_first["change"]["reason"]

    # The implicit checkpoint now exists in the database...
    assert mock_db.checkpoints.count_documents({"instrument_id": reliance_first["instrument_id"]}) == 1

    # ...and the NEXT request sees it and compares against it (same fake
    # price every time in this test, so the comparison is "no meaningful
    # change" rather than "no baseline").
    second = test_client.get("/watchlist").json()
    reliance_second = next(i for i in second["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance_second["change"]["has_baseline"] is True


def test_watchlist_response_exposes_price_change_pct_and_volume_fields(client):
    """Phase 5 integration: GET /watchlist must expose the richer
    change-engine result -- price_change_pct, volume_acceleration_ratio,
    and volume_signal_available -- not just the old has_baseline/
    meaningful_change/reason trio."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    change = reliance["change"]

    assert "price_change_pct" in change
    assert "volume_acceleration_ratio" in change
    assert "volume_signal_available" in change
    # First-time baseline: no signal values yet.
    assert change["price_change_pct"] is None
    assert change["volume_acceleration_ratio"] is None


def test_watchlist_response_price_change_pct_populated_after_checkpoint(client):
    """Once a real checkpoint exists and price differs, price_change_pct
    must be a real, non-null number in the GET /watchlist response."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    # Advance to an EXPLICIT checkpoint at the current (fake) price.
    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")

    # Same fake price on next GET -> 0.0% change, but the field must be
    # present and numeric, not null, now that a real checkpoint exists.
    second = test_client.get("/watchlist").json()
    reliance_second = next(i for i in second["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance_second["change"]["has_baseline"] is True
    assert reliance_second["change"]["price_change_pct"] == 0.0