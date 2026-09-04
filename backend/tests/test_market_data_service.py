from datetime import datetime, timezone

from app.models.market_snapshot import SnapshotStatus
from app.providers.base import MarketDataProvider, RawQuote
from app.services.market_data_service import MarketDataService


class FakeProvider(MarketDataProvider):
    """A test double implementing the real MarketDataProvider interface
    -- not a mock of internals, an actual alternative implementation,
    which is exactly what the abstraction is for."""

    def __init__(self, quotes: list[RawQuote]):
        self._quotes = quotes

    def get_quotes(self, symbols: list[str]) -> list[RawQuote]:
        return self._quotes


def make_quote(**overrides) -> RawQuote:
    defaults = dict(
        symbol="RELIANCE.NS",
        last_price=1326.4,
        previous_close=1302.6,
        volume=9122871,
        provider_timestamp=1788509522,
        fetched_at=datetime.now(timezone.utc),
        fetch_succeeded=True,
        error_message=None,
    )
    defaults.update(overrides)
    return RawQuote(**defaults)


def test_valid_quote_produces_valid_snapshot():
    provider = FakeProvider([make_quote()])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.instrument_id == "inst123"
    assert snap.last_price == 1326.4
    assert snap.status == SnapshotStatus.OK


def test_percent_change_is_computed_not_passed_through():
    provider = FakeProvider([make_quote(last_price=1326.4, previous_close=1302.6)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    expected = (1326.4 - 1302.6) / 1302.6 * 100
    assert snapshots[0].percent_change == expected


def test_provider_timestamp_is_stored_but_not_used_for_status():
    """Even with a provider_timestamp far in the past or future, status
    must be computed from fetched_at, not provider_timestamp."""
    provider = FakeProvider(
        [make_quote(provider_timestamp=1, fetched_at=datetime.now(timezone.utc))]
    )
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots[0].provider_timestamp == 1
    assert snapshots[0].status == SnapshotStatus.OK  # fresh, because fetched_at is now


def test_invalid_price_produces_no_snapshot():
    """A failed fetch (no usable price) must not produce a snapshot at
    all -- there is nothing valid to persist."""
    provider = FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_missing_previous_close_produces_no_snapshot():
    """previous_close is required to compute percent_change -- without
    it, we cannot assemble a valid snapshot per the model's contract."""
    provider = FakeProvider([make_quote(previous_close=None)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_missing_volume_degrades_gracefully_price_still_usable():
    """Per architecture.md's Invalid Data Rules: missing volume must NOT
    discard an otherwise-valid price."""
    provider = FakeProvider([make_quote(volume=None)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1326.4
    assert snapshots[0].volume == 0  # graceful fallback, not a crash


def test_negative_volume_from_provider_is_not_passed_through_as_negative():
    """A provider should never send negative volume, but this defends
    against it anyway -- falls back to 0 rather than violating the
    model's volume >= 0 constraint."""
    provider = FakeProvider([make_quote(volume=-5)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots[0].volume == 0


def test_provider_total_failure_does_not_crash_service():
    """Simulates the provider returning a failed RawQuote for every
    symbol (e.g., total outage) -- must return an empty list, not raise."""
    provider = FakeProvider(
        [make_quote(fetch_succeeded=False, last_price=None, error_message="boom")]
    )
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []  # no crash, just nothing usable


def test_multiple_instruments_mixed_success_and_failure():
    provider = FakeProvider(
        [
            make_quote(symbol="RELIANCE.NS"),
            make_quote(symbol="TCS.NS", fetch_succeeded=False, last_price=None),
        ]
    )
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots(
        {"RELIANCE.NS": "inst-reliance", "TCS.NS": "inst-tcs"}
    )

    assert len(snapshots) == 1
    assert snapshots[0].instrument_id == "inst-reliance"


def test_unrequested_symbol_from_provider_is_ignored():
    """Defensive: if a provider somehow returns a quote for a symbol we
    didn't ask about, it must not crash the mapping lookup."""
    provider = FakeProvider([make_quote(symbol="UNKNOWN.NS")])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []
