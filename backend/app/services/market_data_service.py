"""
Market Data Service.

Owns the translation from a provider's RawQuote into a persisted, valid
MarketSnapshot -- this is exactly the "assembly policy" boundary flagged
in Phase 1's test_market_snapshot.py as deliberately out of scope for the
model itself. This module is that boundary.

Responsibilities (per architecture.md):
- Compute percent_change ourselves (never trust a provider field).
- Compute `status` from OUR OWN fetched_at, never from provider_timestamp.
- Reject invalid prices; degrade gracefully on invalid/missing volume
  (price can still be usable even if volume isn't).
- Never let a provider failure raise into a caller.
"""
from datetime import datetime, timezone

from app.models.market_snapshot import MarketSnapshot, SnapshotStatus
from app.providers.base import MarketDataProvider, RawQuote

# architecture.md: "stale" is roughly 2x the poll/refresh interval.
# For this first slice there's no background poll loop yet (data is
# fetched on-demand per API call -- see the routes layer), so this
# threshold governs how old a PERSISTED snapshot can be before a fresh
# fetch attempt is considered overdue, not a poll-cycle multiple.
STALE_THRESHOLD_SECONDS = 120


class MarketDataService:
    def __init__(self, provider: MarketDataProvider):
        self._provider = provider

    def fetch_snapshots(self, symbol_to_instrument_id: dict[str, str]) -> list[MarketSnapshot]:
        """
        Fetch current data for the given symbols and return valid,
        assembled MarketSnapshot objects.

        symbol_to_instrument_id maps a yfinance-ready ticker string
        (e.g. "RELIANCE.NS") to our own instrument_id, since RawQuote
        only knows the provider's symbol string, not our internal id.

        Returns one MarketSnapshot per symbol that produced a usable
        price. A symbol that fails entirely (no usable price) is
        SKIPPED here, not included as a broken document -- the caller
        (the route/service that persists these) is responsible for
        deciding what happens to an instrument with no new snapshot
        this cycle (i.e., keep serving its last-known-good snapshot,
        per architecture.md's failure table). This function's only job
        is "give me the valid snapshots you could get," not "explain
        what to do about the ones you couldn't."
        """
        symbols = list(symbol_to_instrument_id.keys())
        raw_quotes = self._provider.get_quotes(symbols)

        snapshots: list[MarketSnapshot] = []
        for quote in raw_quotes:
            instrument_id = symbol_to_instrument_id.get(quote.symbol)
            if instrument_id is None:
                continue  # defensive: provider returned an unrequested symbol

            snapshot = self._assemble_snapshot(quote, instrument_id)
            if snapshot is not None:
                snapshots.append(snapshot)

        return snapshots

    def _assemble_snapshot(self, quote: RawQuote, instrument_id: str) -> MarketSnapshot | None:
        if not quote.fetch_succeeded or quote.last_price is None or quote.previous_close is None:
            # No usable price -> no snapshot at all. Per architecture.md,
            # a missing/invalid PRICE invalidates the whole update; there
            # is nothing to persist as "invalid" here in the sense of a
            # bad-but-present value -- we simply have nothing new.
            return None

        percent_change = MarketSnapshot.compute_percent_change(
            quote.last_price, quote.previous_close
        )

        # Volume: missing/bad volume degrades gracefully (does not
        # invalidate price), per architecture.md's Invalid Data Rules.
        # For THIS model, volume is a required field (Phase 1 decision,
        # documented in test_market_snapshot.py), so "volume unavailable"
        # is represented by falling back to 0 combined with a status that
        # a later Change Engine phase can treat specially. For this
        # slice (price-only meaningful change), no caller reads volume
        # for signal purposes yet, so this fallback is safe and simple
        # rather than premature.
        volume = quote.volume if quote.volume is not None and quote.volume >= 0 else 0

        status = self._compute_status(quote.fetched_at)

        return MarketSnapshot(
            instrument_id=instrument_id,
            last_price=quote.last_price,
            previous_close=quote.previous_close,
            percent_change=percent_change,
            volume=volume,
            session_date=quote.fetched_at.date(),
            fetched_at=quote.fetched_at,
            provider_timestamp=quote.provider_timestamp,
            status=status,
        )

    @staticmethod
    def _compute_status(fetched_at: datetime) -> SnapshotStatus:
        """
        Status computed from OUR OWN fetched_at, per architecture.md and
        decisions.md -- provider_timestamp is never used here.
        """
        age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age_seconds > STALE_THRESHOLD_SECONDS:
            return SnapshotStatus.STALE
        return SnapshotStatus.OK
