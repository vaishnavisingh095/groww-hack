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
