// Backend API client. The frontend NEVER calls yfinance directly --
// every request goes through our own FastAPI backend, which is the only
// thing that talks to the market data provider. One place for the base
// URL and every backend call, so nothing else in the app hand-rolls a
// fetch() and risks drifting from the real route paths.
const API_BASE = 'http://127.0.0.1:8000'

async function parseErrorDetail(response) {
  const body = await response.json().catch(() => ({}))
  return body.detail || `Server returned ${response.status}`
}

export async function fetchWatchlist() {
  const response = await fetch(`${API_BASE}/watchlist`)
  if (!response.ok) {
    throw new Error(await parseErrorDetail(response))
  }
  const data = await response.json()
  return data.instruments
}

export async function fetchAttention() {
  const response = await fetch(`${API_BASE}/watchlist/attention`)
  if (!response.ok) {
    throw new Error(await parseErrorDetail(response))
  }
  const data = await response.json()
  return data.attention_items
}

// Explicit single-instrument "mark as seen" -- the only acknowledgement
// action this milestone exposes (no "mark all" button; the backend
// supports one, but nothing in the approved UX requires it here).
// Path matches architecture.md's documented contract exactly:
// POST /watchlist/instruments/{id}/checkpoint.
export async function markInstrumentAsSeen(instrumentId) {
  const response = await fetch(
    `${API_BASE}/watchlist/instruments/${instrumentId}/checkpoint`,
    { method: 'POST' }
  )
  if (!response.ok) {
    throw new Error(await parseErrorDetail(response))
  }
  return response.json()
}
