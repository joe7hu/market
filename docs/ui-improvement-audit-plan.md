# Market UI Improvement Audit And Plan

## Shipped In This Pass

- Opportunities now has a top ranked ticker, wired filters, a ranked table, and separate source panels for technical setups, liquidity, valuation, theses/memos, trader filings, and news/catalysts.
- The backend now exposes typed read models for `opportunities_ranked`, `opportunity_sources`, `technicals`, and `research_packets`, so the UI is no longer the only place defining the main decision surfaces.
- Research now has a ticker-level deep analysis workbench with score, confidence, decision, why-now, next action, invalidation, component breakdown, and ticker-specific evidence fallbacks.
- Trader Filings now render as one card per investor with aggregate value, filing dates, top ticker, and top holdings.
- Ticker detail now embeds a TradingView daily chart configured with moving average, RSI, and MACD studies, plus a local technical summary from stored feature rows.
- User-facing source copy no longer exposes implementation noise such as "Live DuckDB".
- Regression coverage now checks the new API routes and read models.

## Remaining Follow-Ups

- Filter state is local to the page. Persisted saved views need a user settings table or local browser storage contract.
- The frontend model builders still live in `App.tsx`; split them into a testable module if they grow further.
- TradingView is an embedded public widget. The local technical read model provides a fallback summary, but the chart itself still depends on TradingView availability.
- Research packets are deterministic summaries. A future LLM-assisted memo path can sit behind the same packet contract once source quality is high enough.

## Recommended Next Implementation Steps

1. Add saved filter views once Joe has used the current screens enough to know which views matter.
2. Split frontend model builders from `App.tsx` when the next UI feature touches the same model layer.
3. Add richer primary-source research packet sections as Arco/Birdclaw ticker evidence improves.
