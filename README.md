# Smart Market Watchlist

> **A watchlist that monitors your stocks for you — and tells you what meaningfully changed since you last checked.**

[**🚀 Live Demo**](https://smart-market-watchlist-six.vercel.app/)

---

## The Problem

A traditional watchlist tells you what your stocks are doing **right now**.

But when you return after some time, you still have to scan prices, percentages, and volumes to figure out:

> **What actually changed, and what deserves my attention?**

We built **Smart Market Watchlist** around a different idea:

### Don't make users monitor the market. Monitor the watchlist for them.

---

## What We Built

The core experience is **Since You Last Checked**.

Instead of treating every stock movement equally, the system maintains the user's last acknowledged checkpoint and evaluates new market observations against it.

When something meaningfully changes, the system:

**Detects → Explains → Ranks → Surfaces**

The result is an attention-first watchlist that helps users understand what matters without manually scanning every stock.

---

# Our Key Decisions

The challenge intentionally leaves the product and architecture open. These are the decisions we made.

## 1. What counts as a meaningful change?

We use two deterministic signals: **price movement** and **volume acceleration**.

### Price

The price threshold adapts to the observed intraday range and is frozen when the checkpoint is created.

- `0.25 × intraday range`
- Bounded between `0.5%` and `3%`
- `1%` fallback when adaptive inputs are unavailable
- Checkpoints created within the first 15 minutes use the `1%` fallback

This prevents the definition of "meaningful" from changing while the system is measuring the same change.

### Volume

Instead of comparing cumulative volume directly, we compare the **rate of trading before and after the checkpoint within the same trading session**.

```text
rate_before = baseline volume / minutes since market open

rate_after = volume change / minutes since checkpoint

acceleration = rate_after / rate_before

A volume acceleration of ≥ 2× is considered meaningful.

Cross-session volume acceleration is treated as unavailable rather than being fabricated from incompatible observations.

Combination

A change is meaningful when:

Price signal OR Volume signal

This keeps the detection logic deterministic and explainable.

2. What information should we surface?

We chose an attention-first experience rather than making users scan their entire watchlist.

The primary experience is:

Since You Last Checked

The strongest changes are surfaced first, followed by changes worth checking, with the complete watchlist available below.

Every surfaced change explains:

What changed
Which signal triggered it
The threshold involved
Current data status

The system therefore answers both:

What changed?

and

Why was this surfaced?

3. How does state persist across sessions and devices?

"Last checked" is not treated as temporary browser state.

Watchlist membership, checkpoints, market snapshots, and detected changes are persisted server-side in MongoDB.

Anonymous users receive a persistent httpOnly capability cookie so the backend can associate requests with the same watchlist owner.

Most importantly:

Opening or refreshing the application does not silently change the checkpoint.

The checkpoint advances through an explicit Mark as Seen action.

This gives the system a stable definition of:

Since when?

4. How do we handle stale, delayed, or conflicting data?

Market data is an external dependency, so we assume it can fail.

Fresh data

Current valid market data is used normally.

Stale / delayed data

If current provider data cannot be obtained, the system can fall back to the last known good snapshot while explicitly marking it as delayed.

Unavailable data

If no valid snapshot exists, the instrument is shown as unavailable rather than inventing a value.

Invalid data

Invalid or conflicting observations are not used to create meaningful-change events.

A key invariant is:

Stale or unavailable data cannot silently create a new checkpoint or meaningful-change event.

We also distinguish:

bar_timestamp — when the market observation occurred
fetched_at — when our backend obtained the data

This prevents an ambiguous "updated" timestamp from misleading the user about market-data freshness.

5. How does the system scale?

We separate instrument-level market data from user-level state.

Market snapshots belong to instruments and can therefore be shared across users, while watchlist membership, checkpoints, and change events remain user-scoped.

The current system uses cache-first market reads to avoid unnecessary provider requests.

At larger scale, the first architectural evolution would be shared background polling by distinct instruments, allowing many users to consume the same refreshed market snapshots.

We deliberately did not introduce distributed infrastructure before the product required it.

6. Where do we keep things simple vs. add complexity?

The architecture is intentionally small:

React
  ↓
FastAPI
  ↓
MongoDB
  ↓
Market Data Provider

Inside the FastAPI application, responsibilities are separated into:

Anonymous Identity
Watchlist Service
Market Data Service
Checkpoint Service
Meaningful Change Engine
ChangeEvent persistence
Attention Engine

We intentionally did not introduce:

Kafka
Redis
WebSockets
Kubernetes
Microservices
Message queues
Background workers
LLMs in the core detection path

The principle was:

Add complexity only when it solves a demonstrated problem.

For the core detection engine, deterministic rules give us reproducibility, explainability, and testability.

Architecture
                         SMART MARKET WATCHLIST

┌───────────────┐       ┌──────────────────────────────────┐
│    BROWSER    │       │        FASTAPI APPLICATION       │
│               │       │                                  │
│ React         │──────►│  Market Data Service             │
│               │ REST  │          │                       │
│ Since You     │       │          ▼                       │
│ Last Checked  │       │  Market Snapshot                │
│ Watchlist     │       │          │                       │
│ Search/Filter │       │          │ + Checkpoint          │
└───────────────┘       │          ▼                       │
                        │  ┌────────────────────────────┐  │
                        │  │  MEANINGFUL CHANGE ENGINE  │  │
                        │  │                            │  │
                        │  │ Price Signal │ Volume     │  │
                        │  │ adaptive     │ same-session│ │
                        │  └──────────────┬─────────────┘  │
                        │                 ▼                │
                        │           ChangeEvent            │
                        │                 ▼                │
                        │           Attention Engine      │
                        └─────────────────┬────────────────┘
                                          │
                                          ▼
                                   ┌─────────────┐
                                   │   MongoDB   │
                                   │             │
                                   │ Instrument  │
                                   │ Watchlist   │
                                   │ Snapshot    │
                                   │ Checkpoint  │
                                   │ ChangeEvent │
                                   └─────────────┘

                         Market Data Service
                                  │
                                  ▼
                         ┌─────────────────┐
                         │  Yahoo Finance  │
                         │    yfinance     │
                         └─────────────────┘
Important architecture boundaries
The frontend communicates only with the FastAPI backend.
Only the Market Data Service communicates with yfinance.
The Meaningful Change Engine contains the core detection logic.
ChangeEvent stores detected changes.
Attention is derived, not stored as a separate database collection.
MongoDB persists user state and market snapshots.
How the Core Flow Works
User's Checkpoint
       +
Current Market Observation
       ↓
Meaningful Change Engine
       ↓
 ┌─────────────┐
 │ Price OR    │
 │ Volume      │
 └──────┬──────┘
        ↓
 Meaningful?
        ↓
   ChangeEvent
        ↓
 Attention Engine
        ↓
 What deserves
 my attention?

When the user selects Mark as Seen, a fresh market observation becomes the new checkpoint.

A normal page visit or refresh does not silently reset the baseline.

Market Data Reliability

The market-data provider is treated as an unreliable external boundary.

For normal watchlist reads:

GET /watchlist
      ↓
Persisted MarketSnapshot
      ↓
Fresh?
 ┌────┴────┐
Yes       No
 ↓         ↓
Serve    Provider
           ↓
      ┌────┴────┐
   Success    Failure
      ↓          ↓
   Persist   Last-known-good

The cache considers snapshots fresh for up to 120 seconds.

Provider HTTP calls are bounded by a 5-second timeout, with no automatic retries.

We do not claim guaranteed real-time market data.

Data Model
Collection	Purpose
Instrument	Global instrument reference data
Watchlist	Owner-scoped watchlist membership
MarketSnapshot	Market-data cache and last-known-good observation
Checkpoint	Frozen "last seen" baseline
ChangeEvent	Persisted detected change

Attention is derived at request time and is not stored.

Technology
Frontend
React
JavaScript
Backend
Python
FastAPI
Pydantic
Database
MongoDB
Market Data
yfinance
Yahoo Finance
Why This Is Different

A traditional watchlist answers:

"What are my stocks doing?"

Smart Market Watchlist answers:

"What meaningfully changed since I last checked, and what deserves my attention?"

The product is built around four ideas:

Persistent checkpoints

Know exactly what "since last checked" means.

Deterministic detection

Define meaningful change with reproducible signals.

Explainable attention

Tell users what changed and why it was surfaced.

Explicit trust states

Never silently present stale or unavailable data as current.

Engineering Principles
Correctness before complexity

We chose the simplest architecture that could support the product's core guarantees.

Explicit state over implicit state

A page refresh should not redefine the user's last checkpoint.

Explainability over opacity

The system should be able to explain why an instrument received attention.

Trust over false precision

When market data is stale or unavailable, the product says so.

Attention over information overload

The goal isn't to show users more data.

It is to help them understand what matters.

Future Evolution

The core architecture is intentionally extensible.

Potential future improvements include:

Shared background polling for larger watchlists and user populations
Historical baselines for more sophisticated volume analysis
Additional deterministic signals such as volatility or gap detection
Additional market-data providers behind the existing provider abstraction
Stronger concurrency controls as multi-user usage grows

These are intentionally outside the current core because they are not required to validate the central product idea.

The Idea

Don't build the obvious watchlist.

Build the watchlist that tells users what changed, why it matters, and what deserves their attention now.

🚀 Try It

Open Smart Market Watchlist →


**One recommendation:** keep the README exactly this kind of length. Don't add scre
