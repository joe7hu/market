# Architecture Map

Navigation guide for the Market codebase. Package-level; skim this before grepping.
For deeper detail, every package's `__init__.py` and each submodule carry a one-line
responsibility docstring — `ls` a package and read the docstrings.

## Request flow (read path)

```
Browser (frontend/src) ──HTTP──▶ app/main.py (FastAPI routes)
                                      │
                                      ▼
                          app/data_access/ (load + normalize for API)
                                      │
                                      ▼
                core/panel/ (assemble read models from DuckDB) ──▶ core/db.py (DuckDB)
```

Write path: `jobs/*` (pipeline entry points) pull from `providers/` + `core/*`,
compute via `analysis/`, and persist read-model tables that the read path serves.

## Backend layout (`src/investment_panel/` unless noted)

| Location | Responsibility | Go here to change… |
|---|---|---|
| `app/main.py` | FastAPI app: all HTTP routes, refresh-job triggers, SPA serving | an API endpoint / route shape |
| `app/data_access/` | Load core read models and normalize them into API payloads (types, config, loaders, mutations, payloads, ticker dossier, decision brief, settings) | how data is shaped for the UI / a payload field |
| `app/panel_contracts.py` | Scope→table contracts (which tables each page needs) | which tables a page scope loads |
| `app/scheduler.py` | In-app background refresh scheduler | scheduled in-app agent passes |
| `core/panel/` | **Read-model layer**: ~120 `con→list[dict]` accessors + `load_panel_data` dispatcher. Submodules: `snapshot` (orchestration), `read_equity`/`read_options`/`read_learning` (accessors), `market_environment`, `feed`, `technicals`, `disclosures`, `sources`, `metrics`, `coerce` | a read model / what a table returns |
| `core/decision/` | **Decision engine**: universe build, freshness, queue, readiness, grading, watchlist, market calendar. Submodules: `builders`, `read_models`, `grading`, `readiness`, `freshness`, `calendar`, `watchlist`, `portfolio`, `quotes`, `service`, `coerce`, `constants` | decision grades, gating, freshness rules |
| `core/options_radar/` | Options-radar pipeline: candidates, gates, scoring, opportunities, alerts, learning loop | options-radar logic |
| `core/brokers/` | Broker integration: `ibkr`, `moomoo` providers, `persistence`, `read_models`, `policy`, `recommendations`, `service` | broker data, paper orders, agent recs |
| `core/free_sources/` | Free market-data updates: `tradingview_sources`, `yfinance_sources`, `options`, `store`, `provenance`, `coerce` | TradingView/yfinance ingestion + storage |
| `core/source_ingestion/` | Followed-source directory ingestion (canonical, health, definitions). `raw_sources/` is a sub-package: `sync` (orchestration), `tweets`, `browser`, `io`, `coerce`, `constants` | followed-source pipeline / raw Birdclaw+browser ingest |
| `core/disclosures/` | SEC 13F + public/House disclosure ingestion. Submodules: `config`, `public_csv`, `house`, `prices`, `replica` (replica portfolios), `thirteen_f` (13F + SEC XML), `coerce`, `constants` | a disclosure ingestion / 13F parsing rule |
| `core/option_agent_thesis/` | Structured agent-thesis handoff for the options radar. Submodules: `requests` (build/retire requests + prompt), `thesis` (upsert/normalize/attach), `validation` (proof/catalyst/red-team checks), `dbutil`, `coerce`, `constants` | agent-thesis request/validation logic |
| `core/portfolio_intelligence/` | Portfolio-level risk read models. Submodules: `exposure` (clusters), `correlation` (edges), `cards` (risk cards + review actions), `holdings` (shared accessors), `coerce` | portfolio risk/exposure read models |
| `core/db.py` + `core/schema.py` | DuckDB connection/helpers (`db`, `init_db`, `query_rows`, `json_dumps`) and the full DDL string | a table definition (`schema.py`) or a DB helper (`db.py`) |
| `core/config.py` | App config loading (`AppConfig`, `load_config`) | config defaults/shape |
| `core/*.py` (leaf modules) | Domain helpers: `signals`, `sources`, `technicals`, `scoring`, `prices`, `fundamentals`, `portfolio`, `thesis_monitor`, `daily_brief`, `ibkr_options`, `options_intelligence`, `option_agent_runner`/`option_agent_postmortem`, `research`, `instruments`, `event_calendar`, `sec`, `arco`, `crypto`, `refresh_jobs` | that specific domain |
| `analysis/` | Pure computations: `valuation`, `sepa`, `liquidity`, `correlation`, `earnings_setup`, `option_ev`, `options_payoff`, `market_environment`, `stats` (+ `registry`, `run`) | a quantitative model |
| `providers/` | External data adapters: `yfinance_provider`, `tradingview`, `opencli` | how an external source is fetched |
| `jobs/` | Pipeline entry points (run as scripts / refresh jobs): `full_market_refresh`, `update_*`, `refresh_*`, `daily_screen`, `run_option_agents`, etc. | a pipeline step / job orchestration |

## Frontend layout (`frontend/src/`)

| Location | Responsibility |
|---|---|
| `App.tsx`, `main.tsx` | Router + app shell |
| `pages/*Route.tsx` | One route per page (Market, Watchlist, OptionsRadar, Health, Ticker, …) — thin; compose views |
| `views/` | Feature view modules. Larger features are folders: `health/`, `optionsRadar/`, `watchlist/` (`index` composition, `columns`, `format`, `cells`, `table`, `controls`). Shared: `rowFormat.ts`, `workspacePage.tsx` |
| `components/` | Reusable UI primitives (incl. `components/ui/`, `components/market/`) |
| `api.ts`, `marketData.tsx`, `model.ts`, `types.ts`, `hooks.ts`, `utils.ts` | API client, data context/model, shared types/hooks/utils |

## Conventions (read before adding code)

- **Facade packages, not god-files.** The large former monoliths (`panel`,
  `data_access`, `decision`, `brokers`, `free_sources`, `disclosures`) are now packages: the
  package `__init__.py` re-exports the public API; logic lives in responsibility
  submodules. **Import from the package** (`from investment_panel.core.panel import X`),
  not from submodules, in external code.
- **Don't re-grow a monolith.** Add a new responsibility submodule and re-export it
  from the facade rather than appending to an existing large file. Keep submodules
  scannable (target < ~700 lines). Keep orchestration thin.
- **Move, don't rewrite.** When extracting, preserve behavior and the public import
  contract; tests and the API import names from the facade.
- **Schema lives in `core/schema.py`** (one DDL string), applied by `core/db.py:init_db`.
  Frontend page = `pages/XRoute.tsx`; its logic/components live under `views/`.

## Verify changes

- Backend: `.venv/bin/python -m pytest tests/<relevant>.py -q` (focused first).
  Full suite hits a shared DuckDB; if a live `uvicorn --reload` dev server is running
  it holds the write lock and the run hangs — set `MARKET_DUCKDB_PATH=/tmp/x.duckdb`
  to use a fresh DB (but then skip the few tests that assert the default path).
- Frontend: `node_modules/.bin/tsc --noEmit` (typecheck); `npm run build` for a full build.
