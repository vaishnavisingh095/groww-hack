from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models.market_snapshot import SnapshotStatus
from app.providers.base import MarketDataProvider, RawQuote
from app.services.market_data_service import (
    STALE_THRESHOLD_SECONDS,
    MarketDataService,
)


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


def test_bar_timestamp_survives_raw_quote_to_market_snapshot():
    """(Focused regression, market-bar timestamp propagation milestone.)
    bar_timestamp must pass through _assemble_snapshot unchanged,
    tzinfo included -- never recomputed, never fabricated from
    fetched_at."""
    bar_timestamp = datetime(2026, 9, 4, 9, 17, tzinfo=ZoneInfo("Asia/Kolkata"))
    provider = FakeProvider([make_quote(bar_timestamp=bar_timestamp)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].bar_timestamp == bar_timestamp
    assert snapshots[0].bar_timestamp.utcoffset() == timedelta(hours=5, minutes=30)


def test_missing_bar_timestamp_degrades_gracefully_price_still_usable():
    """Per this field's own contract: a missing bar_timestamp must NOT
    invalidate an otherwise-valid snapshot -- mirrors day_high/day_low's
    existing degrade-gracefully behavior."""
    provider = FakeProvider([make_quote(bar_timestamp=None)])
    service = MarketDataService(provider)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1326.4
    assert snapshots[0].bar_timestamp is None


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


# --- Last-known-good snapshot persistence + fallback ------------------
#
# These tests exercise the optional `db` argument to MarketDataService:
# a successful fetch persists into market_snapshots, and a later failed
# fetch for the same instrument falls back to that persisted document
# (reported as STALE) instead of vanishing outright. Every test above
# this section constructs MarketDataService with NO db, and continues
# to pass unchanged -- confirming persistence/fallback is strictly
# additive, opt-in behavior.


def test_successful_fetch_is_persisted_to_market_snapshots(mock_db):
    provider = FakeProvider([make_quote()])
    service = MarketDataService(provider, mock_db)

    service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    doc = mock_db.market_snapshots.find_one({"instrument_id": "inst123"})
    assert doc is not None
    assert doc["last_price"] == 1326.4
    assert doc["status"] == SnapshotStatus.OK.value


def test_persisted_snapshot_round_trips_bar_timestamp(mock_db):
    """(Focused regression, market-bar timestamp propagation milestone.)
    bar_timestamp must survive the existing Mongo persist/read cycle
    (model_dump(mode="json") -> Mongo -> MarketSnapshot(**doc)) with no
    migration or new persistence mechanism -- this is the SAME generic
    round-trip every other field already gets, exercised here via the
    stale-fallback read path, which reconstructs a MarketSnapshot from a
    persisted document."""
    bar_timestamp = datetime(2026, 9, 4, 9, 17, tzinfo=ZoneInfo("Asia/Kolkata"))
    good_provider = FakeProvider([make_quote(bar_timestamp=bar_timestamp)])
    MarketDataService(good_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    doc = mock_db.market_snapshots.find_one({"instrument_id": "inst123"})
    assert doc["bar_timestamp"] == bar_timestamp.isoformat()  # stored as ISO-8601, offset included

    failing_provider = FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    fallback = MarketDataService(failing_provider, mock_db).fetch_snapshots(
        {"RELIANCE.NS": "inst123"}
    )

    assert len(fallback) == 1
    assert fallback[0].bar_timestamp == bar_timestamp
    assert fallback[0].bar_timestamp.utcoffset() == timedelta(hours=5, minutes=30)


def test_provider_failure_with_prior_valid_snapshot_returns_stale_fallback(mock_db):
    """(b) A previously-persisted valid snapshot is served, marked
    STALE, when this cycle's fetch fails entirely."""
    good_provider = FakeProvider([make_quote(last_price=1326.4)])
    MarketDataService(good_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    failing_provider = FakeProvider(
        [make_quote(fetch_succeeded=False, last_price=None)]
    )
    service = MarketDataService(failing_provider, mock_db)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].instrument_id == "inst123"
    assert snapshots[0].last_price == 1326.4
    assert snapshots[0].status == SnapshotStatus.STALE


def test_provider_failure_with_no_saved_snapshot_remains_unavailable(mock_db):
    """(c) Same failure, but nothing was ever persisted for this
    instrument -- must degrade to "no snapshot" exactly as it did
    before this feature existed (the caller reports unavailable)."""
    failing_provider = FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    service = MarketDataService(failing_provider, mock_db)

    snapshots = service.fetch_snapshots({"RELIANCE.NS": "inst123"})

    assert snapshots == []


def test_invalid_provider_data_does_not_overwrite_saved_snapshot(mock_db):
    """(d) A good snapshot is persisted; a later cycle's invalid quote
    (e.g. zero previous_close) must not touch the stored document --
    the next failure still falls back to the ORIGINAL good values."""
    good_provider = FakeProvider([make_quote(last_price=1326.4, previous_close=1302.6)])
    MarketDataService(good_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    invalid_provider = FakeProvider([make_quote(previous_close=0.0)])
    MarketDataService(invalid_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    doc = mock_db.market_snapshots.find_one({"instrument_id": "inst123"})
    assert doc["last_price"] == 1326.4
    assert doc["previous_close"] == 1302.6

    failing_provider = FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    fallback = MarketDataService(failing_provider, mock_db).fetch_snapshots(
        {"RELIANCE.NS": "inst123"}
    )
    assert fallback[0].last_price == 1326.4
    assert fallback[0].previous_close == 1302.6


def test_provider_failure_never_deletes_saved_snapshot(mock_db):
    """(5) A provider failure must never delete/corrupt the persisted
    document -- it must still be readable (and usable as a fallback)
    after any number of subsequent failed cycles."""
    good_provider = FakeProvider([make_quote(last_price=1326.4)])
    MarketDataService(good_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    failing_provider = FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    for _ in range(3):
        MarketDataService(failing_provider, mock_db).fetch_snapshots(
            {"RELIANCE.NS": "inst123"}
        )

    doc = mock_db.market_snapshots.find_one({"instrument_id": "inst123"})
    assert doc is not None
    assert doc["last_price"] == 1326.4


def test_fresh_valid_data_replaces_previous_snapshot(mock_db):
    """(6) Fresh, valid provider data DOES replace the previous
    snapshot -- persistence is a last-known-good cache, not a one-time
    write. A later successful fetch with a new price must overwrite the
    old one, so a subsequent failure falls back to the NEWEST good
    value, not the original."""
    first_provider = FakeProvider([make_quote(last_price=1326.4)])
    MarketDataService(first_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    second_provider = FakeProvider([make_quote(last_price=1350.0)])
    MarketDataService(second_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    failing_provider = FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    fallback = MarketDataService(failing_provider, mock_db).fetch_snapshots(
        {"RELIANCE.NS": "inst123"}
    )
    assert fallback[0].last_price == 1350.0


def test_stale_fallback_age_is_calculated_from_saved_snapshot_timestamp(mock_db):
    """(f) The fallback's fetched_at must be the ORIGINAL persisted
    fetch time, not "now" -- that original timestamp is what the route
    layer's _status_label uses to compute how old the data really is.

    The initial fetch must be fresh (fetched_at="now") to actually get
    persisted at all -- _persist_snapshot only ever saves a snapshot
    whose own computed status is OK, and a quote fetched >120s ago would
    already compute as STALE at assembly time. So elapsed time is
    simulated the way it really occurs: by backdating the ALREADY
    PERSISTED document directly, not the quote that produced it."""
    good_provider = FakeProvider([make_quote()])
    MarketDataService(good_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    old_fetched_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    mock_db.market_snapshots.update_one(
        {"instrument_id": "inst123"},
        {"$set": {"fetched_at": old_fetched_at.isoformat()}},
    )

    failing_provider = FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    fallback = MarketDataService(failing_provider, mock_db).fetch_snapshots(
        {"RELIANCE.NS": "inst123"}
    )

    assert len(fallback) == 1
    age = (datetime.now(timezone.utc) - fallback[0].fetched_at).total_seconds()
    assert 590 <= age <= 610  # ~10 minutes, not "now"


def test_missing_quote_entirely_still_falls_back_to_saved_snapshot(mock_db):
    """A provider that omits a requested symbol from its response
    altogether (not even a failed RawQuote) must be treated the same as
    an explicit failure for fallback purposes."""
    good_provider = FakeProvider([make_quote()])
    MarketDataService(good_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    empty_provider = FakeProvider([])  # no quote at all for RELIANCE.NS
    fallback = MarketDataService(empty_provider, mock_db).fetch_snapshots(
        {"RELIANCE.NS": "inst123"}
    )

    assert len(fallback) == 1
    assert fallback[0].status == SnapshotStatus.STALE


def test_no_db_supplied_skips_persistence_and_fallback_entirely(mock_db):
    """Backward-compatibility guard: MarketDataService constructed
    without a db (as every test above this section, and
    watchlist_service.add_instrument's provider-existence check, do)
    must neither persist nor fall back -- exact previous behavior."""
    good_provider = FakeProvider([make_quote()])
    MarketDataService(good_provider, None).fetch_snapshots({"RELIANCE.NS": "inst123"})
    assert mock_db.market_snapshots.count_documents({}) == 0

    failing_provider = FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    snapshots = MarketDataService(failing_provider).fetch_snapshots(
        {"RELIANCE.NS": "inst123"}
    )
    assert snapshots == []


# --- get_snapshots: cache-first read path for GET /watchlist ----------
#
# fetch_snapshots always calls the provider. get_snapshots (used only by
# GET /watchlist) skips the provider call entirely when a fresh-enough
# persisted snapshot already exists, and otherwise delegates to
# fetch_snapshots unchanged -- so persist-on-success and stale-fallback-
# on-failure are exercised through the exact same code already tested
# above, not reimplemented.


class CountingProvider(MarketDataProvider):
    """Wraps another provider and counts get_quotes calls, so a test can
    assert the provider was (or was NOT) actually invoked -- the whole
    point of get_snapshots is to skip that call when possible."""

    def __init__(self, inner: MarketDataProvider):
        self._inner = inner
        self.call_count = 0

    def get_quotes(self, symbols: list[str]) -> list[RawQuote]:
        self.call_count += 1
        return self._inner.get_quotes(symbols)


def test_fresh_persisted_snapshot_avoids_provider_call(mock_db):
    seed_provider = FakeProvider([make_quote(last_price=1326.4)])
    MarketDataService(seed_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    counting_provider = CountingProvider(FakeProvider([make_quote(last_price=9999.0)]))
    service = MarketDataService(counting_provider, mock_db)

    snapshots = service.get_snapshots({"RELIANCE.NS": "inst123"})

    assert counting_provider.call_count == 0
    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1326.4  # the persisted value, not the provider's
    assert snapshots[0].status == SnapshotStatus.OK


def test_stale_persisted_snapshot_triggers_provider_fetch(mock_db):
    seed_provider = FakeProvider([make_quote(last_price=1326.4)])
    MarketDataService(seed_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    old_fetched_at = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_THRESHOLD_SECONDS + 1
    )
    mock_db.market_snapshots.update_one(
        {"instrument_id": "inst123"},
        {"$set": {"fetched_at": old_fetched_at.isoformat()}},
    )

    counting_provider = CountingProvider(FakeProvider([make_quote(last_price=1350.0)]))
    service = MarketDataService(counting_provider, mock_db)

    snapshots = service.get_snapshots({"RELIANCE.NS": "inst123"})

    assert counting_provider.call_count == 1
    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1350.0
    assert snapshots[0].status == SnapshotStatus.OK


def test_successful_fetch_after_stale_replaces_persisted_snapshot(mock_db):
    seed_provider = FakeProvider([make_quote(last_price=1326.4)])
    MarketDataService(seed_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    old_fetched_at = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_THRESHOLD_SECONDS + 1
    )
    mock_db.market_snapshots.update_one(
        {"instrument_id": "inst123"},
        {"$set": {"fetched_at": old_fetched_at.isoformat()}},
    )

    fresh_provider = FakeProvider([make_quote(last_price=1350.0)])
    MarketDataService(fresh_provider, mock_db).get_snapshots({"RELIANCE.NS": "inst123"})

    doc = mock_db.market_snapshots.find_one({"instrument_id": "inst123"})
    assert doc["last_price"] == 1350.0
    assert doc["status"] == SnapshotStatus.OK.value


def test_provider_failure_after_stale_returns_last_known_good_as_stale(mock_db):
    seed_provider = FakeProvider([make_quote(last_price=1326.4)])
    MarketDataService(seed_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    old_fetched_at = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_THRESHOLD_SECONDS + 1
    )
    mock_db.market_snapshots.update_one(
        {"instrument_id": "inst123"},
        {"$set": {"fetched_at": old_fetched_at.isoformat()}},
    )

    counting_provider = CountingProvider(
        FakeProvider([make_quote(fetch_succeeded=False, last_price=None)])
    )
    service = MarketDataService(counting_provider, mock_db)

    snapshots = service.get_snapshots({"RELIANCE.NS": "inst123"})

    assert counting_provider.call_count == 1  # a live fetch WAS attempted
    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1326.4  # last-known-good, unchanged
    assert snapshots[0].status == SnapshotStatus.STALE

    # The stale persisted document itself is untouched by the failure.
    doc = mock_db.market_snapshots.find_one({"instrument_id": "inst123"})
    assert doc["last_price"] == 1326.4


def test_invalid_provider_data_after_stale_does_not_overwrite_persisted_snapshot(mock_db):
    seed_provider = FakeProvider(
        [make_quote(last_price=1326.4, previous_close=1302.6)]
    )
    MarketDataService(seed_provider, mock_db).fetch_snapshots({"RELIANCE.NS": "inst123"})

    old_fetched_at = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_THRESHOLD_SECONDS + 1
    )
    mock_db.market_snapshots.update_one(
        {"instrument_id": "inst123"},
        {"$set": {"fetched_at": old_fetched_at.isoformat()}},
    )

    invalid_provider = FakeProvider([make_quote(previous_close=0.0)])
    service = MarketDataService(invalid_provider, mock_db)

    snapshots = service.get_snapshots({"RELIANCE.NS": "inst123"})

    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1326.4
    assert snapshots[0].status == SnapshotStatus.STALE

    doc = mock_db.market_snapshots.find_one({"instrument_id": "inst123"})
    assert doc["last_price"] == 1326.4
    assert doc["previous_close"] == 1302.6


def test_missing_persisted_snapshot_attempts_provider_fetch_via_get_snapshots(mock_db):
    """No persisted document at all -- same as "stale," a live fetch
    must still be attempted rather than silently reporting nothing."""
    counting_provider = CountingProvider(FakeProvider([make_quote(last_price=1326.4)]))
    service = MarketDataService(counting_provider, mock_db)

    snapshots = service.get_snapshots({"RELIANCE.NS": "inst123"})

    assert counting_provider.call_count == 1
    assert len(snapshots) == 1
    assert snapshots[0].last_price == 1326.4


def test_get_snapshots_with_no_db_falls_back_to_fetch_snapshots_behavior(mock_db):
    """No db supplied: get_snapshots must behave exactly like
    fetch_snapshots (always call the provider) -- same backward-
    compatibility guarantee as fetch_snapshots itself."""
    counting_provider = CountingProvider(FakeProvider([make_quote(last_price=1326.4)]))
    service = MarketDataService(counting_provider, None)

    snapshots = service.get_snapshots({"RELIANCE.NS": "inst123"})

    assert counting_provider.call_count == 1
    assert len(snapshots) == 1
    assert mock_db.market_snapshots.count_documents({}) == 0
