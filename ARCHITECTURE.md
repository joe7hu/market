# Market architecture

Market has one runtime data authority: PostgreSQL 18. Alembic owns schema
changes. The NAS keeps historical migration evidence only. It is not a live
database, fallback store, restore path, or dual-write target.

## Request flow

```text
Browser
  -> frontend/src/apiTransport.ts
  -> frontend/src/api/{panel,options,agent,portfolio,userState}.ts
  -> app/routers/
  -> app/dependencies.py / app/panel_snapshot.py / app/job_control.py
  -> domain action or PostgreSQL read-model owner
  -> PostgreSQL 18
```

Routers define HTTP ownership and use typed FastAPI dependencies. They do not
construct database adapters or import database implementation modules. The
canonical deep Read Model interface is `/api/panel-snapshot`; domain detail
and mutation routes remain separate.

## Stable owners

| Owner | Interface | Change this when… |
|---|---|---|
| `app/main.py` | `create_app()` | app wiring or router registration changes |
| `app/contracts.py` / `app/response_contracts.py` | named Pydantic HTTP models | an HTTP request or response contract changes |
| `app/dependencies.py` | typed config, runtime, repository, and authorization providers | a route needs a new dependency |
| `app/panel_snapshot.py` | panel scopes, pagination, freshness, and last-good cache | a panel read changes |
| `app/job_control.py` | refresh start, heartbeat, and subprocess boundary | refresh control changes |
| `app/actions/options.py` | option workflow sequencing and fail-closed gates | options actions change |
| `app/actions/event_scout.py` | Event Scout packet, cooldown, and replay workflow | Event Scout mutation changes |
| `app/data_access/loaders.py` | panel query composition | a Read Model scope needs bounded loading |
| `src/investment_panel/core/panel/` | panel contract and payload rules | a canonical panel shape changes |
| `src/investment_panel/core/event_scout.py` | Event Scout public rules and packet interface | signal normalization changes |
| `src/investment_panel/core/event_scout_runtime.py` | runtime packet processing | Event Scout runtime sequencing changes |
| `src/investment_panel/providers/advisory.py` | `StructuredProviderRequest`, result, and `invoke_structured` | provider behavior changes |
| `src/investment_panel/database/panel_models.py` | PostgreSQL model catalog and retrieval | a named Read Model is added or moved |
| `src/investment_panel/database/panel_queries.py` | panel query policies | a canonical panel query changes |
| `src/investment_panel/database/options_history.py` | Option History capture, history policy, health, and retention | historical option evidence changes |
| `src/investment_panel/database/options_research.py` | research candidates, event studies, and learning | research-only option reads change |
| `src/investment_panel/database/options_decision_system.py` | Decision Truth and readiness | option decision publication changes |
| `src/investment_panel/database/options_execution.py` | Option Ticket and paper execution | ticket or execution gates change |
| `src/investment_panel/database/options_recovery_read.py` | recovery research Read Models | recovery evidence changes |
| `src/investment_panel/database/ingestion.py` | managed ingestion lifecycle | collector lifecycle changes |
| `src/investment_panel/database/portfolio_ledger.py` | transaction, reversal, and position projection | portfolio accounting changes |
| `src/investment_panel/database/source_facts.py` | source facts and publication inputs | source facts change |
| `src/investment_panel/core/refresh_jobs.py` | canonical job allowlist and identity | a scheduled job changes |
| `migrations/versions/` | Alembic migrations | PostgreSQL schema changes |
| `frontend/src/apiTransport.ts` | browser transport and HTTP errors | transport behavior changes |
| `frontend/src/api/<domain>.ts` | one domain's request functions | a frontend request path changes |
| `frontend/src/generated/` | generated contract artifacts | backend schemas change; regenerate, do not hand-edit |

Package interfaces use explicit imports and `__all__`. Internal implementation
modules do not become public seams. A short forwarding module still fails the
deletion test; a deep coherent module may exceed 700 lines.

## Configuration

`investment_panel.core.config` is the one typed configuration owner. Internal
callers use `AppConfig` or a narrow typed subsection. Only the redacted
settings HTTP response may become a dictionary. No action or database owner
accepts both `AppConfig` and arbitrary dictionaries.

## Frontend contracts

Backend Pydantic response models own direct domain contracts. The generated
OpenAPI files are reproducible build outputs. `frontend/src/apiTransport.ts`
owns transport behavior; domain request modules own URL and request shaping;
views import domain modules directly. `RowRecord` is kept only at the dynamic
panel-table seam. Do not hand-edit or routinely inspect generated schemas,
bundles, or full build logs. Use the contract checks, TypeScript check, and
production build to verify them.

## Focused change recipes

Each recipe starts with no more than three owner interfaces:

1. HTTP endpoint: `app/routers/<domain>.py`, `app/response_contracts.py`, and
   the domain owner. Run `make test-api` and `make check`.
2. Panel Read Model: `core/panel/`, `database/panel_models.py`, and the
   owning database query module. Run `make test-postgres` and `make check`.
3. Options behavior: `app/actions/options.py`, one deep options owner, and its
   public test interface. Run `make test-options` and `make check`.
4. Provider behavior: `providers/advisory.py`, the option-agent workflow, and
   its adapter tests. Run `make test-unit` and `make check`.
5. Configuration: `core/config.py`, `app/dependencies.py`, and the settings
   route. Run `make test-api`, `make check`, and the config-focused tests.
6. Frontend request: `frontend/src/api/<domain>.ts`, the backend response
   owner, and the affected view. Run Vitest, TypeScript, and `npm run build`.

## Guardrails and inventory

`make check` runs generated-contract checks, static architecture guards, Ruff,
frontend tests, and TypeScript checking. The guards fail on import cycles,
production private cross-module imports, router/database imports, dynamic
facade exports, retired compatibility files or routes, stale generated
contracts, and unregistered console commands. Ruff includes F401 unused-import
and F811 redefinition checks. File length is inventory information only.

Use the compact inventory for navigation:

```sh
uv run python scripts/architecture_inventory.py
uv run python scripts/architecture_inventory.py --area api
uv run python scripts/architecture_inventory.py --area config
uv run python scripts/architecture_inventory.py --area options
uv run python scripts/architecture_inventory.py --area providers
uv run python scripts/architecture_inventory.py --area frontend
```

The full output stays below 200 lines. Area output stays below 120 lines. The
inventory reports subsystem lines, route categories, explicit owner exports,
cycles, private imports, router boundaries, re-export-only modules, console
entry points, generated-contract presence, and compatibility markers. It does
not print the complete import graph.

## Verification

```sh
make test-unit
make test-api
make test-options
make test-postgres
make test-all
make check
uv lock --check
uv build --wheel
npm run build
```

The storage archive tests require at least the configured free-space reserve.
If the host cannot satisfy that precondition, report the environment failure;
do not weaken the storage safety gate.

For live checks, bind API and Vite to `0.0.0.0`, probe `/api/status` and the
changed routes, and compare the served frontend asset between `:5173` and
canonical `:8000`. Paper execution, strategy promotion, and Telegram remain
fail-closed unless their independent deterministic gates pass.
