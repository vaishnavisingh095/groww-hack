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
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.routes.watchlist as watchlist_routes
from app.db.indexes import ensure_indexes
from app.main import app
from app.providers.base import MarketDataProvider, RawQuote
from app.services.change_engine import ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT


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
    now = datetime.now(timezone.utc)
    return RawQuote(
        symbol=symbol,
        last_price=last_price,
        previous_close=previous_close,
        volume=volume,
        provider_timestamp=1788509522,
        fetched_at=now,
        fetch_succeeded=True,
        # A real, provider-derived session_date is required by
        # MarketDataService as of the session_date correctness fix (see
        # decisions.md) -- these API-level tests aren't exercising the
        # "bars are from a different day than our fetch time" scenario
        # (that's covered at the provider/service level), so this
        # simply matches fetched_at's own date.
        session_date=now.date(),
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


def test_get_watchlist_never_creates_a_checkpoint(client):
    """
    CRITICAL (checkpoint semantics contract): opening/rendering/
    refreshing the watchlist is NEVER acknowledgement. GET /watchlist
    must not write a Checkpoint document -- not on the first request,
    and not on any repeated request -- regardless of how many times an
    instrument with valid data is viewed. Only an explicit "mark as
    seen" action may create a checkpoint (see
    test_mark_as_seen_persists_checkpoint_and_read_reflects_it).
    """
    test_client, mock_db = client

    test_client.get("/watchlist")
    assert mock_db.checkpoints.count_documents({}) == 0

    # Repeated GETs (edge case B/D): still zero, no implicit
    # establishment on any subsequent read either.
    test_client.get("/watchlist")
    test_client.get("/watchlist")
    assert mock_db.checkpoints.count_documents({}) == 0


def test_mark_as_seen_on_nonexistent_instrument_returns_404(client):
    test_client, _ = client
    response = test_client.post("/watchlist/instruments/000000000000000000000000/checkpoint")
    assert response.status_code == 404


def test_mark_as_seen_with_malformed_instrument_id_returns_400(client):
    test_client, _ = client
    response = test_client.post("/watchlist/instruments/not-a-valid-object-id/checkpoint")
    assert response.status_code == 400


@pytest.mark.parametrize(
    "bad_id",
    [
        "x" * 5000,  # extremely long
        "!!!not-hex!!!",  # unexpected characters
        "507f1f77bcf86cd799439011extra",  # right-length-prefix but invalid
    ],
    ids=["extremely_long", "unexpected_characters", "invalid_with_valid_prefix"],
)
def test_mark_as_seen_with_various_malformed_instrument_ids_returns_400_not_500(client, bad_id):
    """Step 18: every malformed-ObjectId shape must be caught by
    _to_object_id's InvalidId handler and rejected with a safe 400 --
    never an unhandled 500, and never a MongoDB internal error leaked
    to the response body. (A trailing-slash-unsafe id like an empty
    string is exercised at the URL-routing level separately.)"""
    test_client, mock_db = client

    response = test_client.post(f"/watchlist/instruments/{bad_id}/checkpoint")

    assert response.status_code == 400
    assert "detail" in response.json()
    assert mock_db.checkpoints.count_documents({}) == 0


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


def test_malformed_zero_previous_close_for_one_symbol_does_not_500_or_affect_others(
    client, monkeypatch
):
    """REGRESSION: a provider quote with previous_close=0.0 (or any
    non-positive/non-finite price) used to raise an unhandled
    ZeroDivisionError/pydantic.ValidationError inside
    MarketDataService.fetch_snapshots, which had no per-quote
    try/except -- crashing the ENTIRE GET /watchlist call for every
    instrument in the batch, not just the malformed one. This proves,
    through the full API/DB path, that such a quote instead degrades
    to "unavailable" for just that one instrument while a healthy
    sibling is returned correctly."""
    test_client, mock_db = client

    malformed_provider = FakeProvider(
        {
            "RELIANCE.NS": make_quote("RELIANCE.NS", 1326.4, 0.0, 9122871),
            "TCS.NS": make_quote("TCS.NS", 2312.8, 2320.0, 1722049),
        }
    )
    monkeypatch.setattr(watchlist_routes, "_provider", malformed_provider)

    response = test_client.get("/watchlist")
    assert response.status_code == 200
    body = response.json()

    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    tcs = next(i for i in body["instruments"] if i["symbol"] == "TCS")

    assert reliance["status"] == "unavailable"
    assert reliance["price"] is None
    assert tcs["status"] == "ok"
    assert tcs["price"] == 2312.8


def test_malformed_quote_after_checkpoint_exists_does_not_corrupt_or_advance_it(
    client, monkeypatch
):
    """REGRESSION + edge case E: once a real checkpoint exists for an
    instrument, a later provider response with a poisonous price
    (previous_close=0.0, fetch_succeeded=True) must not crash, must not
    advance/replace the checkpoint, and must not corrupt the frozen
    baseline.

    Since the last-known-good fallback feature, the instrument no
    longer degrades all the way to "unavailable" here -- the first GET
    in this test (with healthy data) persisted a valid snapshot, so the
    malformed cycle now falls back to that snapshot as STALE instead.
    What must still hold, unchanged, is the checkpoint itself: it is
    never advanced/replaced/corrupted by this malformed cycle, and no
    ChangeEvent is created from the stale fallback."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    checkpoint_before = mock_db.checkpoints.find_one({"instrument_id": instrument_id})

    malformed_provider = FakeProvider(
        {
            "RELIANCE.NS": make_quote("RELIANCE.NS", 1326.4, 0.0, 9122871),
            "TCS.NS": make_quote("TCS.NS", 2312.8, 2320.0, 1722049),
        }
    )
    monkeypatch.setattr(watchlist_routes, "_provider", malformed_provider)

    response = test_client.get("/watchlist")
    assert response.status_code == 200
    body = response.json()

    reliance_after = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    # Falls back to the last-known-good snapshot (persisted by the
    # first GET above), reported as stale -- not fabricated, not
    # unavailable, and not silently treated as fresh.
    assert reliance_after["status"] == "stale"
    assert reliance_after["price"] == 1326.4
    assert reliance_after["change"]["meaningful_change"] is False

    checkpoint_after = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_after["checkpoint_at"] == checkpoint_before["checkpoint_at"]
    assert (
        checkpoint_after["baseline_snapshot"]["last_price"]
        == checkpoint_before["baseline_snapshot"]["last_price"]
    )
    assert mock_db.checkpoints.count_documents({}) == 1
    # A stale fallback must never create a ChangeEvent, even when a
    # checkpoint exists to compare against.
    assert mock_db.change_events.count_documents({}) == 0


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
    """FAILURE HANDLING (edge case G): if the current market data is
    unavailable for this instrument, mark-as-seen must fail explicitly
    (503) rather than fabricate a checkpoint from missing/invalid data,
    and must leave an existing EXPLICIT checkpoint completely
    untouched -- it remains frozen until a successful explicit
    acknowledgement."""
    test_client, mock_db = client

    response = test_client.get("/watchlist")
    reliance = next(i for i in response.json()["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    # Establish an explicit checkpoint first (GET no longer creates one
    # implicitly) -- capture its state so we can confirm it's untouched
    # below.
    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
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
    # Exactly one checkpoint still exists (the earlier explicit one),
    # untouched -- the failed attempt did not fabricate or replace it.
    assert mock_db.checkpoints.count_documents({"instrument_id": instrument_id}) == 1
    checkpoint_after = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_after["baseline_snapshot"]["last_price"] == price_before
    assert checkpoint_after["source"] == "explicit"  # not touched


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


# --- Last-known-good market data fallback (stale, display-only) -------


def test_get_watchlist_stale_fallback_shows_truthful_freshness(client, monkeypatch):
    """End-to-end: a successful GET persists a snapshot; a later GET
    with a total provider outage falls back to it, truthfully reporting
    status=stale and a freshness label/age reflecting the ORIGINAL
    fetch, not a fabricated "now"."""
    test_client, mock_db = client

    test_client.get("/watchlist")  # persists all five instruments

    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider({}))
    response = test_client.get("/watchlist")
    assert response.status_code == 200
    body = response.json()

    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance["status"] == "stale"
    assert reliance["price"] == 1326.4
    assert reliance["data_age_seconds"] is not None
    assert reliance["data_age_seconds"] >= 0
    assert "ago" in reliance["freshness_label"]


def test_mark_all_as_seen_skips_stale_fallback_and_advances_nothing(client, monkeypatch):
    """(e) A last-known-good STALE fallback must not be usable as a
    mark-all-as-seen baseline either -- every instrument falls back to
    stale (persisted by the earlier GET) and must all be skipped, with
    zero checkpoints created."""
    test_client, mock_db = client

    test_client.get("/watchlist")  # persists all five instruments

    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider({}))
    response = test_client.post("/watchlist/checkpoint")

    assert response.status_code == 200
    body = response.json()
    assert body["updated"] == []
    assert len(body["skipped"]) == 5
    assert mock_db.checkpoints.count_documents({}) == 0


def test_mark_as_seen_rejects_stale_fallback_snapshot(client, monkeypatch):
    """(e) Single-instrument mark-as-seen must likewise reject a STALE
    fallback snapshot -- it is display-only and must never become a new
    checkpoint baseline, with or without a prior explicit checkpoint."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()  # persists all five instruments
    instrument_id = next(
        i for i in first["instruments"] if i["symbol"] == "RELIANCE"
    )["instrument_id"]

    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider({}))
    response = test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")

    assert response.status_code == 503
    assert mock_db.checkpoints.count_documents({"instrument_id": instrument_id}) == 0


def test_mark_all_as_seen_replaces_existing_checkpoints_not_duplicates(client):
    """Calling mark-all-as-seen twice must advance/replace, never
    duplicate, each instrument's checkpoint."""
    test_client, mock_db = client

    test_client.post("/watchlist/checkpoint")
    test_client.post("/watchlist/checkpoint")

    assert mock_db.checkpoints.count_documents({}) == 5  # still 5, not 10


def test_repeated_get_with_no_checkpoint_always_reports_baseline_pending(client):
    """Edge cases A & B: with no explicit checkpoint ever established,
    EVERY GET /watchlist request -- first or repeated -- must report
    baseline_pending (has_baseline=False), never silently resolving to
    a baseline on a later request the way the old implicit-checkpoint
    mechanism did."""
    test_client, mock_db = client

    for _ in range(3):
        body = test_client.get("/watchlist").json()
        reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
        assert reliance["change"]["has_baseline"] is False
        assert "Baseline pending" in reliance["change"]["reason"]

    assert mock_db.checkpoints.count_documents({}) == 0


def test_get_with_existing_explicit_checkpoint_compares_without_modifying_it(client):
    """Edge cases C, D & G: once an EXPLICIT checkpoint exists (created
    only via the mark-as-seen endpoint), GET /watchlist may evaluate
    meaningful change against it, but must never modify checkpoint_at or
    the frozen baseline values -- including across repeated GETs."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    checkpoint_before = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_before["source"] == "explicit"

    for _ in range(3):
        body = test_client.get("/watchlist").json()
        reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
        assert reliance["change"]["has_baseline"] is True
        # Fake provider returns the same price every call -> no
        # meaningful change, but a real (zero) comparison, not "pending".
        assert reliance["change"]["price_change_pct"] == 0.0

    checkpoint_after = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_after["checkpoint_at"] == checkpoint_before["checkpoint_at"]
    assert (
        checkpoint_after["baseline_snapshot"]["last_price"]
        == checkpoint_before["baseline_snapshot"]["last_price"]
    )
    assert mock_db.checkpoints.count_documents({}) == 1


def test_get_with_unavailable_data_does_not_create_or_advance_checkpoint(client, monkeypatch):
    """Edge case E: stale/invalid/unavailable market data must never
    create or advance checkpoint state, whether or not a checkpoint
    already exists."""
    test_client, mock_db = client

    empty_provider = FakeProvider({})  # every symbol fails
    monkeypatch.setattr(watchlist_routes, "_provider", empty_provider)

    test_client.get("/watchlist")
    test_client.get("/watchlist")

    assert mock_db.checkpoints.count_documents({}) == 0


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


# --- ChangeEvent persistence/lifecycle (API-level wiring) ------------------


def _quotes_with_reliance_price(price: float) -> dict:
    """All 5 default fake quotes, with RELIANCE's price overridden --
    used to simulate a price move between a checkpoint and a later GET."""
    return {
        "RELIANCE.NS": make_quote("RELIANCE.NS", price, 1302.6, 9122871),
        "TCS.NS": make_quote("TCS.NS", 2312.8, 2320.0, 1722049),
        "HDFCBANK.NS": make_quote("HDFCBANK.NS", 715.6, 706.6, 9937354),
        "INFY.NS": make_quote("INFY.NS", 1129.0, 1130.3, 3875864),
        "ICICIBANK.NS": make_quote("ICICIBANK.NS", 1432.5, 1430.0, 4072353),
    }


def test_get_watchlist_creates_change_event_for_meaningful_change_against_explicit_checkpoint(
    client, monkeypatch
):
    """Edge case B (API level): explicit checkpoint + a real, meaningful
    price move on a status-OK snapshot must persist exactly one
    ChangeEvent, tied to the checkpoint's id."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    checkpoint = mock_db.checkpoints.find_one({"instrument_id": instrument_id})

    # Simulate a >2% price move since the checkpoint.
    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )

    body = test_client.get("/watchlist").json()
    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance["change"]["meaningful_change"] is True

    assert mock_db.change_events.count_documents({}) == 1
    event = mock_db.change_events.find_one({"instrument_id": instrument_id})
    assert event["checkpoint_id"] == checkpoint["id"]
    assert event["acknowledged"] is False


def test_repeated_get_with_same_meaningful_change_does_not_duplicate_change_event(
    client, monkeypatch
):
    """Edge case C (API level): repeated GET/refresh while the same
    checkpoint stays active and the same meaningful change persists must
    not create additional ChangeEvent documents."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )

    test_client.get("/watchlist")
    test_client.get("/watchlist")
    test_client.get("/watchlist")

    assert mock_db.change_events.count_documents({"instrument_id": instrument_id}) == 1


def test_mark_as_seen_acknowledges_active_change_event(client, monkeypatch):
    """Edge case E: explicitly marking an instrument as seen must
    acknowledge its currently active ChangeEvent(s), tied to the
    checkpoint version being superseded."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    old_checkpoint = mock_db.checkpoints.find_one({"instrument_id": instrument_id})

    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )
    test_client.get("/watchlist")  # creates the active ChangeEvent

    active_event = mock_db.change_events.find_one({"instrument_id": instrument_id})
    assert active_event["acknowledged"] is False
    assert active_event["checkpoint_id"] == old_checkpoint["id"]

    # Explicit re-acknowledgement at the new (now-current) price.
    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")

    acknowledged_event = mock_db.change_events.find_one({"_id": active_event["_id"]})
    assert acknowledged_event["acknowledged"] is True
    # Still tied to the OLD checkpoint version -- history is not rewritten.
    assert acknowledged_event["checkpoint_id"] == old_checkpoint["id"]

    new_checkpoint = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert new_checkpoint["id"] != old_checkpoint["id"]


def test_mark_all_as_seen_acknowledges_only_successful_instruments_active_events(
    client, monkeypatch
):
    """Edge case K: mark-all-as-seen must acknowledge active ChangeEvents
    only for instruments whose checkpoint actually advanced this call --
    an instrument skipped for lack of valid data must keep its active
    event untouched."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    tcs = next(i for i in first["instruments"] if i["symbol"] == "TCS")
    reliance_id = reliance["instrument_id"]
    tcs_id = tcs["instrument_id"]

    # Give both RELIANCE and TCS an explicit checkpoint, then a
    # meaningful price move each, so both have an active ChangeEvent
    # before the mark-all call.
    test_client.post(f"/watchlist/instruments/{reliance_id}/checkpoint")
    test_client.post(f"/watchlist/instruments/{tcs_id}/checkpoint")

    monkeypatch.setattr(
        watchlist_routes,
        "_provider",
        FakeProvider(
            {
                **_quotes_with_reliance_price(1400.0),
                "TCS.NS": make_quote("TCS.NS", 2500.0, 2320.0, 1722049),  # >2% move
            }
        ),
    )
    test_client.get("/watchlist")  # creates both active ChangeEvents

    assert mock_db.change_events.count_documents({"acknowledged": False}) == 2

    # Now mark-all, but with TCS's data unavailable this cycle -- TCS
    # must be skipped, not advanced, and its active event left alone.
    partial_provider = FakeProvider(
        {
            "RELIANCE.NS": make_quote("RELIANCE.NS", 1400.0, 1302.6, 9122871),
            # TCS deliberately omitted -> unavailable this cycle
            "HDFCBANK.NS": make_quote("HDFCBANK.NS", 715.6, 706.6, 9937354),
            "INFY.NS": make_quote("INFY.NS", 1129.0, 1130.3, 3875864),
            "ICICIBANK.NS": make_quote("ICICIBANK.NS", 1432.5, 1430.0, 4072353),
        }
    )
    monkeypatch.setattr(watchlist_routes, "_provider", partial_provider)

    response = test_client.post("/watchlist/checkpoint")
    body = response.json()
    assert {u["instrument_id"] for u in body["updated"]} >= {reliance_id}
    assert any(s["instrument_id"] == tcs_id for s in body["skipped"])

    reliance_event = mock_db.change_events.find_one({"instrument_id": reliance_id})
    tcs_event = mock_db.change_events.find_one({"instrument_id": tcs_id})
    assert reliance_event["acknowledged"] is True  # advanced -> acknowledged
    assert tcs_event["acknowledged"] is False  # skipped -> untouched


def test_get_watchlist_with_stale_snapshot_creates_no_change_event(client, monkeypatch):
    """Edge case I (API level): a snapshot old enough to be classified
    stale must never create a ChangeEvent, even if the price move looks
    meaningful -- staleness is still safe to DISPLAY, just never
    eligible for change detection persistence."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")

    stale_quotes = _quotes_with_reliance_price(1400.0)
    stale_quotes["RELIANCE.NS"] = RawQuote(
        symbol="RELIANCE.NS",
        last_price=1400.0,
        previous_close=1302.6,
        volume=9122871,
        provider_timestamp=1788509522,
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=300),
        fetch_succeeded=True,
        session_date=datetime.now(timezone.utc).date(),
    )
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(stale_quotes))

    body = test_client.get("/watchlist").json()
    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance["status"] == "stale"  # still displayed, correctly labeled
    assert reliance["price"] == 1400.0  # still shown

    assert mock_db.change_events.count_documents({}) == 0


def test_get_watchlist_with_unavailable_snapshot_creates_no_change_event(client, monkeypatch):
    """Edge case J (API level): no snapshot at all must never create a
    ChangeEvent, regardless of any prior checkpoint."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")

    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider({}))
    test_client.get("/watchlist")

    assert mock_db.change_events.count_documents({}) == 0


# --- GET /watchlist/attention (Attention Engine API integration) -----------


def test_get_attention_returns_empty_list_when_no_active_events(client):
    """Empty state must be a normal 200, not an error."""
    test_client, _ = client

    response = test_client.get("/watchlist/attention")

    assert response.status_code == 200
    assert response.json() == {"attention_items": []}


def test_get_attention_includes_item_for_meaningful_change_with_correct_fields(
    client, monkeypatch
):
    """Covers: meaningful change appears, correct symbol, correct score,
    correct level, correct explanation, null volume_acceleration_ratio
    when unavailable, ISO-format detected_at, lowercase attention_level
    string, end-to-end symbol resolution."""
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")  # baseline @ 1326.4, volume 9122871
    checkpoint = mock_db.checkpoints.find_one({"instrument_id": instrument_id})

    # Volume LOWER than the checkpoint's baseline deterministically trips
    # change_engine.py's non-monotonic/bad-data guard, marking the volume
    # signal unavailable regardless of real wall-clock time (unlike a
    # cross-session gap, which would depend on when the suite happens to
    # run relative to market open/midnight IST).
    quotes = _quotes_with_reliance_price(1400.0)
    quotes["RELIANCE.NS"] = make_quote("RELIANCE.NS", 1400.0, 1302.6, 9000000)
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(quotes))
    test_client.get("/watchlist")  # creates the active ChangeEvent

    response = test_client.get("/watchlist/attention")
    assert response.status_code == 200
    items = response.json()["attention_items"]
    assert len(items) == 1
    item = items[0]

    assert item["instrument_id"] == instrument_id
    assert item["symbol"] == "RELIANCE"
    assert item["checkpoint_id"] == checkpoint["id"]

    # ISO-format string, not a bare/epoch timestamp -- must round-trip.
    assert isinstance(item["detected_at"], str)
    datetime.fromisoformat(item["detected_at"])

    expected_pct = (1400.0 - 1326.4) / 1326.4 * 100
    assert item["price_change_pct"] == pytest.approx(expected_pct, abs=1e-3)

    # No volume data was given -> must be null, not omitted, not 0.
    assert item["volume_acceleration_ratio"] is None
    assert item["volume_acceleration_available"] is False

    # The checkpoint created above was established from a FakeProvider
    # quote with no day_high/day_low set (day_high/day_low default to
    # None), so its price_threshold_applied resolved to
    # ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT (1.0), not the old fixed 2.0.
    expected_score = abs(expected_pct) / ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT
    assert item["attention_score"] == pytest.approx(expected_score, abs=1e-3)
    assert item["price_threshold_applied"] == ADAPTIVE_PRICE_THRESHOLD_FALLBACK_PCT

    # Lowercase string, never a Python Enum repr like "AttentionLevel.HIGH".
    assert item["attention_level"] == "high"
    assert isinstance(item["attention_level"], str)

    assert item["explanation"] == "RELIANCE moved +5.5% since your last check."
    assert item["rank"] == 1


def test_get_attention_excludes_acknowledged_events(client, monkeypatch):
    test_client, _ = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    instrument_id = reliance["instrument_id"]

    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )
    test_client.get("/watchlist")  # creates the active ChangeEvent

    assert test_client.get("/watchlist/attention").json()["attention_items"] != []

    # Explicit re-acknowledgement at the new price supersedes it.
    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")

    assert test_client.get("/watchlist/attention").json() == {"attention_items": []}


def test_get_attention_orders_multiple_instruments_by_descending_score(client, monkeypatch):
    test_client, _ = client

    first = test_client.get("/watchlist").json()
    reliance = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")
    tcs = next(i for i in first["instruments"] if i["symbol"] == "TCS")
    reliance_id = reliance["instrument_id"]
    tcs_id = tcs["instrument_id"]

    test_client.post(f"/watchlist/instruments/{reliance_id}/checkpoint")  # baseline @ 1326.4
    test_client.post(f"/watchlist/instruments/{tcs_id}/checkpoint")  # baseline @ 2312.8

    monkeypatch.setattr(
        watchlist_routes,
        "_provider",
        FakeProvider(
            {
                **_quotes_with_reliance_price(1360.0),  # ~2.5% -> weaker meaningful move
                "TCS.NS": make_quote("TCS.NS", 2500.0, 2320.0, 1722049),  # ~8.1% -> stronger
            }
        ),
    )
    test_client.get("/watchlist")  # creates both active ChangeEvents

    items = test_client.get("/watchlist/attention").json()["attention_items"]

    assert [item["symbol"] for item in items] == ["TCS", "RELIANCE"]
    assert [item["rank"] for item in items] == [1, 2]
    assert items[0]["attention_score"] > items[1]["attention_score"]


# --- POST /watchlist/instruments (Add Stock) --------------------------------


def _quotes_with_wipro_added() -> dict:
    """The default 5 seed quotes plus a 6th (WIPRO) -- used to exercise
    Add Stock without disturbing the existing seeded instruments'
    resolvability."""
    return {
        "RELIANCE.NS": make_quote("RELIANCE.NS", 1326.4, 1302.6, 9122871),
        "TCS.NS": make_quote("TCS.NS", 2312.8, 2320.0, 1722049),
        "HDFCBANK.NS": make_quote("HDFCBANK.NS", 715.6, 706.6, 9937354),
        "INFY.NS": make_quote("INFY.NS", 1129.0, 1130.3, 3875864),
        "ICICIBANK.NS": make_quote("ICICIBANK.NS", 1432.5, 1430.0, 4072353),
        "WIPRO.NS": make_quote("WIPRO.NS", 480.0, 475.0, 5000000),
    }


def test_add_instrument_creates_a_new_trackable_instrument(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    response = test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NSE"})

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "WIPRO"
    assert body["exchange"] == "NSE"
    assert body["created"] is True
    assert "instrument_id" in body

    doc = mock_db.instruments.find_one({"symbol": "WIPRO", "exchange": "NSE"})
    assert doc is not None
    assert str(doc["_id"]) == body["instrument_id"]


def test_add_instrument_normalizes_symbol_case(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    response = test_client.post("/watchlist/instruments", json={"symbol": "wipro", "exchange": "NSE"})

    assert response.status_code == 200
    assert response.json()["symbol"] == "WIPRO"
    assert mock_db.instruments.count_documents({"symbol": "WIPRO", "exchange": "NSE"}) == 1


def test_add_instrument_is_idempotent_for_a_duplicate(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    first = test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NSE"})
    second = test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NSE"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["instrument_id"] == second.json()["instrument_id"]
    assert mock_db.instruments.count_documents({"symbol": "WIPRO", "exchange": "NSE"}) == 1


def test_add_instrument_rejects_invalid_exchange(client):
    """Instrument's own Exchange enum, applied automatically by FastAPI
    on request-body parsing -- no duplicated validation logic in the
    route, and nothing is ever created for a rejected request."""
    test_client, mock_db = client

    response = test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NYSE"})

    assert response.status_code == 422
    assert mock_db.instruments.count_documents({}) == 0


def test_add_instrument_rejects_blank_symbol(client):
    test_client, mock_db = client

    response = test_client.post("/watchlist/instruments", json={"symbol": "   ", "exchange": "NSE"})

    assert response.status_code == 422
    assert mock_db.instruments.count_documents({}) == 0


def test_add_instrument_returns_503_and_creates_nothing_when_provider_cannot_resolve(client):
    """The default fixture's FakeProvider only recognizes the 5 seed
    symbols -- "NOTREAL" falls through to its own "no test data
    configured" failure quote, exactly like any genuinely unresolvable
    real-world symbol would."""
    test_client, mock_db = client

    response = test_client.post("/watchlist/instruments", json={"symbol": "NOTREAL", "exchange": "NSE"})

    assert response.status_code == 503
    assert mock_db.instruments.count_documents({"symbol": "NOTREAL"}) == 0


def test_newly_added_instrument_appears_in_get_watchlist(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    add_response = test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NSE"})
    assert add_response.status_code == 200

    body = test_client.get("/watchlist").json()
    symbols = {i["symbol"] for i in body["instruments"]}

    assert "WIPRO" in symbols
    assert len(body["instruments"]) == 6

    wipro = next(i for i in body["instruments"] if i["symbol"] == "WIPRO")
    assert wipro["price"] == 480.0
    assert wipro["status"] == "ok"


def test_existing_seed_instruments_still_appear_after_add_stock(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NSE"})

    body = test_client.get("/watchlist").json()
    symbols = {i["symbol"] for i in body["instruments"]}
    assert symbols == {"RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "WIPRO"}


def test_newly_added_instrument_has_no_baseline_and_creates_no_attention(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NSE"})

    watchlist_body = test_client.get("/watchlist").json()
    wipro = next(i for i in watchlist_body["instruments"] if i["symbol"] == "WIPRO")
    assert wipro["change"]["has_baseline"] is False
    assert "Baseline pending" in wipro["change"]["reason"]

    assert mock_db.change_events.count_documents({"instrument_id": wipro["instrument_id"]}) == 0

    attention_body = test_client.get("/watchlist/attention").json()
    attention_ids = {item["instrument_id"] for item in attention_body["attention_items"]}
    assert wipro["instrument_id"] not in attention_ids


def test_newly_added_instrument_can_progress_through_checkpoint_and_attention_like_any_seed(
    client, monkeypatch
):
    """Proves Add Stock didn't just avoid breaking existing behavior --
    a newly added instrument genuinely flows through the SAME
    checkpoint -> change detection -> attention pipeline as any seed
    instrument, with no special-casing anywhere in that pipeline."""
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    add_response = test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NSE"})
    wipro_id = add_response.json()["instrument_id"]

    # Explicit "mark as seen" establishes a real checkpoint baseline @ 480.0.
    checkpoint_response = test_client.post(f"/watchlist/instruments/{wipro_id}/checkpoint")
    assert checkpoint_response.status_code == 200

    # Simulate a real >2% move.
    moved_quotes = _quotes_with_wipro_added()
    moved_quotes["WIPRO.NS"] = make_quote("WIPRO.NS", 520.0, 475.0, 5000000)
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(moved_quotes))

    watchlist_body = test_client.get("/watchlist").json()
    wipro = next(i for i in watchlist_body["instruments"] if i["symbol"] == "WIPRO")
    assert wipro["change"]["meaningful_change"] is True

    assert mock_db.change_events.count_documents({"instrument_id": wipro_id}) == 1

    attention_body = test_client.get("/watchlist/attention").json()
    attention_symbols = {item["symbol"] for item in attention_body["attention_items"]}
    assert "WIPRO" in attention_symbols


# --- Persistent Anonymous Watchlist (owner identity + ownership) -----------
#
# TestClient (httpx under the hood) persists cookies across calls made on
# the SAME client instance, exactly like a real browser -- so a bare
# test_client.get(...) with no explicit `cookies=` naturally simulates
# "the same browser, again" once a cookie has been issued. Tests that
# need two DISTINCT owners pass explicit `cookies={"watchlist_owner": ...}`
# per request instead, for full determinism about which owner each call
# belongs to.


def test_first_request_without_cookie_creates_owner_and_sets_cookie(client):
    test_client, mock_db = client

    response = test_client.get("/watchlist")

    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie")
    assert set_cookie is not None
    assert "watchlist_owner=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert mock_db.watchlists.count_documents({}) == 1


def test_repeat_request_with_same_cookie_resolves_same_owner(client):
    test_client, mock_db = client

    test_client.get("/watchlist")
    assert mock_db.watchlists.count_documents({}) == 1
    owner_id = mock_db.watchlists.find_one({})["user_id"]

    test_client.get("/watchlist")  # same client -> same cookie sent automatically

    assert mock_db.watchlists.count_documents({}) == 1  # still exactly one owner
    assert mock_db.watchlists.find_one({})["user_id"] == owner_id


def test_watchlist_state_survives_between_requests(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    test_client.post("/watchlist/instruments", json={"symbol": "WIPRO", "exchange": "NSE"})
    first_symbols = {i["symbol"] for i in test_client.get("/watchlist").json()["instruments"]}
    second_symbols = {i["symbol"] for i in test_client.get("/watchlist").json()["instruments"]}

    assert "WIPRO" in first_symbols
    assert first_symbols == second_symbols


def test_different_owner_cookies_get_independent_watchlists(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    test_client.post(
        "/watchlist/instruments",
        json={"symbol": "WIPRO", "exchange": "NSE"},
        cookies={"watchlist_owner": "owner-a"},
    )

    owner_a = test_client.get("/watchlist", cookies={"watchlist_owner": "owner-a"}).json()
    owner_b = test_client.get("/watchlist", cookies={"watchlist_owner": "owner-b"}).json()

    assert "WIPRO" in {i["symbol"] for i in owner_a["instruments"]}
    assert "WIPRO" not in {i["symbol"] for i in owner_b["instruments"]}
    # Owner B still independently gets the same 5-seed default.
    assert {i["symbol"] for i in owner_b["instruments"]} == {
        "RELIANCE",
        "TCS",
        "HDFCBANK",
        "INFY",
        "ICICIBANK",
    }


def test_add_stock_is_isolated_between_owners(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    test_client.post(
        "/watchlist/instruments",
        json={"symbol": "WIPRO", "exchange": "NSE"},
        cookies={"watchlist_owner": "owner-a"},
    )

    wipro_id = mock_db.instruments.find_one({"symbol": "WIPRO"})["_id"]
    owner_a_doc = mock_db.watchlists.find_one({"user_id": "owner-a"})
    owner_b_doc = mock_db.watchlists.find_one({"user_id": "owner-b"})

    assert str(wipro_id) in owner_a_doc["instrument_ids"]
    assert owner_b_doc is None  # owner B has never made a request yet -- no watchlist exists for them at all


def test_mark_seen_is_isolated_between_owners(client):
    test_client, mock_db = client

    first = test_client.get("/watchlist", cookies={"watchlist_owner": "owner-a"}).json()
    instrument_id = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")["instrument_id"]

    checkpoint_response = test_client.post(
        f"/watchlist/instruments/{instrument_id}/checkpoint",
        cookies={"watchlist_owner": "owner-a"},
    )
    assert checkpoint_response.status_code == 200

    owner_b_watchlist = test_client.get("/watchlist", cookies={"watchlist_owner": "owner-b"}).json()
    reliance_b = next(i for i in owner_b_watchlist["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance_b["change"]["has_baseline"] is False

    assert mock_db.checkpoints.count_documents({"user_id": "owner-a", "instrument_id": instrument_id}) == 1
    assert mock_db.checkpoints.count_documents({"user_id": "owner-b", "instrument_id": instrument_id}) == 0


def test_attention_is_isolated_between_owners(client, monkeypatch):
    test_client, mock_db = client

    first = test_client.get("/watchlist", cookies={"watchlist_owner": "owner-a"}).json()
    instrument_id = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")["instrument_id"]

    test_client.post(
        f"/watchlist/instruments/{instrument_id}/checkpoint",
        cookies={"watchlist_owner": "owner-a"},
    )
    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )
    test_client.get("/watchlist", cookies={"watchlist_owner": "owner-a"})  # creates owner-a's ChangeEvent

    owner_a_attention = test_client.get(
        "/watchlist/attention", cookies={"watchlist_owner": "owner-a"}
    ).json()
    owner_b_attention = test_client.get(
        "/watchlist/attention", cookies={"watchlist_owner": "owner-b"}
    ).json()

    assert any(item["symbol"] == "RELIANCE" for item in owner_a_attention["attention_items"])
    assert owner_b_attention["attention_items"] == []


def test_mark_as_seen_rejects_instrument_not_in_owners_watchlist(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    test_client.post(
        "/watchlist/instruments",
        json={"symbol": "WIPRO", "exchange": "NSE"},
        cookies={"watchlist_owner": "owner-a"},
    )
    wipro_id = str(mock_db.instruments.find_one({"symbol": "WIPRO"})["_id"])

    # Owner B never added WIPRO -- must be rejected exactly like a
    # genuinely nonexistent instrument_id (same 404, see mark_as_seen's
    # own docstring on why not a distinct 403).
    response = test_client.post(
        f"/watchlist/instruments/{wipro_id}/checkpoint",
        cookies={"watchlist_owner": "owner-b"},
    )

    assert response.status_code == 404
    assert mock_db.checkpoints.count_documents({"user_id": "owner-b", "instrument_id": wipro_id}) == 0


def test_missing_or_unusual_cookie_never_causes_a_server_error(client):
    test_client, mock_db = client

    assert test_client.get("/watchlist").status_code == 200
    assert test_client.get("/watchlist", cookies={"watchlist_owner": ""}).status_code == 200

    response = test_client.get(
        "/watchlist", cookies={"watchlist_owner": "not-a-real-issued-token"}
    )
    assert response.status_code == 200
    # An unrecognized-but-well-formed value is trusted as its own (new,
    # currently-empty-until-seeded) owner identity -- never a crash, and
    # never silently mapped onto a different real owner's data.
    assert mock_db.watchlists.count_documents({"user_id": "not-a-real-issued-token"}) == 1


def test_extremely_long_cookie_value_never_causes_a_server_error(client):
    """Step 4 edge case: an extremely long (but otherwise ordinary,
    wire-transmittable) cookie value is trusted as its own distinct
    (new, currently-empty-until-seeded) owner identity -- no length
    limit, no crash, no collision with a different real owner's data."""
    test_client, mock_db = client
    cookie_value = "x" * 4000

    response = test_client.get("/watchlist", cookies={"watchlist_owner": cookie_value})

    assert response.status_code == 200
    assert mock_db.watchlists.count_documents({"user_id": cookie_value}) == 1


def test_request_body_user_id_is_ignored_identity_comes_from_cookie(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    response = test_client.post(
        "/watchlist/instruments",
        json={"symbol": "WIPRO", "exchange": "NSE", "user_id": "attacker-chosen-owner"},
        cookies={"watchlist_owner": "real-cookie-owner"},
    )

    assert response.status_code == 200
    # The Instrument model has no user_id field at all -- Pydantic
    # silently drops the unknown field -- and ownership always comes
    # from the cookie, never the request body.
    assert mock_db.watchlists.count_documents({"user_id": "attacker-chosen-owner"}) == 0
    real_owner_doc = mock_db.watchlists.find_one({"user_id": "real-cookie-owner"})
    assert real_owner_doc is not None
    wipro_id = str(mock_db.instruments.find_one({"symbol": "WIPRO"})["_id"])
    assert wipro_id in real_owner_doc["instrument_ids"]


def test_default_seed_watchlist_created_exactly_once_per_owner(client):
    test_client, mock_db = client

    test_client.get("/watchlist")
    test_client.get("/watchlist")
    test_client.get("/watchlist/attention")

    assert mock_db.watchlists.count_documents({}) == 1
    assert len(mock_db.watchlists.find_one({})["instrument_ids"]) == 5


def test_add_stock_reuses_existing_global_instrument_across_owners(client, monkeypatch):
    test_client, mock_db = client
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(_quotes_with_wipro_added()))

    test_client.post(
        "/watchlist/instruments",
        json={"symbol": "WIPRO", "exchange": "NSE"},
        cookies={"watchlist_owner": "owner-a"},
    )
    test_client.post(
        "/watchlist/instruments",
        json={"symbol": "WIPRO", "exchange": "NSE"},
        cookies={"watchlist_owner": "owner-b"},
    )

    assert mock_db.instruments.count_documents({"symbol": "WIPRO", "exchange": "NSE"}) == 1

    owner_a = test_client.get("/watchlist", cookies={"watchlist_owner": "owner-a"}).json()
    owner_b = test_client.get("/watchlist", cookies={"watchlist_owner": "owner-b"}).json()
    assert "WIPRO" in {i["symbol"] for i in owner_a["instruments"]}
    assert "WIPRO" in {i["symbol"] for i in owner_b["instruments"]}


def test_legacy_demo_user_data_is_never_touched(client):
    test_client, mock_db = client

    # Simulate pre-existing legacy data from before anonymous identity
    # existed -- inserted directly, shaped like the real documents
    # CheckpointService/ChangeEventService already write.
    mock_db.checkpoints.insert_one(
        {
            "id": "legacy-checkpoint-id",
            "user_id": "demo-user",
            "instrument_id": "000000000000000000000001",
            "checkpoint_at": "2026-01-01T00:00:00+00:00",
            "session_date": "2026-01-01",
            "baseline_snapshot": {"last_price": 100.0, "volume": 1000, "percent_change": 0.0},
            "source": "explicit",
        }
    )
    mock_db.change_events.insert_one(
        {
            "id": "legacy-change-event-id",
            "user_id": "demo-user",
            "instrument_id": "000000000000000000000001",
            "checkpoint_id": "legacy-checkpoint-id",
            "detected_at": "2026-01-01T00:00:00+00:00",
            "signals": {
                "price_change_pct": 3.0,
                "volume_acceleration_ratio": None,
                "volume_acceleration_available": False,
            },
            "reason": "Legacy reason",
            "acknowledged": False,
        }
    )
    legacy_checkpoint_before = mock_db.checkpoints.find_one({"user_id": "demo-user"})
    legacy_event_before = mock_db.change_events.find_one({"user_id": "demo-user"})

    # A brand-new anonymous owner does a full round of normal activity.
    test_client.get("/watchlist", cookies={"watchlist_owner": "new-owner"})
    test_client.get("/watchlist/attention", cookies={"watchlist_owner": "new-owner"})
    first = test_client.get("/watchlist", cookies={"watchlist_owner": "new-owner"}).json()
    reliance_id = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")["instrument_id"]
    test_client.post(
        f"/watchlist/instruments/{reliance_id}/checkpoint",
        cookies={"watchlist_owner": "new-owner"},
    )

    assert mock_db.checkpoints.find_one({"user_id": "demo-user"}) == legacy_checkpoint_before
    assert mock_db.change_events.find_one({"user_id": "demo-user"}) == legacy_event_before
    # The new owner's own checkpoint is genuinely separate.
    assert mock_db.checkpoints.count_documents({"user_id": "new-owner"}) == 1


def test_ensure_seed_instruments_survives_concurrent_seeding_race(mock_db, monkeypatch):
    """
    REGRESSION (P0 Hardening #5): ensure_seed_instruments is called by
    EVERY brand-new owner's first-ever request (via
    get_or_create_watchlist), and on a fresh database the 5 seed
    Instrument documents don't exist yet -- so two genuinely
    first-ever, concurrent requests (from two DIFFERENT brand-new
    owners, or two tabs of the same one) can both see a given seed
    symbol as "not yet created" and both attempt to insert it. Unlike
    get_or_create_watchlist and add_instrument, this loop had no
    DuplicateKeyError recovery around its own insert_one -- the losing
    request would raise an unhandled 500 instead of reusing the
    instrument the winning request just created.

    Forced deterministically (not via real threads) with the same
    "find_one misses once" technique used for the analogous Watchlist-
    creation and Add-Stock races.
    """
    from app.models.instrument import Instrument
    from app.services.watchlist_service import SEED_INSTRUMENTS, ensure_seed_instruments

    symbol, exchange = SEED_INSTRUMENTS[0]

    # A "concurrent" request already won the race and inserted this
    # exact seed instrument first.
    winning_doc = Instrument(symbol=symbol, exchange=exchange).model_dump(mode="json")
    insert_result = mock_db.instruments.insert_one(winning_doc)
    winning_doc["_id"] = insert_result.inserted_id

    real_find_one = mock_db.instruments.find_one
    call_count = {"n": 0}

    def find_one_misses_once(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_find_one(*args, **kwargs)

    monkeypatch.setattr(mock_db.instruments, "find_one", find_one_misses_once)

    result = ensure_seed_instruments(mock_db)

    assert len(result) == len(SEED_INSTRUMENTS)
    first_result_doc = next(d for d in result if d["symbol"] == symbol)
    assert first_result_doc["_id"] == winning_doc["_id"]
    assert (
        mock_db.instruments.count_documents(
            {"symbol": symbol, "exchange": exchange.value}
        )
        == 1
    )


def test_get_or_create_watchlist_survives_concurrent_creation_race(mock_db, monkeypatch):
    """
    Backend-side safety net for the scenario the frontend's own
    first-request sequencing (see App.jsx's loadAll -- decisions.md's
    "Persistent anonymous watchlist identity" and its concurrency
    hardening follow-up) is designed to make rare in practice: two
    requests for the SAME brand-new owner_id whose Watchlist-creation
    attempts genuinely interleave (e.g. two browser tabs opened at the
    exact same first-ever instant, which frontend-side sequencing alone
    cannot fully prevent, since each tab is an independent JS context).

    This forces that exact interleaving deterministically -- rather than
    relying on real thread timing, which would make the test flaky --
    by making get_or_create_watchlist's own find_one miss a document
    that a "concurrent" request already inserted, so its insert_one
    hits the real unique index (uniq_user_id, Phase 1) and raises
    DuplicateKeyError. The function must recover by reading back the
    document that actually won, never crash, and never create a second
    Watchlist for the same owner.
    """
    from app.services.watchlist_service import ensure_seed_instruments, get_or_create_watchlist

    owner_id = "racing-owner"
    seed_instruments = ensure_seed_instruments(mock_db)

    # A "concurrent" request for this exact owner_id already won the
    # race and inserted first.
    mock_db.watchlists.insert_one(
        {
            "user_id": owner_id,
            "instrument_ids": [str(inst["_id"]) for inst in seed_instruments],
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )

    # This call's OWN find_one is forced to miss that just-inserted
    # document exactly once (simulating "it checked before the other
    # request's insert had happened"), so it still proceeds down the
    # create-a-new-one path.
    real_find_one = mock_db.watchlists.find_one
    call_count = {"n": 0}

    def find_one_misses_once(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_find_one(*args, **kwargs)

    monkeypatch.setattr(mock_db.watchlists, "find_one", find_one_misses_once)

    result = get_or_create_watchlist(mock_db, owner_id)

    assert mock_db.watchlists.count_documents({"user_id": owner_id}) == 1
    assert result.user_id == owner_id
    assert len(result.instrument_ids) == 5


def test_concurrent_membership_additions_of_different_symbols_do_not_clobber_each_other(
    mock_db, monkeypatch
):
    """
    Step 4/16: _add_instrument_to_watchlist uses $addToSet (an atomic,
    single-document array-append operator), not a read-modify-write
    replace_one -- so two updates adding DIFFERENT symbols to the SAME
    owner's Watchlist can never lose one membership to a last-write-wins
    overwrite, regardless of ordering. Simulated here as three
    successive additions (each is what a single concurrent request
    would issue) rather than real threads, since $addToSet's atomicity
    is a per-call MongoDB guarantee, not something that depends on
    ordering to prove -- if a $push/replace_one were used instead, this
    exact sequence would still only leave whichever update ran last.
    """
    from app.services.watchlist_service import (
        _add_instrument_to_watchlist,
        get_or_create_watchlist,
    )

    owner_id = "owner-concurrent-adds"
    get_or_create_watchlist(mock_db, owner_id)  # seeds the 5 default instruments

    tcs_id = str(mock_db.instruments.find_one({"symbol": "TCS"})["_id"])
    infy_id = str(mock_db.instruments.find_one({"symbol": "INFY"})["_id"])
    hdfc_id = str(mock_db.instruments.find_one({"symbol": "HDFCBANK"})["_id"])

    # Each call is independently atomic -- none of these should observe
    # or overwrite the others' effect.
    _add_instrument_to_watchlist(mock_db, owner_id, tcs_id)
    _add_instrument_to_watchlist(mock_db, owner_id, infy_id)
    _add_instrument_to_watchlist(mock_db, owner_id, hdfc_id)

    watchlist = mock_db.watchlists.find_one({"user_id": owner_id})
    assert tcs_id in watchlist["instrument_ids"]
    assert infy_id in watchlist["instrument_ids"]
    assert hdfc_id in watchlist["instrument_ids"]
    # $addToSet never introduces a duplicate for a symbol already
    # present from seeding (all 5 seeds already include TCS/INFY/HDFCBANK).
    assert watchlist["instrument_ids"].count(tcs_id) == 1


def test_add_instrument_survives_concurrent_creation_race_for_same_new_symbol(
    mock_db, monkeypatch
):
    """
    REGRESSION (P0 Hardening #4, Step 10's known Add Stock concurrency
    gap): two owners simultaneously Add Stock for the same brand-new
    (symbol, exchange) pair that does not exist globally yet. Both
    requests' own find_one sees "doesn't exist" before either has
    inserted, so both proceed to validate via the provider and attempt
    insert_one -- the losing request must hit the real unique index
    (uniq_symbol_exchange) and recover by joining the Instrument the
    winning request actually created, rather than raising an unhandled
    500 and leaving the losing owner with no membership at all.

    Forced deterministically (not via real threads) by making this
    request's OWN find_one miss a document a "concurrent" request
    already inserted -- the same pattern
    test_get_or_create_watchlist_survives_concurrent_creation_race uses
    for the analogous Watchlist-creation race.
    """
    from app.models.instrument import Exchange, Instrument
    from app.services.watchlist_service import add_instrument

    symbol, exchange = "WIPRO", Exchange.NSE
    provider = FakeProvider({"WIPRO.NS": make_quote("WIPRO.NS", 480.0, 475.0, 5_000_000)})

    # A "concurrent" request (owner A) for this exact (symbol, exchange)
    # already won the race and inserted the global Instrument first.
    winning_doc = Instrument(symbol=symbol, exchange=exchange).model_dump(mode="json")
    insert_result = mock_db.instruments.insert_one(winning_doc)
    winning_doc["_id"] = insert_result.inserted_id

    real_find_one = mock_db.instruments.find_one
    call_count = {"n": 0}

    def find_one_misses_once(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return real_find_one(*args, **kwargs)

    monkeypatch.setattr(mock_db.instruments, "find_one", find_one_misses_once)

    doc, created = add_instrument(
        mock_db, provider, "owner-b", Instrument(symbol=symbol, exchange=exchange)
    )

    assert created is False  # owner-b did not create it -- joined owner A's
    assert doc["_id"] == winning_doc["_id"]
    assert (
        mock_db.instruments.count_documents({"symbol": symbol, "exchange": exchange.value}) == 1
    )

    watchlist_b = mock_db.watchlists.find_one({"user_id": "owner-b"})
    assert str(winning_doc["_id"]) in watchlist_b["instrument_ids"]


# --- P0 Hardening: observation must never become acknowledgement -----------
#
# The call-graph inspection behind these tests found the invariant already
# holds (GET /watchlist can reach checkpoints.replace_one? No -- it never
# calls create_checkpoint_from_snapshot. GET /watchlist/attention performs
# zero writes at all -- confirmed by grepping every insert_one/update_one/
# update_many/replace_one call site in app/). These tests close specific,
# verified gaps in existing coverage rather than fix any production bug.


def test_new_owner_repeated_observation_creates_no_checkpoint_or_change_event(client):
    """
    Edge cases A1/A2/A3: a brand-new owner with no checkpoint at all.
    Repeated, INTERLEAVED GET /watchlist and GET /watchlist/attention
    calls must never create a Checkpoint or a ChangeEvent, and must
    never alter the owner's own Watchlist membership -- observation
    alone, no matter how many times repeated, is never acknowledgement.
    """
    test_client, mock_db = client

    for _ in range(3):
        test_client.get("/watchlist")
        test_client.get("/watchlist/attention")

    assert mock_db.checkpoints.count_documents({}) == 0
    assert mock_db.change_events.count_documents({}) == 0
    assert mock_db.watchlists.count_documents({}) == 1
    assert len(mock_db.watchlists.find_one({})["instrument_ids"]) == 5


def test_checkpoint_remains_unchanged_when_get_detects_a_meaningful_change(client, monkeypatch):
    """
    Edge case C: GET /watchlist may OBSERVE a meaningful change and
    persist the resulting ChangeEvent (a detection fact -- see
    ChangeEventService's own docstring), but the underlying Checkpoint
    itself must never advance or be modified merely because that
    detection happened, including across repeated GETs afterward.
    """
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    instrument_id = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")[
        "instrument_id"
    ]
    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    checkpoint_before = mock_db.checkpoints.find_one({"instrument_id": instrument_id})

    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )

    # The GET that actually detects and persists the ChangeEvent.
    body = test_client.get("/watchlist").json()
    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    assert reliance["change"]["meaningful_change"] is True
    assert mock_db.change_events.count_documents({}) == 1

    checkpoint_after_detection = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_after_detection == checkpoint_before

    # A further repeated GET while the same change stays unacknowledged.
    test_client.get("/watchlist")
    test_client.get("/watchlist")
    checkpoint_after_repeats = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_after_repeats == checkpoint_before
    assert mock_db.checkpoints.count_documents({}) == 1
    assert mock_db.change_events.count_documents({}) == 1  # no duplicate either


def test_simulated_frontend_polling_sequence_never_acknowledges_or_advances(client, monkeypatch):
    """
    Edge case E: simulates the real frontend's own loadAll() pattern --
    GET /watchlist and GET /watchlist/attention, repeated several times
    (as the 60s poll would) -- while a real unacknowledged meaningful
    change exists. None of this presentation-layer polling may silently
    acknowledge the event, advance the checkpoint, replace the baseline,
    or create a duplicate ChangeEvent.
    """
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    instrument_id = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")[
        "instrument_id"
    ]
    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    checkpoint_before = mock_db.checkpoints.find_one({"instrument_id": instrument_id})

    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )

    for _ in range(4):
        test_client.get("/watchlist")
        test_client.get("/watchlist/attention")

    checkpoint_after = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_after == checkpoint_before
    assert mock_db.checkpoints.count_documents({}) == 1
    assert mock_db.change_events.count_documents({}) == 1

    event = mock_db.change_events.find_one({"instrument_id": instrument_id})
    assert event["acknowledged"] is False

    attention_items = test_client.get("/watchlist/attention").json()["attention_items"]
    assert any(item["symbol"] == "RELIANCE" for item in attention_items)

    assert len(mock_db.watchlists.find_one({})["instrument_ids"]) == 5


def test_repeated_mark_as_seen_on_same_instrument_is_safe(client):
    """
    Edge case G: calling Mark as Seen repeatedly for the same instrument
    is safe -- each call is a real, explicit user action (unlike a GET),
    so it's expected (existing, intended semantics) to advance the
    checkpoint again each time, never error, and never duplicate the
    checkpoint document.
    """
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    instrument_id = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")[
        "instrument_id"
    ]

    response1 = test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    assert response1.status_code == 200
    checkpoint_after_first = mock_db.checkpoints.find_one({"instrument_id": instrument_id})

    response2 = test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    assert response2.status_code == 200
    checkpoint_after_second = mock_db.checkpoints.find_one({"instrument_id": instrument_id})

    # Still exactly one checkpoint document -- advanced, not duplicated.
    assert mock_db.checkpoints.count_documents({"instrument_id": instrument_id}) == 1
    # A fresh explicit action always writes a new checkpoint id, even
    # with an unchanged price (architecture.md's "advancing the
    # checkpoint replaces the previous one" -- existing semantics,
    # unchanged by this hardening pass).
    assert checkpoint_after_second["id"] != checkpoint_after_first["id"]

    response3 = test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    assert response3.status_code == 200
    assert mock_db.checkpoints.count_documents({"instrument_id": instrument_id}) == 1


def test_mark_all_as_seen_does_not_touch_another_owners_state(client, monkeypatch):
    """
    Edge case H.7: Mark All as Seen for owner A must never create,
    advance, or acknowledge anything belonging to a DIFFERENT owner --
    only the calling owner's own membership/checkpoints/events are ever
    touched.
    """
    test_client, mock_db = client

    # Owner B establishes their own checkpoint + active ChangeEvent first.
    owner_b_first = test_client.get(
        "/watchlist", cookies={"watchlist_owner": "owner-b"}
    ).json()
    reliance_id = next(i for i in owner_b_first["instruments"] if i["symbol"] == "RELIANCE")[
        "instrument_id"
    ]
    test_client.post(
        f"/watchlist/instruments/{reliance_id}/checkpoint",
        cookies={"watchlist_owner": "owner-b"},
    )
    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )
    test_client.get("/watchlist", cookies={"watchlist_owner": "owner-b"})

    owner_b_checkpoint_before = mock_db.checkpoints.find_one(
        {"user_id": "owner-b", "instrument_id": reliance_id}
    )
    owner_b_event_before = mock_db.change_events.find_one(
        {"user_id": "owner-b", "instrument_id": reliance_id}
    )
    assert owner_b_event_before["acknowledged"] is False

    # Owner A (a completely separate, brand-new owner) calls Mark All.
    response = test_client.post(
        "/watchlist/checkpoint", cookies={"watchlist_owner": "owner-a"}
    )
    assert response.status_code == 200

    owner_b_checkpoint_after = mock_db.checkpoints.find_one(
        {"user_id": "owner-b", "instrument_id": reliance_id}
    )
    owner_b_event_after = mock_db.change_events.find_one(
        {"user_id": "owner-b", "instrument_id": reliance_id}
    )

    assert owner_b_checkpoint_after == owner_b_checkpoint_before
    assert owner_b_event_after == owner_b_event_before
    assert owner_b_event_after["acknowledged"] is False

    # Owner A's own mark-all only ever touched owner A's own checkpoints.
    assert mock_db.checkpoints.count_documents({"user_id": "owner-a"}) == 5
    assert mock_db.checkpoints.count_documents({"user_id": "owner-b"}) == 1


def test_provider_failure_for_one_instrument_does_not_affect_a_siblings_existing_checkpoint(
    client, monkeypatch
):
    """
    Edge case I: a provider failure for ONE instrument must never
    mutate a DIFFERENT instrument's existing checkpoint/change-event
    state. Both RELIANCE and TCS get real checkpoints first; then TCS's
    data fails while RELIANCE keeps moving meaningfully -- RELIANCE's
    own detection must proceed completely normally, unaffected by TCS's
    failure, and TCS's existing checkpoint must stay exactly as it was.

    Since the last-known-good fallback feature, TCS degrades to a
    stale fallback here (not "unavailable") because the initial GET
    above already persisted a valid TCS snapshot -- that fallback must
    still leave TCS's checkpoint/change-events completely untouched.
    """
    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    reliance_id = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")[
        "instrument_id"
    ]
    tcs_id = next(i for i in first["instruments"] if i["symbol"] == "TCS")["instrument_id"]

    test_client.post(f"/watchlist/instruments/{reliance_id}/checkpoint")
    test_client.post(f"/watchlist/instruments/{tcs_id}/checkpoint")
    tcs_checkpoint_before = mock_db.checkpoints.find_one({"instrument_id": tcs_id})

    partial_provider = FakeProvider(
        {
            "RELIANCE.NS": make_quote("RELIANCE.NS", 1400.0, 1302.6, 9122871),
            # TCS deliberately omitted -> unavailable this cycle
            "HDFCBANK.NS": make_quote("HDFCBANK.NS", 715.6, 706.6, 9937354),
            "INFY.NS": make_quote("INFY.NS", 1129.0, 1130.3, 3875864),
            "ICICIBANK.NS": make_quote("ICICIBANK.NS", 1432.5, 1430.0, 4072353),
        }
    )
    monkeypatch.setattr(watchlist_routes, "_provider", partial_provider)

    body = test_client.get("/watchlist").json()
    reliance = next(i for i in body["instruments"] if i["symbol"] == "RELIANCE")
    tcs = next(i for i in body["instruments"] if i["symbol"] == "TCS")

    assert reliance["change"]["meaningful_change"] is True
    assert tcs["status"] == "stale"

    # RELIANCE's own detection proceeded normally.
    assert mock_db.change_events.count_documents({"instrument_id": reliance_id}) == 1
    # TCS's existing checkpoint is completely untouched by its own
    # provider failure.
    tcs_checkpoint_after = mock_db.checkpoints.find_one({"instrument_id": tcs_id})
    assert tcs_checkpoint_after == tcs_checkpoint_before
    assert mock_db.change_events.count_documents({"instrument_id": tcs_id}) == 0


def test_checkpoint_unchanged_across_a_session_boundary(client, monkeypatch):
    """
    Edge case L: a checkpoint set in one trading session, observed again
    once the current snapshot belongs to a LATER session, must remain
    completely unchanged -- GET never replaces a baseline merely because
    the session rolled over. (The volume-acceleration-unavailable-
    across-a-session-boundary computation itself is already covered at
    the unit level in test_change_engine.py; this test is specifically
    about checkpoint immutability, not re-deriving that rule.)
    """
    from datetime import date

    test_client, mock_db = client

    first = test_client.get("/watchlist").json()
    instrument_id = next(i for i in first["instruments"] if i["symbol"] == "RELIANCE")[
        "instrument_id"
    ]
    test_client.post(f"/watchlist/instruments/{instrument_id}/checkpoint")
    checkpoint_before = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    checkpoint_session_date = date.fromisoformat(checkpoint_before["session_date"])

    next_session_quotes = _quotes_with_reliance_price(1400.0)
    next_session_quotes["RELIANCE.NS"] = RawQuote(
        symbol="RELIANCE.NS",
        last_price=1400.0,
        previous_close=1302.6,
        volume=9122871,
        provider_timestamp=1788509522,
        fetched_at=datetime.now(timezone.utc),
        fetch_succeeded=True,
        session_date=checkpoint_session_date + timedelta(days=1),
    )
    monkeypatch.setattr(watchlist_routes, "_provider", FakeProvider(next_session_quotes))

    test_client.get("/watchlist")
    test_client.get("/watchlist")

    checkpoint_after = mock_db.checkpoints.find_one({"instrument_id": instrument_id})
    assert checkpoint_after == checkpoint_before
    assert mock_db.checkpoints.count_documents({}) == 1


def test_owner_as_get_requests_cannot_touch_owner_bs_active_event_or_checkpoint(
    client, monkeypatch
):
    """
    Edge case N: owner B has an active, unacknowledged ChangeEvent and a
    real checkpoint. Owner A performing repeated GET /watchlist and GET
    /watchlist/attention calls -- even ones that establish A's OWN
    checkpoints/events along the way -- must never read, advance, or
    acknowledge anything of B's.
    """
    test_client, mock_db = client

    owner_b_first = test_client.get(
        "/watchlist", cookies={"watchlist_owner": "owner-b"}
    ).json()
    reliance_id = next(i for i in owner_b_first["instruments"] if i["symbol"] == "RELIANCE")[
        "instrument_id"
    ]
    test_client.post(
        f"/watchlist/instruments/{reliance_id}/checkpoint",
        cookies={"watchlist_owner": "owner-b"},
    )
    monkeypatch.setattr(
        watchlist_routes, "_provider", FakeProvider(_quotes_with_reliance_price(1400.0))
    )
    test_client.get("/watchlist", cookies={"watchlist_owner": "owner-b"})

    owner_b_checkpoint_before = mock_db.checkpoints.find_one({"user_id": "owner-b"})
    owner_b_event_before = mock_db.change_events.find_one({"user_id": "owner-b"})
    assert owner_b_event_before["acknowledged"] is False

    # Owner A does a full round of their OWN observation activity -- this
    # must never reach into B's state at all.
    for _ in range(3):
        test_client.get("/watchlist", cookies={"watchlist_owner": "owner-a"})
        test_client.get("/watchlist/attention", cookies={"watchlist_owner": "owner-a"})

    owner_b_checkpoint_after = mock_db.checkpoints.find_one({"user_id": "owner-b"})
    owner_b_event_after = mock_db.change_events.find_one({"user_id": "owner-b"})

    assert owner_b_checkpoint_after == owner_b_checkpoint_before
    assert owner_b_event_after == owner_b_event_before
    assert owner_b_event_after["acknowledged"] is False

    # Owner A's attention list never includes B's item.
    owner_a_attention = test_client.get(
        "/watchlist/attention", cookies={"watchlist_owner": "owner-a"}
    ).json()
    assert owner_a_attention["attention_items"] == []