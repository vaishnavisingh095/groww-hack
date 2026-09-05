// Backend API client. The frontend NEVER calls yfinance directly --
// every request goes through our own FastAPI backend, which is the only
// thing that talks to the market data provider. One place for the base
// URL and every backend call, so nothing else in the app hand-rolls a
// fetch() and risks drifting from the real route paths.
const API_BASE = 'http://127.0.0.1:8000'

async function parseErrorDetail(response) {
  const body = await response.json().catch(() => ({}))
  // Every existing error this backend raises via HTTPException(detail=...)
  // is a plain string. FastAPI's OWN automatic request-body validation
  // (422, e.g. an invalid exchange or blank symbol) instead returns
  // detail as a LIST of {loc, msg, type} objects -- stringifying that
  // array directly would surface something like "[object Object]"
  // rather than a truthful, readable message.
  if (typeof body.detail === 'string') {
    return body.detail
  }
  if (Array.isArray(body.detail) && body.detail.length > 0 && body.detail[0].msg) {
    return body.detail[0].msg
  }
  return `Server returned ${response.status}`
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

// Explicit single-instrument "mark as seen".
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

// Explicit whole-watchlist "mark all as seen". The backend already
// treats per-instrument failure as a partial-success case, not a whole-
// request failure -- it returns {updated: [...], skipped: [...]} with a
// 200 rather than throwing, so this only rejects on a genuine request
// failure (network/backend unreachable, non-2xx status), never because
// some instruments were skipped. Path matches architecture.md's
// documented contract exactly: POST /watchlist/checkpoint.
export async function markAllAsSeen() {
  const response = await fetch(`${API_BASE}/watchlist/checkpoint`, {
    method: 'POST',
  })
  if (!response.ok) {
    throw new Error(await parseErrorDetail(response))
  }
  return response.json()
}

// Explicit "add a new instrument to track" action (Add Stock). Path
// matches the backend's documented contract: POST /watchlist/instruments.
// The backend itself validates/normalizes symbol+exchange (reusing the
// existing Instrument model) and confirms the provider can resolve the
// instrument before creating anything -- this call surfaces whatever
// truthful error it returns (422 for invalid input, 503 if the provider
// can't resolve it) rather than duplicating that logic here.
export async function addInstrument(symbol, exchange) {
  const response = await fetch(`${API_BASE}/watchlist/instruments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol, exchange }),
  })
  if (!response.ok) {
    throw new Error(await parseErrorDetail(response))
  }
  return response.json()
}
