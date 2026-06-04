# Full-Market Refresh

The decision-grade app needs one daily workflow that refreshes every source
cluster before the UI ranks opportunities. The workflow is intentionally
sequential because later steps depend on earlier evidence and market-data rows.

## Command

```bash
cd /Users/joehu/proj/market
git pull --rebase origin main
uv run python -m investment_panel.jobs.full_market_refresh --config config.yaml
```

Run this on `mini1.local` from the canonical checkout. Do not schedule the full
refresh from stale worktrees such as
`/Users/joehu/proj/market-source-modularization`; their status files can look
fresh while the primary app checkout remains stale.

For LAN access after the refresh:

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
npx vite --host 0.0.0.0
```

Verify the API from another local device:

```text
http://192.168.50.197:8000/api/status
```

## Step Order

1. `update_arco_data`: import the latest Arco brief evidence from
   `/Volumes/agent/brain/raw/sources/arco`.
2. `daily_screen`: rebuild the configured and evidence-derived universe, daily
   prices, fundamentals, technicals, candidates, and base analyses.
3. `update_free_sources`: refresh TradingView/OpenCLI and yfinance rows for
   quotes, screeners, options, news, TradingView symbol/watchlist/alert/chart
   metadata, estimates, earnings, ETF premiums, SEPA, liquidity, correlations,
   options payoff scenarios, earnings setup scores, and deterministic
   DCF/relative/blended valuations.
   Decision-model rebuilds now sync existing rows into canonical
   `source_registry`, `source_runs`, `source_items`, and
   `ticker_source_signals`, then promote source-discovered tickers to
   refreshable instruments with explicit market-context blockers.
4. `options_radar`: materialize the deterministic 10x options radar from the
   refreshed option/stock rows: point-in-time snapshots, 10x math, feature
   scores, candidate events, shadow-trade marks, attribution, state
   transitions, missed-winner events, cohort results, and agent work queues.
   This is also exposed as the standalone `refresh_options_radar` refresh job
   for local reruns after option-source changes.
5. `update_broker_sources`: refresh read-only broker account, position, and
   recommendation context.
6. `update_disclosures`: refresh public disclosures, official House filings,
   configured 13F trackers, disclosure symbol prices, and trader replicas.
   Daily runs default to metadata/light holdings; pass `--fetch-holdings` when
   a heavier 13F holdings refresh is intended.
7. `update_event_calendar`: refresh macro, earnings, filing, and watchlist
   events.
8. `snapshot_database`: copy the local DuckDB to the NAS snapshot archive.

The orchestrator writes `/Volumes/agent/data-sources/status/mini-market-full-refresh.json`.
Each underlying job still writes its own status file.

## Agent Handoff

The radar exposes open hypothesis work through `GET /api/agent-thesis-requests`
and `GET /api/agent-postmortem-requests`. Agents fulfill those requests by
posting structured JSON to the local-only endpoints:

- `POST /api/agent-thesis`: stores an `agent_thesis`, attaches it to matching
  candidate events, and immediately runs deterministic thesis validation.
- `POST /api/agent-postmortems`: stores an `agent_postmortem`, materializes any
  proposed strategy mutation, and immediately runs deterministic backtest and
  forward-test gates.

These endpoints are handoff boundaries, not trading commands. Agent payloads are
hypotheses and proposals only; deterministic code still owns option math,
candidate state, validation, backtests, forward tests, and human-approval gates.

## Freshness Contracts

- Intraday quotes, options, and news are stale after `4` market hours.
- Daily prices, technicals, SEPA, liquidity, and correlation rows are stale
  after `1` trading day.
- Fundamentals, 13F rows, and disclosure rows are stale by filing cadence, not
  daily market time.
- Arco thesis evidence is stale after `7` days unless refreshed or reinforced.
- Documentation rows are documentation. They must not count as healthy provider
  runs.

## Daily Acceptance Checks

After a successful refresh:

- `/api/source-freshness` shows no stale source as healthy.
- `/api/sources` lists enabled source families with latest run, item counts,
  ticker counts, and any failure/detail state.
- `/api/ticker-source-signals` shows source-discovered ticker evidence; rows
  missing quote/daily analysis are marked `needs_market_context`.
- `/api/decision-queue` has nonempty `Act`, `Research`, `Watch`, `Reject`, and
  `Stale` buckets when seeded data supports them.
- Top `Act` rows have current `as_of` values, nonzero source/evidence counts,
  no stale-data blocking gates, and explicit invalidation.
- Source-thin or stale opportunities are not silently promoted into the top
  ranked queue.
- `/api/tickers/{symbol}/decision-snapshot` explains action grade, source
  cluster, freshness, decision basis, blocking gates, portfolio impact, and
  invalidation.
- `/api/panel-snapshot?scope=options-radar` includes nonempty radar tables when
  option chains exist: `option_snapshot`, `option_features`, `stock_features`,
  `candidate_event`, `candidate_event_mark`, `candidate_event_attribution`,
  `shadow_trade`, `radar_state_transition`, `missed_winner_event`, and
  strategy validation/proposal tables.

## Suggested Daily Schedule

Use the existing automation runner or launchd to run once before the local
investment review window, for example:

```bash
cd /Users/joehu/proj/market
uv run python -m investment_panel.jobs.full_market_refresh --config config.yaml
```

Keep the separate disclosure automation if it already exists; this full refresh
is the missing broad-market workflow that ensures the decision desk has current
market, evidence, event, analysis, and snapshot state.

Cross-machine freshness is checked from Arco:

```bash
cd /Users/joehu/proj/arco
node bin/arco.mjs status-gates
```

The Market gates are `mini-market-full-refresh.json` and
`mini-market-db-snapshot.json`, both fresh under 24 hours.
