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
uv run python -m investment_panel.jobs.snapshot_database --config config.yaml
uv run python -m investment_panel.jobs.research_candidate TSLA --config config.yaml
uv run python -m investment_panel.jobs.weekly_portfolio_review --config config.yaml
```

## API

- `GET /api/status`
- `GET /api/dashboard`
- `GET /api/candidates`
- `GET /api/tickers/{symbol}`
- `GET /api/portfolio`
- `GET /api/theses`
- `GET /api/trader-twins`
- `GET /api/catalysts`
- `GET /api/settings`

## Data Sources

Verified source notes are in [docs/data-sources.md](docs/data-sources.md).

The app defaults to deterministic sample market data for local development.
Set `market_data.mode: online` in `config.yaml` to attempt online price fetches
through optional adapters while preserving fallback behavior.

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
`mini-market-crypto.json`, and `mini-market-disclosures.json`. The snapshot job
copies the local DuckDB file to the NAS without running DuckDB over SMB.
