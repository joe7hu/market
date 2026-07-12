# Page Data Audit — 2026-07-12

## Outcome

All 13 navigation routes were exercised against the live PostgreSQL runtime after the DuckDB cutover. The four systemically empty investment surfaces (Feed, Sources, Market, and Options) now have current data, and the partially populated Today and Calendar surfaces now include source changes and earnings.

## Live contract reconciliation

| Route | Before | After |
| --- | ---: | ---: |
| Today | 4 brief rows; 0 source changes | 46 brief rows; 12 source changes |
| Feed | 0 cards | 48 source cards |
| Watchlist | 13 symbols after load | 13 symbols, 221 option signal groups, 1,177 valuation rows |
| Portfolio | 2 holdings | 2 holdings, 2 risk cards, 2 review actions |
| Options | stale cached empty payload | 1,483 decisions, 268 summaries, 8 strategies |
| Theses | 13 monitor rows | 13 monitor rows plus restored source context |
| Superinvestors | 1,319 disclosures after load | 1,319 disclosures, 78 ownership rows |
| Calendar | 34 macro catalysts; 0 earnings | 713 catalysts, 679 earnings |
| Sources | 0 ranked tickers or consensus | 756 ranked tickers, 18 consensus groups, 500 current signal rows |
| Market | 0 models | 641 assets, 4 drivers, 5 full valuation series |
| Agent | control plane only | control plane plus 52 theses and 16 postmortems in the analytical store |
| Health | generic/duplicate unknown rows | 103 uniquely identified source categories; no React key errors |
| Settings | configured | configured; unchanged |

Every final route returned PostgreSQL as its API source and produced no browser console errors.

## Storage policy applied

- Retained all compact durable facts: 24,522 content items, 18,752 content-to-instrument links, 18,726 source signals, 1,319 disclosures, 713 events, 1,394 fundamental/estimate/valuation observations, 370,945 daily bars, portfolio state, watchlist state, theses, strategies, and historical agent artifacts.
- Compacted 39,973 market-valuation points into five normalized observations containing their complete historical series.
- Retained only the newest option chain per symbol: 9,202 quotes in 268 snapshots instead of copying 851,084 legacy option-chain rows.
- Did not copy 7,467,487 legacy candidate events, 1,279,590 legacy option features, 1,345,371 legacy option snapshots, 6,224 alerts, or 6,387 shadow trades. These are recomputed analytical outputs, not raw facts.
- Rebuilt Market, Options, and Today publications from normalized PostgreSQL facts after import.
- Left the 32 GB DuckDB unchanged and read-only.

## Verification

- `make check`: passed (architecture guards and Ruff).
- `make coverage`: 598 passed, 2 skipped, 84.24% coverage (80% gate).
- `node_modules/.bin/tsc --noEmit`: passed.
- Live `/api/status`: ready, PostgreSQL, schema revision `20260711_0004`.
- Browser audit: all 13 routes loaded with zero console errors after the final Health identity and Today source-change fixes.

## Migration authority

The importer is idempotent. It preserves original source identities, separates raw content links from analytical source signals, normalizes non-finite legacy JSON values to `null`, and rebuilds publications after reconciliation. The DuckDB is now a read-only rollback/archive source rather than an application dependency.
