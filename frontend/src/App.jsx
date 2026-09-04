import { useState, useEffect, useCallback } from 'react'
import './App.css'

// Backend base URL. The frontend NEVER calls yfinance directly -- every
// request goes through our own FastAPI backend, which is the only thing
// that talks to the market data provider.
const API_BASE = 'http://127.0.0.1:8000'

// Simple polling interval per the approved design: no WebSockets, just
// a periodic re-fetch. 60s matches the backend's own documented target
// freshness window.
const POLL_INTERVAL_MS = 60_000

function formatPrice(price) {
  if (price === null || price === undefined) return '—'
  return `₹${price.toFixed(2)}`
}

function formatVolume(volume) {
  if (volume === null || volume === undefined) return '—'
  if (volume >= 1_000_000) return `${(volume / 1_000_000).toFixed(1)}M`
  if (volume >= 1_000) return `${(volume / 1_000).toFixed(1)}K`
  return String(volume)
}

function formatPercentChange(pct) {
  if (pct === null || pct === undefined) return '—'
  const sign = pct >= 0 ? '+' : ''
  return `${sign}${pct.toFixed(2)}%`
}

function StatusBadge({ status }) {
  const labels = {
    ok: { text: 'Fresh', className: 'status-ok' },
    stale: { text: 'Delayed', className: 'status-stale' },
    unavailable: { text: 'Unavailable', className: 'status-unavailable' },
    invalid: { text: 'Invalid', className: 'status-unavailable' },
  }
  const info = labels[status] || { text: status, className: '' }
  return <span className={`status-badge ${info.className}`}>{info.text}</span>
}

function ChangeIndicator({ change }) {
  if (!change.has_baseline) {
    return <span className="change-baseline">{change.reason}</span>
  }
  if (change.meaningful_change) {
    return (
      <span className="change-meaningful">
        ⚠ Meaningfully changed — {change.reason}
      </span>
    )
  }
  return <span className="change-none">No meaningful change</span>
}

function WatchlistRow({ instrument, onMarkAsSeen, markingInFlight }) {
  const [savedMessage, setSavedMessage] = useState(null)

  const handleMarkAsSeen = async () => {
    setSavedMessage(null)
    const result = await onMarkAsSeen(instrument.instrument_id)
    if (result?.message) {
      setSavedMessage(result.message)
    }
  }

  return (
    <tr>
      <td className="col-symbol">{instrument.symbol}</td>
      <td className="col-price">{formatPrice(instrument.price)}</td>
      <td
        className={
          'col-percent ' +
          (instrument.percent_change > 0
            ? 'percent-up'
            : instrument.percent_change < 0
              ? 'percent-down'
              : '')
        }
      >
        {formatPercentChange(instrument.percent_change)}
      </td>
      <td className="col-volume">{formatVolume(instrument.cumulative_volume)}</td>
      <td className="col-freshness">
        {instrument.freshness_label}
        <br />
        <StatusBadge status={instrument.status} />
      </td>
      <td className="col-change">
        <ChangeIndicator change={instrument.change} />
      </td>
      <td className="col-action">
        <button
          onClick={handleMarkAsSeen}
          disabled={markingInFlight || instrument.status === 'unavailable'}
        >
          Mark as seen
        </button>
        {savedMessage && <div className="saved-message">{savedMessage}</div>}
      </td>
    </tr>
  )
}

export default function App() {
  const [instruments, setInstruments] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [markingInFlight, setMarkingInFlight] = useState(false)

  const fetchWatchlist = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/watchlist`)
      if (!response.ok) {
        throw new Error(`Server returned ${response.status}`)
      }
      const data = await response.json()
      setInstruments(data.instruments)
      setError(null)
    } catch (err) {
      // The backend itself never crashes on provider failure (per
      // architecture.md); this catch is for the case where the
      // backend/network is unreachable from the frontend's perspective,
      // which is a different, rarer failure mode worth showing
      // distinctly rather than silently leaving stale UI state.
      setError('Could not reach the backend. Is it running?')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchWatchlist()
    const interval = setInterval(fetchWatchlist, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [fetchWatchlist])

  const handleMarkAsSeen = async (instrumentId) => {
    setMarkingInFlight(true)
    try {
      const response = await fetch(
        `${API_BASE}/watchlist/${instrumentId}/checkpoint`,
        { method: 'POST' }
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || `Server returned ${response.status}`)
      }
      const result = await response.json()
      // Immediately re-fetch so the row's change-state reflects the new
      // checkpoint right away, rather than waiting for the next poll.
      await fetchWatchlist()
      return result
    } catch (err) {
      setError(err.message)
      return null
    } finally {
      setMarkingInFlight(false)
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Smart Market Watchlist</h1>
      </header>

      {error && <div className="error-banner">{error}</div>}

      {loading ? (
        <p className="loading">Loading watchlist...</p>
      ) : (
        <table className="watchlist-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Price</th>
              <th>Day %</th>
              <th>Volume</th>
              <th>Freshness</th>
              <th>Change</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {instruments.map((instrument) => (
              <WatchlistRow
                key={instrument.instrument_id || instrument.symbol}
                instrument={instrument}
                onMarkAsSeen={handleMarkAsSeen}
                markingInFlight={markingInFlight}
              />
            ))}
          </tbody>
        </table>
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
