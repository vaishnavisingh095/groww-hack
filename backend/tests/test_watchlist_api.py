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
from app.services.change_engine import PRICE_CHANGE_THRESHOLD_PCT


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

    expected_score = abs(expected_pct) / PRICE_CHANGE_THRESHOLD_PCT
    assert item["attention_score"] == pytest.approx(expected_score, abs=1e-3)

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