# Market: Personal Investment Panel

Local research command center for public equities, crypto, Arco/Birdclaw thesis
flow, thesis tracking, and evidence-backed trading signals.

## Stack

- Python + DuckDB for data, scoring, jobs, and research packets.
- FastAPI for the local API.
- React + TanStack Table + Vite for the web app.
- Arco is the upstream weak-signal/evidence layer.
- Birdclaw remains the raw X/Twitter ingestion layer.

## Run

```bash
uv sync --extra test
npm install
uv run python -m investment_panel.jobs.daily_screen --config config.yaml
npm run build
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

For frontend-only development:

```bash
npm run dev
npm run api
```

## Jobs

```bash
uv run python -m investment_panel.jobs.daily_screen --config config.yaml
uv run python -m investment_panel.jobs.update_equity_data --config config.yaml
uv run python -m investment_panel.jobs.update_crypto_data --config config.yaml
uv run python -m investment_panel.jobs.update_arco_data --config config.yaml
uv run python -m investment_panel.jobs.update_disclosures --config config.yaml --online-check
uv run python -m investment_panel.jobs.update_free_sources --config config.yaml
uv run python -m investment_panel.jobs.snapshot_database --config config.yaml
uv run python -m investment_panel.jobs.research_candidate TSLA --config config.yaml
uv run python -m investment_panel.jobs.weekly_portfolio_review --config config.yaml
```

Disclosure ingestion should also run daily. The scheduled command is:

```bash
uv run python -m investment_panel.jobs.update_disclosures --config config.yaml --skip-holdings
```

Use the backfill job once when onboarding a trader with full historical filings:

```bash
uv run python -m investment_panel.jobs.backfill_trader_disclosures --config config.yaml --trader "Trader Name"
```

New trader onboarding and the normalized public-disclosure CSV contract are
documented in [docs/trader-disclosure-pipeline.md](docs/trader-disclosure-pipeline.md).

## API

- `GET /api/status`
- `GET /api/dashboard`
- `GET /api/candidates`
- `GET /api/tickers/{symbol}`
- `GET /api/portfolio`
- `GET /api/theses`
- `GET /api/trader-twins`
- `GET /api/catalysts`
- `GET /api/fundamentals`
- `GET /api/disclosures`
- `GET /api/quotes`
- `GET /api/screener`
- `GET /api/options-expiries`
- `GET /api/options-chain`
- `GET /api/news`
- `GET /api/sepa`
- `GET /api/liquidity`
- `GET /api/correlations`
- `GET /api/etf-premiums`
- `GET /api/analyst-estimates`
- `GET /api/earnings`
- `GET /api/valuations`
- `GET /api/provider-runs`
- `GET /api/source-health`
- `GET /api/settings`

## Data Sources

Verified source notes are in [docs/data-sources.md](docs/data-sources.md).

Trader portfolios are modeled from primary public disclosure records. Market
does not ingest third-party tracker pages as source data; comparable tracker
products are useful only as UI/product references.

The app defaults to deterministic sample market data for local development.
Set `market_data.mode: online` in `config.yaml` to attempt online price fetches
through optional adapters while preserving fallback behavior.

Free-source enrichment is handled by `market-update-free-sources`. It uses the
local OpenCLI TradingView adapter for quotes, screeners, options, and news, plus
yfinance for estimates, earnings, and ETF NAV/premium data. Funda and Adanos are
intentionally not integrated.

The web app renders the derived data as an operational workbench: score bars,
positive/negative percentage pills, status badges, and summary cards highlight
quote breadth, SEPA strength, liquidity coverage, valuation signal, and source
health. The valuation endpoint is a low-confidence proxy only; it drops rows
with implausible fundamentals and reports upside in percentage points.

## Current Limitation

The app can generate deterministic research packets and memos from stored
evidence. Historical trader philosophy profiles are intentionally manual:
curating primary writings, interviews, letters, and books is the one area that
needs Joe's input before the trader-twin feature should be treated as serious.

## Shared Source Archive

Market keeps its live DuckDB local for safe writes, but the Mac mini should
publish autonomous source-job status and database snapshots to the NAS source
archive configured under `nas:` in `config.yaml`.

Default shared paths:

```text
/Volumes/agent/data-sources/market-mini/
/Volumes/agent/data-sources/market-mini/duckdb-snapshots/
/Volumes/agent/data-sources/status/
```

The daily screen and update jobs write status JSON files such as
`mini-market-ingest.json`, `mini-market-equity.json`,
`mini-market-crypto.json`, `mini-market-disclosures.json`, and
`mini-market-free-sources.json`. The snapshot job copies the local DuckDB file
to the NAS without running DuckDB over SMB.
