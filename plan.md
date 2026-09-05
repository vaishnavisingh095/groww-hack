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
- Historical price charting UI, or any persisted tick/snapshot history.
- Alerts/notifications (push, email, SMS).
- Horizontal scaling, load balancing, multi-region deployment.
- Admin tooling / back-office views.
- Payments, blockchain, x402, or any unrelated infra.
- Portfolio tracking, holdings/P&L views, or buy/sell recommendations —
  this product surfaces "what changed," never a position or a trade
  suggestion.
- News/social features.
- A persisted "last-known-good" snapshot fallback for provider failure.
- Full authentication/account system (OAuth, passwords, login UI) — see
  the Persistent Anonymous Watchlist Identity note under Current Status
  for what identity mechanism was actually built instead.

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

**Current implementation note**: this section describes the originally
designed shared backend poll loop. The implementation instead fetches
on demand, synchronously, inside `GET /watchlist`'s own request
handler — there is no standalone backend process polling on a timer,
and no fan-out deduplication across users yet. The "every 60 seconds"
behavior a user actually experiences today comes from the **frontend's**
own client-side poll (`App.jsx`'s `setInterval`), which simply calls
`GET /watchlist`/`GET /watchlist/attention` again. This is a documented,
deliberate sequencing choice, not an oversight — see `decisions.md`'s
"On-demand fetch per request, not a separate background poll process"
entry for the full reasoning and its known trade-offs (no shared-fetch
protection under concurrent users yet).

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

Product implementation is functionally complete. Seven dedicated P0
adversarial-hardening passes (identity/authorization, market-data
correctness, checkpoint/ChangeEvent lifecycle, MongoDB concurrency,
API/frontend failure handling, and a final end-to-end regression audit)
have run against the finished feature set and their fixes are committed
(`21e45bc fix: harden watchlist state and failure handling`). See
`decisions.md` for the specific decisions and fixes behind each item
below.

**COMPLETED:**

Backend:
1. Watchlist CRUD (add + list; no remove-instrument endpoint was ever
   built — see Known Deviations below).
2. Persistent anonymous watchlist identity (httpOnly capability cookie,
   CSPRNG-generated owner token — not an account/login system).
3. Owner-scoped watchlist membership (`Watchlist.instrument_ids`
   activated as the real per-owner record; `Instrument` remains global).
4. Deterministic price + volume meaningful-change engine (2.0% price /
   2.0× volume-acceleration thresholds, locked).
5. Session-aware volume acceleration (same-session only; cross-session
   comparisons explicitly unavailable, never fabricated).
6. Checkpoint semantics (explicit-only advancement — observation never
   silently becomes acknowledgement).
7. ChangeEvent persistence, deduplication, and acknowledgement lifecycle.
8. Attention Engine (`GET /watchlist/attention`) with HIGH/MEDIUM/WATCH
   ranking bands.
9. Freshness / delayed / unavailable / invalid snapshot handling.
10. Provider failure isolation (one instrument's bad data can no longer
    take down its siblings in the same batch — P0 Hardening #2 fix).
11. Checkpoint/ChangeEvent race hardening (a `GET` racing a concurrent
    Mark Seen can no longer persist a stale-checkpoint `ChangeEvent` —
    P0 Hardening #3 fix).
12. MongoDB concurrency hardening (`DuplicateKeyError` recovery added
    for concurrent Add Stock and concurrent first-owner seed creation —
    P0 Hardening #4/#5 fixes).

Frontend:
13. Attention-first home experience ("Since You Last Checked" above the
    full watchlist table).
14. High Attention / Worth Checking hierarchy.
15. Structured What changed / Why it matters / Signals explanation per
    attention item.
16. View Details (local expandable card state — no API call, no
    checkpoint/acknowledgement side effect).
17. Explicit Mark as Seen (per instrument).
18. Mark All as Seen (whole-watchlist, with truthful partial-failure
    reporting).
19. Search (frontend-only, symbol/exchange, case-insensitive).
20. Watchlist filters (All / Attention / Normal / Baseline Pending —
    frontend-only).
21. Add Stock (symbol + exchange, with provider-resolution validation).
22. API/frontend failure-state handling — a failed fetch is never
    presented as confirmed-empty or confirmed-caught-up, and a
    malformed-but-200 response can no longer crash the page (P0
    Hardening #6 fixes).

**CURRENT (in progress / not yet done):**
- Deployment configuration (see "Remaining" below) — a pre-deployment
  environment inspection has already identified the exact two
  hardcoded-origin edits required (CORS allow-list, frontend
  `API_BASE`) and the environment variables to set; neither edit has
  been made yet.

**REMAINING (intended order):**
1. Deployment configuration (the two hardcoded-origin edits identified
   by the environment inspection, plus setting `ENVIRONMENT=production`
   and the MongoDB Atlas connection variables on the chosen hosts).
2. Deployment (static frontend host + FastAPI backend host + the
   already-provisioned MongoDB Atlas cluster).
3. Live browser QA against the deployed environment.
4. Fix only deployment/real-environment issues surfaced by that QA —
   not a new feature or hardening pass.
5. Final UI polish, if the live QA surfaces anything worth it.
6. Final documentation verification (re-check this file and
   `architecture.md`/`decisions.md` against whatever the deployment
   step actually required).
7. Final Git cleanup (confirm no scratch/local files or secrets are
   staged).
8. Push.
9. Demo/pitch/Q&A preparation.

**DEFERRED / CUT (explicitly not built — see the CUT section above for
the original list; reconfirmed accurate as of this status update):**
- WebSockets/SSE, Redis, Kafka, message queues, microservices,
  Kubernetes — no code path in this project needs them (see
  `decisions.md`'s "No microservices, queues, Redis, Kafka, WebSockets,
  Kubernetes, or LLM" decision).
- LLM/AI involvement in change detection or attention scoring — fully
  deterministic, rule-based, and remains so.
- Alerts/notifications (push, email, SMS).
- A portfolio/P&L tracking view, or buy/sell recommendations — this
  product surfaces "what changed," never a position, holding, or trade
  suggestion.
- News/social features.
- Historical price charting UI, or any tick-level/append-only history —
  `MarketSnapshot`-shaped data is a per-request value object, not a
  persisted history (see Known Deviations below).
- A persisted "last-known-good" snapshot fallback on provider failure —
  a failed instrument reports `unavailable` for that request; there is
  no cached prior value served in its place. This is a real, current
  gap (not a design choice made lightly) — see `architecture.md`'s
  Known Limitations.
- Full authentication/account system (OAuth, passwords, login UI,
  multi-tenant orgs) — identity is an anonymous capability cookie, not
  an account system; see `decisions.md`'s "Persistent anonymous
  watchlist identity."
- Remove-instrument from the watchlist — Add Stock exists; there is no
  corresponding remove endpoint or UI action.

**Known Deviations from the original design (see `decisions.md` for
full reasoning on each):**
- Market data is fetched **on demand, synchronously inside the request
  handler**, not via a separate always-on background poll loop writing
  to MongoDB on a timer. The "poll every 60 seconds" behavior that
  exists today is the **frontend's** client-side `setInterval` calling
  `GET /watchlist`/`GET /watchlist/attention` again — not a backend
  process. `MarketSnapshot`'s shape is used as an in-memory value
  object per request; no `market_snapshots` documents are ever written,
  despite the collection's index existing. See decisions.md's
  "On-demand fetch per request, not a separate background poll process"
  entry.
- Two UI-polish-era decisions ("No 'Mark all as seen' CTA introduced,"
  "No 'View details' action added") were later superseded once those
  features were actually built as real, scoped features — see
  `decisions.md`'s newer entries, which explicitly mark that
  supersession rather than silently contradicting the earlier ones.
