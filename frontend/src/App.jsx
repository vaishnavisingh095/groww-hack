import { useCallback, useEffect, useState } from 'react'
import './App.css'
import { fetchAttention, fetchWatchlist, markAllAsSeen, markInstrumentAsSeen } from './api'
import AttentionSection from './components/AttentionSection'
import WatchlistTable from './components/WatchlistTable'

// Simple polling per the approved design: no WebSockets, one shared
// timer for both endpoints. 60s matches the backend's own documented
// target freshness window (see decisions.md's Freshness Policy).
const POLL_INTERVAL_MS = 60_000

export default function App() {
  const [instruments, setInstruments] = useState([])
  const [attentionItems, setAttentionItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  // Per-instrument action state, not global -- an instrument can now be
  // acted on from two places (its watchlist row AND, if active, its
  // attention card), and marking one instrument must never disable or
  // report errors for an unrelated one.
  const [inFlightIds, setInFlightIds] = useState(() => new Set())
  const [actionErrors, setActionErrors] = useState({})
  const [savedMessages, setSavedMessages] = useState({})

  // Whole-watchlist "mark all as seen" state -- separate from the
  // per-instrument state above, since this is one action affecting many
  // instruments at once, not an action scoped to a single instrument id.
  const [markAllInFlight, setMarkAllInFlight] = useState(false)
  const [markAllError, setMarkAllError] = useState(null)
  const [markAllPartialMessage, setMarkAllPartialMessage] = useState(null)

  // Both endpoints are fetched together, on the SAME timer -- still
  // exactly one polling interval, not two. They are independent reads,
  // though: Promise.allSettled (not Promise.all) is used deliberately,
  // so a failure in EITHER request never discards the other's
  // successfully-fetched data. GET /watchlist and GET /watchlist/attention
  // have genuinely different reliability profiles (the former makes a
  // live, documented-as-unreliable provider call per request; the
  // latter is a pure Mongo read), so treating "one failed" as "the
  // backend is unreachable" was itself untruthful -- it discarded real
  // data and blamed the whole backend for a single route's problem.
  const loadAll = useCallback(async () => {
    const [watchlistResult, attentionResult] = await Promise.allSettled([
      fetchWatchlist(),
      fetchAttention(),
    ])

    // Each dataset is only ever replaced by a REAL, successful
    // response -- a failed request leaves whatever was already
    // displayed in place rather than fabricating an empty/missing
    // state for data we simply couldn't refresh this cycle.
    if (watchlistResult.status === 'fulfilled') {
      setInstruments(watchlistResult.value)
    }
    if (attentionResult.status === 'fulfilled') {
      setAttentionItems(attentionResult.value)
    }

    const watchlistFailed = watchlistResult.status === 'rejected'
    const attentionFailed = attentionResult.status === 'rejected'

    if (watchlistFailed && attentionFailed) {
      // Both independent reads failed on the same backend in the same
      // cycle -- this IS real evidence the backend/network itself is
      // unreachable, not just one route having a transient problem.
      setError('Could not reach the backend. Is it running?')
    } else if (watchlistFailed) {
      // Attention succeeded -- the backend is demonstrably reachable,
      // only the watchlist read failed this cycle. Never blame the
      // whole backend for one route's problem.
      setError('Could not load your watchlist right now — attention data is up to date.')
    } else if (attentionFailed) {
      setError('Could not load attention items right now — watchlist data is up to date.')
    } else {
      setError(null)
    }

    setLoading(false)
  }, [])

  useEffect(() => {
    loadAll()
    const interval = setInterval(loadAll, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [loadAll])

  const handleMarkAsSeen = useCallback(
    async (instrumentId) => {
      setInFlightIds((prev) => new Set(prev).add(instrumentId))
      setActionErrors((prev) => ({ ...prev, [instrumentId]: null }))

      try {
        const result = await markInstrumentAsSeen(instrumentId)
        setSavedMessages((prev) => ({ ...prev, [instrumentId]: result.message }))
        // Re-fetch both endpoints so the UI reflects real, current
        // server state -- the acknowledged instrument's row and its
        // (now superseded) attention item update from that response,
        // never from a locally-guessed optimistic update.
        await loadAll()
      } catch (err) {
        // Explicit failure: do NOT touch instruments/attentionItems --
        // an unacknowledged item must never look acknowledged.
        setActionErrors((prev) => ({ ...prev, [instrumentId]: err.message }))
      } finally {
        setInFlightIds((prev) => {
          const next = new Set(prev)
          next.delete(instrumentId)
          return next
        })
      }
    },
    [loadAll]
  )

  const handleMarkAllAsSeen = useCallback(async () => {
    setMarkAllInFlight(true)
    setMarkAllError(null)
    setMarkAllPartialMessage(null)

    try {
      const result = await markAllAsSeen()
      // Truthful partial-success reporting: the backend already tells us
      // exactly which instruments were skipped (no valid current data)
      // rather than silently acknowledging only some of them. Never
      // invent a specific reason beyond what the response provides.
      if (result.skipped && result.skipped.length > 0) {
        setMarkAllPartialMessage(
          'Some stocks couldn’t be updated because current market data was unavailable.'
        )
      }
      // Re-fetch both endpoints so the UI reflects real, current server
      // state -- exactly the acknowledged instruments (per `updated`)
      // and only those disappear from the attention list; never a local,
      // optimistic removal of items this response didn't confirm.
      await loadAll()
    } catch (err) {
      // Explicit failure: do NOT touch instruments/attentionItems -- an
      // unacknowledged watchlist must never look acknowledged.
      setMarkAllError(err.message)
    } finally {
      setMarkAllInFlight(false)
    }
  }, [loadAll])

  return (
    <div className="app">
      <header className="app-header">
        <h1>Smart Market Watchlist</h1>
        <p className="app-tagline">Your market, filtered for attention.</p>
        <p className="app-subtitle">
          See what meaningfully changed in your watchlist while you were away.
        </p>
      </header>

      {error && <div className="error-banner">{error}</div>}

      {loading ? (
        <p className="loading">Loading watchlist...</p>
      ) : (
        <>
          <AttentionSection
            items={attentionItems}
            instruments={instruments}
            onMarkAsSeen={handleMarkAsSeen}
            inFlightIds={inFlightIds}
            actionErrors={actionErrors}
            onMarkAllAsSeen={handleMarkAllAsSeen}
            markAllInFlight={markAllInFlight}
            markAllError={markAllError}
            markAllPartialMessage={markAllPartialMessage}
          />

          <section className="watchlist-section">
            <div className="watchlist-header">
              <h2 className="section-title">Your Watchlist</h2>
              <p className="watchlist-subtitle">
                The complete list, including instruments with no meaningful change right now.
              </p>
            </div>
            <WatchlistTable
              instruments={instruments}
              onMarkAsSeen={handleMarkAsSeen}
              inFlightIds={inFlightIds}
              actionErrors={actionErrors}
              savedMessages={savedMessages}
            />
          </section>
        </>
      )}

      <footer className="app-footer">
        <p>
          Data via Yahoo Finance (unofficial). Not guaranteed real-time.
          Refreshes every 60 seconds.
        </p>
      </footer>
    </div>
  )
}
