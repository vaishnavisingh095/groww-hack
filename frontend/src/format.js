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
