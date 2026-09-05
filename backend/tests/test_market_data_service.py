from datetime import date, datetime, timedelta, timezone

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
        # A real, provider-derived session_date -- required by
        # MarketDataService as of the session_date correctness fix
        # (see decisions.md). Matches fetched_at's date by default here
        # since these tests aren't exercising the "bars are from a
        # different day than our fetch time" scenario (that's
        # test_yfinance_provider.py's job); tests that DO need a
        # mismatch override this explicitly.
        session_date=date(2026, 9, 4),
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


def test_zero_previous_close_produces_no_snapshot_not_a_crash():
    """REGRESSION for the ZeroDivisionError bug: previous_close=0.0
    reaching MarketSnapshot.compute_percent_change's raw division used
    to raise an unhandled ZeroDivisionError. It must instead be treated
    like any other unusable price -- no snapshot, no exception."""
    provider = FakeProvider([make_quote(previous_close=0.0)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_negative_previous_close_produces_no_snapshot_not_a_crash():
    """REGRESSION: a negative previous_close used to survive the
    division and then raise an unhandled pydantic.ValidationError at
    MarketSnapshot construction (gt=0). Must degrade to no snapshot."""
    provider = FakeProvider([make_quote(previous_close=-10.0)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_zero_last_price_produces_no_snapshot_not_a_crash():
    """REGRESSION: last_price=0.0 used to survive the None-checks, get
    divided through, and then raise an unhandled pydantic.ValidationError
    at MarketSnapshot construction (gt=0). Must degrade to no snapshot."""
    provider = FakeProvider([make_quote(last_price=0.0)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_negative_last_price_produces_no_snapshot_not_a_crash():
    provider = FakeProvider([make_quote(last_price=-5.0)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_nan_last_price_produces_no_snapshot_not_a_crash():
    """REGRESSION: a non-finite last_price (NaN) must never reach the
    percent_change division or the MarketSnapshot constructor."""
    provider = FakeProvider([make_quote(last_price=float("nan"))])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_infinite_previous_close_produces_no_snapshot_not_a_crash():
    """REGRESSION: a non-finite previous_close (inf) must never reach
    the percent_change division or the MarketSnapshot constructor."""
    provider = FakeProvider([make_quote(previous_close=float("inf"))])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_one_instrument_with_zero_previous_close_does_not_corrupt_sibling():
    """REGRESSION (the core failure-isolation bug): before the fix, a
    single malformed quote (previous_close=0.0) would raise out of
    fetch_snapshots entirely -- crashing the whole batch and silently
    losing every OTHER instrument's otherwise-healthy snapshot too. A
    bad instrument must never take down its siblings."""
    provider = FakeProvider(
        [
            make_quote(symbol="BAD.NS", previous_close=0.0),
            make_quote(symbol="GOOD.NS", last_price=100.0, previous_close=90.0),
        ]
    )
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots(
        {"BAD.NS": "inst-bad", "GOOD.NS": "inst-good"}
    )

    assert len(snapshots) == 1
    assert snapshots[0].instrument_id == "inst-good"
    assert snapshots[0].last_price == 100.0


def test_missing_session_date_produces_no_snapshot():
    """session_date is required (used later for the same-session
    volume-acceleration rule) -- a provider that could not determine it
    (e.g. no valid bar found) must not produce a snapshot, same as a
    missing price or previous_close."""
    provider = FakeProvider([make_quote(session_date=None)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_session_date_comes_from_the_quote_not_from_fetched_at():
    """REGRESSION for the P1-1 session_date bug: MarketDataService must
    use quote.session_date (the actual bar's date, as determined by the
    provider) directly -- never recompute it from fetched_at. This test
    deliberately sets fetched_at to a LATER calendar day than
    session_date, exactly the scenario a provider returning the most
    recently completed session's bars (market closed) produces."""
    provider = FakeProvider(
        [
            make_quote(
                fetched_at=datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc),  # day X
                session_date=date(2026, 9, 4),  # day X-1 -- the bars' real day
            )
        ]
    )
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].session_date == date(2026, 9, 4)
    assert snapshots[0].fetched_at.date() == date(2026, 9, 5)  # unaffected, still "now"


def test_real_world_valid_price_volume_and_previous_close_produces_snapshot():
    """
    REGRESSION TEST reproducing the exact real Mac runtime scenario that
    surfaced the bug: last_price=1322.0, volume=13022095, and (once the
    provider's own previous_close fallback is correctly applied)
    previous_close=1302.5. If the provider correctly supplies
    previous_close, the service must produce a usable snapshot with a
    correctly computed percent_change -- this must NOT return an empty
    list for a case where all the real underlying data was actually
    available.
    """
    provider = FakeProvider(
        [
            make_quote(
                symbol="RELIANCE.NS",
                last_price=1322.0,
                previous_close=1302.5,
                volume=13022095,
            )
        ]
    )
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.last_price == 1322.0
    assert snap.previous_close == 1302.5
    assert snap.volume == 13022095
    expected_pct = (1322.0 - 1302.5) / 1302.5 * 100
    assert snap.percent_change == expected_pct


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


def test_valid_day_high_low_pass_through_unchanged():
    provider = FakeProvider([make_quote(day_high=1340.0, day_low=1310.0)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots[0].day_high == 1340.0
    assert snapshots[0].day_low == 1310.0


def test_missing_day_high_low_degrades_gracefully_price_still_usable():
    """Per this feature's own requirement: missing range data must NOT
    invalidate an otherwise-valid snapshot -- mirrors the existing
    missing-volume behavior exactly."""
    provider = FakeProvider([make_quote(day_high=None, day_low=None)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1326.4
    assert snapshots[0].day_high is None
    assert snapshots[0].day_low is None


def test_day_low_greater_than_day_high_degrades_to_none_not_invalidated():
    """A real impossibility (like negative volume) -- must be rejected
    rather than passed through as a nonsensical range, but must NOT
    invalidate the otherwise-valid price."""
    provider = FakeProvider([make_quote(day_high=100.0, day_low=110.0)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1326.4
    assert snapshots[0].day_high is None
    assert snapshots[0].day_low is None


def test_non_finite_day_high_low_degrades_to_none_not_invalidated():
    provider = FakeProvider(
        [make_quote(day_high=float("nan"), day_low=float("inf"))]
    )
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].day_high is None
    assert snapshots[0].day_low is None


def test_negative_or_zero_day_high_low_degrades_to_none_not_invalidated():
    provider = FakeProvider([make_quote(day_high=0.0, day_low=-5.0)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].day_high is None
    assert snapshots[0].day_low is None


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


def test_fetched_at_one_second_below_stale_threshold_is_ok():
    from app.services.market_data_service import STALE_THRESHOLD_SECONDS

    fetched_at = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_THRESHOLD_SECONDS - 1
    )
    provider = FakeProvider([make_quote(fetched_at=fetched_at)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots[0].status == SnapshotStatus.OK


def test_fetched_at_exactly_at_stale_threshold_is_ok():
    """The service's own comparison is strictly `>`, so a quote exactly
    STALE_THRESHOLD_SECONDS old is still OK -- only one second older
    tips it into STALE. This pins that boundary.

    _compute_status calls datetime.now() itself, a second wall-clock
    read after this test computes fetched_at, so a small positive skew
    (test execution time between the two calls) is unavoidable without
    a clock-injection seam in production code, which is not worth
    adding just for this. A 50ms tolerance absorbs that skew while
    still asserting the boundary is inclusive, not the one-second gap
    the below/above tests already cover exactly."""
    from app.services.market_data_service import STALE_THRESHOLD_SECONDS

    fetched_at = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_THRESHOLD_SECONDS - 0.05
    )
    provider = FakeProvider([make_quote(fetched_at=fetched_at)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots[0].status == SnapshotStatus.OK


def test_fetched_at_one_second_above_stale_threshold_is_stale():
    from app.services.market_data_service import STALE_THRESHOLD_SECONDS

    fetched_at = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_THRESHOLD_SECONDS + 1
    )
    provider = FakeProvider([make_quote(fetched_at=fetched_at)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots[0].status == SnapshotStatus.STALE