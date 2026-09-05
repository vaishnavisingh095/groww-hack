# Architecture: Smart Market Watchlist

## System Components & Responsibilities

| Component | Responsibility | Owns |
|---|---|---|
| Anonymous Identity (`identity.py`) | Resolves/issues the per-browser anonymous capability cookie; the one place `user_id` is ever derived | Nothing persisted — the cookie value itself, set on the HTTP response |
| Watchlist Service | Owner-scoped watchlist membership; global Instrument creation/reuse; Add Stock | Instrument (global), Watchlist (owner-scoped) documents |
| Market Data Service | Talks to the external provider and assembles snapshots; **as currently implemented, fetches live on demand per request rather than via a background poll loop** (see Data Flow above) | Nothing persisted — in-memory `MarketSnapshot` value objects per request |
| Checkpoint Service | Creates/advances user checkpoints (explicit only in practice — see Checkpoint Model below) | Checkpoint documents |
| Meaningful Change Engine | Compares a checkpoint's baseline snapshot to the current snapshot using fixed deterministic rules | Nothing persisted — a pure function of the values passed in |
| ChangeEvent Service | Persists/dedupes ChangeEvents from the Change Engine's output; acknowledges them on explicit action | ChangeEvent documents |
| Attention Engine | Ranks active (unacknowledged) change events per user; generates explanations | Nothing persisted — pure computation over ChangeEvents |
| Frontend presentation/state layer | Renders backend-derived state truthfully; owns only local UI state (search, filters, View Details expansion, in-flight/error flags) | Nothing backend-relevant — no owner identity, no business state |

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

**CURRENT IMPLEMENTATION NOTE — this diagram describes the originally
designed data flow, not what actually runs today.** There is no
standalone backend poll loop and no persisted `MarketSnapshot` document.
`MarketDataService.fetch_snapshots` is called synchronously, live,
inside `GET /watchlist`'s (and the two Mark Seen/Mark All routes')
own request handler, every time one of those routes is called — the
"poll" a user experiences is the **frontend's** own client-side
`setInterval` re-calling `GET /watchlist`/`GET /watchlist/attention`
every 60 seconds, not a backend process. The real, current data flow is:

```
[Frontend poll / user action]
        |
        v
GET /watchlist  (or a Mark Seen/Mark All route)
        |
        v
MarketDataService.fetch_snapshots  --(live call, every invocation)--> [External Provider]
        |
   in-memory MarketSnapshot value objects (never written to Mongo)
        |
        v
[Checkpoint] (Mongo, read-only on this path) ---compare---> [Meaningful Change Engine]
        |
   writes (if a real checkpoint exists, snapshot status is OK, and the
   change is meaningful)
        v
[ChangeEvent] (Mongo)
        |
   read at request time (a separate request, GET /watchlist/attention)
        v
[Attention Engine] --ranked + explained--> [Frontend]
```

This is a documented, deliberate sequencing choice (the on-demand
approach reuses the same `MarketDataService`/`MarketDataProvider`
abstraction the background loop would have used), not an unnoticed gap
— see `decisions.md`'s "On-demand fetch per request, not a separate
background poll process" entry for the full reasoning and its known
trade-off: N concurrent users currently cause N concurrent provider
fetches for the same instrument, not one shared fetch.

## API Boundaries

- Frontend never talks to the external market-data provider directly —
  always through the backend, so freshness/staleness logic is enforced in
  one place.
- **As currently implemented**, the Market Data Service is called
  directly from three request handlers (`GET /watchlist`, the two Mark
  Seen/Mark All routes) — there is no separate poll process, and no
  fan-out deduplication across concurrent requests yet (see the
  Current Implementation Note above). The originally-designed
  poll-loop-is-the-only-writer property does not currently hold.
- The Meaningful Change Engine only runs comparisons when a checkpoint
  exists; it never invents a baseline.

## Data Ownership / Source of Truth

- **Instrument identity & metadata**: our own `Instrument` collection is
  the source of truth for what's trackable; the provider is the source of
  truth for price/volume values only.
- **Current market state**: `MarketSnapshot` — the latest fetched value
  per instrument, but **as currently implemented, this is an in-memory
  value object computed fresh on every request, never written to
  MongoDB** (see the Current Implementation Note under Data Flow above).
  The `market_snapshots` collection and its unique index on
  `instrument_id` exist (see Database Integrity below) but no code path
  writes to that collection today — it is dormant infrastructure, kept
  for the originally-designed persisted-poll-loop model, not currently
  load-bearing.
- **What the user has seen**: `Checkpoint` — entirely our own state, never
  derived from the provider.
- **What changed**: `ChangeEvent` — computed once by us, then persisted as
  fact (so re-computation doesn't silently change history — see hard
  question H).
- **Attention ranking**: never persisted as its own source of truth — it's
  fully derivable from active `ChangeEvent`s at any moment, so persisting
  it would create a second, potentially-stale copy of derivable data.
- **Owner identity**: `user_id` (used by `Checkpoint`, `ChangeEvent`, and
  `Watchlist`) is an opaque, server-generated token resolved from an
  anonymous capability cookie — see decisions.md's "Persistent anonymous
  watchlist identity." It is never accepted from a request body, query
  parameter, or header. See "Anonymous Identity" below for the full
  mechanism.
- **Watchlist membership**: `Watchlist.instrument_ids` is the source of
  truth for which instruments belong to a given owner. `Instrument`
  documents remain global/shared reference data regardless of who
  references them.

**Ownership scope, at a glance:**

| Collection | Scope |
|---|---|
| `Instrument` | GLOBAL — shared reference data, not owned by anyone |
| `Watchlist` | OWNER-SCOPED — one document per anonymous owner |
| `Checkpoint` | OWNER + INSTRUMENT — one active document per pair |
| `ChangeEvent` | OWNER + INSTRUMENT + CHECKPOINT — see Database Integrity |
| Attention | DERIVED AT REQUEST TIME from active `ChangeEvent`s — never persisted |

A global `Instrument` document existing in MongoDB does **not** mean
every owner watches it — membership is exclusively determined by
`Watchlist.instrument_ids`, checked per owner on every read.

## Anonymous Identity

**This is anonymous, capability-based identity for the hackathon — it
is explicitly NOT full authentication and is never described as one
anywhere in this app or its docs.** There are no accounts, no
passwords, and no login flow.

- `app/services/identity.py`'s `resolve_owner_id` is the single FastAPI
  dependency every route uses to obtain `user_id` — no route reads it
  any other way.
- On a request with no existing cookie (or an empty one), the backend
  generates a token via `secrets.token_urlsafe(32)` (256 bits,
  cryptographically secure — never `random`, never a timestamp or
  sequential id) and sets it as the `watchlist_owner` cookie.
- On a request with an existing, non-empty cookie value, that value is
  trusted **as-is** as the `user_id` — there is no database lookup to
  "validate" it, no separate `owners` collection, and no
  signature/verification step. An unissued/garbage value simply becomes
  its own fresh, currently-empty owner identity; by construction (the
  token's entropy), it can never collide with a real existing owner.
- Cookie attributes: `httponly=True` (always — the frontend cannot read
  this cookie via JavaScript and never attempts to), `samesite="lax"`
  (always), `secure=True` only when `settings.environment == "production"`
  (an exact string match — local development runs over plain HTTP,
  which rejects `Secure` cookies outright), `max_age` ≈ 1 year (a flat
  expiry, not sliding), `path="/"`.
- The frontend never receives this value in any response body, never
  stores an owner id anywhere itself (no `localStorage`, no
  `sessionStorage`), and `api.js` sends `credentials: 'include'` on
  every call so the browser carries the cookie automatically. CORS
  (`app/main.py`) has `allow_credentials=True` paired with an explicit
  origin list — never a wildcard, which browsers reject outright when
  combined with credentials.
- **Accepted limitation, disclosed, not an oversight**: possession of
  the cookie *is* access to that owner's watchlist — there is no
  password behind it, so a stolen or shared cookie grants full
  read/write access to that anonymous owner's state. `httpOnly` (no JS
  exfiltration via a script) and `Secure` in production (no plaintext
  network capture) are the mitigations, not a claim of stronger
  protection. This is not intended as production-grade identity
  management for a real financial product; see `decisions.md`'s
  "Persistent anonymous watchlist identity" decision for the full
  reasoning.

## Database Integrity

Verified, actual MongoDB indexes (`app/db/indexes.py`, applied
idempotently on every app startup):

| Collection | Unique index | Enforces |
|---|---|---|
| `instruments` | `(symbol, exchange)` | One global Instrument per tradeable listing |
| `watchlists` | `(user_id)` | One Watchlist per owner |
| `checkpoints` | `(user_id, instrument_id)` | One active checkpoint per owner+instrument — advancing replaces it |
| `change_events` | `(user_id, instrument_id, checkpoint_id)` | At most one ChangeEvent per checkpoint version (`acknowledged` deliberately excluded from this key — an event transitions in place, never duplicates because of that transition) |

`change_events` additionally has a non-unique `(user_id, acknowledged)`
index for the Attention Engine's "active events for this user" query.

**Concurrency strategy — no transactions, no distributed locks.**
Every write in this codebase is a single-document MongoDB operation
(`insert_one`, `update_one`/`update_many`, or `replace_one` with
`upsert=True`), each of which is already atomic at the document level
without needing a transaction. Two categories of race are handled, both
without new infrastructure:
- **Find-then-insert races** (checking a document doesn't exist, then
  inserting it) — `Watchlist` creation, Add Stock's `Instrument`
  creation, and first-owner seed `Instrument` creation all catch
  `DuplicateKeyError` and recover by re-fetching the document the
  winning concurrent request actually persisted, rather than raising.
  The unique index is the real source of truth under concurrency; the
  service's own find-before-insert check is only the common-case fast
  path.
- **Membership updates** use `$addToSet` (atomic, never a lost update
  from a concurrent addition of a *different* instrument to the same
  owner's watchlist), never `$push` (which would not itself cause
  duplicates here, but doesn't express the "no duplicate membership"
  intent) and never a read-modify-write `replace_one` (which could lose
  a concurrent sibling update).
- **ChangeEvent creation** additionally re-validates the checkpoint it
  was evaluated against is still current before persisting (P0
  Hardening #3's fix) — this is a correctness guard against stale data,
  not a concurrency-control mechanism in itself.

**Why no transaction/lock layer was introduced**: every race actually
identified and reproduced during hardening was fully resolved by an
existing unique constraint plus an atomic single-document operation
(and, where a find-then-insert gap existed, a `DuplicateKeyError`
recovery path). No demonstrated correctness problem in this codebase
requires cross-document atomicity — introducing transactions or
distributed locks for a problem already solved at the single-document
level would be exactly the kind of unjustified infrastructure this
project's engineering rules explicitly warn against.

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
support in MUST HAVE). This model was defined from Phase 1 but sat
unused until the Persistent Anonymous Watchlist milestone activated it
as the real per-owner membership record — see decisions.md.
`user_id` here is the same anonymous capability-cookie identity used by
`Checkpoint`/`ChangeEvent`, not a separate account system.

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
This is the originally designed shape: one document per instrument,
upserted every poll cycle (not an append-only history — see
`decisions.md` on why we don't keep tick history for the hackathon
scope). **As currently implemented, this shape exists as a Pydantic
value object (`app/models/market_snapshot.py`) constructed fresh inside
each request that needs it and never persisted to MongoDB** — see the
Current Implementation Note under Data Flow above. The
`market_snapshots` collection/index described elsewhere in this
document is real (index creation is idempotent and runs on every
startup) but currently unused by any write path.

`status` is computed at write time from `fetched_at` (see Freshness Model
below), not left for the reader to infer, and never from
`provider_timestamp`.

### Checkpoint

**OBSERVATION ≠ ACKNOWLEDGEMENT — the invariant this whole model
exists to enforce.** `GET /watchlist` OBSERVES current market state and
MAY persist a `ChangeEvent` recording that a checkpoint's baseline was
meaningfully exceeded (a market-observed fact) — but it never advances,
replaces, or creates a `Checkpoint`, under any condition, including a
brand-new instrument with no checkpoint at all. Only an explicit Mark as
Seen (single instrument) or Mark All as Seen action ever writes a
`Checkpoint` — establishing the first baseline, advancing an existing
one, and acknowledging that instrument's active `ChangeEvent`(s) in the
same action. `id` below is a durable, application-assigned UUID
(distinct from MongoDB's own `_id`), regenerated on every explicit
advance, so a `ChangeEvent.checkpoint_id` can durably reference the
exact checkpoint version it was detected against even after that
(owner, instrument) pair's checkpoint is later replaced — see
`decisions.md`'s "Checkpoint gets a durable, application-assigned `id`"
decision.

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
a snapshot belongs to before using cumulative-volume deltas. `session_date`
is derived from the actual intraday bar `last_price` came from — the same
bar the Market Data Provider used to determine the current price — not
from our own `fetched_at` clock. yfinance's intraday history index is
already timezone-localized to the exchange (Asia/Kolkata for NSE), so
that bar's own date is the trading-session date directly, with no
separate UTC/IST conversion required. (An earlier MVP implementation
derived `session_date` from `fetched_at` converted to IST calendar date
at write time instead; this was corrected because `history(period="1d")`
can return the most recently completed session's bars while the market
is closed, which could mislabel that data's actual session under the
fetch-time approach — see decisions.md.) This remains a simple rule that
does not attempt to model partial/special sessions, and non-trading-day
detection beyond calendar-date comparison is explicitly not attempted
(see Unresolved / Out of Scope below); closed-market yfinance response
behavior itself remains empirically unverified from this environment,
independent of this fix.

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

**No baseline case (hard question G) — CORRECTED from the original
design.** If no `Checkpoint` exists for a (user, instrument) pair, the
Change Engine does not run a comparison — it cannot invent one. `GET
/watchlist` reports `change.has_baseline: false` with
`reason: "Baseline pending — no previous check to compare against."`
for that instrument. **The originally-designed implicit checkpoint
creation on first sight does NOT run from this (or any) read path.**
`CheckpointService.ensure_initial_checkpoint` (the create-if-absent,
IMPLICIT-source primitive) still exists as a correct, tested, isolated
method, but is called from no production code path — see
`decisions.md`'s "Explicit checkpoints are never silently overwritten
by implicit ones" entry and the Checkpoint Model / OBSERVATION ≠
ACKNOWLEDGEMENT distinction below for why: calling it from `GET
/watchlist` would mean a mere page load could establish a baseline the
very next read would then treat as an acknowledged comparison point —
opening the app is not acknowledgement. A `baseline_pending` instrument
stays `baseline_pending` across any number of repeated `GET` calls;
only an explicit Mark as Seen / Mark All action ever resolves it.

**Not re-surfacing the same change (hard question H):** a `ChangeEvent` is
created once, at detection time, tied to the checkpoint it was detected
against. When the user's checkpoint advances via an **explicit** Mark as
Seen / Mark All action (there is no implicit next-session advancement —
see above), events tied to the prior checkpoint are marked
`acknowledged: true` and excluded from the Attention Engine's active
list. The Change Engine does not re-run comparisons against an
already-superseded checkpoint, and — per the P0 Hardening #3 fix — a
`GET` evaluation that was computed against a checkpoint later superseded
by a concurrent Mark Seen is detected and discarded rather than
persisted as a stale, orphaned event.

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

**Attention levels** (locked bands, inclusive lower bounds — matching
the Change Engine's own inclusive `>=` threshold convention): `HIGH` when
`score >= 2.0`; `MEDIUM` when `1.25 <= score < 2.0`; `WATCH` when
`1.0 <= score < 1.25`. A `ChangeEvent` only exists at all when at least
one signal was already `>= 1.0×` its own threshold at detection time
(that is what `meaningful_change` meant) — so `WATCH` is the true floor,
never a catch-all for an unreachable case below it. `rank` is assigned
after sorting the active set by `attention_score` descending — it is a
position, not a separately computed value, and the frontend never
recomputes or re-sorts by anything else (see Frontend Architecture
below).

**Explanation generation**: a template filled from the `signals` object
already stored on the `ChangeEvent`. **Exact required wording for the
volume signal**: `"Trading volume accelerated to {ratio}× the rate
observed before you last checked."` — never phrased as "{ratio}x normal
volume" or any wording implying a historical-normal baseline, since the
signal is a same-session rate comparison, not a measure of what's
"normal" for the instrument. The volume clause is appended **only when
both** `volume_acceleration_available` is true **and** the ratio itself
meets `VOLUME_ACCELERATION_THRESHOLD` — available-but-below-threshold
(e.g. a computed `0.0×` when checkpoint and current volume are equal)
is not "acceleration" and must never be worded as such; a mere
`available=true` is not by itself sufficient. This was a real bug found
during manual browser QA (`AttentionEngine._build_explanation` used to
gate on availability alone, producing "Trading volume accelerated to
0.0× the rate observed before you last checked" for a purely
price-driven event) and fixed — see `decisions.md`'s "Attention Engine
explanation volume-threshold wording fix." When the volume clause is
omitted (either because it's unavailable, or available but not
meaningful), nothing is substituted in its place. Price-only example:
`"{symbol} moved {pct}%."` Combined example: `"{symbol} moved {pct}%.
Trading volume accelerated to {ratio}× the rate observed before you
last checked."` Not freeform text, not an LLM call — a deterministic
string built from numbers we already have.

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
5. ~~The poll loop fetches each **distinct** instrument at most once per
   cycle, regardless of how many users/watchlists reference it.~~ **Does
   not currently hold** — this was the originally-designed invariant for
   the background-poll-loop model. As implemented (on-demand fetch per
   request), each request fetches independently; N concurrent users
   currently cause N concurrent provider fetches for the same
   instrument, with no shared-fetch deduplication. See the Current
   Implementation Note under Data Flow and `decisions.md`'s "On-demand
   fetch" entry for this known, disclosed gap.
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
9. `user_id` is always resolved server-side from the anonymous capability
   cookie — never accepted from a request body, query parameter, or
   header.
10. An owner may only checkpoint an instrument that is actually in their
    own `Watchlist.instrument_ids`; an instrument that exists globally
    but isn't theirs is rejected with the same 404 used for a genuinely
    nonexistent id, so a caller can never distinguish "not yours" from
    "doesn't exist."

## Failure Modes & Responses

**Current implementation note**: the rows below describing "keep/retain
last known valid snapshot" and the backoff policy paragraph describe the
originally-designed, persisted-poll-loop model. **As currently
implemented, there is no persisted snapshot to retain** — each request
fetches live, and a failed instrument simply reports `unavailable` for
that one request, with no cached prior value served in its place (a real
current gap, not a design choice — see Known Limitations). This is
called out per-row below rather than only once, since it changes what
the user actually sees for several of these rows.

| Failure | Backend behavior | User sees |
|---|---|---|
| Provider timeout | **As implemented**: no persisted snapshot exists to retain; this instrument reports `unavailable` for this request only, and may succeed again on the next request/poll with no memory of the failure. *(Originally designed: retain last known valid snapshot, mark `stale` after threshold.)* | "Data unavailable" this cycle; may recover next poll |
| Provider rate-limited (429) | No backoff loop exists (there is no poll loop to back off) — a 429 is caught the same as any other provider failure, per-instrument, per-request; not exercised by any live test. *(Originally designed: exponential backoff on the poll loop.)* | Same as any other single-request provider failure — "Data unavailable" this cycle |
| Provider returns malformed/partial response | Reject the update for that instrument this request; no snapshot is produced (see the market-data hardening fix below for the specific non-finite/non-positive-price case) | "Data unavailable" this cycle |
| Price missing, non-numeric, or non-finite | Reject the value outright; no snapshot for that instrument this request — and, per the P0 Hardening #2 fix, this failure is scoped to the one malformed instrument and never aborts sibling instruments' snapshots in the same batch | "Data unavailable" this cycle for that instrument; siblings unaffected |
| Volume missing, non-numeric, non-finite, or negative | Reject the volume value; retain the price if it is otherwise valid, mark the volume signal unavailable rather than discarding the whole snapshot (negative volume is rejected as a real impossibility; missing/non-numeric volume degrades gracefully rather than invalidating the snapshot) | Price still shown if valid; volume/change-detection simply omitted for that update |
| Provider unavailable entirely (network failure, service down) | Do not fabricate a snapshot; report `unavailable` for every affected instrument this request | "Data unavailable" this cycle |
| Checkpoint and current snapshot span a session boundary | Volume acceleration marked unavailable; price comparison still computed | Change may still surface on price alone; no volume claim shown |
| New instrument, no snapshot yet | `status: unavailable` | "Fetching initial data..." |
| New instrument, no checkpoint yet | Change Engine skips it | "Baseline pending" instead of a change or a crash |
| Two requests advance the same checkpoint concurrently | Last-write-wins via `replace_one(upsert=True)` — a single atomic MongoDB operation, so the result is always one complete, valid checkpoint document (never a torn/partial write), whichever request's write lands last | No visible inconsistency for a single user's own action |
| A `GET` evaluation races a concurrent Mark Seen on the same instrument | `ChangeEventService` re-validates the checkpoint is still current before persisting a `ChangeEvent` (P0 Hardening #3 fix) — a stale evaluation is silently dropped, never persisted against a superseded checkpoint | No orphaned/resurfaced attention item |
| Concurrent Add Stock / first-owner seeding for the same new `(symbol, exchange)` | `DuplicateKeyError` recovery re-fetches the winning request's document and adds it to the losing request's own membership (P0 Hardening #4/#5 fix) | Both owners end up with correct membership; no 500 |
| Empty watchlist | Return empty list, not an error | "Your watchlist is empty — add a stock to get started" |
| `GET /watchlist`/`GET /watchlist/attention` request itself fails, or returns a malformed 200 | Frontend distinguishes "never successfully loaded" from "confirmed empty/caught-up" (P0 Hardening #6 fix); a malformed-shape 200 is treated as a failure, not rendered | Truthful "unavailable"/error state, never a false "empty"/"caught up" claim |

**Note on removed assumptions**: an earlier draft of this table listed
"zero volume mid-session" and "circuit-limit-violating jump" as examples
of implausible values to reject. Both have been removed per explicit
review — see Invalid Data Rules in the Freshness Model section above for
why: zero volume was observed under normal conditions in the live test
and is not reliably a failure signal, and circuit-limit bounds are not
data we actually have access to, so validating against them would mean
inventing an exchange constraint rather than checking a real one.

## API Contracts (frontend-facing)

**The shapes below are the actual, current contract**, read directly
from `app/routes/watchlist.py` — not the originally-illustrative shape
this section used to show (see `decisions.md`'s "Attention Engine
exposed as its own endpoint" decision, which already noted that the
original illustration was aspirational and never matched what
`GET /watchlist` actually returns).

```
GET  /watchlist                     -> current owner's watchlist:
                                        current price/volume/status per
                                        instrument, plus that
                                        instrument's own change
                                        comparison against its
                                        checkpoint (if any). Read-only
                                        with respect to Checkpoint state;
                                        may persist a ChangeEvent (see
                                        Checkpoint Model below).

GET  /watchlist/attention           -> this owner's ranked, active
                                        (unacknowledged) attention items.
                                        Pure read — creates, advances, or
                                        acknowledges nothing.

POST /watchlist/instruments         -> Add Stock (body: symbol,
                                        exchange). No remove-instrument
                                        endpoint exists — there is no
                                        DELETE route in this API.

POST /watchlist/instruments/{id}/checkpoint
                                     -> explicit "mark as seen" for one
                                        instrument (advances checkpoint,
                                        acknowledges its active change
                                        events). 404 if the instrument
                                        doesn't exist OR isn't in the
                                        caller's own watchlist (same
                                        status for both, so a caller
                                        cannot distinguish the two).

POST /watchlist/checkpoint          -> explicit "mark all as seen" for
                                        the whole watchlist; reports
                                        which instruments were updated
                                        vs. skipped (no valid current
                                        data), never fails the whole
                                        batch for one instrument.
```

`GET /watchlist` response shape (actual):
```json
{
  "instruments": [
    {
      "instrument_id": "66f...",
      "symbol": "RELIANCE",
      "exchange": "NSE",
      "price": 2456.75,
      "percent_change": 0.5,
      "cumulative_volume": 8234567,
      "status": "ok",
      "freshness_label": "Updated 42s ago",
      "data_age_seconds": 42,
      "change": {
        "has_baseline": true,
        "meaningful_change": true,
        "price_change_pct": 4.2,
        "volume_acceleration_ratio": 2.3,
        "volume_signal_available": true,
        "reason": "Price moved +4.2% and trading activity accelerated to 2.3× its baseline rate."
      }
    }
  ]
}
```
`status` is one of `ok`/`stale`/`unavailable`/`invalid` (`invalid` is
modeled but never actually serialized by this route today — an invalid
snapshot is reported as `unavailable`, per the market-data hardening
work). When `status` is `unavailable`, `price`/`percent_change`/
`cumulative_volume` are all `null` and `change` reports
`has_baseline: false` regardless of whether a real checkpoint exists in
storage — the response never claims to compare against a baseline it
cannot currently verify (the checkpoint document itself, in Mongo, is
left untouched).

`GET /watchlist/attention` response shape (actual):
```json
{
  "attention_items": [
    {
      "instrument_id": "66f...",
      "symbol": "RELIANCE",
      "checkpoint_id": "9b8...",
      "detected_at": "2026-09-04T10:15:00+00:00",
      "price_change_pct": 4.2,
      "volume_acceleration_ratio": 2.3,
      "volume_acceleration_available": true,
      "attention_score": 2.1,
      "attention_level": "high",
      "explanation": "RELIANCE moved +4.2% since your last check. Trading volume accelerated to 2.3× the rate observed before you last checked.",
      "rank": 1
    }
  ]
}
```

`POST /watchlist/instruments/{id}/checkpoint` response shape (actual):
```json
{
  "instrument_id": "66f...",
  "symbol": "RELIANCE",
  "checkpoint_price": 2456.75,
  "checkpoint_at": "2026-09-04T10:16:00+00:00",
  "message": "Baseline saved at ₹2456.75"
}
```
Returns `503` (not a checkpoint) if no valid current snapshot is
available for that instrument this request — invalid/unavailable data
can never become a baseline.

`POST /watchlist/checkpoint` response shape (actual):
```json
{
  "updated": [ { "instrument_id": "66f...", "symbol": "RELIANCE", "checkpoint_price": 2456.75 } ],
  "skipped": [ { "instrument_id": "70a...", "symbol": "INFY" } ]
}
```

`POST /watchlist/instruments` response shape (actual):
```json
{ "instrument_id": "70a...", "symbol": "WIPRO", "exchange": "NSE", "created": true }
```
`created: false` means the global `Instrument` already existed (this
owner's membership was still added, or was already present) — see
`decisions.md`'s "Add Stock's three-case duplicate-add rule."

## Frontend Architecture

The frontend (`frontend/src/`) is a single-page React app (`App.jsx`)
with two presentational children (`AttentionSection.jsx`,
`WatchlistTable.jsx`) and a thin API client (`api.js`). It introduces no
new backend API or business logic of its own — it is a read-only
consumer of `GET /watchlist`/`GET /watchlist/attention`, and a caller of
the three mutation routes, never a second implementation of any backend
decision.

**Attention Data Consumption**

- The attention experience is `GET /watchlist/attention`'s
  `attention_items` joined, client-side, with `GET /watchlist`'s
  `instruments` by `instrument_id`. Neither response's shape changed to
  support this; the frontend only reads fields both already return.
- The frontend keeps `price_change_pct` (since-checkpoint, from
  `attention_items`) and `percent_change` (day-over-day, from
  `instruments`) visually and semantically distinct — they are rendered
  as two separately labeled figures, never combined or substituted for
  one another.
- `detected_at` (when the `ChangeEvent` was created) is displayed as-is
  as a relative-time label; it is not recomputed or treated as "now."
- Attention levels, scores, and ranking remain entirely backend-derived
  (`AttentionEngine`); the frontend only partitions the already-sorted
  list by the existing `attention_level` value into two display groups
  ("High Attention" for `high`, "Worth Checking" for everything else) —
  it does not sort, score, or reclassify (see Important Invariant 4).
- Freshness/status (`freshness_label`, `status`) remain backend/provider-
  derived and are displayed unchanged; the frontend does not compute or
  infer freshness itself.
- The attention card does not display a company name (`GET /watchlist`
  does not return one) or a calculated "strongest signal" — neither
  exists in the backend response or in any frontend logic; see
  `decisions.md` for why each was deliberately left out rather than
  invented.

**Search and Filters** (`App.jsx`'s `searchQuery`/`watchlistFilter`
state, applied by pure functions `matchesSearch`/`matchesWatchlistFilter`)

- Frontend-only, presentation-layer filtering over already-fetched
  arrays. Neither triggers a fetch, changes the global attention
  count/breakdown shown in the banner (computed from the full,
  unfiltered `attentionItems`), mutates a checkpoint or `ChangeEvent`,
  or affects polling — search/filter changes never call `loadAll()`.
- Search matches `symbol`/`exchange`, case-insensitively, applied
  identically to the watchlist table and to attention cards.
- Filters (`All`/`Attention`/`Normal`/`Baseline Pending`) apply only to
  `WatchlistTable`'s rows, derived entirely from fields `GET /watchlist`
  already returns (`change.has_baseline`, membership in the already-
  fetched attention list) — no new classification logic.
- A search/filter combination that matches zero rows renders a distinct,
  truthful "no results" message — never the same message used for a
  genuinely empty watchlist or a genuinely zero-item attention list (see
  Failure Model below).

**Add Stock** — a form (`addSymbol`/`addExchange`/`addFormOpen` state)
calling `POST /watchlist/instruments`. Clears and re-fetches
(`loadAll()`) only after a confirmed success; a failure leaves the form
and existing state untouched (no optimistic membership). A newly added
instrument never fabricates a meaningful change or a baseline — it
enters exactly the same `baseline_pending` state as any other
never-checkpointed instrument, resolved only by a later explicit Mark
as Seen.

The symbol field is a searchable suggestion dropdown
(`frontend/src/stockSuggestions.js`), backed by a **frontend-maintained,
curated static list of exactly 30 NSE and 30 BSE companies** (plain
`{symbol, name}` pairs, using the same unsuffixed ticker convention
`yfinance_ticker_for` already expects). Focusing the field shows the
full curated list for the currently selected exchange; typing filters
it case-insensitively by symbol or company name. Selecting a suggestion
fills the field with its canonical symbol; changing NSE ↔ BSE swaps the
list and clears the typed symbol only if it isn't also valid for the
newly selected exchange. Submission is rejected client-side (no network
call) unless the typed text exactly matches a curated entry for the
current exchange — arbitrary free text can never reach `POST
/watchlist/instruments`. **This list is intentionally NOT a live,
market-wide search** — see `decisions.md`'s "Add Stock curated
suggestion dropdown; unrestricted instrument discovery deliberately not
implemented" for why: the available provider (`yfinance`) has no
documented, reliable way to constrain a search to exactly NSE or BSE
(its `Search`/`Lookup` helpers wrap Yahoo's own undocumented global
autocomplete endpoint, with no verified exchange-filtering contract),
so building live discovery on top of it was assessed and deliberately
not attempted rather than shipping an unverified filter.

**Mark as Seen / Mark All as Seen** — `handleMarkAsSeen`/
`handleMarkAllAsSeen` call the corresponding backend routes, tracked by
their own in-flight/error state (per-instrument `inFlightIds`/
`actionErrors` for the former, dedicated `markAllInFlight`/
`markAllError`/`markAllPartialMessage` for the latter, since Mark All
affects many instruments at once). Both re-fetch via the same
`loadAll()` only after a *confirmed* backend success — neither ever
optimistically removes an attention item or marks a row acknowledged
before the backend confirms it. `markAllPartialMessage` truthfully
surfaces the backend's own `skipped` list rather than implying every
instrument was acknowledged.

**View Details** — a local `useState` toggle inside `AttentionCard`
(`AttentionSection.jsx`), built entirely from props the card already
has. No route, no modal, no API call, no checkpoint write, no
acknowledgement — see `decisions.md`'s "View Details as a local
expandable card" decision. Its "Result: Threshold crossed / Below
threshold" row is a plain `>=` restatement of the same locked 2.0%/2.0×
constants the backend already used to flag the item meaningful — it
does not independently decide meaningfulness.

**Attention card identity (React key)** — each rendered `AttentionCard`
is keyed by `item.checkpoint_id`, not `item.instrument_id`. This matters
because multiple independent `ChangeEvent`s can legitimately share one
instrument (different checkpoint versions, both still active/
unacknowledged) — `checkpoint_id` is the true stable identity of one
specific attention event, while `instrument_id` only identifies which
stock it's about. Keying by `instrument_id` caused a real, QA-found bug
(duplicate React keys, and stale cards left behind in the DOM across a
re-render when the array shrank) — see `decisions.md`'s "Attention card
React-key identity fix." The same reasoning applies to `AttentionCard`'s
`detailsId` (used for the View Details toggle's `aria-controls`), which
is also derived from `checkpoint_id` for the same uniqueness reason.

**Attention card grid layout** — `.attention-list` is a CSS Grid
(`repeat(auto-fit, minmax(280px, 360px))`) with `align-items: start`.
Without this, Grid's default `stretch` cross-axis alignment forces every
card in a row to match the tallest card's height — so expanding one
card's View Details panel visibly stretched its collapsed row-mates,
leaving empty space inside them. `align-items: start` lets each card
still grow to its own content while no longer dragging its siblings up
with it — see `decisions.md`'s "Attention card grid stretch fix."

**Failure-state truthfulness** (P0 Hardening #6) — `App.jsx` tracks,
independently of the single combined `error` banner string, whether
each of `GET /watchlist`/`GET /watchlist/attention` has *ever*
succeeded (`hasLoadedWatchlistOnce`/`hasLoadedAttentionOnce`). Before
either feed's first success, its section shows a distinct "unavailable"
message rather than the default empty-state copy ("Your watchlist is
empty.", "You're all caught up...") — an absence of data is never
presented as a backend-confirmed empty/caught-up state. `WatchlistTable`
additionally distinguishes a real empty watchlist from a non-empty one
whose current search/filter matched nothing. `api.js` rejects a 200
response with a missing/non-array `instruments`/`attention_items` field,
routing it through the same failure-handling path as a network/HTTP
error rather than letting it crash the page downstream.

## Known Limitations

Consolidated from scattered notes above — none of these are treated as
problems requiring a fix before this hackathon; they are disclosed,
accepted boundaries of this build's scope.

- **Anonymous capability identity is not full authentication.** No
  password, no account recovery, no multi-device linking beyond sharing
  the same browser/cookie. See the Anonymous Identity section above.
- **yfinance/Yahoo Finance freshness is not guaranteed.** No SLA, no
  documented rate limit, no verified exchange-trade-time field — see
  External Dependency below.
- **Request-time market-data fetching is not a background pipeline.**
  Every `GET /watchlist` (and the two Mark Seen/Mark All routes) makes a
  live provider call; there is no shared poll process and no fan-out
  deduplication across concurrent users. See the Current Implementation
  Note under Data Flow above and `decisions.md`'s "On-demand fetch per
  request" entry.
- **No persisted last-known-good snapshot.** A provider failure for an
  instrument reports `unavailable` for that request only, with nothing
  cached to fall back to — unlike the originally-designed
  persisted-poll-loop model, which would have retained a prior valid
  value. This is the direct consequence of the on-demand-fetch deviation
  above, not a separate choice.
- **No frontend automated test harness.** The frontend has no test
  framework (no vitest/jest/testing-library) in `package.json`; frontend
  correctness is verified by `npm run lint`/`npm run build` and direct
  code tracing, not automated tests. The backend has an extensive
  `pytest` suite; this asymmetry is disclosed, not hidden.
- **No real-time WebSocket/SSE stream.** Polling only, from the frontend
  — see `decisions.md`'s "No WebSockets / real-time push" decision.
- **No transaction/distributed-lock layer.** Every write is a
  single-document atomic MongoDB operation; see Database Integrity
  above for why this has been sufficient for every race actually found.
- **No unrestricted/market-wide instrument discovery.** Add Stock's
  suggestion dropdown is a curated static list of 30 NSE + 30 BSE
  companies, not a live search — `yfinance` has no documented, reliable
  way to constrain a search to a specific exchange, so live discovery
  was deliberately not built rather than shipped on an unverified
  filter. See the Frontend Architecture Add Stock section above and
  `decisions.md`'s corresponding entry.
- **Hackathon-scale architecture throughout** — a handful of users, a
  handful of instruments, one FastAPI process, one MongoDB database. No
  claim of horizontal scalability, multi-region deployment, or
  production-grade uptime is made anywhere in this document.

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
