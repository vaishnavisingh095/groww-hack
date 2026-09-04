# Plan: Smart Market Watchlist (Groww Hackathon)

## Problem

Users track Indian equities on a watchlist but the raw numbers don't tell
them what's worth their attention. The system must let users build a
watchlist, see current market data, and — critically — understand what has
**meaningfully changed** since they last looked, ranked by what deserves
attention now, with an explanation of why.

This is not a stock-tracking CRUD app with a diff bolted on. The
differentiator is: persistent per-user checkpoints, deterministic and
explainable change detection, and attention ranking.

## Product Behavior

- User creates/manages a watchlist of Indian equities (NSE/BSE).
- User views current market data for their watchlist.
- The system remembers, per user, per instrument, what the market looked
  like the last time the user checked (a **checkpoint**).
- On return, the system computes what changed since that checkpoint using
  fixed, explainable rules — not an LLM, not a black box.
- Changes are ranked by an **attention score** so the most important
  changes surface first, each with a plain-language reason.
- The system never presents stale or failed data as if it were live.

## In-Scope Requirements (MUST HAVE)

- Watchlist CRUD (add/remove instrument, list watchlist).
- Fetch and display current market snapshot per instrument (price, day
  change %, volume) from a real external provider.
- Explicit checkpoint action ("mark as seen" / equivalent) — the primary
  mechanism per product decision.
- Implicit checkpoint creation as a secondary mechanism (e.g., first
  visit/session creates an initial checkpoint automatically if none
  exists), so the system is usable without the user knowing the explicit
  action exists on day one.
- Deterministic meaningful-change detection using **price movement** and
  **volume anomaly** signals (decided; volatility is explicitly deferred,
  see Should-Have). Volume acceleration is valid **only when comparing
  snapshots within the same trading session** — see the Same-Session
  Volume Semantics requirement below.
- Session-awareness: the system must recognize which trading session a
  checkpoint belongs to, so that a checkpoint from a prior session never
  has its cumulative volume subtracted from the current session's
  cumulative volume.
- Persisted change events so the same change is not re-surfaced after the
  user has already seen it.
- Attention ranking: ordered list of active changes, most attention-worthy
  first, each with a human-readable explanation of why it was flagged.
- Explicit data-freshness/staleness indication in both API responses and
  UI — never silently show old data as current.
- Graceful handling of provider failure (serve last-known-good, marked
  stale, never crash the view).
- Handling of newly-added instruments with no baseline (explicit
  "baseline established" state, not a fabricated 0%/crash).
- End-to-end: React frontend + FastAPI backend + MongoDB, deployed enough
  to demo.

## SHOULD HAVE (build if MUST HAVE is solid and stable)

- Volatility as a third change-detection signal (e.g., intraday range vs.
  historical average range), layered on top of price/volume once those
  are proven.
- Dense, information-rich watchlist UI (DexScreener-inspired density,
  FinBoard-inspired information layout) — visual polish beyond a
  functional table.
- Tunable-parameter exposure (e.g., a settings panel to adjust
  sensitivity thresholds) rather than fixed constants only.
- Multiple named watchlists per user.

## NICE TO HAVE

- Manual refresh button in addition to the 60-second poll cycle.
- Historical change-event log/timeline view per instrument.
- Sector/grouping view within a watchlist.

## CUT (explicitly not building; revisit only if core is done early and stable)

- Authentication beyond a minimal user identifier (no OAuth, no password
  reset flows, no multi-tenant org model).
- Real-time push (WebSockets/SSE) — 60-second poll + client refresh is
  sufficient and avoids a whole connection-management problem.
- Redis, Kafka, message queues, microservices, Kubernetes.
- LLM/AI involvement in the core change-detection or attention-scoring
  logic (explicitly excluded — must be deterministic and explainable).
- Options/F&O instruments, multi-exchange arbitrage detection.
- Historical price charting UI.
- Alerts/notifications (push, email, SMS).
- Horizontal scaling, load balancing, multi-region deployment.
- Admin tooling / back-office views.
- Payments, blockchain, x402, or any unrelated infra.

## Data Source Decision (locked — confirmed by live test)

Market data via **`yfinance`**, an open-source Python library that talks
directly to Yahoo Finance's unofficial/undocumented internal endpoints,
with NSE (`.NS`) and BSE (`.BO`) ticker suffix support. Accessed behind a
`MarketDataProvider` abstraction so it can be swapped for a broker/paid
provider later without touching the rest of the system.

This replaces the previously-considered third-party REST wrapper
(a bare-IP hosted service) — that option had no verifiable ownership,
uptime, or rate-limit guarantee beyond a single README's claims.

**GO decision confirmed by a live investigation** (see `decisions.md` for
full detail): all 5 target NSE large-caps (RELIANCE, TCS, HDFCBANK, INFY,
ICICIBANK) fetched successfully in a real, unrestricted-network run
during live NSE market hours, including a successful batched
multi-symbol request. This is evidence from one clean run, not a
reliability guarantee — the provider is still treated as first-class
unreliable infrastructure (no SLA, no documented rate ceiling, reactive
backoff only). **No mock data will be used in the demo.**

## Freshness Policy (locked — confirmed and corrected by live test)

We do **not** claim real-time or guaranteed ≤60-second data. Polling at
~60 seconds is a **target**, not an SLA.

**Our own `fetched_at` timestamp — the moment our backend received the
data — is the sole authoritative timestamp for all freshness and
staleness calculations.** The live investigation examined the provider's
own `regularMarketTime` field and found its exact semantic meaning is
**not independently verified**: it was populated, its decoded value was
consistently close to our local fetch time, and it changed between
requests in a way that tracked our own polling rather than clearly
tracking exchange trade activity. This makes it unsuitable as an
independent freshness or trade-time signal. It may be stored for
diagnostics only — it is never used to compute or display freshness/age,
and it is never presented to the user as an exchange trade timestamp.

Every `MarketSnapshot` carries a `status` of `ok` (displayed as "Fresh"),
`stale` (displayed as "Delayed"), `unavailable`, or `invalid`, and the UI
surfaces the actual age computed from `fetched_at` (e.g., "Fresh · 42s
ago", "Delayed · 3m ago") — never a bare number presented as if it were
live. See `architecture.md` for the full status model.

## Polling Interval (locked — confirmed by live test)

Backend polls all **distinct** instruments across all users' watchlists
once every **60 seconds** (a target, adjusted downward automatically if
the provider signals rate-limiting — see Failure Handling in
`architecture.md`), using `yfinance`'s batched multi-ticker call
(`yf.download()`). This avoids per-user API fan-out (N users × M
instruments) by deduplicating to one shared fetch per distinct
instrument.

**Batching confirmed viable by live test**: fetching all 5 target symbols
in one `yf.download()` call succeeded in ~0.234 seconds, versus
~1+ second per symbol when fetched individually — roughly 5–7x faster,
directly supporting the shared-poll-loop design. `yfinance` has no
self-published rate limit to design a safety margin against — the
polling loop must react to actual error/429 responses rather than assume
a fixed ceiling (none was observed in the live test, and none is
assumed).

## Build Order

1. Data model + MongoDB schema for all five core entities (see
   `architecture.md`).
2. Watchlist CRUD (backend + minimal frontend).
3. Market data service: provider client, batched polling loop, shared
   snapshot cache, freshness computation.
4. Checkpoint service: explicit + implicit checkpoint creation.
5. Meaningful Change Engine: price + volume signal computation against
   checkpoint baseline, persisted change events.
6. Attention Engine: ranking + explanation generation from active change
   events (computed at request time, not persisted as ground truth).
7. Frontend: watchlist view with freshness indicators, change
   highlights, attention-ranked ordering.
8. Hardening pass: provider failure simulation, stale-data paths,
   new-instrument-no-baseline path, concurrent-checkpoint-update path.
9. Should-have items only if 1–8 are demo-solid with time remaining.

## Hardening Priorities

1. Never show stale data as fresh (silent staleness is the single worst
   failure mode for a trust-based product like this).
2. Never crash on provider failure — degrade visibly instead.
3. Never re-surface an already-seen change.
4. Never fabricate a change for an instrument with no baseline.
5. Never compute volume acceleration across a session boundary (a
   checkpoint from a prior trading day must not have its volume
   subtracted from today's cumulative volume).

## Testing Priorities

1. Change-detection engine: pure-function unit tests against fixed
   snapshot/checkpoint pairs (deterministic, no I/O — same discipline as
   the rehearsal).
2. Freshness/staleness classification: boundary tests around the
   staleness threshold, using our own `fetched_at` — not any
   provider-supplied timestamp.
3. Attention ranking: ordering tests with multiple simultaneous changes.
4. API contract tests for watchlist CRUD and checkpoint endpoints.
5. Provider-failure simulation tests (timeout, malformed response, empty
   response).
6. Same-session volume semantics: explicit tests for (a) checkpoint and
   current snapshot in the same session — acceleration computed
   normally; (b) checkpoint from a prior session — acceleration marked
   unavailable, price comparison still computed; (c) checkpoint at/near
   market open — division-by-near-zero guard exercised directly.

## Optional Work

See SHOULD HAVE / NICE TO HAVE above. Not scheduled unless core is done
early.

## Current Status

Backend and frontend both implemented; UI polish in progress. Not yet
committed past UI Polish Pass 3 — see `decisions.md` for the specific
decisions behind each item below.

**Completed:**
- Backend: checkpoint semantics (explicit-only advancement), Meaningful
  Change Engine, ChangeEvent persistence, Attention Engine, the
  `GET /watchlist/attention` endpoint, and the `session_date`
  correctness fix (P1-1).
- Frontend: attention-first home experience (`GET /watchlist/attention`
  and `GET /watchlist` joined client-side by `instrument_id`, rendered
  ahead of the full watchlist table).
- Frontend: structured "What changed / Why it matters / Signals"
  explanation per attention item, built only from backend-computed
  fields (`attention_level`, `price_change_pct`,
  `volume_acceleration_ratio`) — no new frontend scoring logic.
- Frontend: High Attention / Worth Checking hierarchy, partitioning the
  backend-ranked attention list into two groups without re-sorting or
  re-scoring.
- Frontend: light desktop web-dashboard visual foundation (color/spacing/
  radius tokens, re-themed surfaces), header/hero composition (tagline,
  subtitle), "Since You Last Checked" banner with a high/worth-checking
  breakdown, and attention-card visual polish (price area with
  since-checkpoint vs. day-over-day movement, signals list, detection
  timestamp). Purely presentational — no backend or API changes.

**Remaining (intended order):**
1. Watchlist table UI polish (currently only re-themed via Pass 1's
   color tokens; structure/layout untouched).
2. Real browser visual verification of the full dashboard.
3. Full frontend/backend regression pass.
4. Documentation consistency check.
5. Final Git audit.
6. Demo rehearsal / submission.
