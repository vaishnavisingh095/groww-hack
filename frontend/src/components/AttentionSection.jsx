import { formatPrice, formatPercentChange } from '../format'

// Mirrors app/services/attention_engine.py's AttentionLevel enum values
// exactly (already serialized lowercase by the API via `.value`).
const LEVEL_LABELS = {
  high: 'High',
  medium: 'Medium',
  watch: 'Watch',
}

function AttentionCard({ item, watchlistEntry, onMarkAsSeen, inFlight, actionError }) {
  // GET /watchlist/attention does not include current price or
  // freshness/status -- those live on GET /watchlist. Both responses
  // share instrument_id, so the card looks up the matching watchlist
  // row to show them; this is real data from a real endpoint, joined
  // client-side, not invented. If the two responses are ever
  // momentarily out of sync (e.g. a poll landed between the two
  // fetches), the fields below degrade to "—" rather than guessing.
  const price = watchlistEntry ? watchlistEntry.price : null
  const freshnessLabel = watchlistEntry ? watchlistEntry.freshness_label : null

  const pctClass =
    item.price_change_pct > 0 ? 'percent-up' : item.price_change_pct < 0 ? 'percent-down' : ''

  return (
    <li className={`attention-card attention-level-${item.attention_level}`}>
      <div className="attention-card-header">
        <span className="attention-rank">#{item.rank}</span>
        <span className="attention-symbol">{item.symbol}</span>
        <span className={`attention-pct ${pctClass}`}>{formatPercentChange(item.price_change_pct)}</span>
        <span className={`attention-badge attention-badge-${item.attention_level}`}>
          {LEVEL_LABELS[item.attention_level] || item.attention_level}
        </span>
      </div>

      <div className="attention-card-meta">
        <span className="attention-price">{formatPrice(price)}</span>
        {freshnessLabel && <span className="attention-freshness">{freshnessLabel}</span>}
      </div>

      <p className="attention-explanation">{item.explanation}</p>

      <div className="attention-card-action">
        <button onClick={() => onMarkAsSeen(item.instrument_id)} disabled={inFlight}>
          {inFlight ? 'Saving…' : 'Mark as seen'}
        </button>
        {actionError && <div className="action-error">{actionError}</div>}
      </div>
    </li>
  )
}

export default function AttentionSection({ items, instruments, onMarkAsSeen, inFlightIds, actionErrors }) {
  const instrumentsById = new Map(instruments.map((inst) => [inst.instrument_id, inst]))

  return (
    <section className="attention-section">
      <h2 className="section-title">Since You Last Checked</h2>
      {items.length === 0 ? (
        <p className="attention-empty">Nothing has meaningfully changed since you last checked.</p>
      ) : (
        <ul className="attention-list">
          {items.map((item) => (
            <AttentionCard
              key={item.instrument_id}
              item={item}
              watchlistEntry={instrumentsById.get(item.instrument_id)}
              onMarkAsSeen={onMarkAsSeen}
              inFlight={inFlightIds.has(item.instrument_id)}
              actionError={actionErrors[item.instrument_id]}
            />
          ))}
        </ul>
      )}
    </section>
  )
}
