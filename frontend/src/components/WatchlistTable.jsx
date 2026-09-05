import { formatPrice, formatVolume, formatPercentChange, formatMarketDataLabel } from '../format'

// Status labels/classes match app/models/market_snapshot.py's
// SnapshotStatus exactly (ok/stale/unavailable/invalid) -- "invalid" is
// mapped the same way as "unavailable" because GET /watchlist never
// actually serializes "invalid" today (an invalid-price snapshot is
// reported as "unavailable" at the route level, per
// app/routes/watchlist.py), but the badge stays defensive rather than
// assuming that will always hold.
const STATUS_LABELS = {
  ok: { text: 'Fresh', className: 'status-ok' },
  stale: { text: 'Delayed', className: 'status-stale' },
  unavailable: { text: 'Unavailable', className: 'status-unavailable' },
  invalid: { text: 'Invalid', className: 'status-unavailable' },
}

function StatusBadge({ status }) {
  const info = STATUS_LABELS[status] || { text: status, className: '' }
  return <span className={`status-badge ${info.className}`}>{info.text}</span>
}

function ChangeIndicator({ change }) {
  // has_baseline is false both when no checkpoint has ever been set
  // AND when the current snapshot is unavailable -- either way, the
  // backend's own `reason` string already says the right thing
  // ("Baseline pending..." / "Data unavailable..."), so it's shown
  // as-is rather than re-deriving a label here.
  if (!change.has_baseline) {
    return <span className="change-baseline">{change.reason}</span>
  }
  if (change.meaningful_change) {
    // Deliberately muted relative to the Attention section above --
    // that's the primary "look here" surface; this is a secondary,
    // in-row confirmation of the same fact, not a second alarm.
    return <span className="change-meaningful">{change.reason}</span>
  }
  return <span className="change-none">No meaningful change</span>
}

function WatchlistRow({ instrument, onMarkAsSeen, inFlight, actionError, savedMessage }) {
  const canMarkAsSeen = instrument.status === 'ok'

  return (
    <tr>
      <td className="col-symbol">
        <span className="symbol-primary">{instrument.symbol}</span>
        {instrument.exchange && <span className="symbol-exchange">{instrument.exchange}</span>}
      </td>
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
        {formatMarketDataLabel(instrument)}
        <br />
        <StatusBadge status={instrument.status} />
      </td>
      <td className="col-change">
        <ChangeIndicator change={instrument.change} />
      </td>
      <td className="col-action">
        <button
          onClick={() => onMarkAsSeen(instrument.instrument_id)}
          disabled={!canMarkAsSeen || inFlight}
          title={!canMarkAsSeen ? 'No current data to acknowledge' : undefined}
        >
          {inFlight ? 'Saving…' : 'Mark as seen'}
        </button>
        {actionError && <div className="action-error">{actionError}</div>}
        {!actionError && savedMessage && <div className="saved-message">{savedMessage}</div>}
      </td>
    </tr>
  )
}

export default function WatchlistTable({
  instruments,
  totalCount,
  unavailable,
  onMarkAsSeen,
  inFlightIds,
  actionErrors,
  savedMessages,
}) {
  // Three distinct reasons this list can be empty must not collapse
  // into one misleading message: the watchlist fetch never
  // successfully loaded (unavailable -- do NOT claim a confirmed
  // empty watchlist), the real watchlist has items but the current
  // search/filter matched none of them, or the watchlist is genuinely
  // empty. `totalCount` is the real, unfiltered instrument count --
  // `instruments` here is already search/filter-narrowed.
  if (instruments.length === 0) {
    if (unavailable) {
      return <p className="empty-state">Could not load your watchlist. Retrying shortly.</p>
    }
    if (totalCount > 0) {
      return <p className="empty-state">No stocks in your watchlist match the current search/filter.</p>
    }
    return <p className="empty-state">Your watchlist is empty.</p>
  }

  return (
    <div className="table-scroll">
      <table className="watchlist-table">
        <thead>
          <tr>
            <th className="col-symbol">Symbol</th>
            <th className="col-price">Price</th>
            <th className="col-percent">Day %</th>
            <th className="col-volume">Volume</th>
            <th className="col-freshness">Freshness</th>
            <th className="col-change">Change</th>
            <th className="col-action">Action</th>
          </tr>
        </thead>
        <tbody>
          {instruments.map((instrument) => (
            <WatchlistRow
              key={instrument.instrument_id || instrument.symbol}
              instrument={instrument}
              onMarkAsSeen={onMarkAsSeen}
              inFlight={inFlightIds.has(instrument.instrument_id)}
              actionError={actionErrors[instrument.instrument_id]}
              savedMessage={savedMessages[instrument.instrument_id]}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}
