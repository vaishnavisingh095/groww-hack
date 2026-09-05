// Frontend-only curated stock suggestions for the Add Stock autocomplete.
// Not fetched from the backend, not validated against the provider here
// -- selecting one only pre-fills the existing Add Stock form fields;
// the backend's own provider-resolvability check (already in place,
// unchanged by this file) remains the real source of truth for whether
// a symbol is actually addable. Symbols use the same plain, unsuffixed
// ticker convention app/services/watchlist_service.py's
// yfinance_ticker_for already expects (it appends .NS or .BO itself),
// matching the existing 5 seed instruments' own symbol style.
//
// Exactly two exchanges, matching app/models/instrument.py's Exchange
// enum ("NSE" / "BSE") exactly -- these keys are looked up directly by
// the currently-selected addExchange value, never transformed.
export const STOCK_SUGGESTIONS = {
  NSE: [
    { symbol: 'RELIANCE', name: 'Reliance Industries Ltd' },
    { symbol: 'TCS', name: 'Tata Consultancy Services Ltd' },
    { symbol: 'HDFCBANK', name: 'HDFC Bank Ltd' },
    { symbol: 'ICICIBANK', name: 'ICICI Bank Ltd' },
    { symbol: 'INFY', name: 'Infosys Ltd' },
    { symbol: 'ITC', name: 'ITC Ltd' },
    { symbol: 'SBIN', name: 'State Bank of India' },
    { symbol: 'BHARTIARTL', name: 'Bharti Airtel Ltd' },
    { symbol: 'AXISBANK', name: 'Axis Bank Ltd' },
    { symbol: 'KOTAKBANK', name: 'Kotak Mahindra Bank Ltd' },
    { symbol: 'LT', name: 'Larsen & Toubro Ltd' },
    { symbol: 'M&M', name: 'Mahindra & Mahindra Ltd' },
    { symbol: 'MARUTI', name: 'Maruti Suzuki India Ltd' },
    { symbol: 'TITAN', name: 'Titan Company Ltd' },
    { symbol: 'SUNPHARMA', name: 'Sun Pharmaceutical Industries Ltd' },
    { symbol: 'HINDUNILVR', name: 'Hindustan Unilever Ltd' },
    { symbol: 'HCLTECH', name: 'HCL Technologies Ltd' },
    { symbol: 'TECHM', name: 'Tech Mahindra Ltd' },
    { symbol: 'TATACONSUM', name: 'Tata Consumer Products Ltd' },
    { symbol: 'TATASTEEL', name: 'Tata Steel Ltd' },
    { symbol: 'HINDALCO', name: 'Hindalco Industries Ltd' },
    { symbol: 'ADANIENT', name: 'Adani Enterprises Ltd' },
    { symbol: 'ADANIPORTS', name: 'Adani Ports and Special Economic Zone Ltd' },
    { symbol: 'NTPC', name: 'NTPC Ltd' },
    { symbol: 'POWERGRID', name: 'Power Grid Corporation of India Ltd' },
    { symbol: 'ONGC', name: 'Oil & Natural Gas Corporation Ltd' },
    { symbol: 'COALINDIA', name: 'Coal India Ltd' },
    { symbol: 'BPCL', name: 'Bharat Petroleum Corporation Ltd' },
    { symbol: 'BAJFINANCE', name: 'Bajaj Finance Ltd' },
    { symbol: 'BAJAJFINSV', name: 'Bajaj Finserv Ltd' },
  ],
  BSE: [
    { symbol: 'RELIANCE', name: 'Reliance Industries Ltd' },
    { symbol: 'TCS', name: 'Tata Consultancy Services Ltd' },
    { symbol: 'HDFCBANK', name: 'HDFC Bank Ltd' },
    { symbol: 'ICICIBANK', name: 'ICICI Bank Ltd' },
    { symbol: 'INFY', name: 'Infosys Ltd' },
    { symbol: 'ITC', name: 'ITC Ltd' },
    { symbol: 'SBIN', name: 'State Bank of India' },
    { symbol: 'AXISBANK', name: 'Axis Bank Ltd' },
    { symbol: 'KOTAKBANK', name: 'Kotak Mahindra Bank Ltd' },
    { symbol: 'LT', name: 'Larsen & Toubro Ltd' },
    { symbol: 'M&M', name: 'Mahindra & Mahindra Ltd' },
    { symbol: 'MARUTI', name: 'Maruti Suzuki India Ltd' },
    { symbol: 'TITAN', name: 'Titan Company Ltd' },
    { symbol: 'ASIANPAINT', name: 'Asian Paints Ltd' },
    { symbol: 'NESTLEIND', name: 'Nestlé India Ltd' },
    { symbol: 'HINDUNILVR', name: 'Hindustan Unilever Ltd' },
    { symbol: 'SUNPHARMA', name: 'Sun Pharmaceutical Industries Ltd' },
    { symbol: 'HCLTECH', name: 'HCL Technologies Ltd' },
    { symbol: 'TECHM', name: 'Tech Mahindra Ltd' },
    { symbol: 'TATACONSUM', name: 'Tata Consumer Products Ltd' },
    { symbol: 'TATASTEEL', name: 'Tata Steel Ltd' },
    { symbol: 'HINDALCO', name: 'Hindalco Industries Ltd' },
    { symbol: 'ADANIENT', name: 'Adani Enterprises Ltd' },
    { symbol: 'ADANIPORTS', name: 'Adani Ports and Special Economic Zone Ltd' },
    { symbol: 'APOLLOHOSP', name: 'Apollo Hospitals Enterprise Ltd' },
    { symbol: 'BRITANNIA', name: 'Britannia Industries Ltd' },
    { symbol: 'GRASIM', name: 'Grasim Industries Ltd' },
    { symbol: 'NTPC', name: 'NTPC Ltd' },
    { symbol: 'POWERGRID', name: 'Power Grid Corporation of India Ltd' },
    { symbol: 'BAJFINANCE', name: 'Bajaj Finance Ltd' },
  ],
}

// Case-insensitive match against symbol OR company name -- an empty
// query returns the full curated list for that exchange (used on
// focus/click, before the user has typed anything).
export function filterStockSuggestions(exchange, query) {
  const list = STOCK_SUGGESTIONS[exchange] || []
  const q = query.trim().toLowerCase()
  if (!q) return list
  return list.filter(
    (s) => s.symbol.toLowerCase().includes(q) || s.name.toLowerCase().includes(q)
  )
}

// Exact (case-insensitive) symbol match against the curated list for
// one exchange -- the only thing that makes a typed/selected value
// eligible for submission. Returns the canonical entry (correct casing)
// or undefined.
export function findStockSuggestion(exchange, symbolText) {
  const list = STOCK_SUGGESTIONS[exchange] || []
  const target = symbolText.trim().toLowerCase()
  if (!target) return undefined
  return list.find((s) => s.symbol.toLowerCase() === target)
}
