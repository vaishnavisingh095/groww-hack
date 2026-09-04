// Shared display formatters -- used by both the watchlist table and the
// attention section, so the same number always reads the same way
// wherever it appears.

export function formatPrice(price) {
  if (price === null || price === undefined) return '—'
  return `₹${price.toFixed(2)}`
}

export function formatVolume(volume) {
  if (volume === null || volume === undefined) return '—'
  if (volume >= 1_000_000) return `${(volume / 1_000_000).toFixed(1)}M`
  if (volume >= 1_000) return `${(volume / 1_000).toFixed(1)}K`
  return String(volume)
}

export function formatPercentChange(pct) {
  if (pct === null || pct === undefined) return '—'
  const sign = pct >= 0 ? '+' : ''
  return `${sign}${pct.toFixed(2)}%`
}

// Presentation-only relative-time label ("Detected 12m ago"), mirroring
// the backend's own age-labeling convention (_status_label in
// app/routes/watchlist.py) -- this is display formatting of an
// already-fetched timestamp, not a business calculation.
export function formatRelativeTime(isoString) {
  if (!isoString) return null
  const thenMs = new Date(isoString).getTime()
  if (Number.isNaN(thenMs)) return null

  const diffSeconds = Math.max(0, Math.floor((Date.now() - thenMs) / 1000))
  if (diffSeconds < 60) return 'Detected just now'

  const diffMinutes = Math.floor(diffSeconds / 60)
  if (diffMinutes < 60) return `Detected ${diffMinutes}m ago`

  const diffHours = Math.floor(diffMinutes / 60)
  return `Detected ${diffHours}h ago`
}
