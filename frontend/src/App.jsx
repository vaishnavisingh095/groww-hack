import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import { addInstrument, fetchAttention, fetchWatchlist, markAllAsSeen, markInstrumentAsSeen } from './api'
import AttentionSection from './components/AttentionSection'
import WatchlistTable from './components/WatchlistTable'
import { filterStockSuggestions, findStockSuggestion } from './stockSuggestions'
// TEMPORARY DEMO MODE — screenshot use only. See demoAttentionFixture.js
// for the exact removal steps; this import is one of them.
import { DEMO_ATTENTION_ITEMS, DEMO_WATCHLIST_ENTRIES } from './demoAttentionFixture'

// Simple polling per the approved design: no WebSockets, one shared
// timer for both endpoints. 60s matches the backend's own documented
// target freshness window (see decisions.md's Freshness Policy).
const POLL_INTERVAL_MS = 60_000

// TEMPORARY DEMO MODE — screenshot use only, not production behavior.
// When true, 3 fabricated attention cards (see demoAttentionFixture.js)
// are prepended to the REAL attention items/instruments passed into
// AttentionSection below — real state (attentionItems/instruments) is
// never mutated, only what's passed to that one component. Set to
// false to instantly disable, or delete this flag + the import above +
// the two `DEMO_MODE ? ... : ...` lines below to fully remove.
const DEMO_MODE = true

// Case-insensitive match against an instrument's symbol or exchange --
// the only fields GET /watchlist already returns that a "search your
// watchlist" query could sensibly mean (no company name, no other
// metadata). Pure/stateless and defined once here so the watchlist
// table and the attention cards apply the exact same rule, rather than
// two slightly-different reimplementations.
function matchesSearch(symbol, exchange, query) {
  if (!query) return true
  const q = query.toLowerCase()
  return (
    (symbol && symbol.toLowerCase().includes(q)) ||
    (exchange && exchange.toLowerCase().includes(q))
  )
}

// Watchlist-table-only classification, over already-fetched data --
// no new fields, no backend call. "attention" mirrors exactly which
// instruments are currently represented in attentionItems (the same
// membership test AttentionSection's own `attentionIds` set already
// performs, just recomputed here from the same source array rather than
// threaded through as a prop); "baseline_pending" and "normal" read the
// existing change.has_baseline flag GET /watchlist already returns.
function matchesWatchlistFilter(instrument, filter, attentionInstrumentIds) {
  switch (filter) {
    case 'attention':
      return attentionInstrumentIds.has(instrument.instrument_id)
    case 'baseline_pending':
      return !instrument.change.has_baseline
    case 'normal':
      return instrument.change.has_baseline && !attentionInstrumentIds.has(instrument.instrument_id)
    case 'all':
    default:
      return true
  }
}

const WATCHLIST_FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'attention', label: 'Attention' },
  { value: 'normal', label: 'Normal' },
  { value: 'baseline_pending', label: 'Baseline Pending' },
]

// Mirrors app/models/instrument.py's Exchange enum exactly -- the only
// two values the backend will ever accept.
const EXCHANGE_OPTIONS = ['NSE', 'BSE']

// Same per-promise result shape Promise.allSettled itself produces, so
// the merge logic in loadAll() below can treat this identically to a
// real allSettled entry regardless of which code path produced it.
async function settleFetch(promise) {
  try {
    const value = await promise
    return { status: 'fulfilled', value }
  } catch (reason) {
    return { status: 'rejected', reason }
  }
}

export default function App() {
  const [instruments, setInstruments] = useState([])
  const [attentionItems, setAttentionItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  // Pure presentation filters -- neither ever touches instruments/
  // attentionItems themselves, never affects loadAll(), polling, or any
  // acknowledgement handler below. watchlistFilter only affects what
  // WatchlistTable renders, never AttentionSection.
  const [searchQuery, setSearchQuery] = useState('')
  const [watchlistFilter, setWatchlistFilter] = useState('all')

  // Per-instrument action state, not global -- an instrument can now be
  // acted on from two places (its watchlist row AND, if active, its
  // attention card), and marking one instrument must never disable or
  // report errors for an unrelated one.
  const [inFlightIds, setInFlightIds] = useState(() => new Set())
  const [actionErrors, setActionErrors] = useState({})
  const [savedMessages, setSavedMessages] = useState({})

  // Whole-watchlist "mark all as seen" state -- separate from the
  // per-instrument state above, since this is one action affecting many
  // instruments at once, not an action scoped to a single instrument id.
  const [markAllInFlight, setMarkAllInFlight] = useState(false)
  const [markAllError, setMarkAllError] = useState(null)
  const [markAllPartialMessage, setMarkAllPartialMessage] = useState(null)

  // Add Stock form state -- closed by default; opened by the "+ Add
  // stock" toggle. addSymbol/addExchange are the controlled form
  // fields, cleared only after a real, confirmed creation (never
  // optimistically, and never on failure, so the user can fix and
  // retry without retyping).
  const [addFormOpen, setAddFormOpen] = useState(false)
  const [addSymbol, setAddSymbol] = useState('')
  const [addExchange, setAddExchange] = useState('NSE')
  const [addInFlight, setAddInFlight] = useState(false)
  const [addError, setAddError] = useState(null)
  // Suggestion-dropdown visibility only -- the suggestion DATA itself
  // (which entries are shown) is derived fresh on every render from
  // addExchange/addSymbol via filterStockSuggestions, never duplicated
  // into its own state.
  const [addSuggestionsOpen, setAddSuggestionsOpen] = useState(false)

  // Both endpoints are fetched together, on the SAME timer -- still
  // exactly one polling interval, not two. They are independent reads,
  // though: Promise.allSettled (not Promise.all) is used deliberately,
  // so a failure in EITHER request never discards the other's
  // successfully-fetched data. GET /watchlist and GET /watchlist/attention
  // have genuinely different reliability profiles (the former makes a
  // live, documented-as-unreliable provider call per request; the
  // latter is a pure Mongo read), so treating "one failed" as "the
  // backend is unreachable" was itself untruthful -- it discarded real
  // data and blamed the whole backend for a single route's problem.
  //
  // hasResolvedIdentity guards a narrower concern: the backend's
  // anonymous owner cookie (see decisions.md's "Persistent anonymous
  // watchlist identity") is issued by whichever request reaches the
  // backend first when no cookie exists yet. Firing both requests
  // concurrently on the very first load of a fresh browser session
  // would let each independently generate its OWN new owner token --
  // whichever response's Set-Cookie the browser processes last would
  // silently win, orphaning the other's just-created Watchlist. This
  // frontend cannot check whether a cookie already exists before
  // fetching (it's httpOnly by design, and must stay that way), so the
  // first call in a given browser session is resolved alone, letting
  // its Set-Cookie land before the second request is even sent; every
  // call after that (this same initial round's second fetch, every
  // 60s poll, and every action's own follow-up loadAll()) safely
  // resumes the normal concurrent Promise.allSettled path below, since
  // a real cookie is already established by then. This costs one extra
  // sequential round-trip once per page load, never on every render or
  // poll, and adds no new request.
  const hasResolvedIdentity = useRef(false)

  // Truthfulness guards, independent of `error` (a single combined
  // banner string): `error` already tells the user a request failed,
  // but the WATCHLIST/ATTENTION SECTIONS THEMSELVES must not fall back
  // to their default empty-state copy ("Your watchlist is empty." /
  // "You're all caught up...") when that emptiness is only because the
  // very first fetch failed and no real data has EVER been confirmed
  // yet -- that would assert a positive fact (confirmed-empty,
  // confirmed-caught-up) the backend never actually returned. Once a
  // feed has genuinely succeeded at least once, a LATER transient
  // failure correctly keeps showing that last real data (or a real
  // confirmed-empty state) rather than flipping back to "unavailable"
  // -- these flags only ever matter before the first real success.
  const hasLoadedWatchlistOnce = useRef(false)
  const hasLoadedAttentionOnce = useRef(false)
  const [watchlistUnavailable, setWatchlistUnavailable] = useState(false)
  const [attentionUnavailable, setAttentionUnavailable] = useState(false)

  const loadAll = useCallback(async () => {
    let watchlistResult
    let attentionResult

    if (hasResolvedIdentity.current) {
      ;[watchlistResult, attentionResult] = await Promise.allSettled([
        fetchWatchlist(),
        fetchAttention(),
      ])
    } else {
      watchlistResult = await settleFetch(fetchWatchlist())
      attentionResult = await settleFetch(fetchAttention())
      hasResolvedIdentity.current = true
    }

    // Each dataset is only ever replaced by a REAL, successful
    // response -- a failed request leaves whatever was already
    // displayed in place rather than fabricating an empty/missing
    // state for data we simply couldn't refresh this cycle.
    if (watchlistResult.status === 'fulfilled') {
      setInstruments(watchlistResult.value)
      hasLoadedWatchlistOnce.current = true
      setWatchlistUnavailable(false)
    } else if (!hasLoadedWatchlistOnce.current) {
      // Never successfully loaded -- `instruments` is still its
      // pristine [] default, which must not be presented as a
      // confirmed empty watchlist.
      setWatchlistUnavailable(true)
    }
    if (attentionResult.status === 'fulfilled') {
      setAttentionItems(attentionResult.value)
      hasLoadedAttentionOnce.current = true
      setAttentionUnavailable(false)
    } else if (!hasLoadedAttentionOnce.current) {
      // Never successfully loaded -- `attentionItems` is still its
      // pristine [] default, which must not be presented as a
      // confirmed "you're all caught up."
      setAttentionUnavailable(true)
    }

    const watchlistFailed = watchlistResult.status === 'rejected'
    const attentionFailed = attentionResult.status === 'rejected'

    if (watchlistFailed && attentionFailed) {
      // Both independent reads failed on the same backend in the same
      // cycle -- this IS real evidence the backend/network itself is
      // unreachable, not just one route having a transient problem.
      setError('Could not reach the backend. Is it running?')
    } else if (watchlistFailed) {
      // Attention succeeded -- the backend is demonstrably reachable,
      // only the watchlist read failed this cycle. Never blame the
      // whole backend for one route's problem.
      setError('Could not load your watchlist right now — attention data is up to date.')
    } else if (attentionFailed) {
      setError('Could not load attention items right now — watchlist data is up to date.')
    } else {
      setError(null)
    }

    setLoading(false)
  }, [])

  useEffect(() => {
    loadAll()
    const interval = setInterval(loadAll, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [loadAll])

  const handleMarkAsSeen = useCallback(
    async (instrumentId) => {
      setInFlightIds((prev) => new Set(prev).add(instrumentId))
      setActionErrors((prev) => ({ ...prev, [instrumentId]: null }))

      try {
        const result = await markInstrumentAsSeen(instrumentId)
        setSavedMessages((prev) => ({ ...prev, [instrumentId]: result.message }))
        // Re-fetch both endpoints so the UI reflects real, current
        // server state -- the acknowledged instrument's row and its
        // (now superseded) attention item update from that response,
        // never from a locally-guessed optimistic update.
        await loadAll()
      } catch (err) {
        // Explicit failure: do NOT touch instruments/attentionItems --
        // an unacknowledged item must never look acknowledged.
        setActionErrors((prev) => ({ ...prev, [instrumentId]: err.message }))
      } finally {
        setInFlightIds((prev) => {
          const next = new Set(prev)
          next.delete(instrumentId)
          return next
        })
      }
    },
    [loadAll]
  )

  const handleMarkAllAsSeen = useCallback(async () => {
    setMarkAllInFlight(true)
    setMarkAllError(null)
    setMarkAllPartialMessage(null)

    try {
      const result = await markAllAsSeen()
      // Truthful partial-success reporting: the backend already tells us
      // exactly which instruments were skipped (no valid current data)
      // rather than silently acknowledging only some of them. Never
      // invent a specific reason beyond what the response provides.
      if (result.skipped && result.skipped.length > 0) {
        setMarkAllPartialMessage(
          'Some stocks couldn’t be updated because current market data was unavailable.'
        )
      }
      // Re-fetch both endpoints so the UI reflects real, current server
      // state -- exactly the acknowledged instruments (per `updated`)
      // and only those disappear from the attention list; never a local,
      // optimistic removal of items this response didn't confirm.
      await loadAll()
    } catch (err) {
      // Explicit failure: do NOT touch instruments/attentionItems -- an
      // unacknowledged watchlist must never look acknowledged.
      setMarkAllError(err.message)
    } finally {
      setMarkAllInFlight(false)
    }
  }, [loadAll])

  const handleAddInstrument = useCallback(
    async (e) => {
      e.preventDefault()

      const trimmedSymbol = addSymbol.trim()
      if (!trimmedSymbol) {
        setAddError('Enter a symbol.')
        return
      }

      // Only a curated suggestion actually selected (or exactly typed)
      // for the CURRENTLY selected exchange may be submitted -- this is
      // a pure client-side check against stockSuggestions.js, never a
      // network call, and never a replacement for the backend's own
      // provider-resolvability check below. Using the matched entry's
      // own canonical-cased symbol (not the raw typed text) means
      // casing is always consistent regardless of how the user typed it.
      const matchedSuggestion = findStockSuggestion(addExchange, trimmedSymbol)
      if (!matchedSuggestion) {
        setAddError('Select a stock from the suggestions.')
        return
      }

      setAddInFlight(true)
      setAddError(null)

      try {
        // The backend does the real validation/normalization (via the
        // existing Instrument model) and the provider-resolvability
        // check -- this call does not duplicate that logic, it just
        // surfaces whatever truthful error comes back.
        await addInstrument(matchedSuggestion.symbol, addExchange)
        // Clear the form and re-fetch real server state only after a
        // confirmed success -- never optimistically add to instruments/
        // attentionItems, and never touch searchQuery/watchlistFilter.
        setAddSymbol('')
        setAddExchange('NSE')
        setAddFormOpen(false)
        setAddSuggestionsOpen(false)
        await loadAll()
      } catch (err) {
        setAddError(err.message)
      } finally {
        setAddInFlight(false)
      }
    },
    [addSymbol, addExchange, loadAll]
  )

  // Changing the exchange replaces the suggestion list; a currently
  // typed/selected symbol is cleared only if it's NOT also valid for
  // the newly selected exchange (a symbol that happens to be curated
  // for both is left alone, per the requirement to clear only an
  // INCOMPATIBLE selection).
  const handleAddExchangeChange = useCallback((e) => {
    const nextExchange = e.target.value
    setAddExchange(nextExchange)
    setAddSymbol((prevSymbol) => {
      const trimmed = prevSymbol.trim()
      if (trimmed && !findStockSuggestion(nextExchange, trimmed)) {
        return ''
      }
      return prevSymbol
    })
  }, [])

  // Small, simple filter -- the dataset is 5 instruments, not large
  // enough to warrant useMemo. instruments itself is never reassigned or
  // mutated here; this is a new derived array passed only to
  // WatchlistTable below. attentionInstrumentIds is recomputed from the
  // FULL attentionItems array (never a filtered one), so "Attention"/
  // "Normal" classification is always correct regardless of search.
  const attentionInstrumentIds = new Set(attentionItems.map((item) => item.instrument_id))
  const filteredInstruments = instruments.filter(
    (inst) =>
      matchesSearch(inst.symbol, inst.exchange, searchQuery) &&
      matchesWatchlistFilter(inst, watchlistFilter, attentionInstrumentIds)
  )

  // Curated suggestions for the CURRENTLY selected Add Stock exchange,
  // filtered by whatever's typed so far -- recomputed on every render
  // (the underlying list is a small, static, in-memory array; no reason
  // to memoize it separately from addExchange/addSymbol themselves).
  const addStockSuggestions = filterStockSuggestions(addExchange, addSymbol)

  // TEMPORARY DEMO MODE — screenshot use only (see flag above). Fed only
  // into AttentionSection's props below, never into WatchlistTable or
  // any real state — the demo cards appear in "Since You Last Checked"
  // only, not in the watchlist table itself.
  const displayedAttentionItems = DEMO_MODE
    ? [...DEMO_ATTENTION_ITEMS, ...attentionItems]
    : attentionItems
  const displayedAttentionInstruments = DEMO_MODE
    ? [...DEMO_WATCHLIST_ENTRIES, ...instruments]
    : instruments

  return (
    <div className="app">
      <header className="app-header">
        <h1>Watchly</h1>
        <p className="app-subtitle">See what meaningfully changed while you were away.</p>
      </header>

      {error && <div className="error-banner">{error}</div>}

      {loading ? (
        <p className="loading">Loading watchlist...</p>
      ) : (
        <>
          <AttentionSection
            items={displayedAttentionItems}
            instruments={displayedAttentionInstruments}
            onMarkAsSeen={handleMarkAsSeen}
            inFlightIds={inFlightIds}
            actionErrors={actionErrors}
            onMarkAllAsSeen={handleMarkAllAsSeen}
            markAllInFlight={markAllInFlight}
            markAllError={markAllError}
            markAllPartialMessage={markAllPartialMessage}
            searchQuery={searchQuery}
            matchesSearch={matchesSearch}
            attentionUnavailable={attentionUnavailable}
          />

          <section className="watchlist-section">
            <div className="watchlist-header">
              <div className="watchlist-header-text">
                <h2 className="section-title">Your Watchlist</h2>
                <p className="watchlist-subtitle">
                  The complete list, including instruments with no meaningful change right now.
                </p>
              </div>
              {/* Search belongs to this section, not the page header --
                  it's a control over the watchlist, not part of the
                  product's identity/positioning copy above. Same
                  searchQuery state, same matchesSearch function, same
                  input -- only its position in the tree moved. */}
              <div className="watchlist-controls">
                <div className="watchlist-filter-group" role="group" aria-label="Filter your watchlist">
                  {WATCHLIST_FILTERS.map((filter) => (
                    <button
                      key={filter.value}
                      type="button"
                      className={
                        'watchlist-filter-button' +
                        (watchlistFilter === filter.value ? ' watchlist-filter-button-active' : '')
                      }
                      aria-pressed={watchlistFilter === filter.value}
                      onClick={() => setWatchlistFilter(filter.value)}
                    >
                      {filter.label}
                    </button>
                  ))}
                </div>
                <input
                  type="search"
                  className="search-input"
                  placeholder="Search your watchlist..."
                  aria-label="Search your watchlist"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                />
                <div className="add-stock-control">
                  {addFormOpen ? (
                    <form className="add-stock-form" onSubmit={handleAddInstrument}>
                      {/* Wrapper is the position:relative anchor for the
                          suggestions dropdown -- everything else in this
                          form (select/submit/cancel) is unaffected. */}
                      <div className="add-stock-symbol-field">
                        <input
                          type="text"
                          className="add-stock-symbol-input"
                          placeholder="Symbol"
                          aria-label="New instrument symbol"
                          value={addSymbol}
                          onChange={(e) => setAddSymbol(e.target.value)}
                          onFocus={() => setAddSuggestionsOpen(true)}
                          onBlur={() => {
                            // Deferred so a suggestion's onClick (fired
                            // on mousedown-prevented buttons below)
                            // still registers before the list unmounts.
                            window.setTimeout(() => setAddSuggestionsOpen(false), 100)
                          }}
                          disabled={addInFlight}
                          autoComplete="off"
                        />
                        {addSuggestionsOpen && addStockSuggestions.length > 0 && (
                          <ul className="add-stock-suggestions">
                            {addStockSuggestions.map((suggestion) => (
                              <li key={suggestion.symbol}>
                                <button
                                  type="button"
                                  className="add-stock-suggestion"
                                  onMouseDown={(e) => e.preventDefault()}
                                  onClick={() => {
                                    setAddSymbol(suggestion.symbol)
                                    setAddSuggestionsOpen(false)
                                  }}
                                >
                                  <span className="add-stock-suggestion-symbol">
                                    {suggestion.symbol}
                                  </span>
                                  <span className="add-stock-suggestion-name">
                                    {suggestion.name}
                                  </span>
                                </button>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                      <select
                        className="add-stock-exchange-select"
                        aria-label="New instrument exchange"
                        value={addExchange}
                        onChange={handleAddExchangeChange}
                        disabled={addInFlight}
                      >
                        {EXCHANGE_OPTIONS.map((exchange) => (
                          <option key={exchange} value={exchange}>
                            {exchange}
                          </option>
                        ))}
                      </select>
                      <button type="submit" className="add-stock-submit" disabled={addInFlight}>
                        {addInFlight ? 'Adding…' : 'Add'}
                      </button>
                      <button
                        type="button"
                        className="add-stock-cancel"
                        disabled={addInFlight}
                        onClick={() => {
                          setAddFormOpen(false)
                          setAddError(null)
                          setAddSuggestionsOpen(false)
                        }}
                      >
                        Cancel
                      </button>
                      {addError && <div className="action-error">{addError}</div>}
                    </form>
                  ) : (
                    <button
                      type="button"
                      className="add-stock-toggle"
                      onClick={() => setAddFormOpen(true)}
                    >
                      + Add stock
                    </button>
                  )}
                </div>
              </div>
            </div>
            <WatchlistTable
              instruments={filteredInstruments}
              totalCount={instruments.length}
              unavailable={watchlistUnavailable}
              onMarkAsSeen={handleMarkAsSeen}
              inFlightIds={inFlightIds}
              actionErrors={actionErrors}
              savedMessages={savedMessages}
            />
          </section>
        </>
      )}

      <footer className="app-footer">
        <p>
          Data via Yahoo Finance (unofficial). Not guaranteed real-time.
          Refreshes every 60 seconds.
        </p>
      </footer>
    </div>
  )
}
