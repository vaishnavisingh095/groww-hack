import { formatPrice, formatPercentChange, formatRelativeTime } from '../format'

// Mirrors app/services/attention_engine.py's AttentionLevel enum values
// exactly (already serialized lowercase by the API via `.value`).
const LEVEL_LABELS = {
  high: 'High',
  medium: 'Medium',
  watch: 'Watch',
}

// "Why it matters" copy is keyed ONLY off the backend-computed
// attention_level bucket -- never the raw attention_score (an
// implementation detail with no established user-facing meaning) and
// never a specific signal (the persisted data doesn't prove which
// signal, if only one, actually drove the classification -- see
// decisions.md / the Change #2 inspection report). The three tiers
// already encode "how far past meaningful" ordinally without exposing
// the underlying number.
const WHY_IT_MATTERS = {
  high: 'One of the strongest moves in your watchlist right now.',
  medium: 'A clear, meaningful move in your watchlist.',
  watch: 'Just crossed into meaningful-change territory.',
}

function AttentionCard({ item, watchlistEntry, onMarkAsSeen, inFlight, actionError }) {
  // GET /watchlist/attention does not include current price, day-over-
  // day change, exchange, or freshness/status -- those live on GET
  // /watchlist. Both responses share instrument_id, so the card looks
  // up the matching watchlist row to show them; this is real data from
  // a real endpoint, joined client-side, not invented. If the two
  // responses are ever momentarily out of sync (e.g. a poll landed
  // between the two fetches), the fields below degrade to "—"/absence
  // rather than guessing.
  const price = watchlistEntry ? watchlistEntry.price : null
  const freshnessLabel = watchlistEntry ? watchlistEntry.freshness_label : null
  const exchange = watchlistEntry ? watchlistEntry.exchange : null
  // Day-over-day change, from GET /watchlist's percent_change --
  // deliberately a DIFFERENT field from item.price_change_pct
  // (since-checkpoint). Rendered separately, smaller, and explicitly
  // captioned so the two can never be mistaken for one another.
  const dayChangePct = watchlistEntry ? watchlistEntry.percent_change : null

  const pctClass =
    item.price_change_pct > 0 ? 'percent-up' : item.price_change_pct < 0 ? 'percent-down' : ''
  const dayPctClass =
    dayChangePct > 0 ? 'percent-up' : dayChangePct < 0 ? 'percent-down' : ''

  const detectedLabel = formatRelativeTime(item.detected_at)

  return (
    <li className={`attention-card attention-level-${item.attention_level}`}>
      <div className="attention-card-header">
        <span className="attention-rank">#{item.rank}</span>
        <span className="attention-symbol">{item.symbol}</span>
        {exchange && <span className="attention-exchange">{exchange}</span>}
        <span className={`attention-badge attention-badge-${item.attention_level}`}>
          {LEVEL_LABELS[item.attention_level] || item.attention_level}
        </span>
      </div>

      {/* PRICE AREA -- current price is the strongest numerical element
          on the card; since-checkpoint movement (item.price_change_pct)
          stays prominent next to it. Day-over-day (dayChangePct) is a
          different number from a different source and is deliberately
          smaller, separately captioned, and never merged with the
          since-checkpoint figure. Neither value is recalculated here --
          both are rendered exactly as the API returned them. */}
      <div className="attention-price-area">
        <span className="attention-price-large">{formatPrice(price)}</span>
        <span className={`attention-pct-large ${pctClass}`}>
          {formatPercentChange(item.price_change_pct)}
          <span className="attention-pct-caption">since checkpoint</span>
        </span>
        {dayChangePct !== null && dayChangePct !== undefined && (
          <span className={`attention-day-pct ${dayPctClass}`}>
            {formatPercentChange(dayChangePct)}
            <span className="attention-pct-caption">today</span>
          </span>
        )}
      </div>

      {/* WHAT CHANGED -- the observed movement since the checkpoint,
          restated as a readable sentence (the price area above is the
          quick-glance figure; this is the explanatory sentence). */}
      <div className="attention-block">
        <p className="attention-block-label">What changed</p>
        <p className="attention-block-body">
          Now <span className="attention-price">{formatPrice(price)}</span> —{' '}
          <span className={pctClass}>{formatPercentChange(item.price_change_pct)}</span> since your last
          check.
        </p>
      </div>

      {/* WHY IT MATTERS -- priority framing from the backend-computed
          attention_level only. No raw score, no signal attribution. */}
      <div className="attention-block">
        <p className="attention-block-label">Why it matters</p>
        <p className="attention-block-body">
          {WHY_IT_MATTERS[item.attention_level] || 'This crossed the meaningful-change threshold.'}
        </p>
      </div>

      {/* SIGNALS -- the raw measured values only, never a per-signal
          verdict (the persisted data doesn't prove which one, if only
          one, actually triggered). Volume is explicit text, never a
          fabricated 0x, when unavailable. */}
      <div className="attention-block">
        <p className="attention-block-label">Signals</p>
        <ul className="attention-signals">
          <li>
            <span className="attention-signal-name">Price</span>
            <span className={`attention-signal-value ${pctClass}`}>
              {formatPercentChange(item.price_change_pct)}
            </span>
          </li>
          <li>
            <span className="attention-signal-name">Volume</span>
            <span className="attention-signal-value">
              {item.volume_acceleration_available
                ? `${item.volume_acceleration_ratio.toFixed(1)}×`
                : 'Not available'}
            </span>
          </li>
        </ul>
      </div>

      {/* FOOTER / TRUST INFORMATION -- freshness (relocated from the
          old "What changed" text) and the ChangeEvent's own detection
          time, both quiet/small. Neither implies real-time data --
          freshnessLabel is the same honest "Fresh/Delayed/Unavailable"
          label used everywhere else in the app, and detectedLabel is
          when this specific change was detected, not "now." */}
      {(freshnessLabel || detectedLabel) && (
        <div className="attention-footer">
          {freshnessLabel && <span>{freshnessLabel}</span>}
          {freshnessLabel && detectedLabel && <span aria-hidden="true"> · </span>}
          {detectedLabel && <span>{detectedLabel}</span>}
        </div>
      )}

      <div className="attention-card-action">
        <button onClick={() => onMarkAsSeen(item.instrument_id)} disabled={inFlight}>
          {inFlight ? 'Saving…' : 'Mark as seen'}
        </button>
        {actionError && <div className="action-error">{actionError}</div>}
      </div>
    </li>
  )
}

/**
 * Truthful summary of the instruments NOT currently in the attention
 * list. Deliberately does not use `instruments.length - items.length`
 * and call the result "unchanged" -- an instrument can be missing from
 * the attention list for several different real reasons (no baseline
 * yet, unavailable data, or genuinely no meaningful change), and a
 * stale snapshot can even report meaningful_change=true in GET
 * /watchlist without ever producing a persisted ChangeEvent (see
 * decisions.md), so "not in the attention list" alone does not by
 * itself mean "unchanged."
 *
 * Only every field already computed by the backend
 * (`status`, `change.has_baseline`, `change.meaningful_change`) is
 * used here -- nothing is re-derived from raw price/volume numbers.
 * The stronger claim ("no significant changes") is only made when
 * EVERY remaining instrument is cleanly available, has a real
 * baseline, and was not flagged meaningful; otherwise neutral wording
 * is used, exactly per the approved scope.
 */
function summarizeRemaining(remaining) {
  if (remaining.length === 0) {
    return null
  }

  const allCleanlyUnchanged = remaining.every(
    (inst) => inst.status !== 'unavailable' && inst.change.has_baseline && !inst.change.meaningful_change
  )

  const noun = remaining.length === 1 ? 'stock' : 'stocks'

  if (allCleanlyUnchanged) {
    const verb = remaining.length === 1 ? 'has' : 'have'
    return `${remaining.length} other ${noun} ${verb} had no significant changes.`
  }

  const verb = remaining.length === 1 ? 'is' : 'are'
  return `${remaining.length} other ${noun} ${verb} in your watchlist.`
}

// One labeled sub-group of the (already backend-sorted) attention list.
// Renders nothing when its slice is empty -- callers don't need to
// guard against an empty heading with nothing under it. Factored out
// only to avoid maintaining two copies of the same AttentionCard
// mapping/prop-wiring in sync with each other; it is not a separate
// abstraction over new behavior.
function AttentionGroup({
  title,
  variant,
  groupItems,
  instrumentsById,
  onMarkAsSeen,
  inFlightIds,
  actionErrors,
}) {
  if (groupItems.length === 0) {
    return null
  }

  return (
    <div className={`attention-group attention-group-${variant}`}>
      <h3 className="attention-group-heading">{title}</h3>
      <ul className="attention-list">
        {groupItems.map((item) => (
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
    </div>
  )
}

export default function AttentionSection({
  items,
  instruments,
  onMarkAsSeen,
  inFlightIds,
  actionErrors,
  onMarkAllAsSeen,
  markAllInFlight,
  markAllError,
  markAllPartialMessage,
}) {
  const instrumentsById = new Map(instruments.map((inst) => [inst.instrument_id, inst]))

  const attentionIds = new Set(items.map((item) => item.instrument_id))
  const remaining = instruments.filter((inst) => !attentionIds.has(inst.instrument_id))
  const remainingSummary = summarizeRemaining(remaining)

  // Partition only -- items already arrive from GET /watchlist/attention
  // sorted by the backend-computed attention_score (see
  // AttentionEngine.get_ranked_active_items), and filtering a
  // stably-sorted array preserves the relative order of what survives.
  // No re-sorting, no new scoring: "medium" and "watch" both land in
  // "Worth Checking" via the same `!== 'high'` check, so an unknown/
  // unexpected level value safely falls into "Worth Checking" too,
  // rather than being silently dropped or invented into a new category
  // -- its card still renders with its own real (unmodified) badge.
  const highItems = items.filter((item) => item.attention_level === 'high')
  const worthCheckingItems = items.filter((item) => item.attention_level !== 'high')

  const groupProps = { instrumentsById, onMarkAsSeen, inFlightIds, actionErrors }

  return (
    <section className="attention-section">
      {/* The banner is purely a presentational wrapper around the same
          heading/count this section already rendered -- the new
          high/worth-checking breakdown line reads directly off
          highItems.length / worthCheckingItems.length, the exact same
          arrays (same filters, same source order) the groups below are
          already built from. No new count logic, no new grouping rule. */}
      <div className="attention-banner">
        <h2 className="attention-heading">Since You Last Checked</h2>

        {items.length === 0 ? (
          <p className="attention-caught-up">
            You're all caught up — nothing has meaningfully changed since you last checked.
          </p>
        ) : (
          // Meta text (count/breakdown) and the Mark-all action sit as a
          // horizontal row -- text on the left, the one primary action on
          // the right -- rather than stacked, so the banner reads as an
          // inbox header, not a vertical list of paragraphs. Same data,
          // same button, same behavior; only the layout wrapper is new.
          <div className="attention-banner-row">
            <div className="attention-banner-meta">
              <p className="attention-count">
                {items.length} {items.length === 1 ? 'thing deserves' : 'things deserve'} your attention
              </p>
              <p className="attention-breakdown">
                {highItems.length} high-priority {highItems.length === 1 ? 'change' : 'changes'}
                {' • '}
                {worthCheckingItems.length} worth checking
              </p>
            </div>
            {/* Whole-watchlist acknowledgement. Only rendered when there
                is at least one active attention item -- the caught-up
                branch above never reaches this, so there is nothing to
                mark as seen when the button would otherwise appear. */}
            <div className="attention-banner-actions">
              <button
                type="button"
                className="mark-all-button"
                onClick={onMarkAllAsSeen}
                disabled={markAllInFlight}
              >
                {markAllInFlight ? 'Marking…' : 'Mark all as seen'}
              </button>
              {markAllError && <div className="action-error">{markAllError}</div>}
              {!markAllError && markAllPartialMessage && (
                <div className="mark-all-partial-message">{markAllPartialMessage}</div>
              )}
            </div>
          </div>
        )}
      </div>

      {items.length > 0 && (
        <>
          <AttentionGroup title="High Attention" variant="high" groupItems={highItems} {...groupProps} />
          <AttentionGroup
            title="Worth Checking"
            variant="secondary"
            groupItems={worthCheckingItems}
            {...groupProps}
          />
        </>
      )}

      {remainingSummary && <p className="attention-remaining-summary">{remainingSummary}</p>}
    </section>
  )
}
