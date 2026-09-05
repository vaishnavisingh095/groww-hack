import { useState } from 'react'
import { formatPrice, formatPercentChange, formatRelativeTime, formatMarketDataLabel } from '../format'

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

// Absolute rendering of the SAME item.detected_at value the footer's
// relative label already uses -- not a new fact, just a second, precise
// formatting of one already-displayed field. No new dependency:
// Date/toLocaleString are built into the JS runtime.
function formatAbsoluteDetectedAt(isoString) {
  if (!isoString) return null
  const date = new Date(isoString)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

// Mirrors WatchlistTable.jsx's own STATUS_LABELS wording exactly (Fresh/
// Delayed/Unavailable/Invalid). Duplicated here as a tiny local display
// map rather than imported -- the two components don't share a module
// for display-only lookups -- but it is the same real
// watchlistEntry.status field, never reinterpreted.
const STATUS_LABELS = {
  ok: 'Fresh',
  stale: 'Delayed',
  unavailable: 'Unavailable',
  invalid: 'Invalid',
}

// VOLUME_ACCELERATION_THRESHOLD mirrors app/services/change_engine.py's
// module constant exactly (locked at 2.0 -- see decisions.md's "Locked
// starting thresholds"). No API response field carries it (it's a
// fixed, shared constant, never per-event), so it's duplicated here as
// a plain literal, the same pattern this file already uses for
// LEVEL_LABELS/WHY_IT_MATTERS. If this backend constant is ever
// changed, this copy must be updated by hand -- there is no automatic
// sync.
//
// The PRICE threshold is deliberately NOT duplicated here as a
// constant -- it is no longer fixed (see decisions.md's "Adaptive price
// meaningful-change threshold" entry): each attention item carries its
// own backend-computed, backend-persisted price_threshold_applied
// value, read directly off `item` below. This is not a frontend
// calculation -- it's the exact number the backend already applied,
// only ever compared/displayed here, never derived.
const VOLUME_ACCELERATION_THRESHOLD = 2.0

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
  // formatMarketDataLabel builds "Market data · HH:MM IST · Xm ago" from
  // watchlistEntry's bar_timestamp + status + data_age_seconds when a
  // bar_timestamp is available, falling back to the backend's own
  // freshness_label verbatim otherwise -- see format.js. status/
  // data_age_seconds (freshness/staleness) are unchanged; bar_timestamp
  // is purely additional context, never used to decide them.
  const freshnessLabel = formatMarketDataLabel(watchlistEntry)
  const status = watchlistEntry ? watchlistEntry.status : null
  // Mirrors WatchlistTable.jsx's own canMarkAsSeen guard exactly -- this
  // button and the watchlist row's button both call the SAME
  // onMarkAsSeen handler (App.jsx's handleMarkAsSeen), which always
  // fetches fresh and always advances the checkpoint; there is no
  // separate "acknowledge without a fresh fetch" action anywhere in
  // this app. A stale/delayed watchlistEntry must disable this button
  // for the same reason it already disables the watchlist row's.
  const canMarkAsSeen = watchlistEntry?.status === 'ok'
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
  const absoluteDetectedLabel = formatAbsoluteDetectedAt(item.detected_at)

  // View Details is OBSERVATION ONLY -- local component state, no prop,
  // no callback into App.jsx, no API call. Toggling it never touches
  // instruments/attentionItems, never calls loadAll(), never creates a
  // checkpoint or acknowledges anything, and never affects
  // searchQuery/watchlistFilter. React preserves this state across
  // polling refreshes because AttentionGroup keys each card by the
  // stable item.checkpoint_id -- not item.instrument_id, since multiple
  // independent attention events (different checkpoint_id) can
  // legitimately share the same instrument_id.
  const [expanded, setExpanded] = useState(false)
  const detailsId = `attention-details-${item.checkpoint_id}`

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

      {/* VIEW DETAILS -- deliberately does NOT repeat What Changed / Why
          It Matters / Signals, which the card above already covers.
          This explains HOW the item was detected: the existing
          meaningful-change rule applied to this item's own already-
          known values (never a new rule, never a new score -- "Result"
          below is a plain >= comparison of two already-backend-provided
          numbers, item.price_change_pct and item.price_threshold_applied
          -- the price threshold is per-event and adaptive, not a fixed
          frontend constant, see decisions.md; volume still compares
          against the locked VOLUME_ACCELERATION_THRESHOLD), plus
          data-trust facts not already prominent on the card. Built
          entirely from item/watchlistEntry, already passed into this
          component -- no new calculation, only a comparison of values
          the backend already computed. */}
      <button
        type="button"
        className="attention-details-toggle"
        aria-expanded={expanded}
        aria-controls={detailsId}
        onClick={() => setExpanded((prev) => !prev)}
      >
        {expanded ? 'Hide details' : 'View details'}
      </button>

      {expanded && (
        <div className="attention-details" id={detailsId}>
          <p className="attention-details-heading">Details</p>

          <p className="attention-block-label">Detection</p>
          <div className="attention-details-row">
            <span className="attention-details-label">Detected</span>
            <span className="attention-details-value">{absoluteDetectedLabel || '—'}</span>
          </div>
          <div className="attention-details-row">
            <span className="attention-details-label">Attention</span>
            <span className="attention-details-value">{item.attention_level.toUpperCase()}</span>
          </div>
          <div className="attention-details-row">
            <span className="attention-details-label">Since checkpoint</span>
            <span className={`attention-details-value ${pctClass}`}>
              {formatPercentChange(item.price_change_pct)}
            </span>
          </div>

          <p className="attention-block-label">Detection basis</p>
          <div className="attention-details-row">
            <span className="attention-details-label">Price movement</span>
            <span className={`attention-details-value ${pctClass}`}>
              {formatPercentChange(item.price_change_pct)}
            </span>
          </div>
          <div className="attention-details-row">
            <span className="attention-details-label">Meaningful threshold</span>
            <span className="attention-details-value">
              {item.price_threshold_applied != null
                ? `≥ ${item.price_threshold_applied.toFixed(2)}%`
                : 'Not available'}
            </span>
          </div>
          <div className="attention-details-row">
            <span className="attention-details-label">Result</span>
            <span className="attention-details-value">
              {item.price_threshold_applied != null
                ? Math.abs(item.price_change_pct) >= item.price_threshold_applied
                  ? 'Threshold crossed'
                  : 'Below threshold'
                : 'Not available'}
            </span>
          </div>

          {/* "Not available" for all three rows -- never a fabricated
              0.0x or a fabricated threshold/status -- when the volume
              signal itself is unavailable. Missing volume is never
              interpreted as zero. */}
          <p className="attention-block-label">Volume signal</p>
          <div className="attention-details-row">
            <span className="attention-details-label">Volume acceleration</span>
            <span className="attention-details-value">
              {item.volume_acceleration_available
                ? `${item.volume_acceleration_ratio.toFixed(2)}×`
                : 'Not available'}
            </span>
          </div>
          <div className="attention-details-row">
            <span className="attention-details-label">Meaningful threshold</span>
            <span className="attention-details-value">
              {item.volume_acceleration_available
                ? `≥ ${VOLUME_ACCELERATION_THRESHOLD.toFixed(2)}×`
                : 'Not available'}
            </span>
          </div>
          <div className="attention-details-row">
            <span className="attention-details-label">Status</span>
            <span className="attention-details-value">
              {item.volume_acceleration_available ? 'Available' : 'Not available'}
            </span>
          </div>

          <p className="attention-block-label">Data context</p>
          <div className="attention-details-row">
            <span className="attention-details-label">Data status</span>
            <span className="attention-details-value">
              {status ? STATUS_LABELS[status] || status : '—'}
            </span>
          </div>
          <div className="attention-details-row">
            <span className="attention-details-label">Updated</span>
            <span className="attention-details-value">{freshnessLabel || '—'}</span>
          </div>
        </div>
      )}

      <div className="attention-card-action">
        <button
          onClick={() => onMarkAsSeen(item.instrument_id)}
          disabled={!canMarkAsSeen || inFlight}
          title={!canMarkAsSeen ? 'No current data to acknowledge' : undefined}
        >
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
            key={item.checkpoint_id}
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
  searchQuery,
  matchesSearch,
  attentionUnavailable,
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

  // Search is applied ONLY here, after highItems/worthCheckingItems (and
  // therefore the banner's count/breakdown text above, which reads off
  // items.length/highItems.length/worthCheckingItems.length -- the FULL,
  // unfiltered arrays) are already computed. remaining/remainingSummary
  // above are also computed from the full `instruments` prop, not this
  // filter -- the global summary never changes because of a search
  // query. instrumentsById already carries `exchange`, which GET
  // /watchlist/attention's own items don't include, so the same
  // matchesSearch rule the watchlist table uses applies identically to
  // an attention card's underlying instrument.
  const visibleHighItems = highItems.filter((item) =>
    matchesSearch(item.symbol, instrumentsById.get(item.instrument_id)?.exchange, searchQuery)
  )
  const visibleWorthCheckingItems = worthCheckingItems.filter((item) =>
    matchesSearch(item.symbol, instrumentsById.get(item.instrument_id)?.exchange, searchQuery)
  )
  const noSearchMatches =
    items.length > 0 &&
    searchQuery &&
    visibleHighItems.length === 0 &&
    visibleWorthCheckingItems.length === 0

  const groupProps = { instrumentsById, onMarkAsSeen, inFlightIds, actionErrors }

  return (
    <section className="attention-section">
      {/* The banner is purely a presentational wrapper around the same
          heading/count this section already rendered -- the new
          high/worth-checking breakdown line reads directly off
          highItems.length / worthCheckingItems.length, the exact same
          arrays (same filters, same source order) the groups below are
          already built from. No new count logic, no new grouping rule. */}
      <div className={`attention-banner${items.length === 0 ? ' attention-banner-compact' : ''}`}>
        <h2 className="attention-heading">Since You Last Checked</h2>

        {items.length === 0 ? (
          // attentionUnavailable is true only when GET /watchlist/attention
          // has NEVER successfully returned data -- `items` being empty in
          // that case is an absence of information, not a backend-confirmed
          // "zero active changes." Asserting "caught up" here would claim a
          // fact the backend never actually returned. Once attention has
          // succeeded at least once, a later transient failure keeps
          // showing the last real (possibly genuinely empty) result
          // instead, which is legitimate per the existing "keep last known
          // good data" behavior loadAll() already applies everywhere else.
          attentionUnavailable ? (
            <p className="attention-caught-up">
              Attention data is temporarily unavailable — trying again shortly.
            </p>
          ) : (
            <p className="attention-caught-up">
              You're all caught up — nothing has meaningfully changed since you last checked.
            </p>
          )
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
          <AttentionGroup
            title="High Attention"
            variant="high"
            groupItems={visibleHighItems}
            {...groupProps}
          />
          <AttentionGroup
            title="Worth Checking"
            variant="secondary"
            groupItems={visibleWorthCheckingItems}
            {...groupProps}
          />
          {/* Truthful, search-scoped empty state -- only shown when real
              attention items exist but none match the current query.
              Never replaces or alters the "caught up" message above,
              which is about there being no attention items at all. */}
          {noSearchMatches && (
            <p className="attention-search-empty">No attention items match "{searchQuery}".</p>
          )}
        </>
      )}

      {remainingSummary && <p className="attention-remaining-summary">{remainingSummary}</p>}
    </section>
  )
}
