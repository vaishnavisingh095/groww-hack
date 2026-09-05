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

// bar_timestamp arrives as an ISO-8601 string carrying its own explicit
// UTC offset, exactly as yfinance/pandas reported it (Asia/Kolkata for
// NSE/BSE) -- never stripped or converted upstream (see
// app/providers/base.py / decisions.md's "Market-bar timestamp
// propagation" entry). It is deliberately reformatted here with an
// EXPLICIT timeZone: 'Asia/Kolkata' rather than left to the browser's
// own local timezone: a JS Date always stores an absolute instant
// (UTC millis under the hood), so without pinning the timezone
// explicitly, a viewer whose machine is not set to IST would see the
// wrong wall-clock hour. Pinning to Asia/Kolkata -- the exchange's own
// timezone -- is what makes "the market clock read 15:15" true for
// every viewer, not just one physically in India. This is NOT a UTC
// conversion and does not lose information; it renders the SAME
// instant in the zone the data actually originated in.
function formatBarTime(isoString) {
  const ms = new Date(isoString).getTime()
  if (Number.isNaN(ms)) return null
  const time = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(ms)
  return `${time} IST`
}

// Mirrors app/routes/watchlist.py's _status_label age-to-text
// conversion exactly (ok: always seconds -- bounded by
// STALE_THRESHOLD_SECONDS=120 by construction; stale: minutes, with a
// sub-minute fallback) -- reimplemented here only for the age SUFFIX so
// it can be recombined with the new bar-time prefix below. The
// underlying age NUMBER is still exactly data_age_seconds as returned
// by the backend, never recomputed from bar_timestamp or the browser's
// own clock.
function formatAgeSuffix(status, dataAgeSeconds) {
  if (dataAgeSeconds === null || dataAgeSeconds === undefined) return null
  if (status === 'ok') return `${dataAgeSeconds}s ago`
  const minutes = Math.floor(dataAgeSeconds / 60)
  return minutes >= 1 ? `${minutes}m ago` : `${dataAgeSeconds}s ago`
}

// Market-data freshness label for the watchlist/attention views.
// Deliberately keeps TWO independently-sourced facts side by side
// rather than deriving one from the other:
//   - WHEN the price was actually observed on the exchange
//     (bar_timestamp, rendered in market-local IST) -- new.
//   - HOW LONG AGO our backend fetched it (status + data_age_seconds)
//     -- this is still the exact same authoritative freshness/
//     staleness signal as before status/data_age_seconds are computed
//     from fetched_at on the backend and are NOT recomputed here;
//     bar_timestamp is never used to decide ok/stale/unavailable.
// Falls back to the backend's own freshness_label verbatim (today's
// exact wording) whenever bar_timestamp is missing, or status is
// anything other than ok/stale -- never fabricates a label for data
// that isn't there.
export function formatMarketDataLabel(instrument) {
  if (!instrument) return null
  const { status, data_age_seconds: dataAgeSeconds, bar_timestamp: barTimestamp, freshness_label: freshnessLabel } = instrument

  if (status !== 'ok' && status !== 'stale') {
    return freshnessLabel ?? null
  }

  const barTime = barTimestamp ? formatBarTime(barTimestamp) : null
  const ageSuffix = formatAgeSuffix(status, dataAgeSeconds)

  if (!barTime || !ageSuffix) {
    return freshnessLabel ?? null
  }

  return `Market data · ${barTime} · ${ageSuffix}`
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
