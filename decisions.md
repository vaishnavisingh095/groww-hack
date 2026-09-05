# Decisions Log: Smart Market Watchlist

## Decision: Meaningful-change signals for the hackathon build

### Problem
"Meaningful change" needed a concrete, deterministic definition before any
detection logic could be designed.

### Options
- Price movement only.
- Price movement + volume anomaly.
- Price movement + volume anomaly + volatility.

### Decision
Price movement + volume anomaly. Volatility deferred to Should-Have.

### Why
Two independent, well-understood, easily-explained signals are enough to
demonstrate real engineering depth in the Change Engine without needing a
volatility model (e.g., rolling standard deviation) that requires
historical tick data we won't have time to properly source and validate
in 72 hours.

### Trade-off
We won't catch "high volatility with no net price change" patterns
(e.g., a stock whipsawing intraday but closing flat). Acceptable: this is
a rarer and harder-to-explain pattern than the two we do cover.

### Consequence
The Change Engine's `signals` object and thresholds are built around
exactly two named values; adding volatility later means adding a third
named threshold and signal, not restructuring the engine.

---

## Decision: Market data provider — `yfinance` library (revised)

### Problem
Real Indian equity data requires either a broker API (Zerodha Kite, ICICI
Breeze — both need account setup/KYC), a paid vendor (e.g., TrueData), or
a free/open-source route to Yahoo Finance data. We needed a source usable
within 72 hours with no paid signup friction, no mock data in the demo,
and — per explicit direction — no invented or assumed rate limits.

### Options considered
- Broker API (requires existing trading account + app registration).
- Paid market-data vendor.
- A third-party hosted REST wrapper at a bare IP address (`0xramm`'s
  "Indian-Stock-Market-API" on GitHub), investigated first.
- `yfinance` — an open-source Python library calling Yahoo's own
  undocumented internal endpoints directly.
- Fully simulated/mocked data generator (explicitly excluded by
  direction: no mock data in the actual demo).

### Investigation performed
The bare-IP REST wrapper was investigated first and its README claimed a
self-stated "60 requests/minute" limit and a 30-second minimum cache
recommendation. On review, this candidate was rejected before being
locked in: it is a single-maintainer hosted service at a numeric IP with
no domain, no HTTPS, no ownership verification, and no way to confirm the
README's rate-limit claim is authoritative or current — using it would
have meant *inventing* trust in a number we couldn't verify, which
conflicts directly with the instruction not to assume rate limits.

`yfinance` was then investigated as the alternative: confirmed via
multiple current (2026-dated) sources to support NSE (`.NS`) and BSE
(`.BO`) tickers, confirmed to support batched multi-ticker calls, and
confirmed via its own GitHub issue tracker to have **no official
published rate limit** — because it calls an undocumented, unofficial
Yahoo endpoint, there is no rate-limit contract to read. Community
reports on the issue tracker mention an unofficial, unconfirmed figure
near 360 requests/hour, and separately document a case where a
previously-stable high-volume workflow started receiving 429 errors with
no warning — evidence that whatever limit exists is both undocumented
and subject to change without notice.

### Decision
Use `yfinance`, accessed only through a `MarketDataProvider` abstraction,
with no invented rate-limit number — the polling loop reacts to actual
429/error responses via backoff rather than assuming a safe ceiling in
advance (see `architecture.md` backoff policy).

### Why
`yfinance` calls Yahoo directly rather than through an unverifiable
third-party intermediary, is open-source and inspectable, and is
widely-used enough that its failure modes (429s, occasional NSE/BSE
reliability issues) are documented by other users rather than unknown.
None of this makes it *reliable* — it makes it *honestly uncertain*,
which is the correct foundation for a system whose freshness/failure
model is supposed to reflect reality rather than an assumed guarantee.

### Trade-off
We have no rate-limit number to design a safety margin against in
advance — the system must be reactive (backoff on actual 429s) rather
than proactive (staying under a known ceiling). We also don't yet know
the true data latency for NSE/BSE specifically, or whether our exact
target tickers behave reliably — these are named as explicit unresolved
risks requiring a live test before implementation (see
`architecture.md`, "What remains genuinely unknown").

### Consequence
The Market Data Service's poll loop must implement real backoff logic
(not just a fixed retry), and a live-test script must run and its
results reviewed before the Feature Design Checkpoint for the Market
Data Service — this is now a required pre-implementation step, not
optional due diligence.

### Confirmation (live test performed, GO issued)
A live test was run from an unrestricted network environment against all
5 target NSE symbols (`RELIANCE.NS`, `TCS.NS`, `HDFCBANK.NS`, `INFY.NS`,
`ICICIBANK.NS`) during active NSE market hours. All 5 succeeded on both
of two fetch passes, with a batched multi-symbol call also succeeding in
0.234 seconds. **GO is confirmed** for the hackathon MVP based on this
evidence. This is one clean run under normal conditions, not a
reliability guarantee — the reactive-backoff design, the "no assumed
rate ceiling" stance, and the `MarketDataProvider` abstraction all remain
unchanged, since none of them depended on the test succeeding to be the
right design. See the architecture.md External Dependency section for
the full confirmed field mapping and the unknowns that remain open even
after this successful test (practical rate ceiling, true exchange
latency, off-hours behavior).

---

## Decision: Shared poll loop instead of per-request or per-user fetching

### Problem
If N users each have a watchlist of M instruments, naively fetching data
per-request or per-user would multiply API calls by N — risking
rate-limiting on a provider with no documented ceiling to design against
(see provider decision above), and wastefully re-fetching the same
instrument multiple times per minute regardless of the actual limit.

### Options
- Fetch live from the provider on every `GET /watchlist` request.
- Per-user background polling.
- One shared backend poll loop over the union of all distinct instruments
  across all watchlists, using `yfinance`'s batch call.

### Decision
One shared poll loop over distinct instruments, batched, every 60 seconds
(target), with reactive backoff on actual rate-limit responses.

### Why
Whatever `yfinance`'s real, undocumented limit turns out to be, it is
per-source, not per-user — so the number of calls must scale with the
number of *distinct instruments being tracked platform-wide*, not with
the number of users or requests. A watchlist app realistically tracks a
small number of large-cap instruments in a hackathon demo; one batched
call per minute is the smallest request volume the design can produce,
which is the best available mitigation given that no safe ceiling can be
confirmed in advance (see the unresolved provider unknowns).

### Trade-off
Data is only as fresh as the last successful poll cycle — targeted at
60 seconds, but potentially older under backoff or provider delay (see
Freshness Policy decision) — not truly real-time, and a burst of
newly-added instruments has to wait for the next cycle to get their
first snapshot. Accepted: this is explicitly not a real-time trading
terminal, and the freshness model surfaces actual computed age rather
than implying a guarantee.

### Consequence
`GET /watchlist` reads only ever hit MongoDB, never the external
provider directly — this is what keeps read latency low and decouples
user-facing performance from provider reliability entirely.

---

## Decision: Freshness is computed and displayed, never claimed as a guarantee

### Problem
A 60-second poll target could be misread — by us or by users — as a
promise that data is never more than 60 seconds old. That would be false:
`yfinance` has no documented latency guarantee, the poll loop can fall
behind under backoff, and NSE/BSE data delay via Yahoo is itself an
unverified unknown (see provider decision above).

### Options
- Present the poll interval as an implied freshness guarantee (say
  nothing further, let users assume "60 seconds" means "≤60 seconds
  old").
- Compute and display actual freshness/age on every snapshot, explicitly
  distinct from the polling target.

### Decision
Compute and display actual freshness always; never state or imply a
guarantee tighter than what we can verify.

### Why
The product's core differentiator is trustworthy, explainable
information — silently overstating freshness would undermine exactly the
trust the rest of the system (deterministic change detection, persisted
checkpoints) is built to earn. "Fresh · 42s ago" is a true statement we
can defend; "updates every 60 seconds" alone would imply a guarantee we
cannot verify, given the unresolved provider unknowns.

### Trade-off
Slightly more UI complexity (status word + computed age, not just a
number) and slightly less "polished/simple" marketing framing. Accepted:
an honest weaker claim is worth more than a confident false one, doubly
so for a finance-adjacent product.

### Consequence
`status` and computed age are mandatory fields on every snapshot returned
to the frontend — there is no code path that returns a bare price without
them.

### Confirmation and correction (live test performed)
The live test decoded the provider's `regularMarketTime` field (the
candidate for `provider_timestamp`) for every successful fetch. The
precise, careful statement of what was observed is:
- `regularMarketTime` was populated on every successful fetch.
- Its decoded value was consistently very close to our own local fetch
  time (within a few seconds).
- It changed between our two fetch passes, 45 seconds apart, by an
  amount consistent with the elapsed time between our requests.
- **We have NOT independently verified the semantic meaning of this
  field.** We do not claim it means "Yahoo response time" or any other
  specific thing — only that its observed behavior (populated, close to
  our fetch time, moves with our polling) makes it unsuitable as an
  independent freshness signal or as an exchange trade timestamp.

This confirms and sharpens the decision above: **our own `fetched_at` is
the sole authoritative timestamp for all freshness/staleness
calculations.** `provider_timestamp` is stored on `MarketSnapshot` for
diagnostics only and must never be used to compute `status`, never
displayed as data age, and never presented to the user as an exchange
trade timestamp.

---

## Decision: Volume-anomaly signal — rate-based, not raw cumulative ratio, same-session only

### Problem
The originally-proposed `current_volume / checkpoint_volume` ratio was
flagged for explicit review: NSE/BSE volume accumulates from market open,
so this ratio conflates "time elapsed since checkpoint" with "unusual
trading activity" — it would show a large, seemingly-meaningful number
for every stock on every check simply because more of the trading day
had passed, regardless of whether anything unusual occurred.

### Options considered
- **A. Checkpoint cumulative-volume ratio** (the original, flawed
  proposal): `current_volume / checkpoint_volume`. Correctness: poor —
  dominated by elapsed time, not anomalous activity. Data requirements:
  none beyond existing snapshots. Complexity: trivial. Explainability:
  superficially explainable but *incorrectly* so — the number would
  imply something untrue. Hackathon suitability: fast to build, wrong to
  ship.
- **B. Volume acceleration/rate over comparable intervals**: compare
  shares/minute before vs. after the checkpoint, both measured within the
  same trading session, using market-open time as a fixed reference
  point. Correctness: sound — cancels out the elapsed-time confound
  entirely. Data requirements: only the existing two snapshots plus a
  known constant (NSE/BSE open time). Complexity: low-moderate — more
  arithmetic than A, no new data source. Explainability: genuinely
  correct and verifiable ("trading 2x faster than earlier today").
  Hackathon suitability: buildable in the available time with data we
  already have.
- **C. Historical/rolling volume baseline** (e.g., trailing 20-day
  average volume-by-time-of-day): most statistically rigorous in
  principle — accounts for instrument-specific normal patterns.
  Correctness: best. Data requirements: a historical, time-bucketed
  volume dataset we don't have and would need to backfill. Complexity:
  high — new collection, backfill job, holiday/non-trading-day handling.
  Explainability: correct but harder to verify by eye, since it depends
  on a computation the user can't easily check against two visible
  numbers. Hackathon suitability: disproportionate infrastructure for 72
  hours; the "sophisticated-looking but unjustified" trap explicitly
  flagged to avoid.

### Decision
Option B — same-session rate acceleration relative to market open.

### Why
It directly fixes the real correctness flaw in option A without
requiring the new infrastructure option C would need. It uses only data
already present in the system's design (two snapshots plus one known
constant), stays inside the 72-hour budget, and produces an explanation
that is actually true, not merely plausible-sounding — which matters more
for a system whose stated differentiator is explainability.

### Trade-off
Doesn't capture instrument-specific "normal for this stock" patterns or
cross-day effects (e.g., a stock that always trades heavily right after
open would look "accelerating" relative to later hours even on an
ordinary day). This is a real limitation, explicitly accepted rather than
hidden — the threshold (`VOLUME_RATIO_THRESHOLD`) is documented as a
starting assumption to validate against real intraday data, not a
scientifically-derived constant.

### Consequence
No new collections or backfill jobs are needed for the volume signal.
The Change Engine only needs the checkpoint's frozen baseline, the
current snapshot, and a hardcoded NSE/BSE market-open constant (9:15 AM
IST) — consistent with the plan's instruction not to add
historical-data infrastructure just to look sophisticated.

### Confirmation and required correction (live test performed)

**Confirmed**: the live test verified `fast_info.last_volume` is
cumulative and monotonically increasing within a session — all 5 target
symbols showed a positive, realistic increase in this field across a
45-second gap between two live fetches. This is exactly the behavior
option B's formula assumes, so the formula's data inputs are validated.

**Required correction surfaced during review — same-session boundary.**
The formula as originally written did not explicitly guard against a
checkpoint from a *previous* trading session being compared against
today's cumulative volume. Cumulative volume resets at the start of each
session; a checkpoint left over from a prior day would produce a
`current.volume - baseline.volume` delta dominated entirely by the
overnight reset, not by any real trading activity — reintroducing
essentially the same category of false signal that option A was
rejected for, just triggered by session boundaries instead of same-day
elapsed time.

**Correction adopted**: volume acceleration is computed only when
`checkpoint.session_date == current_snapshot.session_date`. When they
differ, `volume_acceleration_available = false` and
`volume_acceleration_ratio = null` for that comparison — but price
comparison (`price_change_pct`) is still computed normally, since price
is meaningfully comparable across sessions in a way cumulative
same-session volume is not. This is not a new option to weigh — it is a
necessary completion of option B's own logic, not a design alternative.

**Related, still-open guard**: the live test ran mid-session and did not
exercise a checkpoint created very close to market open, where
`minutes_since_open_to_checkpoint` could be near zero. The defensive
floor on this denominator (documented in `architecture.md`) remains an
implementation requirement not validated by this test.

### Explanation wording (required, corrected)
The explanation string must say `"Trading volume accelerated to
{ratio}× the rate observed before you last checked."` — not "{ratio}x
normal volume" or any phrasing implying a historical-normal baseline.
The signal is a same-session, self-relative rate comparison; wording
that implies "normal for this stock" would overstate what was actually
measured and was not the definition we decided to build.

---

## Decision: Invalid-data rules kept conservative, not invented

### Problem
The original freshness/failure design listed example "implausible
values" to reject (zero volume mid-session, circuit-limit-violating
price jumps) without verifying either was actually a reliable failure
signal. On review, both needed to be checked against evidence rather
than kept as assumptions.

### Options
- Keep the original examples as written (zero volume → invalid,
  circuit-limit-bound violations → invalid).
- Review each against what we actually know and adjust to only what's
  defensible.

### Decision
Adjusted validation to a conservative set with clear justification for
each rule:
- Missing/non-numeric/non-finite price → invalid (rejects the whole
  snapshot).
- Missing/non-numeric/non-finite volume → invalidates only the volume
  signal, not the snapshot's price.
- Negative volume → invalid (a real impossibility for cumulative
  volume).
- Zero volume → **not** automatically invalid.
- Circuit-limit-bound violations → **not** used as a validity check at
  all.

### Why
Zero volume was actually observed in the live test's `history()` 1-minute
bar field under normal, successful conditions — not proof that zero is
always benign, but clear evidence it is not reliably a failure signal
either, so treating it as automatic grounds for invalidation would risk
discarding legitimate data based on an untested assumption. Circuit-limit
bounds require real per-instrument exchange data we do not have; inventing
a plausible-sounding threshold to check against would itself be exactly
the kind of fabricated exchange constraint this project is committed to
avoiding. Negative volume, by contrast, is a real mathematical
impossibility for a cumulative counter and can be rejected with full
confidence. Missing/non-numeric price is rejected outright because a
usable price is the minimum viable output of the whole system; the same
class of problem in volume only removes one derived signal, not the
entire snapshot.

### Trade-off
Some genuinely bad volume data might pass through as "just missing the
volume signal" rather than being flagged more visibly as an error.
Accepted: the alternative (guessing at invalidity rules not backed by
evidence) risks the opposite and worse failure — silently discarding good
data or fabricating exchange knowledge we don't have.

### Consequence
The Freshness Model's `invalid` status is reserved for price-level
problems and the one real volume impossibility (negative); everything
else degrades gracefully (missing volume → signal unavailable, provider
timeout → stale) rather than being lumped into a single broad "invalid"
bucket.

---

## Decision: No WebSockets / real-time push

### Problem
A "smart watchlist" could reasonably be built with live-updating prices
via WebSocket/SSE push to the frontend.

### Options
- WebSocket/SSE push from backend to frontend.
- Client polls the backend on an interval; backend serves from its own
  cached snapshots.

### Decision
Client polls the backend (e.g., every 30–60s); no push infrastructure.

### Why
The underlying data itself only refreshes every 60 seconds server-side —
push infrastructure would create the illusion of real-time updates on top
of data that isn't real-time, which is misleading rather than valuable.
Polling the already-cheap, already-cached `GET /watchlist` endpoint
achieves the same practical freshness with far less engineering surface
(no connection lifecycle management, no reconnect logic, no scaling
concerns for concurrent open connections).

### Trade-off
Slightly less "alive-feeling" UI than push-based updates. Accepted: the
product's differentiator is explainable change detection, not visual
real-time-ness.

### Consequence
Frontend architecture stays simple — no socket library, no connection
state to manage, one fewer category of failure mode.

---

## Decision: No LLM in the core meaningful-change or attention logic

### Problem
An LLM could plausibly generate "why this matters" explanations or even
decide what counts as meaningful.

### Options
- LLM-generated change detection and/or explanations.
- Fully deterministic, rule-based detection and templated explanations.

### Decision
Fully deterministic and rule-based, per explicit constraint.

### Why
"Explainable" for a financial product means a user (or judge) can verify
the reasoning against the raw numbers without trusting an opaque model.
A templated explanation built directly from the same numeric thresholds
used for detection is fully auditable; an LLM-generated explanation is
not, and introduces non-determinism into a system whose core value
proposition is trustworthy, repeatable judgments about real money
decisions.

### Trade-off
Explanations are less naturally-worded than an LLM could produce, and the
detection logic can't flexibly handle novel patterns outside the two
named signals. Accepted: this is the correct trade for a finance-adjacent
tool where trust matters more than eloquence.

### Consequence
All explanation strings are template-based, sourced directly from
`ChangeEvent.signals` — never freeform generated text.

---

## Decision: Checkpoint baseline stored as a frozen copy, not a live reference

### Problem
Should a `Checkpoint` store a reference (foreign key) to the
`MarketSnapshot` it was created from, or copy the relevant values
directly?

### Options
- Reference to a `MarketSnapshot._id`.
- Frozen copy of the values at checkpoint time.

### Decision
Frozen copy.

### Why
`MarketSnapshot` is a single upserted document per instrument, overwritten
every poll cycle — it has no history. A foreign-key reference would point
at a document that no longer represents what it represented at checkpoint
time, silently corrupting every future comparison. Copying the values is
the only way to preserve an honest historical baseline given the
upsert-only snapshot design.

### Trade-off
Some data duplication between `Checkpoint` and historical `MarketSnapshot`
states. Accepted: the duplication is small (three numeric fields) and
correctness here is the invariant that matters most in the whole system
(see architecture.md invariant #2 and #3).

### Consequence
This decision is *why* `MarketSnapshot` can safely remain upsert-only
(see architecture.md trade-off on no tick history) — the Checkpoint
design absorbs the need for historical values, so the snapshot collection
doesn't have to.

---

## Decision: No transaction/locking for concurrent checkpoint updates

### Problem
Could two near-simultaneous requests (e.g., duplicate button clicks) to
advance the same user's checkpoint for the same instrument race?

### Options
- Add optimistic locking / versioning on `Checkpoint`.
- Accept last-write-wins with no additional protection.

### Decision
Last-write-wins, no additional protection.

### Why
The only actor who can advance a given user's checkpoint is that same
user, acting on their own watchlist. A race here (e.g., a double-click)
produces at worst a slightly-earlier or slightly-later checkpoint time for
the *same user's own action* — there's no cross-user data corruption risk
and no financial consequence, unlike the rehearsal's SELL/holdings
invariant. Adding locking here would be solving a race condition that
carries no real risk for the actor who causes it.

### Trade-off
In a true multi-device-simultaneous-click edge case, one of the two
"mark as seen" actions could theoretically be silently overwritten by the
other's timestamp. Accepted as immaterial: both actions have the same
intent (acknowledge current changes), so either outcome is correct from
the user's perspective.

### Consequence
No added complexity for a failure mode with no meaningful downside —
consistent with the protocol's guidance not to add concurrency handling
without a concrete problem that requires it.

---

# Additional Decisions (Documentation Pass)

The entries above use the original template
(Problem/Options/Decision/Why/Trade-off/Consequence). The entries below
document decisions already established in earlier phases but not yet
logged here, using the template requested for this documentation pass
(Decision/Why/Alternatives considered/Why rejected/Consequence). Both
templates coexist in this document intentionally — this section adds
missing coverage, it does not replace or restructure the entries above.

Nothing below is a new decision being made now; each entry describes a
decision already reflected in `plan.md`, `architecture.md`, or the
current codebase, being captured here for the first time.

## Decision: Product thesis

### Decision
The watchlist's job is to monitor meaningful changes on the user's
behalf, so the user does not have to continuously watch prices
themselves. The product surfaces "what changed since you last checked,"
not a live ticking price feed.

### Why
A plain price table requires the user to supply their own attention and
judgment about what matters. The differentiator this project is built
around — persistent checkpoints, deterministic change detection,
explainable reasons — only makes sense if the product's actual job is
answering "did anything worth my attention happen," not just displaying
numbers. This framing is stated in `plan.md`'s Problem section but had
not been captured as its own decision entry until now.

### Alternatives considered
- A live/real-time price ticker (the user does the monitoring).
- A portfolio tracker focused on P&L rather than change detection.

### Why rejected
A live ticker puts the burden of noticing back on the user, which is
exactly what this project is trying to remove — it would also imply a
real-time guarantee the provider (see below) cannot support. A P&L-focused
tracker answers a different question ("am I up or down") than the one
this product targets ("did something meaningful just happen").

### Consequence
Every other design choice in this log — frozen checkpoints, deterministic
thresholds, explicit "mark as seen," honest freshness labeling — exists
in service of this thesis. Feature requests that reintroduce
continuous-monitoring burden on the user (e.g., a live-updating chart)
should be checked against this thesis before being added.

---

## Decision: `MarketDataProvider` as a dedicated abstraction boundary

### Decision
All application code (services, routes) depends only on the
`MarketDataProvider` interface (`get_quotes(symbols) -> list[RawQuote]`).
No module outside `app/providers/yfinance_provider.py` imports `yfinance`
directly.

### Why
`yfinance` is an unofficial, undocumented-endpoint wrapper (see the
provider decision above) — a dependency with this level of uncertainty
should not leak into business logic. Isolating it behind one interface
means the Checkpoint Service, Change Engine, and routes never know or
care that the data came from `yfinance` specifically; they only see
`RawQuote` objects with clearly-defined fields (`last_price`,
`previous_close`, `volume`, `fetched_at`, `fetch_succeeded`,
`error_message`).

### Alternatives considered
- Call `yfinance` directly from the route handlers or services that need
  data.
- Wrap `yfinance` in a thin helper function without a formal interface
  (`ABC`) contract.

### Why rejected
Direct calls would scatter `yfinance`-specific error handling and field
names throughout the codebase, and would make swapping to a broker/paid
provider later a multi-file rewrite instead of a single new class. A
helper function without a formal interface would work initially but
provides no enforced contract that a future alternative implementation
(e.g., a broker-backed provider) must satisfy the same shape.

### Consequence
Swapping providers later means writing one new class implementing
`MarketDataProvider` and changing one instantiation site
(`app/routes/watchlist.py`'s `_provider = YFinanceProvider()`) — no other
file needs to change. This was exercised in practice during testing: the
whole test suite uses `FakeProvider` test doubles implementing the same
interface, with zero test-specific branching in application code.

---

## Decision: On-demand fetch per request, not a separate background poll process (documented implementation deviation)

### Decision
The current implementation fetches live data from the provider
synchronously inside the `GET /watchlist` request handler
(`MarketDataService.fetch_snapshots`, called directly from
`app/routes/watchlist.py`), rather than running a separate,
always-on background poll loop that writes to MongoDB on a timer and
serving reads purely from that cache.

### Why
This is flagged explicitly as a **documented deviation** from the
"Shared poll loop" decision earlier in this log, which describes a
background loop as the target design. The simpler on-demand approach was
implemented first to reach a working end-to-end slice quickly (Phase 2's
explicit goal), and has not yet been revisited.

### Alternatives considered
- The originally-decided background poll loop (a scheduled task
  independent of any single request, writing `MarketSnapshot` documents
  on a timer; `GET /watchlist` would only read from MongoDB).
- The current on-demand approach (implemented).

### Why the on-demand approach was used instead (not "rejected" — a
scope/sequencing choice, not a reversal)
Building the background loop first would have meant standing up
scheduling infrastructure before proving the checkpoint/change-detection
logic worked end-to-end at all. The on-demand approach reuses the exact
same `MarketDataService`/`MarketDataProvider` abstraction the background
loop would have used, so the eventual move to a real poll loop is an
internal wiring change (what calls `fetch_snapshots` and when), not a
redesign of the fetch/assembly logic itself.

### Consequence — real, current trade-offs of the deviation
- The "frontend never calls the provider directly" property still holds
  (the frontend only ever calls our backend), but the "backend read path
  never hits the provider directly" property from the original decision
  does **not** currently hold — every `GET /watchlist` call makes a live
  `yfinance` call.
- No fan-out protection yet: N concurrent users would currently cause N
  concurrent provider fetches for the same instruments, not one shared
  fetch. This did not surface as a problem during single-user manual
  testing but is a known, real gap against the original multi-user
  design intent.
- This should be revisited before any multi-user or higher-traffic use
  — reintroducing the background poll loop (writing to MongoDB on a
  timer, with `GET /watchlist` reading only from MongoDB) is the
  documented target design to return to, not a new decision to make.

---

## Decision: Explicit checkpoints are never silently overwritten by implicit ones

### Decision
`CheckpointService.ensure_initial_checkpoint` only creates a checkpoint
when none exists at all for a (user, instrument) pair. If any
checkpoint already exists — explicit or implicit — calling it is a
no-op. Only `CheckpointService.create_checkpoint_from_snapshot` (the
explicit "mark as seen" primitive) ever replaces an existing checkpoint,
and it always does so unconditionally when called.

### Why
An implicit checkpoint exists to give a first-time instrument a baseline
without requiring the user to know a "mark as seen" action exists (see
`plan.md`'s implicit-checkpoint scope note). It must never be allowed to
quietly move a baseline the user (or the system, on their behalf) already
established — doing so would violate the core invariant that a
checkpoint represents "what the user saw at that point in time," per
`architecture.md`'s Checkpoint design.

### Alternatives considered
- A single `create_or_update_checkpoint` method used by both explicit and
  implicit call sites, distinguished only by a `source` parameter.
- Always refresh the checkpoint to the latest snapshot on every
  `GET /watchlist` call regardless of source (i.e., no distinction
  between explicit and implicit at all).

### Why rejected
A single shared method with a `source` flag would still require the
caller to remember never to pass `source=implicit` for an
already-existing checkpoint — the invariant would depend on caller
discipline rather than being structurally enforced. Always refreshing on
every GET was rejected outright in an earlier decision in this log ("A
checkpoint represents what the user saw... GET /watchlist must NOT
silently advance an existing checkpoint on every request" — this
requirement predates this specific implementation and is restated here
because it's the direct reason `ensure_initial_checkpoint` exists as a
structurally separate, no-op-if-exists method).

### Consequence
Two distinct methods exist specifically so the "never silently overwrite
an existing checkpoint implicitly" rule is enforced by which method is
called, not by a caller remembering a flag correctly. This is tested
directly (`test_ensure_initial_checkpoint_does_not_advance_existing_checkpoint`
and its implicit-source counterpart in `test_checkpoint_service.py`).

---

## Decision: Locked starting thresholds — 2.0% price / 2.0x volume acceleration

### Decision
`PRICE_CHANGE_THRESHOLD_PCT = 2.0` and `VOLUME_ACCELERATION_THRESHOLD =
2.0` are the current, locked starting values for the two meaningful-change
signals. Both thresholds are inclusive (`>=`, not `>`) — exactly 2.0% or
exactly 2.0x counts as meaningful.

### Why
These were supplied as explicit, locked product decisions for the Phase
5 Meaningful Change Engine implementation, not derived or chosen by
engineering judgment. They are kept as named constants in
`app/services/change_engine.py` rather than inline literals, so they are
the first and only place to look if the demo data suggests they need
tuning.

### Alternatives considered
None were offered as alternatives for this decision — the exact values
were supplied as a locked decision. (The earlier decision in this log,
"Meaningful-change signals for the hackathon build," did independently
arrive at a 2% starting value with its own reasoning about typical
large-cap volatility, before this value was later locked explicitly — the
two are consistent, not in conflict.)

### Consequence
A `volume_acceleration_ratio` (or `price_change_pct`) can additionally be
`None`/unavailable rather than simply "below threshold" — the engine
distinguishes "evaluated and below 2.0x" from "could not be evaluated at
all" (e.g., cross-session checkpoint, near-market-open guard, missing
timing data). This distinction is what lets a price-only meaningful
change be reported correctly even when the volume signal cannot be
computed, per the Phase 5 requirement that price signal alone is
sufficient.

---

## Decision: No microservices, queues, Redis, Kafka, WebSockets, Kubernetes, or LLM in the core detection path

### Decision
The system is one FastAPI backend, one MongoDB database, and a React
frontend. The core meaningful-change detection path (Checkpoint Service,
Market Data Service, Change Engine) contains no message queue, no cache
layer, no container-orchestration dependency, no real-time push
transport, and no LLM call.

### Why
None of these were ever required by a concrete, demonstrated problem —
the actual scale (a handful of users, a handful of instruments, a
60-second freshness target) does not need infrastructure sized for a
much larger system. A composite/LLM-based change score was separately
rejected because the product's explainability requirement depends on a
score a user (or judge) can verify against the raw numbers, which an LLM
call cannot guarantee (see "No LLM in the core meaningful-change or
attention logic" above).

### Alternatives considered
- Redis for caching market snapshots between polls.
- A message queue (Kafka or similar) between the poll/fetch step and
  checkpoint/change-detection processing.
- WebSockets/SSE for pushing live updates to the frontend instead of
  polling.
- Splitting the backend into separate services (e.g., a standalone
  "market data service" and a "checkpoint service" as independent
  deployables).

### Why rejected
Redis would cache data that MongoDB already persists at the same
freshness granularity — no measured latency problem exists that a second
storage layer would solve. A queue would decouple two steps
(fetch, detect) that currently run in a single request/response cycle
with no throughput problem to justify the added operational complexity.
WebSockets/SSE were rejected in an earlier decision in this log because
the underlying data only refreshes every ~60 seconds regardless of
transport — push infrastructure on top of non-real-time data would be
misleading, not just unnecessary. Splitting into separate deployable
services would introduce network calls and deployment coordination for
communication that is currently a single in-process Python function call.

### Consequence
Every added dependency in this project has to be justified against a
concrete, demonstrated need (per the engineering protocol governing this
project) rather than adopted because it is common practice elsewhere.
Revisiting any of these (e.g., introducing a real background poll loop,
per the on-demand-fetch deviation documented above) should be evaluated
the same way — a specific, named problem first, then the smallest
sufficient tool for it.

---

## Decision: Price/volume sourced from intraday history bars, not fast_info

### Decision
`YFinanceProvider` now derives `last_price` and `volume` from
`ticker.history(period="1d", interval="1m")` — the latest bar with a
valid Close for price, and the sum of all valid per-minute Volume values
for cumulative session volume — instead of `fast_info.last_price` /
`fast_info.last_volume`. `previous_close` is unaffected by this decision
and continues to come from `info["regularMarketPreviousClose"]`, with
`fast_info.previous_close` as a fallback (see the separate decision
below on why that priority order is what it is).

### Why
Direct runtime evidence on the actual Mac execution environment (not
this sandbox, which cannot reach Yahoo Finance) showed
`fast_info.last_price` and `fast_info.last_volume` returning the exact
same values across three reads taken ten seconds apart during live NSE
market hours. `history(period="1d", interval="1m")` for the same symbol
at the same time returned real, distinct bars with changing prices and
per-minute volumes. `fast_info.last_price`'s own implementation confirms
why: it derives from a 1-year DAILY-interval history call, not live
intraday data, so it does not refresh on a timescale usable for
intraday change detection. This is a genuine data-source limitation
discovered empirically, not assumed in advance.

### Alternatives considered
- Keep `fast_info` for price/volume and accept its refresh cadence as a
  known limitation.
- Use `info["regularMarketPrice"]` / `info["regularMarketVolume"]`
  instead of `fast_info` (these were already the existing fallback
  fields).
- Use `history(period="1d", interval="1m")`'s latest valid Close/summed
  Volume (chosen).

### Why rejected
Accepting `fast_info`'s refresh cadence was rejected because it directly
undermines the product's core mechanism — a checkpoint-vs-current
comparison is meaningless if "current" can be identical to what was
captured seconds or minutes earlier purely due to a caching/derivation
artifact, not real market inactivity. `info["regularMarketPrice"]` /
`info["regularMarketVolume"]` were not separately verified to refresh
faster than `fast_info` in the same diagnostic and were not chosen
without that verification, per the standing rule not to swap one
unverified source for another.

### Consequence
`MarketDataProvider` and `RawQuote` (the abstraction boundary) are
unchanged — this fix is entirely internal to `YFinanceProvider`. Bars
with a non-finite or non-positive Close are skipped when searching for
the latest valid price (never assuming the most recent bar is
automatically usable); bars with a non-finite or negative Volume are
excluded from the sum rather than zeroing out or aborting the whole
calculation. An empty or malformed intraday response now fails the
fetch for that symbol explicitly (`fetch_succeeded=False`, with the real
reason in `error_message`) rather than falling through to a stale
daily-derived value.

### Verified session-boundary behavior (empirical, not assumed)
A separate, standalone diagnostic script (not part of the provider or
test suite) was run directly on the Mac execution environment against
live RELIANCE.NS data:

```
timezone: Asia/Kolkata
first timestamp: 2026-09-04 09:15:00+05:30
last timestamp: 2026-09-04 15:15:00+05:30
number of bars: 361
Bars outside 09:15-15:30 IST: count: 0
```

This confirms, for this real run, that `history(period="1d",
interval="1m")` returned bars starting exactly at NSE's regular-session
open (09:15 IST) with zero bars outside the 09:15–15:30 IST regular
session window — no pre-market or post-market bars were present.
`yfinance`'s own source code contains an explicit post-fetch filtering
step (`fix_Yahoo_returning_prepost_unrequested`) whose own comment states
Yahoo's raw feed can return pre/post-market bars unrequested, which this
filter is designed to remove using the exchange's own reported session
boundaries. This one real run is consistent with that filter working
correctly for RELIANCE.NS; it is evidence from a single symbol on a
single day, not a guarantee validated across all instruments, dates, or
market conditions (e.g., half-day sessions, which the filter's own
source comment specifically calls out as a known trigger for the
behavior it corrects).

---

## Decision: previous_close primary/fallback order — info first, fast_info second

### Decision
`previous_close` is sourced from `info["regularMarketPreviousClose"]` as
the primary source, with `fast_info.previous_close` as a fallback only
if `info` does not supply it. This is the reverse priority from an
earlier revision, which tried `fast_info.previous_close` first with
`info["regularMarketPreviousClose"]` as its fallback.

### Why
Real Mac runtime evidence (documented earlier in this log, under the
initial `previous_close` bug) showed `fast_info.previous_close` can
legitimately return `None` even when `fast_info.last_price` and
`fast_info.last_volume` succeed, because it depends on a separate,
independently-fragile weekly pre/post-market history lookup internally.
The same evidence confirmed `info["regularMarketPreviousClose"]`
correctly held the real prior session's close in that exact failing
case. Once price/volume stopped depending on `fast_info` at all (the
decision above), there was no remaining reason to keep `fast_info` as
the primary source for this one field either — the source already
confirmed more reliable was promoted to primary.

### Alternatives considered
- Keep the prior fast_info-first, info-second order unchanged.
- Swap to info-first, fast_info-second (chosen).
- Drop the `fast_info` fallback entirely now that price/volume no
  longer depend on `fast_info` for anything.

### Why rejected
Keeping the prior order was rejected because it was never demonstrated
to be the more reliable order — it was only ever the initial guess
before `info`'s reliability was separately confirmed. Dropping the
`fast_info` fallback entirely was not chosen because no evidence has
shown `info["regularMarketPreviousClose"]` to be reliable in every
case; keeping a real, working fallback costs nothing and preserves the
existing degrade-gracefully behavior for previous_close specifically.

### Consequence
No change to what `previous_close` ultimately resolves to in the cases
already tested (both sources still checked, in the same conditions as
before) — only the order in which they are tried changed, based on
which one is now known to be more reliable.

---

## Decision: Checkpoint gets a durable, application-assigned `id`, distinct from MongoDB's own `_id`

### Problem
The ChangeEvent persistence milestone requires `ChangeEvent.checkpoint_id`
to durably identify the exact checkpoint VERSION a change was detected
against (per architecture.md's own field description). `Checkpoint`
documents are advanced via `replace_one` keyed on
`(user_id, instrument_id)` (see the earlier "frozen copy" decision) —
MongoDB preserves a document's original `_id` across a `replace_one`
match. Using Mongo's `_id` as `checkpoint_id` would mean a `ChangeEvent`
created against checkpoint version N would, after the user's next
explicit acknowledgement, silently reference the *same* id now held by
checkpoint version N+1 — an incorrect, misleading history.

### Options
- Use MongoDB's `_id` as `ChangeEvent.checkpoint_id`.
- Add a new, application-assigned `Checkpoint.id` field (e.g. a UUID),
  regenerated on every write, and use that as `checkpoint_id`.

### Decision
Add `Checkpoint.id: str`, defaulted to a fresh UUID on every
construction. `CheckpointService._write_checkpoint` already constructs a
new `Checkpoint(...)` object on every explicit advance, so this field's
default factory gives each checkpoint version a new, correct identity
with no other logic change.

### Why
This is the smallest schema change that makes the existing, already-
documented `ChangeEvent.checkpoint_id` field actually correct. It does
not change the storage pattern (still one document per
`(user_id, instrument_id)`, replaced in place) — it only adds the one
piece of identity that pattern was missing.

### Trade-off
None material: one additional string field per `Checkpoint` document.

### Consequence
A `ChangeEvent`'s `checkpoint_id` now durably and correctly identifies
"the specific baseline this was detected against," immune to the
checkpoint later being replaced. See `test_checkpoint_service.py`'s
`test_advancing_a_checkpoint_assigns_a_new_id`.

---

## Decision: ChangeEvent persistence and lifecycle (get-or-create + acknowledge)

### Problem
`ChangeEvent` has been a fully-specified, unit-tested Pydantic model and
a declared MongoDB index since Phase 1, but was never actually written
to the database anywhere — `evaluate_change()`'s result was computed
fresh on every `GET /watchlist` and discarded after the response was
sent. This meant the same meaningful change was recalculated (but never
remembered) on every refresh, and an explicit "mark as seen" had nothing
to acknowledge.

### Options
- Persist a `ChangeEvent` unconditionally whenever `evaluate_change()`
  reports `meaningful_change=True`.
- Persist a `ChangeEvent` only when a real acknowledged (explicit)
  baseline exists and the underlying data is trustworthy, with a
  find-before-insert dedup check keyed on the checkpoint version.

### Decision
The latter: a new `ChangeEventService` with two operations —
`get_or_create_active` (called from `GET /watchlist`, per-instrument,
after the existing Change Engine evaluation) and `acknowledge_active`
(called from both explicit checkpoint-advancement endpoints, after that
instrument's checkpoint write succeeds). `get_or_create_active` only
creates a document when an explicit `Checkpoint` exists, the current
snapshot's `status` is `ok`, and `meaningful_change` is `True`; if a
`ChangeEvent` already exists for that exact
`(user_id, instrument_id, checkpoint_id)`, it is returned unchanged —
never re-created, never overwritten with a newer recalculation's
values.

### Why
`GET /watchlist` remains the only place `evaluate_change()` runs (no new
trigger path, no background poller, no queue), so persisting its result
there — gated correctly — is the smallest change that gives ChangeEvent
real persistence. Keying reuse on `checkpoint_id` rather than, e.g., a
time window means "the same logical change" is defined precisely as
"the same checkpoint version," which is exactly what the checkpoint
semantics milestone already established as the unit of "what changed
since the user last acknowledged."

Acknowledging is deliberately triggered only from the explicit
mark-as-seen endpoints, and only after that instrument's checkpoint
write has already succeeded — never from `GET /watchlist` (opening or
refreshing the page is not acknowledgement, per the checkpoint semantics
contract), and never before the checkpoint write it depends on, so a
failed checkpoint write can never falsely acknowledge an active event.
Acknowledgement is scoped to "all currently-active events for this
`(user_id, instrument_id)`," not to a specific `checkpoint_id`, because
at most one checkpoint is ever active per `(user, instrument)` — there
is nothing else it could mean to acknowledge.

### Trade-off
`GET /watchlist` now performs a conditional database write in addition
to reads — a deliberate, approved exception to the checkpoint semantics
contract's "reads don't write" framing, because a `ChangeEvent` records
an observed market FACT ("this checkpoint's baseline was meaningfully
exceeded"), not user acknowledgement. The two remain conceptually and
mechanically distinct: `GET` may write facts, only explicit action may
write acknowledgement.

### Consequence
The Attention Engine (not built yet) will have real, persisted,
deduplicated `ChangeEvent`s to read from once it exists, rather than
needing its own recomputation or persistence logic.

---

## Decision: A stale MarketSnapshot may be displayed but never creates a ChangeEvent

### Problem
The Freshness Model already forbids presenting stale data as if it were
fresh, but it does not say whether a `stale`-but-present snapshot should
be *eligible for change detection persistence* — i.e., whether a
seemingly-large move measured against old data should be allowed to
create a permanent `ChangeEvent` record.

### Options
- Allow `ChangeEvent` creation from any present snapshot regardless of
  `status` (`ok` or `stale`), since the underlying value is real, not
  fabricated.
- Restrict `ChangeEvent` creation to `status == ok` snapshots only;
  `stale` may still be displayed, but never creates or reuses a
  persisted event.

### Decision
The latter. `ChangeEventService.get_or_create_active` checks
`snapshot_status != SnapshotStatus.OK` and returns `None` (no
persistence) for `stale`, in addition to the pre-existing structural
exclusion of `invalid`/`unavailable` (which never even produce a
`MarketSnapshot` or reach this code path at all).

### Why
A persisted `ChangeEvent` is a durable claim ("this specific move was
detected and is worth the user's attention"), not just a display value —
it is stronger than "here is the last number we have, marked old." A
`stale` snapshot may be old enough that the "meaningful" comparison it
produces no longer reflects anything the user should trust as a
detected event, even though it is still honest to *show* the number with
its age. This does not change how staleness is displayed anywhere in
the existing freshness UI/response fields.

### Trade-off
A genuinely large, real move that happens to be measured against a
`stale` snapshot will not create a `ChangeEvent` on that particular
request — it will be picked up on a later request once a fresh snapshot
is available and the same checkpoint is still active (the dedup key is
per checkpoint version, not per request, so this is not lost, only
delayed).

### Consequence
`ChangeEventService` takes `snapshot_status` as an explicit, required
input rather than inferring eligibility from `change_result` alone —
the Change Engine's `ChangeResult` has no concept of snapshot freshness
and is not the right place to add one.

---

## Decision: ChangeEvent uniqueness is (user_id, instrument_id, checkpoint_id), excluding `acknowledged`

### Problem
Nothing previously enforced "at most one ChangeEvent per checkpoint
version" at the database level — the only existing `change_events` index
was a non-unique lookup shape `(user_id, acknowledged)`. Without a real
constraint, a bug in application-level dedup logic (or a genuine
concurrent request race) could silently produce duplicate events for
the same detected change.

### Options
- No database-level constraint; rely entirely on the service's
  find-before-insert check.
- A unique compound index on `(user_id, instrument_id, checkpoint_id)`.
- The same index, but including `acknowledged` in the key (so an
  acknowledged and an unacknowledged event for the same checkpoint could
  coexist).

### Decision
A unique compound index on `(user_id, instrument_id, checkpoint_id)`,
explicitly excluding `acknowledged`.

### Why
The business invariant is "one ChangeEvent per checkpoint version,"
full stop — not "one active one and separately one acknowledged one."
An event transitions from unacknowledged to acknowledged in place; that
transition must never be modeled as a second document. Enforcing this
at the database level (not just in application code) is what makes it
a real constraint rather than a convention that could be silently
violated by a future code path that forgets to check first —
`ChangeEventService`'s own find-before-insert check remains only the
common-case fast path; the index is the actual source of truth under
concurrency, with a `DuplicateKeyError` fallback in the service that
re-fetches and reuses instead of raising.

### Trade-off
None material — no legitimate use case requires more than one
ChangeEvent per checkpoint version to exist simultaneously.

### Consequence
`ensure_indexes` now creates `uniq_user_instrument_checkpoint_change_event`
alongside the pre-existing `user_acknowledged_lookup` index (kept,
unchanged — still the correct shape for a future Attention Engine's
"active events for this user" query). See
`test_db_indexes.py`'s new duplicate-rejection tests.

---

## Decision: Attention Engine exposed as its own endpoint, not embedded in GET /watchlist

### Problem
`architecture.md`'s illustrative `GET /watchlist` response (written
before either the ChangeEvent-persistence or Attention Engine
milestones existed) embeds an `active_change` object with
`attention_rank` inline, per instrument — implying attention ranking
was originally envisioned as part of the same response. By the time the
Attention Engine (`AttentionEngine.get_ranked_active_items`) was built
and approved, the real question was whether to now retrofit that
original inline shape into `GET /watchlist`, or expose it as a
separate read.

### Options
- Extend `GET /watchlist`'s existing response with an embedded
  `active_change`/`attention_rank` field per instrument, matching
  `architecture.md`'s original illustration.
- A new, separate endpoint (`GET /watchlist/attention`) that calls
  `AttentionEngine` independently.

### Decision
A separate endpoint: `GET /watchlist/attention`, returning
`{"attention_items": [...]}`.

### Why
Modifying `GET /watchlist`'s response shape would be a breaking change
to an already-committed, already-tested contract (multiple existing
tests assert its exact key set), with no corresponding frontend change
in scope for this milestone. `AttentionEngine` does its own MongoDB
round-trip per active event (symbol resolution) — running it
unconditionally on every `GET /watchlist` call would add cost to the
highest-traffic read path for a value nothing yet consumes there. This
also keeps the API surface aligned with the same separation already
established for the underlying logic ("Keep Change Detection separate
from Attention Ranking... do not fold ranking logic into the Change
Engine or vice versa") — extended here to mean their API surfaces stay
separable too, until a concrete frontend requirement forces a merge.

### Trade-off
The actual `GET /watchlist` response no longer matches
`architecture.md`'s original illustrative shape for this field (it
never did carry `active_change`/`attention_rank` even before this
decision — that illustration was always aspirational, not yet
implemented). This is a known, disclosed documentation/implementation
gap, not a new one introduced by this decision.

### Consequence
`GET /watchlist/attention` is a second, independent read endpoint over
the same underlying `ChangeEvent` data — both endpoints remain
correctly read-only (neither creates, advances, or acknowledges
anything), and either can be called, polled, or omitted independently
by the frontend without affecting the other's contract.

---

## Decision: session_date is derived from the actual intraday bar, not from fetched_at

### Problem
`session_date` (used by the same-session volume-acceleration check in
the Change Engine, and copied into `Checkpoint.session_date` at
acknowledgement time) was computed as `fetched_at.date()` — our own
backend clock — rather than the calendar date the underlying
price/volume data actually belongs to. `yfinance`'s
`history(period="1d", interval="1m")` can return the most recently
COMPLETED trading session's bars when the market is closed (before
open, after close, non-trading days), meaning a snapshot fetched
outside market hours could carry real data from a prior session while
being stamped with today's calendar date. Because our own same-session
comparison (`checkpoint_session_date != current_session_date`) trusts
`session_date` completely, this risked exactly the failure `plan.md`'s
Hardening Priority #5 names explicitly: a checkpoint's volume from a
prior trading day being silently treated as same-session with a later
observation, once both happened to be (mis)labeled with the same
fetch-time date.

### Options
- Leave `session_date` derived from `fetched_at` (the original MVP rule
  described in this document's "Session recognition" note).
- Derive `session_date` from the actual intraday bar `last_price` came
  from.

### Decision
Derive `session_date` from the same bar used for `last_price`, read
directly from that bar's own index timestamp in `YFinanceProvider` —
yfinance's intraday history index is already timezone-localized to the
exchange (`Asia/Kolkata` for NSE), so no separate UTC/IST conversion is
needed. `fetched_at` is unchanged and remains the sole authoritative
timestamp for freshness/staleness; `provider_timestamp` remains
diagnostics-only and is not used for this either.

### Why
`session_date`'s entire purpose is to answer "which trading session
does this observation belong to" — that question can only be answered
correctly by the data itself, not by when our backend happened to ask
for it. yfinance's intraday index already carries the exchange-local
timestamp for every bar, so this is a data-plumbing correction, not a
new provider capability or a new data source.

### Trade-off
None material — this is strictly a correctness fix to an existing
field; no formula, threshold, checkpoint semantic, or provider choice
changed. Closed-market yfinance response behavior itself remains
empirically unverified from this environment (an existing, unchanged
limitation — see the "Price/volume sourced from intraday history bars"
decision above — not resolved by this fix).

### Consequence
`RawQuote` gained one field (`session_date: date | None`), populated
only by the provider and propagated unchanged by `MarketDataService`
into `MarketSnapshot.session_date` and, from there, into
`Checkpoint.session_date` exactly as before. The same-session
comparison logic in `change_engine.py` is unchanged — only the
correctness of the value being compared changed. See
`architecture.md`'s "Session recognition" note, corrected alongside
this entry.

---

## Decision: Attention-first web dashboard

### Problem
With the Attention Engine and its `GET /watchlist/attention` endpoint
already built, the frontend needed a home-screen layout. A generic
watchlist table (price, change, volume, one row per instrument) puts
the burden of noticing what matters back on the user — the same burden
the product's checkpoint/change-detection/attention machinery exists to
remove (see the "Product thesis" decision above).

### Options
- A single watchlist table, attention data (if shown at all) as an
  extra column or badge per row.
- A dedicated attention-first section — "Since You Last Checked" —
  rendered above the full watchlist table, itself broken into High
  Attention and Worth Checking groups, with the full table remaining
  available below for anything not currently flagged.

### Decision
Attention-first: the desktop dashboard prioritizes "Since You Last
Checked" → High Attention → Worth Checking → the full watchlist table,
in that vertical order.

### Why
The product's job is surfacing meaningful changes quickly, not making
the user manually scan a table to find them — the same reasoning
already governing the backend (checkpoints, deterministic thresholds,
persisted `ChangeEvent`s). The UI's information hierarchy should match
that thesis rather than presenting attention data as a minor addition
to a conventional table.

### Trade-off
Users who want a plain, uniform table view see the attention section
first regardless of preference — no per-user layout choice is offered.
Accepted: out of scope for this build, and the full watchlist table
remains fully present below, unshortened.

### Consequence
`AttentionSection` renders above `WatchlistTable` in `App.jsx`;
`AttentionSection` further partitions its items into "High Attention"
and "Worth Checking" groups (see the Attention Hierarchy work below).
No backend change — this is purely a frontend composition/ordering
decision over data both endpoints already returned.

---

## Decision: Light desktop dashboard visual direction

### Problem
Once the attention-first layout existed, it needed a visual treatment
(colors, spacing, surfaces, typography) — undecided territory not
covered by any prior backend-focused decision in this log.

### Options
- A dark-themed dashboard.
- A light/off-white, "premium financial dashboard" visual language —
  dark navy typography, restrained market colors (green/red kept
  deliberately muted rather than saturated), subtle borders and
  shadows, rounded surfaces, a wider desktop canvas.

### Decision
The light/off-white direction, applied via a CSS custom-property token
layer in `App.css` (page/surface/border/text/positive/negative/
attention-level/freshness tokens).

### Why
This is a presentation-layer choice for the attention-first product
decided above — it does not encode or imply any product-logic or
business-rule change. It must not be read as an architecture decision;
it is recorded here only so the visual direction isn't silently
undocumented.

### Trade-off
None material — a token layer means the direction can be revisited
later by changing token values, not by touching component structure.

### Consequence
`App.css` gained a `:root` token layer; existing components were
re-themed to consume the tokens rather than hardcoded colors.
`architecture.md` intentionally does not enumerate individual token
values (padding, radius, exact hex codes) — those belong in the CSS
itself, not in an architecture document; see the "Preserve semantic
distinctions" entry below for what did warrant recording.

---

## Decision: No "Mark all as seen" CTA introduced during UI polish

### Problem
A UI-polish task described styling the "Since You Last Checked" banner
with a prominent "Mark all as seen" call-to-action, on the assumption
that this functionality already existed. It does not: the frontend only
ever implemented a per-item "Mark as seen" action
(`markInstrumentAsSeen`); no `markAllAsSeen` call exists in `api.js`,
even though the backend endpoint (`POST /watchlist/checkpoint`) does.

### Options
- Style the banner without any mark-all control, exactly as the
  frontend currently behaves.
- Add a real "Mark all as seen" button now, wiring it to the existing
  backend endpoint.

### Decision
Style the banner without a mark-all CTA. (Confirmed with the user via
an explicit choice during Pass 2; this option was selected.)

### Why
UI polish is scoped to visual presentation of existing behavior — adding
a mark-all action would be a new interaction, a new client-side call,
and new state (which instruments it affects, how errors are surfaced
across many instruments at once), i.e. a functional feature, not visual
polish. Introducing it silently, just because a reference description
assumed it existed, would misrepresent what the product does today.

### Trade-off
The banner has no bulk action; a user with many flagged instruments
must still act on them one at a time via each card's own "Mark as seen"
button. Accepted: a real mark-all feature is a legitimate future addition,
but belongs in its own scoped implementation pass, not folded into
visual polish.

### Consequence
The "Since You Last Checked" banner (`.attention-banner` in `App.css`)
contains only the heading, count, and high/worth-checking breakdown —
no button. `POST /watchlist/checkpoint` remains implemented on the
backend and unused by the frontend, exactly as before this pass.

---

## Decision: No "View details" action added during UI polish

### Problem
A UI-polish task's card redesign spec listed "View details" as a
required secondary action on each attention card. No such destination,
route, or handler exists anywhere in the frontend (there is no router
and no per-instrument detail view).

### Options
- Add a "View details" button/link even without a real destination
  (e.g., disabled, or pointing nowhere).
- Omit it entirely.

### Decision
Omit it. No "View details" action exists on the attention card.

### Why
The same UI-polish scope already governing the mark-all decision above
applies here: inventing an action with no real destination would either
mislead the user (a button that does nothing) or require building new
navigation/routing, which is new functionality, not visual polish. The
task's own instructions to not add new actions during this phase
resolved this without needing a separate confirmation.

### Trade-off
None material — the card simply has one action ("Mark as seen"),
matching what the product actually supports today.

### Consequence
`AttentionCard` in `AttentionSection.jsx` renders a single action
(`onMarkAsSeen`). Adding a real details view remains a legitimate future
feature, not addressed by this decision.

---

## Decision: Attention-card data presentation — existing fields only

### Decision
The attention card presents: current price, since-checkpoint movement
(`price_change_pct`), day-over-day movement where available
(`percent_change`, from the joined `GET /watchlist` row), the existing
structured "What changed / Why it matters / Signals" explanation, the
existing price and volume-acceleration signals, freshness/detection
timing (`freshness_label`, `detected_at`), and the existing "Mark as
seen" action. All of these are existing, already-available data values
returned by the two existing endpoints — no new detection or scoring
logic was introduced in the frontend to produce any of them.

### Why
This is recorded explicitly because the card's visual redesign (Pass 3)
touched nearly every field it displays, and it would be easy for a
reader of the diff to assume some of these values (e.g., the
since-checkpoint vs. day-over-day split, or the detection timestamp)
were newly computed by the frontend. They were not — see
"Frontend Consumption of Attention Data" in `architecture.md`.

### Alternatives considered
None — this entry documents what was built, not a choice between
options.

### Consequence
Any future change to what the card displays should be checked against
this list: a new figure on the card should trace back to an existing
backend-computed field, not a new frontend calculation, unless a
separate, explicitly-scoped decision says otherwise.

---

## Decision: Company name not shown — limitation, not fabrication

### Problem
A UI-polish reference for the attention card implied a company name
(e.g., "Reliance Industries Ltd.") alongside the ticker symbol.
`GET /watchlist` does not return a company name field — only `symbol`
and `exchange`.

### Options
- Fabricate or hardcode a name-lookup table in the frontend.
- Change the backend to fetch and return company names, in scope for a
  UI-polish pass.
- Show the data that's actually available (`exchange`) instead, and
  omit any name.

### Decision
Show `exchange` next to the symbol; no company name is displayed.

### Why
Fabricating names risks incorrect or stale data with no backend source
of truth behind it; changing the backend's data contract is out of
scope for a visual-polish pass (see the UI-polish constraints repeated
across every pass in this project). Displaying real, available data
(`exchange`) is preferable to inventing unavailable data.

### Trade-off
Cards are less immediately recognizable to a user who thinks in company
names rather than tickers. Accepted: a real fix (backend returning
company names) is a legitimate future addition, not a documentation or
visual-polish concern.

### Consequence
`AttentionCard` renders `item.symbol` and, when present,
`watchlistEntry.exchange` — nothing else identifies the instrument.

---

## Decision: No "strongest signal" attribution added

### Problem
A UI-polish reference suggested visually highlighting which signal
(price or volume) most drove an item's attention ranking — a
"strongest signal" treatment. The persisted data does not support this:
`AttentionItem`'s `attention_score` is `max(price_strength,
volume_strength)` (see `architecture.md`'s Attention Engine design),
but neither the API response nor any stored field says *which* of the
two produced that maximum for a given item.

### Options
- Compute which signal is "strongest" in the frontend, by comparing
  `price_change_pct` against `volume_acceleration_ratio` relative to
  their respective thresholds.
- Do not introduce this comparison in the frontend; show both signals
  plainly, as already done.

### Decision
No strongest-signal attribution. Both signals are shown plainly, with no
frontend judgment about which one mattered more.

### Why
Computing a per-item "strongest signal" in the frontend would be new
scoring logic living outside the Change Engine / Attention Engine — this
project's standing rule is that meaningful-change and attention scoring
are 100% backend-computed and deterministic, never duplicated or
approximated on the client. A UI-polish pass is explicitly not the place
to introduce a new frontend calculation, however small.

### Trade-off
The card cannot visually emphasize "this is a volume-driven spike"
versus "this is a price-driven spike." Accepted: if this attribution is
wanted, it belongs in the backend (e.g., a new field on `ChangeEvent` or
`AttentionItem`), as its own scoped decision — not inferred client-side
from existing numbers.

### Consequence
The Signals block lists price and volume values side by side with no
relative emphasis between them.

---

## Decision: Preserve semantic distinctions through UI changes

### Decision
Four product-level distinctions must remain stable through any future
UI change, not just the polish passes covered by this documentation
sync:
- Since-checkpoint movement (`price_change_pct`) is not day-over-day
  movement (`percent_change`) — they come from different comparisons
  and must never be merged into one figure or unlabeled.
- Attention level (`attention_level`) is not market direction — a HIGH
  attention item can be a large move in either direction; the level
  reflects magnitude past threshold, not sign.
- Freshness/status (`freshness_label`, `status`) is not attention level
  — a HIGH-attention item can still be Delayed, and a Fresh item can
  have no attention at all. The two must never be conflated or implied
  to depend on one another.
- "Not available" (e.g., `volume_acceleration_available: false`) is not
  zero — an unavailable signal must render as explicit absence text,
  never as `0` or `0.0×`, which would misrepresent "we didn't measure
  this" as "we measured zero."

### Why
Each distinction above was already established by an earlier backend
decision in this log (or by `architecture.md`'s Invariants); this entry
exists so future frontend work has one place confirming they still hold
after the UI polish work, and so they're checked explicitly rather than
assumed the next time the UI changes.

### Alternatives considered
None — this is a consolidation of already-decided semantics, not a new
choice.

### Consequence
Any future UI change should be checked against this list before
shipping; a change that blurs one of these four distinctions (even
unintentionally, e.g. through a shared label or a merged visual element)
should be treated as a product-semantics regression, not a pure style
change.

---

## Decision: Persistent anonymous watchlist identity

### Problem
Every request resolved to a single hardcoded `user_id`
(`DEMO_USER_ID = "demo-user"`), shared by every visitor. A deployed
instance needs each browser to keep returning to its own customized
watchlist across refreshes and later visits, without building real
accounts (no OAuth, no passwords, no login UI — explicitly out of scope
per plan.md's CUT list, which already anticipated "authentication
beyond a minimal user identifier" as the boundary not to cross).

### Options
- Full accounts (email/password or OAuth) with real login.
- A frontend-generated id (localStorage/sessionStorage), sent to the
  backend on each request.
- A server-generated opaque id delivered via a persistent httpOnly
  cookie — the browser carries it automatically; the server never trusts
  a client-supplied identity.

### Decision
The third option: an anonymous, capability-based identity. On first
visit with no cookie, the backend generates a high-entropy opaque token
(`secrets.token_urlsafe(32)`, never `random`, never a timestamp or
sequential id) and sets it as a persistent (~1 year), `httpOnly`,
`SameSite=Lax` cookie, `Secure` only when `ENVIRONMENT=production` (the
local dev setup runs over plain HTTP, which rejects `Secure` cookies
outright). That cookie value *is* the `user_id` used everywhere
`Checkpoint`/`ChangeEvent`/`Watchlist` already keyed on one — no
separate "owners" collection, no signature/verification step. This is
not authentication: there is no password behind it, and it is never
described as one anywhere in the app or its docs.

MongoDB remains the only persistence layer — no PostgreSQL, no new
external service. `Checkpoint` and `ChangeEvent` already had a real,
correctly-indexed `user_id` field since Phase 1; only the *value* fed
into that existing field changed. The `Watchlist` model was likewise
already defined (with a unique index on `user_id`) but had sat
completely unused, since a single implicit user made per-owner
membership pointless — this milestone activates it as the real
membership record (`Watchlist.instrument_ids`) rather than inventing a
second ownership model. `Instrument` documents remain global/shared
reference data, exactly as before — a ticker's metadata isn't owned by
anyone; only *which* instruments a given owner tracks is owner-scoped.
`get_watchlist_instruments()` changed from "every Instrument document
in the collection" to "resolve via this owner's own membership."

### Why
This is the smallest change that satisfies "same browser, same
watchlist, later" without accounts: it reuses MongoDB, reuses two
already-correctly-indexed models untouched, activates a third that was
already designed for exactly this, and requires zero changes to
`CheckpointService`, `ChangeEventService`, `AttentionEngine`, or the
Meaningful Change Engine (all already took `user_id` as a plain
parameter, never hardcoded internally).

### Trade-off
Possession of the capability cookie *is* access to that watchlist —
there is no password behind it, so a stolen or shared cookie grants full
read/write access to that anonymous owner's state. This is an accepted,
disclosed property of anonymous capability-based identity, not an
oversight; `httpOnly` (no JS access) and `Secure` in production (no
plaintext network capture) are the mitigations, not a claim that this is
full authentication.

### Consequence
The single-instrument checkpoint endpoint
(`POST /watchlist/instruments/{id}/checkpoint`) now also verifies the
instrument is actually in the caller's own `Watchlist.instrument_ids`
before checkpointing it, rejecting with the same 404 used for a
genuinely nonexistent id (never a distinct 403, which would let a caller
enumerate other owners' instrument_ids by probing). `main.py`'s CORS
middleware gained `allow_credentials=True` (with the existing explicit
origin list preserved — never combined with a wildcard), and `api.js`
sends `credentials: 'include'` on every call; the frontend never reads,
stores, or otherwise handles the cookie's value.

**Legacy `demo-user` data**: left completely untouched, not migrated,
not deleted, not automatically attached to any new anonymous owner.
`DEMO_USER_ID` remains defined in `watchlist_service.py`, unused by any
route, solely so the exact legacy string stays discoverable for manual
inspection later. A new anonymous owner's random token will not collide
with it, so it simply becomes inert, recoverable-by-hand legacy state,
not a resource visible to anyone unless they already know that literal
value.