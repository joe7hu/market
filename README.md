# Market: Evidence-backed trading workstation

A personal investment research and paper-trading assistant for equities, options,
and crypto. Connect market conditions and new evidence to explicit trade
assessments, portfolio risk, prospective experiments, and reconciled outcomes.
The goal is better risk-adjusted decisions—not more trades or guaranteed profits.

Start with the [product goals](docs/product-goals.md),
[architecture and owner map](ARCHITECTURE.md), and
[paper/opportunities audit and verification guide](docs/paper-opportunities-audit-20260920.md),
and [decision-service root causes and acceptance](docs/decision-service-integrity.md).

## Stack

- Python + PostgreSQL 18 for authoritative data, analysis, jobs, and user state.
- FastAPI for the local API.
- React + TanStack Table + Vite for the web app.
- Arco is the upstream weak-signal/evidence layer.
- Birdclaw remains the raw X/Twitter ingestion layer.

## Run

```bash
uv sync --extra test
npm install --prefix frontend
uv run market-db-migrate
uv run market-full-refresh --config config.yaml
npm --prefix frontend run build
uv run uvicorn investment_panel.api.main:app --host 0.0.0.0 --port 8010
npm --prefix frontend run dev
```

Open:

```text
http://127.0.0.1:5173/today
```

From another device on the local network, use:

```text
http://mini1.local:5173/today
```

Checkout development serves the built frontend from `frontend/dist`. An
installed Python wheel is API-only unless deployment sets
`MARKET_FRONTEND_DIST` to a built frontend directory and
`MARKET_MIGRATIONS_ROOT` to the directory containing `alembic.ini` and
`migrations/`.

For frontend-only development:

```bash
npm --prefix frontend run dev
npm --prefix frontend run api
```

## Jobs

```bash
uv run market-full-refresh --config config.yaml
uv run market-update-market-data --config config.yaml
uv run market-update-content-sources --config config.yaml
uv run market-update-arco-data --config config.yaml
uv run market-update-disclosures --config config.yaml
uv run market-update-event-calendar --config config.yaml
uv run market-update-robinhood-options --config config.yaml
uv run market-refresh-options-radar --config config.yaml
uv run market-premarket-options-intelligence --config config.yaml
uv run market-update-broker-sources --config config.yaml
uv run market-snapshot-database --config config.yaml
```

Disclosure refresh is incremental: configured CSV files are content-addressed,
House PDFs are skipped after their document ID is stored, and current 13F
submission/index/information-table payloads are archived once.

New trader onboarding and the normalized public-disclosure CSV contract are
documented in [docs/trader-disclosure-pipeline.md](docs/trader-disclosure-pipeline.md).

The broad daily refresh workflow is documented in
[docs/full-market-refresh.md](docs/full-market-refresh.md). It coordinates
Arco, market/content sources, deterministic options decisions and outcomes,
broker context, disclosures, event calendar, publications, retention, and a
verified PostgreSQL custom-format backup.
This workflow should run from the canonical `mini1.local` checkout at
`/Users/joehu/proj/market`, not from temporary or topic-specific worktrees.

## API

- `GET /api/status`
- `GET /api/panel-contract`
- `GET /api/panel-snapshot?scope=today`
- `GET /api/tickers/{symbol}`
- `GET /api/quotes?symbols=QQQ`
- `GET /api/options/decision-brief`
- `GET /api/options/workspace`
- `GET /api/options/tickets/{decision_id}`
- `GET /api/options/history/snapshots`
- `GET /api/decision-inbox`
- `GET /api/agent`
- `POST /api/agent-thesis`
- `POST /api/agent-postmortems`
- `GET /api/source-catalog`
- `GET /api/source-ingestion-audit`
- `GET /api/sources/{source_id}`
- `GET /api/portfolio/transactions`
- `POST /api/portfolio/transactions`
- `PUT /api/theses/{symbol}`
- `GET /api/tickers/{symbol}/decision-snapshot`
- `GET /api/settings`

## Data Sources

Verified source notes are in [docs/data-sources.md](docs/data-sources.md).

Trader portfolios are modeled from primary public disclosure records. Market
does not ingest third-party tracker pages as source data; comparable tracker
products are useful only as UI/product references.

The app uses online market data by default. Price provider failures are reported
in job output and source health rather than filled with synthetic rows.

IBKR is available as a read-only broker source through IB Gateway/TWS API. The
default local paper Gateway config is `127.0.0.1:4002`; it syncs account
summary, positions, orders/fills, and quote snapshots into broker read models.
IB market-data rows record whether returned quotes are live or delayed, because
paper access still depends on live-account subscriptions and market-data
sharing.

Market prices are normalized into compact daily bars and one latest quote per
symbol. OpenCLI news/X and configured RSS/Substack sources retain one compressed
provider payload manifest plus query-critical content facts. TradingView
personal-state replication and ETF-premium enrichment are outside the current
read-model contract.

Market codifies high-value finance-skills workflows as deterministic backend
read models where possible: options payoff scenarios, earnings setup scoring,
estimate revision analysis, exchange-qualified TradingView identity,
liquidity/correlation/SEPA,
and DCF/relative/blended valuation rows. LLMs should only be used for
unstructured interpretation, memo prose, or parsing a user-submitted options
screenshot/free-form strategy into structured legs.

The web app defaults to `/today`, with published BUY SETUP / EXIT SETUP / HOLD /
WAIT / AVOID conditions, their original quote/feature clocks, and a separate
qualified capital decision. A required input or publication failure is a named
service incident in System health, not a vague review task or a healthy empty
screen. Measured trend conditions are not calibrated forecasts or permission
to submit an order. Primary navigation is Today, Market, Opportunities, Real portfolio,
Paper trading, and Research. Supporting source health, settings, ticker detail,
calendar and filing views remain drill-downs. Real portfolio data, funded paper
orders, and research observations have distinct accounting authorities.
Paper trading is a first-class workspace; provider, funding, risk, and execution
gates remain explicit. Market does not submit live brokerage orders. The valuation endpoint is a low-confidence
proxy only; it drops rows with implausible fundamentals and reports upside in
percentage points.

## Current Limitation

The app can generate deterministic research packets and memos from stored
evidence. Historical trader philosophy profiles are intentionally manual:
curating primary writings, interviews, letters, and books is the one area that
needs Joe's input before the trader-twin feature should be treated as serious.

## Shared Source Archive

Market keeps its authoritative PostgreSQL database on `mini1.local`. Provider
payloads, autonomous job status, and verified custom-format PostgreSQL backups
are published to the NAS source archive configured under `nas:` in
`config.yaml`; the NAS is not mounted as a live database filesystem.

Default shared paths:

```text
/Volumes/agent/data-sources/market-mini/
/Volumes/agent/data-sources/market-mini/postgres-backups/
/Volumes/agent/data-sources/status/
```

The broad refresh and snapshot jobs write `mini-market-full-refresh.json` and
`mini-market-db-snapshot.json`. Source-level execution history lives in
PostgreSQL `ingest.run`; application job history lives in `ops.job_run`. The
snapshot job streams `pg_dump --format=custom`, verifies all six application
schemas, and records a checksum manifest beside the dump.

## Architecture and verification

Start navigation with the compact owner map:

```bash
uv run python scripts/architecture_inventory.py --area api
uv run python scripts/architecture_inventory.py --area options
```

Focused backend gates are available through Make:

```bash
make typecheck  # TypeScript only
make test-unit
make test-api
make test-options
make test-postgres
make test-all
make check
```

The retained HTTP paths and methods are checked against
[docs/api-route-manifest.json](docs/api-route-manifest.json). Generated
schemas, frontend bundles, and full logs are verification outputs. Do not use
them as normal navigation material; inspect the owning interface and run the
focused check instead.

Database schema changes: [maintenance guide](docs/database-maintenance.md).
Large PostgreSQL histories: [verified NAS migration and storage model](docs/storage-efficiency-migration.md).

## Closed markets, paper trading, and diagnostics

A previous-session price can remain a valid **valuation**, with its original
observation and availability times visible. This does not authorize a fill.
News, event and macro-source refreshes keep their own elapsed-time/provider
cadences; rebuilding a publication does not make old source facts new.

Paper fills require causal, fresh regular-session quotes. A repeated quote
cannot repeatedly supply the same order's partial fills. Expired trade terms
are removed from actionable presentation while their research record is kept.
An empty or waiting paper book is not itself a fault—and is never permission
to relax risk or quote requirements.

Run the existing read-only deployment check (no funding, refresh or order writes):

```bash
uv run python scripts/verify_workstation.py \
  --base-url http://127.0.0.1:8010 \
  --expected-commit "$(git rev-parse HEAD)" \
  --output /tmp/market-verification.json
```

Experimental WebMCP diagnostics are **off by default**. On a browser/origin that
supports `document.modelContext`, enable the five read-only inspection tools:

```bash
VITE_MARKET_WEBMCP=true npm --prefix frontend run dev
# For the bundled frontend, the flag must be set when building:
VITE_MARKET_WEBMCP=true npm --prefix frontend run build
```

This exposes private app evidence to your trusted browser agent, not to a new
public service. No tool can place orders, fund accounts, run jobs or change
settings. Unsupported browsers keep the normal app. See the
[verification guide](docs/paper-opportunities-audit-20260920.md#webmcp) for browser
requirements, limitations and acceptance checks.

### Decision loop and paper experiment traceability

See [the September 21 repair and deployment guide](docs/decision-loop-repair.md)
for canonical capital decisions, monitored-scope diagnostics, quote-backed research
experiment P&L, and independent forecast settlement. Apply migration
`20260921_0033` before starting this revision.

## Hot and cold storage

PostgreSQL serves current state and point-in-time decision evidence; verified NAS
row packs hold eligible raw/derived history. Hourly bounded retention, lossless
decision-input sharing, restore contracts and the Mac deployment sequence are in
[Hot-storage lifecycle](docs/hot-storage-lifecycle.md). Logical cleanup does not
by itself shrink database files.
