# Architecture: Smart Market Watchlist

## System Components & Responsibilities

| Component | Responsibility | Owns |
|---|---|---|
| Watchlist Service | CRUD for user watchlists and instrument membership | Watchlist documents |
| Market Data Service | Talks to the external provider, runs the shared poll loop, maintains the current-snapshot cache, computes freshness | MarketSnapshot documents |
| Checkpoint Service | Creates/advances user checkpoints (explicit + implicit) | Checkpoint documents |
| Meaningful Change Engine | Compares a checkpoint's baseline snapshot to the current snapshot using fixed deterministic rules; persists resulting events | ChangeEvent documents |
| Attention Engine | Ranks active (unacknowledged) change events per user; generates explanations | Nothing persisted — pure computation over ChangeEvents |

No queues, no message bus, no microservices — all five are modules within
one FastAPI application. See `decisions.md` for why.

## Data Flow

```
[External Provider] --(batched poll, every 60s)--> [Market Data Service]
                                                            |
                                                     writes/updates
                                                            v
                                                   [MarketSnapshot] (Mongo)
                                                            |
                    [Checkpoint] (Mongo) ---compare---> [Meaningful Change Engine]
                                                            |
                                                     writes (if changed)
                                                            v
                                                    [ChangeEvent] (Mongo)
                                                            |
                                              read at request time
                                                            v
                                                    [Attention Engine]
                                                            |
                                                     ranked + explained
                                                            v
                                                   [Frontend: GET /watchlist/{id}]
```

User-initiated writes (add to watchlist, mark checkpoint) go directly to
their respective services; they don't flow through the poll loop.

## API Boundaries

- Frontend never talks to the external market-data provider directly —
  always through the backend, so freshness/staleness logic is enforced in
  one place.
- The poll loop is the only writer of `MarketSnapshot`. Request-time reads
  never trigger a live provider call (this is what keeps the fan-out
  bounded — see `decisions.md`).
- The Meaningful Change Engine only runs comparisons when a checkpoint
  exists; it never invents a baseline.

## Data Ownership / Source of Truth

- **Instrument identity & metadata**: our own `Instrument` collection is
  the source of truth for what's trackable; the provider is the source of
  truth for price/volume values only.
- **Current market state**: `MarketSnapshot` — always the latest fetched
  value per instrument, overwritten each poll cycle. This is a cache of
  external truth, not our own truth.
- **What the user has seen**: `Checkpoint` — entirely our own state, never
  derived from the provider.
- **What changed**: `ChangeEvent` — computed once by us, then persisted as
  fact (so re-computation doesn't silently change history — see hard
  question H).
- **Attention ranking**: never persisted as its own source of truth — it's
  fully derivable from active `ChangeEvent`s at any moment, so persisting
  it would create a second, potentially-stale copy of derivable data.

## Data Model

### Instrument
```
{
  _id,
  symbol,            # e.g. "RELIANCE"
  exchange,          # "NSE" | "BSE"
  company_name,
  created_at
}
```
Mostly static reference data. Created the first time any user adds the
symbol to a watchlist.

### Watchlist
```
{
  _id,
  user_id,
  instrument_ids: [ObjectId, ...],
  created_at,
  updated_at
}
```
One watchlist per user for the hackathon scope (per CUT: no multi-list
support in MUST HAVE).

### MarketSnapshot
```
{
  _id,
  instrument_id,
  last_price,
  percent_change,        # computed by us: (last_price - previous_close)
                          # / previous_close * 100 — see Percent Change
                          # Computation below. Never taken from an
                          # unverified provider field.
  previous_close,         # needed to compute percent_change ourselves
  volume,                 # cumulative session volume — see Volume Field
                          # Mapping below
  session_date,           # the trading date (IST) this snapshot's
                          # cumulative volume belongs to — required to
                          # detect session boundaries (see Same-Session
                          # Volume Semantics)
  fetched_at,              # OUR OWN timestamp, when our poll loop
                           # received this data — THE authoritative
                           # timestamp for all freshness/staleness
                           # calculations
  provider_timestamp,      # raw value from the provider's
                           # regularMarketTime field, stored for
                           # diagnostics ONLY. Its exact semantic meaning
                           # is NOT independently verified — observed
                           # behavior (populated, decoded value close to
                           # our own fetch time, changes between polls)
                           # makes it unsuitable as a freshness or trade-
                           # time signal. NEVER used to compute status,
                           # NEVER displayed to the user as an exchange
                           # trade timestamp.
  status: "ok" | "stale" | "invalid" | "unavailable"
}
```
One document per instrument, upserted every poll cycle (not an
append-only history — see `decisions.md` on why we don't keep tick
history for the hackathon scope).

`status` is computed at write time from `fetched_at` (see Freshness Model
below), not left for the reader to infer, and never from
`provider_timestamp`.

### Checkpoint
```
{
  _id,
  user_id,
  instrument_id,
  checkpoint_at,          # OUR OWN timestamp, when this checkpoint was
                           # set (explicit action or implicit creation)
  session_date,            # the trading date (IST) the baseline
                           # snapshot's volume belongs to — copied from
                           # the MarketSnapshot at checkpoint time,
                           # required to detect session boundaries later
  baseline_snapshot: {    # copy of the MarketSnapshot values at
    last_price,           # checkpoint time — frozen, not a live reference
    volume,
    percent_change
  },
  source: "explicit" | "implicit"
}
```
One active checkpoint per (user, instrument) pair — advancing the
checkpoint replaces the previous one. We copy the baseline values into the
checkpoint itself (rather than referencing a MarketSnapshot _id) because
MarketSnapshot is overwritten every poll cycle — the checkpoint needs a
frozen historical baseline, not a pointer to a mutable document.

### ChangeEvent
```
{
  _id,
  user_id,
  instrument_id,
  checkpoint_id,        # which checkpoint this was detected against
  detected_at,
  signals: {
    price_change_pct,
    volume_acceleration_ratio,   # numeric value, OR null if unavailable
    volume_acceleration_available: bool   # false when checkpoint and
                                            # current snapshot are from
                                            # different trading sessions
                                            # — see Same-Session Volume
                                            # Semantics
  },
  reason: "<human-readable explanation string>",
  acknowledged: bool     # true once the user's checkpoint advances past it
}
```
Created once per detected change per checkpoint period. Not recreated on
every page refresh — this is the mechanism that answers hard question H.
`volume_acceleration_available` is explicit rather than inferring
"unavailable" from a null value elsewhere, so downstream code and the UI
never have to guess why the field is absent.

## Meaningful Change Engine — Design

**Goal**: decide, deterministically and explainably, whether the move from
a checkpoint's baseline to the current snapshot is "meaningful," using two
signals: price movement and volume anomaly.

**Signal 1 — Price movement**
```
price_change_pct = (current.last_price - baseline.last_price) / baseline.last_price * 100
```
Threshold: `abs(price_change_pct) >= PRICE_THRESHOLD_PCT`

`last_price` is sourced per the confirmed Provider Field Mapping (see
External Dependency section below): `fast_info.last_price`, with
`info.regularMarketPrice` as fallback. Price comparison is valid **across
sessions** as well as within one — a checkpoint from a prior trading day
can still be meaningfully compared against today's price. This is
explicitly different from the volume signal below.

**`percent_change` is computed by us, not taken from the provider.**
`percent_change = (current.last_price - current.previous_close) /
current.previous_close * 100`, using `fast_info.previous_close`. We do
not depend on any unverified provider-supplied percent-change field —
this keeps the number we display fully traceable to two values we
explicitly fetched and can verify ourselves.

**Signal 2 — Volume anomaly (revised after explicit review, and further
corrected for session boundaries)**

The naive `current_volume / checkpoint_volume` ratio was reviewed and
**rejected** — see `decisions.md` for the full comparison of three
options. NSE/BSE volume is cumulative from market open, so a raw ratio
is dominated by elapsed time between checkpoint and now, not by
anomalous trading. A checkpoint set shortly after market open compared
against a check late in the day would show a large ratio on every stock,
every day, regardless of anything unusual happening — this would be a
confidently wrong signal, not just an imprecise one.

**Chosen definition: volume acceleration (rate-based), self-relative,
same trading session only.**

```
minutes_since_open_to_checkpoint = checkpoint.checkpoint_at - market_open_time
minutes_since_checkpoint         = now - checkpoint.checkpoint_at

rate_before  = baseline.volume / minutes_since_open_to_checkpoint
rate_after   = (current.volume - baseline.volume) / minutes_since_checkpoint

volume_acceleration_ratio = rate_after / rate_before
```

Threshold: `volume_acceleration_ratio >= VOLUME_RATIO_THRESHOLD`

This compares the *rate* of volume accumulation before and after the
checkpoint, both measured in shares/minute, which cancels out the
elapsed-time confound entirely. `market_open_time` is a known constant
for NSE/BSE (9:15 AM IST) — no historical dataset is required, only the
two snapshots we already have plus a fixed constant.

**Volume field mapping (confirmed by live test):** `volume` is sourced
from `fast_info.last_volume`, with `info.regularMarketVolume` as
fallback. A live investigation against all 5 target NSE symbols
confirmed this field is cumulative and monotonically increasing within a
session — verified by observing it increase by realistic, positive
amounts (thousands to tens of thousands of shares) across a ~45-second
gap between two live fetches, for every tested symbol.
`history(interval="1m")`'s per-bar `Volume` field is explicitly **not**
used for this signal — the same live test observed it return zero for
nearly every symbol/bar, which remains an unresolved, unexplained
provider/data-detail question (see External Dependency section) and is
kept entirely out of the MVP signal path rather than guessed at.

**REQUIRED CORRECTION — Same-Session Volume Semantics.** Volume
acceleration is only valid when `baseline.session_date` (from the
checkpoint) equals `current.session_date` (from the current snapshot) —
i.e., both belong to the same trading day. Cumulative volume resets at
the start of each trading session; subtracting a prior session's
cumulative volume from today's would produce a meaningless, arbitrarily
large "delta" driven entirely by the reset, not by trading activity, and
would misleadingly imply the same kind of false signal the original
naive ratio produced.

**Rule**: if `baseline.session_date != current.session_date` (i.e., the
checkpoint and current snapshot belong to different trading sessions):
- `volume_acceleration_available = false`, `volume_acceleration_ratio =
  null` for this comparison.
- Price comparison (`price_change_pct`) is **still computed normally** —
  only the volume signal is suppressed, not the whole comparison.
- The Change Engine may still flag a meaningful change on the price
  signal alone; the explanation in that case simply omits any volume
  claim rather than fabricating one.

**Session recognition**: the system must establish which trading session
a snapshot belongs to before using cumulative-volume deltas. For the
hackathon MVP, `session_date` is derived from `fetched_at` converted to
IST calendar date at write time — a snapshot fetched at any point during
NSE's trading day (9:15 AM–3:30 PM IST) is stamped with that IST
calendar date. This is a simple, defensible rule for the MVP; it does not
attempt to model partial/special sessions, and non-trading-day detection
beyond calendar-date comparison is explicitly not attempted (see
Unresolved / Out of Scope below).

**Near-market-open guard (required, untested edge case).** The live
investigation ran during mid-session hours and did not observe a
checkpoint created very close to 9:15 AM IST. `rate_before` divides by
`minutes_since_open_to_checkpoint`, which could be very small (or, at
the instant of market open, zero) for a checkpoint set immediately after
open. The implementation must guard against division by zero or an
unreasonably tiny denominator — e.g., treat
`minutes_since_open_to_checkpoint` below a fixed floor (a few minutes) as
insufficient data and mark `volume_acceleration_available = false`
rather than compute a wild, meaningless ratio. This is a defensive
requirement carried forward from design, not something confirmed correct
by live data — flagged here so it isn't silently dropped at
implementation time.

**Why not option C (rolling historical baseline):** a trailing-N-day
average volume baseline is more statistically correct in principle, but
requires backfilling and storing historical volume data we don't have,
new time-bucketing logic, and handling for non-trading days — real
infrastructure work disproportionate to a 72-hour build, and explicitly
the kind of "add sophistication for its own sake" the plan warns against.
The rate-based approach is the simplest model that is still *correct*,
which is the bar — not the simplest model that merely looks plausible
(that was option A's failure).

**Combination rule**: a change is flagged as meaningful if **either**
signal crosses its threshold, with the volume signal simply unavailable
— not zero, not "no change" — when session boundaries or the near-open
guard prevent its computation. They are evaluated independently and both
appear (or are explicitly marked unavailable) in the persisted `signals`
object regardless of which one tripped, so the explanation can reference
whichever is relevant and applicable.

**Why threshold-based rather than a single composite score:** a composite
score (e.g., weighted sum of normalized signals) hides *why* something was
flagged behind a number nobody can sanity-check by eye. Two independent,
named thresholds are directly explainable: "this moved more than X%" or
"this is trading at Y times the rate observed earlier today" are
sentences a user can verify against the raw numbers shown next to them.
This directly serves the "explainable" requirement — the trade-off is
that we can't express "medium price move + medium volume = also
meaningful" without either tuning thresholds down or adding a real
composite model later (explicitly deferred, not attempted now).

**Tunable parameters (named constants, not buried magic numbers):**
- `PRICE_THRESHOLD_PCT` — starting value: **2%**. Reasoning: Nifty 50
  large-caps commonly move 0.5–1.5% intraday on ordinary days; 2% is
  chosen as a starting point that filters routine noise while catching
  genuine moves, based on typical large-cap daily volatility ranges. This
  is explicitly a starting point to validate against real data during
  build, not a value treated as final.
- `VOLUME_RATIO_THRESHOLD` — starting value: **2.0x** acceleration.
  Reasoning: trading at double the rate observed earlier in the same
  session is a conventional, easily-explained "something is happening"
  signal. This is explicitly a starting point, not a validated number —
  the live investigation confirmed the underlying volume field behaves
  as expected (cumulative, monotonically increasing) but did not
  validate this specific threshold value against real accelerating/
  decelerating patterns, since it only observed ~45 seconds of steady-
  state trading. Typical rate variation through a trading day (e.g.,
  higher near open/close) remains unvalidated.

Both constants live in one place in code, are named, and are the first
thing to tune if the demo data looks wrong.

**No baseline case (hard question G):** if no `Checkpoint` exists for a
(user, instrument) pair, the Change Engine does not run — it cannot invent
a comparison. The API returns an explicit `baseline_pending` state for
that instrument. The Checkpoint Service creates an implicit checkpoint on
first sight, which resolves this state on the instrument's next poll
cycle.

**Not re-surfacing the same change (hard question H):** a `ChangeEvent` is
created once, at detection time, tied to the checkpoint it was detected
against. When the user's checkpoint advances (explicit "mark as seen" or
implicit next-session logic), events tied to the prior checkpoint are
marked `acknowledged: true` and excluded from the Attention Engine's
active list. The Change Engine does not re-run comparisons against an
already-superseded checkpoint.

## Attention Engine — Design

Computed at request time (not persisted) over all `acknowledged: false`
`ChangeEvent`s for the requesting user:

**Ranking signal**: `max(normalized_price_signal, normalized_volume_signal)`
where each is `actual_value / threshold_value` — i.e., a change that's 3x
past its threshold ranks above one that's 1.1x past its threshold, using
the same two signals already computed by the Change Engine. When
`volume_acceleration_available` is false, only the price signal
contributes to ranking for that change — an unavailable signal is
excluded from the `max()`, never treated as zero (which would
artificially suppress its rank) or as automatically maximal. No new
scoring model is introduced; ranking reuses the Change Engine's own
numbers so the "why is this ranked here" answer is always traceable to the
same explanation already shown for the change itself.

**Explanation generation**: a template filled from the `signals` object
already stored on the `ChangeEvent`. **Exact required wording for the
volume signal**: `"Trading volume accelerated to {ratio}× the rate
observed before you last checked."` — never phrased as "{ratio}x normal
volume" or any wording implying a historical-normal baseline, since the
signal is a same-session rate comparison, not a measure of what's
"normal" for the instrument. When `volume_acceleration_available` is
false, the explanation omits any volume claim entirely rather than
substituting a placeholder. Price-only example: `"{symbol} moved
{pct}%."` Combined example: `"{symbol} moved {pct}%. Trading volume
accelerated to {ratio}× the rate observed before you last checked."` Not
freeform text, not an LLM call — a deterministic string built from
numbers we already have.

## Freshness Model (hard question D — revised, confirmed by live test)

**Explicit policy**: a ~60-second poll interval is a **target for how
often we attempt to refresh data**, not a guarantee about how old the
underlying market data is. `yfinance` has no documented latency guarantee
for NSE/BSE data, so freshness is always **computed and displayed**,
never assumed or claimed.

**Our own `fetched_at` — the moment our backend received the data — is
the sole authoritative timestamp for computing `status` and displayed
age.** The live investigation examined the provider's `regularMarketTime`
field (stored as `provider_timestamp`) and confirmed it is unsuitable for
this purpose: it was populated on every successful fetch, its decoded
value was consistently within a few seconds of our own fetch time, and
it changed between requests in step with our own polling rather than
independently reflecting exchange trade activity. **We have not
independently verified what this field actually represents** — it may be
a response-generation timestamp, a caching timestamp, or something else
entirely. It is stored on `MarketSnapshot` for diagnostics only, is never
used in any `status` or freshness computation, and is never presented to
the user as if it were an exchange trade timestamp.

Every `MarketSnapshot` gets a `status` computed at write time from
`fetched_at`, with at minimum these four values:

- `ok` ("Fresh") — fetched within the current poll cycle and passed
  sanity checks.
- `stale` ("Delayed") — `now - fetched_at` exceeds a threshold (e.g., 2x
  the poll interval, so ~120s), meaning the poll loop is behind or
  failing, but a last-known value exists.
- `invalid` — the returned value fails a conservative sanity check (see
  Invalid Data Rules below). The prior good snapshot is retained
  separately; the invalid value is rejected, never stored as current.
- `unavailable` — no snapshot exists yet for this instrument at all
  (brand new, first poll hasn't completed, or the provider has never
  successfully returned data for it).

**Invalid Data Rules (reviewed and made conservative before
implementation):**
- Missing, non-numeric, or non-finite `last_price` → `invalid`. This is
  the one case that should always reject the snapshot outright, since a
  usable price is the minimum viable output of this system.
- Missing, non-numeric, or non-finite `volume` → the volume signal is
  unavailable for that snapshot, but **this does not by itself invalidate
  an otherwise-usable price** — the snapshot can still be shown with a
  valid price and a suppressed volume signal, rather than being discarded
  entirely. This is a deliberate correction: an earlier draft of this
  model implied any bad volume value would mark the whole snapshot
  invalid, which would be overly aggressive and would throw away a
  perfectly good price for a users watching multiple instruments.
- Volume that is present but numerically **negative** → `invalid`. A
  cumulative session volume cannot be negative under any real
  circumstance; this is a real impossibility, not an assumption about
  exchange mechanics.
- **We do NOT automatically classify zero volume as invalid.** The live
  test observed zero volume in the (separately-tracked, unused-for-
  signals) `history()` 1-minute-bar field under normal, successful
  conditions — meaning zero volume is not reliably synonymous with a
  provider failure or bad data, at least not without further
  investigation we have not done. Treating zero as automatically invalid
  would risk incorrectly discarding legitimate low-liquidity moments.
- **We do NOT use circuit-limit bounds to declare a price invalid.** We
  do not have verified, per-instrument circuit-limit data, and inventing
  a plausible-sounding bound we can't back with a real source would be
  exactly the kind of fabricated exchange constraint this project is
  trying to avoid. If circuit-limit validation becomes valuable later,
  it needs its own real data source, not a guessed threshold.
- Provider timeout, network failure, or malformed/missing response for an
  instrument → does not produce an "invalid" snapshot; the prior valid
  snapshot is retained and its `status` degrades to `stale` or
  `unavailable` based on age, per the normal freshness rules above.

**UI requirement**: the frontend must show the actual computed age, not
just the status word — e.g., `"Fresh · 42s ago"`, `"Delayed · 3m ago"`,
`"Data unavailable · last update 8m ago"`. A bare price with no
freshness indicator is not acceptable output from this system under any
status. This is a hard UI requirement, not a suggestion, because the
product's core trust proposition depends on never silently presenting
stale data as current.

**What we explicitly do not claim**: real-time data, sub-60-second
guaranteed freshness, or that `fetched_at` (or any other timestamp we
have) reflects when a trade actually occurred on the exchange. No field
available to us has been verified to represent genuine exchange trade
time.

## External Dependency: Market Data Provider (GO — confirmed by live test)

**Library**: [`yfinance`](https://pypi.org/project/yfinance/) (open-source
Python package, MIT-licensed, actively maintained — 2026-dated usage
articles found during investigation), version 1.7.0 at time of testing.

**Mechanism**: `yfinance` is not an official Yahoo API — it's a wrapper
that calls Yahoo Finance's internal, undocumented `query1`/`query2`
endpoints, the same endpoints Yahoo's own website uses. There is no
published, official rate limit because there is no official public API
contract — Yahoo can change behavior without notice, and the library's
GitHub issue tracker documents this happening in practice (see below).

**NSE/BSE support — CONFIRMED by live test.** All 5 target NSE large-caps
(`RELIANCE.NS`, `TCS.NS`, `HDFCBANK.NS`, `INFY.NS`, `ICICIBANK.NS`)
fetched successfully in a real run against live Yahoo Finance data during
NSE market hours (`market_state: REGULAR` observed for all 5), from an
unrestricted network environment. 100% success rate on this one clean
run — evidence, not a reliability guarantee.

**Batch capability — CONFIRMED by live test.** `yf.download()` fetched
all 5 target symbols in a single call in **0.234 seconds**, versus
**~0.9–1.7 seconds per symbol** when fetched individually — roughly 5–7x
faster. This directly supports the shared-poll-loop design (see Decision:
Shared poll loop in `decisions.md`).

**Confirmed provider field mapping** (from the live test's actual
response shapes):

| Our field | Source (primary) | Source (fallback) | Status |
|---|---|---|---|
| `last_price` | `fast_info.last_price` | `info.regularMarketPrice` | ✅ confirmed working, both paths |
| `volume` | `fast_info.last_volume` | `info.regularMarketVolume` | ✅ confirmed cumulative & monotonically increasing (see Volume Field Mapping below) |
| `previous_close` | `fast_info.previous_close` | — | ✅ confirmed available, used to self-compute `percent_change` |
| `percent_change` | **computed by us**: `(last_price - previous_close) / previous_close * 100` | — | ✅ confirmed working; never taken from a provider-supplied percent field |
| `provider_timestamp` (diagnostics only) | `info.regularMarketTime` | — | ⚠️ confirmed populated, but semantic meaning NOT independently verified — see Freshness Model |
| `market_state` | `info.marketState` | — | ✅ confirmed present (`"REGULAR"` observed during market hours) |

**Volume Field Mapping — detail.** The live test fetched each symbol
twice, ~45 seconds apart, and compared `fast_info.last_volume` across
both fetches. For **all 5 symbols**, the value increased monotonically by
a realistic amount (thousands to tens of thousands of shares over 45
seconds, scaled sensibly to each stock's typical liquidity) — this is
exactly the behavior the volume-acceleration signal requires. Separately,
`history(interval="1m")`'s per-bar `Volume` field returned **zero for
nearly every symbol on nearly every fetch** (one exception: TCS on the
second pass). This is not explained by the investigation and is
explicitly **not used** for any signal — see Meaningful Change Engine
above.

**Known limitations — verified facts, not assumptions:**
- **No official rate limit exists**, confirmed by design (there is no
  public API contract for an undocumented endpoint) and by the complete
  absence of any rate-limit signal, header, or error in the live test —
  the test intentionally did not probe for a ceiling, so this remains
  genuinely unknown, not merely "not observed yet."
- Community-reported, *unofficial* observations elsewhere (GitHub issues)
  mention a rough figure around 360 requests/hour, but this is not a
  Yahoo-published number and multiple users report it changing or being
  enforced inconsistently — including one case where a long-stable
  workflow suddenly started hitting 429 errors with no prior warning.
  **We do not adopt this number as a design constraint.**
- **429 (Too Many Requests) is a real, documented failure mode** on the
  `yfinance` GitHub issue tracker, though **zero errors of any kind**
  were observed across all 15 requests in our own live test.
- **No SLA, no uptime guarantee, no support channel** — this is a
  community open-source project wrapping an undocumented interface, not
  a vendor relationship.

**Confirmed by live test — previously unresolved unknowns:**
1. ✅ **Ticker reliability**: all 5 target instruments returned reliably
   with `.NS` suffixes on `yfinance` 1.7.0, during one live run.
2. ⚠️ **Practical rate ceiling**: still **UNKNOWN** — the live test did
   not probe for this by design (per explicit instruction not to hammer
   the provider), and zero requests failed, so no ceiling was observed
   either way.
3. ⚠️ **Data latency during NSE hours**: still **UNKNOWN** in the sense
   of "how many seconds behind the real exchange trade" — no field we
   have access to has been verified to represent genuine exchange trade
   time (see Freshness Model). We can only say prices and volume visibly
   changed between two fetches 45 seconds apart, which is consistent
   with live-ish data, but not proof of any specific latency bound.
4. ✅ **Volume field for the rate signal**: confirmed —
   `fast_info.last_volume` / `info.regularMarketVolume`, cumulative and
   monotonically increasing within a session (see Volume Field Mapping
   above). `history()`'s per-bar volume is confirmed **unsuitable** (see
   above) and excluded from the MVP signal path.
5. ⚠️ **Off-hours/holiday behavior**: still **UNKNOWN** — the live test
   ran entirely during active market hours (`market_state: REGULAR`
   throughout); closed-market behavior was not observed and must be
   treated cautiously by the `MarketDataProvider` implementation (any
   `market_state` other than `REGULAR` should not be assumed to carry a
   live price).
6. **NEW unknown surfaced by the live test**: why `history()`'s per-bar
   volume returned near-universal zero. Not investigated further — kept
   entirely out of the MVP signal path rather than guessed at.

**`MarketDataProvider` abstraction**: the Market Data Service depends on
a small internal interface (e.g., `fetch_snapshots(instrument_ids) ->
list[RawQuote]`), with `yfinance` as the only implementation for this
hackathon. No other component imports `yfinance` directly. This is what
allows a future swap to a broker/paid provider (Zerodha Kite, a paid
vendor, etc.) without touching the Watchlist/Checkpoint/Change/Attention
services — only a new implementation of the same interface is needed.

**Failure boundary**: the Market Data Service is the **only** component
that talks to this provider. Its failure never propagates as an exception
to the Watchlist/Checkpoint/Change/Attention services — it only ever
results in a `MarketSnapshot.status` of `stale`/`invalid`/`unavailable`.

## Important Invariants

1. A `MarketSnapshot` is never presented to the frontend without a
   `status` field, and the frontend never renders a price without
   checking it.
2. A `ChangeEvent` is only created when a real, prior `Checkpoint` exists
   for that (user, instrument) pair — never fabricated for a baseline
   that doesn't exist.
3. A `ChangeEvent`, once created, is immutable — it is marked
   `acknowledged`, never edited or deleted, preserving an honest history
   of what was actually detected and when.
4. Attention ranking is always fully derivable from active `ChangeEvent`s;
   it is never the persisted source of truth.
5. The poll loop fetches each **distinct** instrument at most once per
   cycle, regardless of how many users/watchlists reference it.
6. A provider failure degrades a snapshot's status; it never raises an
   unhandled exception into a request-serving code path.
7. `fetched_at` (our own timestamp) is the sole authoritative input to
   every freshness/staleness computation; `provider_timestamp` is never
   used for this purpose.
8. Volume acceleration is never computed across a session boundary — a
   `ChangeEvent`'s `volume_acceleration_ratio` is only populated when the
   checkpoint's `session_date` matches the current snapshot's
   `session_date`; otherwise it is explicitly marked unavailable, never
   computed from mismatched-session data.

## Failure Modes & Responses

| Failure | Backend behavior | User sees |
|---|---|---|
| Provider timeout | Retain last known valid snapshot, mark `stale` after threshold | "Delayed · Xm ago" |
| Provider rate-limited (429) | Exponential backoff on the poll loop (e.g., double the interval up to a cap, reset after a clean cycle); never retry immediately in a tight loop; keep last-known snapshot | "Delayed · Xm ago", no crash |
| Provider returns malformed/partial response | Reject the update for that instrument this cycle, log it, keep prior snapshot | Stale indicator if it persists across cycles |
| Price missing, non-numeric, or non-finite | Reject the value outright, mark `invalid`, retain prior good snapshot | Data-issue indicator, never a wrong price shown as real |
| Volume missing, non-numeric, non-finite, or negative | Reject the volume value; retain the price if it is otherwise valid, mark the volume signal unavailable rather than discarding the whole snapshot (negative volume is rejected as a real impossibility; missing/non-numeric volume degrades gracefully rather than invalidating the snapshot) | Price still shown if valid; volume/change-detection simply omitted for that update |
| Provider unavailable entirely (network failure, service down) | Do not fabricate a snapshot; keep serving last known valid snapshot marked `stale`/`unavailable` as age dictates | "Data unavailable · last update Xm ago" |
| Checkpoint and current snapshot span a session boundary | Volume acceleration marked unavailable; price comparison still computed | Change may still surface on price alone; no volume claim shown |
| New instrument, no snapshot yet | `status: unavailable` | "Fetching initial data..." |
| New instrument, no checkpoint yet | Change Engine skips it | "Baseline pending" instead of a change or a crash |
| Two requests advance the same checkpoint concurrently | Last-write-wins is acceptable here — see `decisions.md` for why this doesn't need transaction-level protection | No visible inconsistency for a single user's own action |
| Empty watchlist | Return empty list, not an error | "Your watchlist is empty — add a stock to get started" |

**Backoff policy detail**: on a 429 or repeated timeout, the poll loop
increases its interval (e.g., 60s → 120s → 240s, capped) rather than
retrying immediately — "do not hammer the provider" is a hard
requirement, not a best-effort preference. No 429s or errors were
observed in the one live test run performed, so this policy remains
untested against a real rate-limit event; it is implemented defensively
regardless. The frontend is unaffected by this backoff beyond seeing
slightly older `fetched_at` timestamps — it always continues serving
whatever the last known valid snapshot was.

**Note on removed assumptions**: an earlier draft of this table listed
"zero volume mid-session" and "circuit-limit-violating jump" as examples
of implausible values to reject. Both have been removed per explicit
review — see Invalid Data Rules in the Freshness Model section above for
why: zero volume was observed under normal conditions in the live test
and is not reliably a failure signal, and circuit-limit bounds are not
data we actually have access to, so validating against them would mean
inventing an exchange constraint rather than checking a real one.

## API Contracts (frontend-facing)

```
GET  /watchlist                     -> current user's watchlist with
                                        snapshots + freshness + active
                                        change events, attention-ranked

POST /watchlist/instruments         -> add instrument (body: symbol,
                                        exchange)

DELETE /watchlist/instruments/{id}  -> remove instrument

POST /watchlist/instruments/{id}/checkpoint
                                     -> explicit "mark as seen" for one
                                        instrument (advances checkpoint,
                                        acknowledges its change events)

POST /watchlist/checkpoint          -> explicit "mark all as seen" for
                                        the whole watchlist
```

`GET /watchlist` response shape (illustrative):
```json
{
  "instruments": [
    {
      "instrument": { "symbol": "RELIANCE", "exchange": "NSE" },
      "snapshot": {
        "last_price": 2456.75,
        "percent_change": 0.5,
        "volume": 8234567,
        "status": "ok",
        "status_label": "Fresh · 42s ago",
        "as_of": "2026-09-04T10:15:00Z"
      },
      "change_state": "baseline_pending" | "no_change" | "changed",
      "active_change": {
        "reason": "RELIANCE moved 4.2%. Trading volume accelerated to 2.3× the rate observed before you last checked.",
        "volume_signal_available": true,
        "attention_rank": 1
      }
    },
    {
      "instrument": { "symbol": "TCS", "exchange": "NSE" },
      "snapshot": { "...": "..." },
      "change_state": "changed",
      "active_change": {
        "reason": "TCS moved 3.1%.",
        "volume_signal_available": false,
        "attention_rank": 2
      }
    }
  ]
}
```
The second example illustrates a change detected across a session
boundary (or where the near-open guard applied): price-only, with
`volume_signal_available: false` and no volume claim in the explanation
string.

## Major Architectural Trade-offs

- **No tick-level history stored** — `MarketSnapshot` is upsert-only, not
  append-only. Trade-off: no historical charting is possible from our own
  data later. Accepted because charting is explicitly out of scope and
  storing full history is unused complexity for this build.
- **Volume signal uses same-session rate acceleration**, not a rolling
  multi-day historical baseline, and is explicitly unavailable across a
  session boundary. Trade-off: doesn't account for instrument-specific
  "normal" volume patterns (e.g., a stock that's naturally bursty near
  market open) or cross-day seasonality (results days, index-rebalancing
  days), and a checkpoint left overnight loses its volume signal on next
  check (price comparison still works). Accepted: achievable in 72 hours
  with data we actually have (two snapshots + a known market-open
  constant + a session-date comparison), and — unlike the raw-ratio
  approach it replaces — it is actually correct for what it claims to
  measure, not just superficially plausible. See `decisions.md` for the
  full comparison that led here.
- **Single watchlist per user** — trade-off: no organization features, but
  removes a whole layer of list-management complexity not required by the
  MUST HAVE scope.
