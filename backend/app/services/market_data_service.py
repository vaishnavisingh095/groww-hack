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
import math
from datetime import datetime, timezone

from pymongo.database import Database

from app.models.market_snapshot import MarketSnapshot, SnapshotStatus
from app.providers.base import MarketDataProvider, RawQuote

# architecture.md: "stale" is roughly 2x the poll/refresh interval.
# For this first slice there's no background poll loop yet (data is
# fetched on-demand per API call -- see the routes layer), so this
# threshold governs how old a PERSISTED snapshot can be before a fresh
# fetch attempt is considered overdue, not a poll-cycle multiple.
STALE_THRESHOLD_SECONDS = 120


class MarketDataService:
    def __init__(self, provider: MarketDataProvider, db: Database | None = None):
        self._provider = provider
        # Optional: last-known-good persistence/fallback is only active
        # when a db handle is supplied. Callers that don't need it (e.g.
        # watchlist_service.add_instrument's provider-existence check,
        # which has no real instrument_id yet to key a snapshot by --
        # see decisions.md) construct this without a db and get the
        # exact previous, persistence-free behavior.
        self._db = db

    def get_snapshots(self, symbol_to_instrument_id: dict[str, str]) -> list[MarketSnapshot]:
        """
        Read-path entry point for GET /watchlist: serve a persisted
        snapshot directly, WITHOUT calling the provider at all, whenever
        that persisted snapshot is still fresh enough (age <=
        STALE_THRESHOLD_SECONDS) to count as a valid current observation
        -- this is the only difference from fetch_snapshots, and it only
        applies when a db was supplied.

        For every symbol whose persisted snapshot is missing or already
        stale, this falls through to fetch_snapshots for exactly that
        subset -- so a live fetch attempt, persist-on-success, and
        last-known-good-fallback-on-failure all behave identically to
        the always-fetch path below; nothing about persist/fallback
        semantics is duplicated or reimplemented here.

        Checkpoint-creating callers (mark_as_seen/mark_all_as_seen)
        deliberately keep calling fetch_snapshots directly, not this
        method -- "mark as seen" means "go get the current price right
        now," and must not silently serve a value already sitting in
        Mongo from a few seconds ago.
        """
        if self._db is None:
            return self.fetch_snapshots(symbol_to_instrument_id)

        cached: list[MarketSnapshot] = []
        needs_fetch: dict[str, str] = {}
        for symbol, instrument_id in symbol_to_instrument_id.items():
            fresh = self._fresh_persisted_snapshot(instrument_id)
            if fresh is not None:
                cached.append(fresh)
            else:
                needs_fetch[symbol] = instrument_id

        if needs_fetch:
            cached.extend(self.fetch_snapshots(needs_fetch))

        return cached

    def _fresh_persisted_snapshot(self, instrument_id: str) -> MarketSnapshot | None:
        """
        Return the persisted snapshot for this instrument ONLY if it is
        still fresh enough (same STALE_THRESHOLD_SECONDS boundary
        _compute_status already uses for a live fetch) to skip a
        provider call entirely. A missing or already-stale persisted
        snapshot returns None so the caller attempts a live fetch --
        this is deliberately NOT the same thing as a stale fallback
        (_fallback_snapshot), which is only ever used after a live fetch
        was actually attempted and failed.
        """
        persisted = self._read_persisted_snapshot(instrument_id)
        if persisted is None:
            return None
        if self._compute_status(persisted.fetched_at) != SnapshotStatus.OK:
            return None
        return persisted

    def fetch_snapshots(self, symbol_to_instrument_id: dict[str, str]) -> list[MarketSnapshot]:
        """
        Fetch current data for the given symbols and return MarketSnapshot
        objects: one per symbol with a fresh, valid price this cycle, plus
        -- when a db was supplied -- one last-known-good FALLBACK snapshot
        (status forced to STALE, per decisions.md) for any symbol that
        failed this cycle but has a previously-persisted valid snapshot.

        symbol_to_instrument_id maps a yfinance-ready ticker string
        (e.g. "RELIANCE.NS") to our own instrument_id, since RawQuote
        only knows the provider's symbol string, not our internal id.

        A symbol with neither a fresh valid price nor a persisted
        fallback is SKIPPED entirely (not included as a broken
        document) -- the caller reports it as unavailable, same as
        before this fallback existed.
        """
        symbols = list(symbol_to_instrument_id.keys())
        raw_quotes = self._provider.get_quotes(symbols)
        quotes_by_symbol = {quote.symbol: quote for quote in raw_quotes}

        snapshots: list[MarketSnapshot] = []
        for symbol, instrument_id in symbol_to_instrument_id.items():
            quote = quotes_by_symbol.get(symbol)
            snapshot = (
                self._assemble_snapshot(quote, instrument_id)
                if quote is not None
                else None
            )

            if snapshot is not None:
                self._persist_snapshot(snapshot)
                snapshots.append(snapshot)
                continue

            fallback = self._fallback_snapshot(instrument_id)
            if fallback is not None:
                snapshots.append(fallback)

        return snapshots

    def _persist_snapshot(self, snapshot: MarketSnapshot) -> None:
        """
        Save a freshly-assembled, valid snapshot as this instrument's
        last-known-good document. Only ever called with a snapshot that
        just passed _assemble_snapshot's validity checks -- invalid or
        failed provider data never reaches here, so it can never
        overwrite a good persisted snapshot with something worse
        (invariant: invalid data must never overwrite last-known-good).

        Upserted keyed on instrument_id, matching the existing unique
        index (uniq_instrument_id, db/indexes.py) -- one document per
        instrument, replaced in place, not an append-only history,
        exactly as that index already documents.
        """
        if self._db is None or snapshot.status != SnapshotStatus.OK:
            return
        self._db.market_snapshots.replace_one(
            {"instrument_id": snapshot.instrument_id},
            snapshot.model_dump(mode="json"),
            upsert=True,
        )

    def _fallback_snapshot(self, instrument_id: str) -> MarketSnapshot | None:
        """
        Look up this instrument's last persisted valid snapshot for use
        as a stale, display-only fallback after this cycle's fetch
        failed or produced nothing usable. A provider failure never
        deletes/mutates the stored document -- this is a read-only
        lookup.

        Always returns the snapshot with status forced to STALE,
        regardless of what it was persisted with -- a fallback is never
        a fresh observation, so it must never be reported as OK.
        """
        persisted = self._read_persisted_snapshot(instrument_id)
        if persisted is None:
            return None
        return persisted.model_copy(update={"status": SnapshotStatus.STALE})

    def _read_persisted_snapshot(self, instrument_id: str) -> MarketSnapshot | None:
        """
        Read-only lookup of this instrument's persisted document,
        reconstructed as-is (status exactly as stored -- always `ok`,
        since _persist_snapshot only ever writes an `ok` snapshot).
        Shared by _fallback_snapshot (forces STALE) and
        _fresh_persisted_snapshot (checks freshness) so the read/
        reconstruct logic exists in exactly one place.
        """
        if self._db is None:
            return None
        doc = self._db.market_snapshots.find_one({"instrument_id": instrument_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return MarketSnapshot(**doc)

    def _assemble_snapshot(self, quote: RawQuote, instrument_id: str) -> MarketSnapshot | None:
        if (
            not quote.fetch_succeeded
            or quote.last_price is None
            or quote.previous_close is None
            or quote.session_date is None
        ):
            # No usable price -> no snapshot at all. Per architecture.md,
            # a missing/invalid PRICE invalidates the whole update; there
            # is nothing to persist as "invalid" here in the sense of a
            # bad-but-present value -- we simply have nothing new.
            #
            # previous_close is included in this check because
            # MarketSnapshot requires a real previous_close to compute
            # percent_change (see MarketSnapshot.compute_percent_change
            # and decisions.md's "never trust a provider percent field"
            # rule) -- there is no meaningful percent_change without it.
            # A provider that cannot supply previous_close through any of
            # its own supported fallbacks (see YFinanceProvider) results
            # in no snapshot here, same as a missing price. This is NOT
            # weakened to accept a None previous_close: doing so would
            # require either fabricating a percent_change or silently
            # treating a genuinely-unavailable value as valid, both
            # explicitly disallowed.
            #
            # session_date is included for the same reason: MarketSnapshot
            # requires a real session_date (used later for the
            # same-session volume-acceleration rule), and it is only ever
            # populated by the provider from the actual bar last_price
            # came from -- never fabricated here from our own clock (see
            # decisions.md's session_date correctness fix).
            return None

        # A non-finite or non-positive last_price/previous_close must
        # never reach the division below or the MarketSnapshot
        # constructor: a zero previous_close divides by zero (an
        # unhandled ZeroDivisionError, raised before MarketSnapshot's
        # own gt=0 validation ever runs), and a non-positive/non-finite
        # value that survives the division still raises an unhandled
        # pydantic.ValidationError at construction. Either way, before
        # this check, ONE instrument's malformed quote could abort this
        # entire fetch_snapshots call -- the surrounding for-loop has no
        # per-quote try/except -- silently taking down every OTHER
        # instrument's snapshot in the same batch too. Reusing this
        # function's own existing "no usable price -> no snapshot"
        # contract (the None-checks above) for this case keeps the
        # failure scoped to this one instrument, exactly like a missing
        # price already is. The real yfinance provider already guards
        # last_price this way when scanning bars (see
        # YFinanceProvider._latest_valid_close), but this assembly
        # boundary should not rely on any one MarketDataProvider
        # implementation being that careful.
        if (
            not math.isfinite(quote.last_price)
            or quote.last_price <= 0
            or not math.isfinite(quote.previous_close)
            or quote.previous_close <= 0
        ):
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

        # day_high/day_low: same degrade-gracefully treatment as volume,
        # for the same reason (feeds a downstream signal -- the adaptive
        # price threshold -- that already has its own fallback for a
        # missing/invalid range; must never invalidate the price itself).
        # day_low > day_high is a real impossibility (like negative
        # volume), so it is rejected the same way rather than passed
        # through as a nonsensical range.
        day_high = self._valid_finite_positive(quote.day_high)
        day_low = self._valid_finite_positive(quote.day_low)
        if day_high is not None and day_low is not None and day_low > day_high:
            day_high, day_low = None, None

        status = self._compute_status(quote.fetched_at)

        return MarketSnapshot(
            instrument_id=instrument_id,
            last_price=quote.last_price,
            previous_close=quote.previous_close,
            percent_change=percent_change,
            volume=volume,
            session_date=quote.session_date,
            bar_timestamp=quote.bar_timestamp,
            day_high=day_high,
            day_low=day_low,
            fetched_at=quote.fetched_at,
            provider_timestamp=quote.provider_timestamp,
            status=status,
        )

    @staticmethod
    def _valid_finite_positive(value: float | None) -> float | None:
        """None-safe finite-and-positive check, shared by the
        day_high/day_low degrade-gracefully handling above."""
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        return value

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