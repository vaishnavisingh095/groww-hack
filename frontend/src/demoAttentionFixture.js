// ============================================================================
// TEMPORARY DEMO FIXTURE — screenshot use only. Not real data.
// ============================================================================
// Provides exactly 3 fabricated "Since You Last Checked" attention cards,
// rendered through the REAL, unmodified AttentionCard/AttentionSection
// components — nothing about the UI is faked, only the data fed into it.
// No backend, API, database, or change-detection logic is touched by this
// file; it is pure frontend fixture data, wired in from ONE clearly marked
// spot in App.jsx (search for DEMO_MODE).
//
// TO REMOVE after the screenshot:
//   1. Delete this file.
//   2. Delete its import in App.jsx.
//   3. Delete the "DEMO MODE" block in App.jsx (search for DEMO_MODE).
// ============================================================================

const minutesAgoIso = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString()

// Shape matches GET /watchlist/attention's real attention_items entries
// exactly (see routes/watchlist.py's get_attention response / decisions.md).
export const DEMO_ATTENTION_ITEMS = [
  {
    instrument_id: 'demo-tcs',
    symbol: 'TCS',
    checkpoint_id: 'demo-checkpoint-tcs',
    detected_at: minutesAgoIso(3),
    price_change_pct: 4.17,
    price_threshold_applied: 1.0,
    volume_acceleration_ratio: null,
    volume_acceleration_available: false,
    attention_score: 4.17,
    attention_level: 'high',
    explanation: 'TCS moved +4.2% since your last check.',
    rank: 1,
  },
  {
    instrument_id: 'demo-reliance',
    symbol: 'RELIANCE',
    checkpoint_id: 'demo-checkpoint-reliance',
    detected_at: minutesAgoIso(6),
    price_change_pct: 0.8,
    price_threshold_applied: 1.0,
    volume_acceleration_ratio: 2.4,
    volume_acceleration_available: true,
    attention_score: 1.2,
    attention_level: 'medium',
    explanation: 'Trading volume accelerated to 2.4× the rate observed before you last checked.',
    rank: 2,
  },
  {
    instrument_id: 'demo-infy',
    symbol: 'INFY',
    checkpoint_id: 'demo-checkpoint-infy',
    detected_at: minutesAgoIso(9),
    price_change_pct: -1.34,
    price_threshold_applied: 1.0,
    volume_acceleration_ratio: null,
    volume_acceleration_available: false,
    attention_score: 1.34,
    attention_level: 'medium',
    explanation: 'INFY moved -1.3% since your last check.',
    rank: 3,
  },
]

// Matching GET /watchlist-shaped entries so AttentionCard's joined price/
// freshness/status/exchange fields render sensibly instead of blank.
// instrument_id matches the items above 1:1, and is deliberately a
// non-ObjectId string so it can never collide with a real instrument_id.
export const DEMO_WATCHLIST_ENTRIES = [
  {
    instrument_id: 'demo-tcs',
    symbol: 'TCS',
    exchange: 'NSE',
    price: 2304.0,
    percent_change: 1.2,
    cumulative_volume: 2720000,
    status: 'ok',
    freshness_label: 'Updated 8s ago',
    data_age_seconds: 8,
    bar_timestamp: minutesAgoIso(0),
  },
  {
    instrument_id: 'demo-reliance',
    symbol: 'RELIANCE',
    exchange: 'NSE',
    price: 1326.4,
    percent_change: 0.6,
    cumulative_volume: 9800000,
    status: 'ok',
    freshness_label: 'Updated 12s ago',
    data_age_seconds: 12,
    bar_timestamp: minutesAgoIso(0),
  },
  {
    instrument_id: 'demo-infy',
    symbol: 'INFY',
    exchange: 'NSE',
    price: 1114.5,
    percent_change: -0.4,
    cumulative_volume: 3900000,
    status: 'ok',
    freshness_label: 'Updated 5s ago',
    data_age_seconds: 5,
    bar_timestamp: minutesAgoIso(0),
  },
]
