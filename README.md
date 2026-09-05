# Smart Market Watchlist

> **A watchlist that monitors your stocks for you — and tells you what meaningfully changed since you last checked.**

[🚀 Live Demo](https://smart-market-watchlist-six.vercel.app/)

## The Problem

A traditional watchlist shows what stocks are doing right now.  
When I return later, I still have to scan the market to understand **what actually changed and what deserves attention**.

### My Idea

**Don't make users monitor the market. Monitor the watchlist for them.**

## What I Built

**Since You Last Checked** is the core experience.

The system maintains an explicit checkpoint, compares new market observations against it, detects meaningful changes, and surfaces the most important ones with an explanation.

**Detect → Explain → Rank → Surface**

---

## My Key Decisions

### 1. What counts as meaningful change?

I use two deterministic signals: **price movement** and **volume acceleration**.

- Price: adaptive threshold based on intraday range, bounded between `0.5%–3%`
- Fallback: `1%`
- Threshold is frozen at the checkpoint
- Volume: same-session trading-rate acceleration of `≥2×`
- Meaningful change = **Price OR Volume**

This makes detection reproducible and explainable.

### 2. What information should I surface?

I chose an **attention-first** experience centered on **Since You Last Checked**.

The strongest changes appear first, with explanations showing what changed, which signal triggered, the threshold involved, and the current data status.

### 3. How does state persist?

I persist watchlist membership, checkpoints, market snapshots, and detected changes in **MongoDB**.

A persistent `httpOnly` anonymous capability cookie identifies the watchlist owner. Opening or refreshing the application does not silently advance the checkpoint; **Mark as Seen** does.

### 4. How do I handle unreliable data?

I treat market data as an external dependency that can be **fresh, stale/delayed, unavailable, or invalid**.

I use a last-known-good snapshot when fresh provider data cannot be obtained, while clearly marking it as stale. Stale or unavailable data cannot silently create a new checkpoint or meaningful-change event.

### 5. How does the system scale?

I separate market data at the instrument level from user-level state.

The current system uses cache-first market reads. At larger scale, shared background polling by distinct instruments would allow many users to consume the same refreshed market snapshots.

### 6. Where do I keep things simple?

I use **React + FastAPI + MongoDB + a market-data provider**.

I deliberately avoided Kafka, Redis, WebSockets, Kubernetes, microservices, queues, and LLMs in the detection path because they were not necessary to solve the core problem.

> **Add complexity only when it solves a demonstrated problem.**

---

## Architecture

The frontend communicates only with the FastAPI backend.

The **Market Data Service** is the only component that communicates with `yfinance` and uses a cache-first strategy with last-known-good fallback.

The **Meaningful Change Engine** evaluates the user's checkpoint against current market observations, creates `ChangeEvent` records, and the **Attention Engine** ranks and explains active changes.

MongoDB stores:

`Instrument` · `Watchlist` · `MarketSnapshot` · `Checkpoint` · `ChangeEvent`

**Attention is derived, not stored.**

---

## Technology

- **Frontend:** React, JavaScript
- **Backend:** Python, FastAPI, Pydantic
- **Database:** MongoDB
- **Market Data:** `yfinance` / Yahoo Finance

I do not claim guaranteed real-time market data. Provider calls are bounded by a 5-second HTTP timeout.

---

## Why This Is Different

A traditional watchlist answers:

> **“What are my stocks doing?”**

Smart Market Watchlist answers:

> **“What meaningfully changed since I last checked, and what deserves my attention?”**

### The Principle

**Persistent checkpoints + deterministic detection + explainable attention + explicit data trust**

[🚀 Open Smart Market Watchlist](https://smart-market-watchlist-six.vercel.app/)
