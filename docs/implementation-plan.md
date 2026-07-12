# Personal Investment Panel Implementation Plan

> Historical v0 plan, superseded by the implemented PostgreSQL architecture.
> It is retained to explain the original product scope, not as current setup or
> operational guidance. Use the repository [README](../README.md),
> [architecture map](../ARCHITECTURE.md), and
> [PostgreSQL migration ledger](postgresql-migration.md) for current commands,
> schema ownership, verification, and runtime behavior.

## Local Context

The `market` repo starts as a greenfield repository. The adjacent local repos are:

- `/Users/joehu/proj/birdclaw`: local-first X/Twitter storage and CLI. It owns archive/live X ingestion and stores canonical Twitter data in SQLite under `~/.birdclaw`.
- `/Users/joehu/proj/arco`: personal intelligence layer over Birdclaw, explicit browser captures, market context, signal radar, belief ledger, and wiki outputs. It writes machine-readable snapshots under `/Volumes/agent/brain/raw/sources/arco`.

The panel should not duplicate Birdclaw or Arco. For v0, Arco is the canonical upstream source for X-derived thesis flow because it already handles source weighting, propagation denoising, author memory, beliefs, browser captures, and qualitative market context. Birdclaw remains the raw X ingestion layer and a manual refresh fallback.

## Architecture Decision

Build this repo as a local Python-backed web application:

- DuckDB for panel state.
- FastAPI for the local API.
- React + TanStack Table + Vite for the review UI.
- Deterministic daily and weekly jobs.
- Optional network fetchers for free public market data.
- Optional LLM research generation; deterministic memo stubs when no API key is configured.

Keep all trading execution, brokerage integration, real-time feeds, paid APIs, vector search, and microservices out of v0.

## Data Flow

1. Market panel reads `config.yaml`.
2. Jobs initialize `data/investment.duckdb`.
3. Jobs import configured watchlist and optional portfolio CSV.
4. Jobs fetch or synthesize daily OHLCV for configured equities and crypto.
5. Jobs calculate transparent technical features.
6. Jobs ingest the latest Arco brief-beliefs artifact as the primary thesis
   layer:
   - `brief-beliefs/brief-beliefs-YYYY-MM-DD.json`
   - latest `source-manifest-YYYY-MM-DD.json` for resolving the brief's selected
     evidence back to exported X/bookmark/observed/web source text
   - `signals.json`, `beliefs.json`, latest `birdclaw-bookmarks-YYYY-MM-DD.json`,
     and latest `web-captures-YYYY-MM-DD.json` remain compatibility fallbacks
     when a brief-beliefs artifact is absent.
7. Jobs normalize thesis/disclosure/category evidence into DuckDB.
8. Scoring combines technicals, fundamentals placeholders, category trend, thesis flow, notable trader signal, and portfolio fit.
9. Research packets are built only for candidates that cross thresholds or are requested manually.
10. Streamlit displays candidates, evidence, portfolio context, theses, trader-twin debate, and reports.

## V0 Scope

Deliver:

- DuckDB schema and repository helpers.
- Config loader.
- Daily screen job.
- Weekly portfolio review job.
- Portfolio CSV import.
- Arco thesis ingestion.
- Optional yfinance/CoinGecko/DefiLlama fetchers with graceful offline behavior.
- Candidate scoring.
- Research packet builder.
- Markdown + JSON research report persistence.
- Thesis tracker storage.
- Trader philosophy profiles.
- FastAPI API with useful empty states.
- React/TanStack web app with dense candidate, portfolio, memo, thesis, catalyst, and settings panels.
- Tests for schema, scoring, ingestion, and report generation.

Defer:

- Full SEC 13F and congressional disclosure automation beyond schema/config placeholders.
- Full fundamentals extraction.
- RAG.
- Auto trading or paper trading.
- Real-time data.

## Verification

Minimum verification before handoff:

- `python -m pytest`
- `python -m investment_panel.jobs.daily_screen --config config.yaml`
- `npm run build`
- `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000` smoke-check.
