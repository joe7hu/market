# Market architecture

Market has one runtime data authority: PostgreSQL 18. Alembic owns schema
changes. The NAS keeps historical migration evidence only. It is not a live
database, fallback store, restore path, or dual-write target.

Availability projections are the sole point-in-time price selector authority.
Confirmation relations are short-lived staging and are never queried by
current-price functions after the verified cutover.

Raw option quotes keep seven complete trading days hot in PostgreSQL. Daily
partitions older than that are verified as native custom-format NAS archives
before detach and drop. The legacy monthly partition is not rewritten; daily
partitioning begins at its next boundary. Derived analytical detail is
recomputable and is retained through the `analysis.run` lifecycle for 30 days
unless publication, outcome, paper evidence, journal, or pinned research
evidence protects it.

## Logical layers and extension flow

Market is one modular monolith with one Python namespace and one React
application. The four logical layers are:

1. `domain/` — pure factors, signals, strategy evaluators, portfolio policy,
   and typed contracts. It does not load configuration, call providers, or
   access delivery or persistence modules.
2. `workflows/` — substantive application sequencing, cutoff handling,
   publication boundaries, and transaction coordination.
3. `infrastructure/` — PostgreSQL authorities, provider normalization, and
   scheduling/process integration.
4. `api/` — FastAPI transport, authorization bindings, response mapping, and
   generated browser contracts.

The ordinary research path is explicit:

```text
persisted strategy revision -> point-in-time datasets -> factors -> signals
                            -> strategy workflow -> research evidence/publications -> typed API/UI
```

Static factor and strategy catalogs bind an identity, implementation version,
typed parameters, dependencies, and a pure evaluator. Reusable signal
definitions consume typed factor requests through the same bounded evaluation
context. PostgreSQL stores the immutable revision, research state, evidence,
and publication identity. A new factor, signal, or strategy adds its concrete
owner, explicit catalog registration, workflow coverage, and tests; it does
not add a shared dispatch branch or a second runtime implementation.

Strategy research resolves the union of typed dataset requirements before the
first evaluation. `infrastructure/postgres/strategy_inputs.py` uses the
existing point-in-time price authority and exact completed-session calendar;
the largest price lookback requests one additional close and never substitutes
an older session. Event and option requirements remain named unavailable when
their source facts are absent. A strategy run records its resolved revisions,
scopes, mode, terminal status, input manifest, and separate input/output
identities. Only a succeeded research run is current; replay rows remain
labeled historical evidence.

The product remains advisory and paper-only: PostgreSQL is authoritative,
research and publication evidence is immutable, and missing authorization or
data blocks action. A Publication is the versioned output selected for API use;
Today is the bounded current decision projection; a Read Model is a bounded
PostgreSQL result for a product surface; and Availability Projection is the
point-in-time fact-to-availability mapping used by current-price selectors.
Event time, observation time, and availability time remain distinct. The
scheduler keeps its fixed capacity of two, and long collector/provider work
stays isolated from database transactions.

## Request flow

```text
Browser
  -> frontend/src/apiTransport.ts
  -> frontend/src/api/{panel,options,agent,portfolio,userState}.ts
  -> src/investment_panel/api/routers/
  -> src/investment_panel/api/dependencies.py / api/job_control.py
  -> src/investment_panel/application/read_models/ or workflow owner
  -> PostgreSQL 18
```

Routers define HTTP ownership and use typed FastAPI dependencies. They do not
construct database adapters or import database implementation modules. The
canonical deep Read Model interface is `/api/panel-snapshot`; domain detail
and mutation routes remain separate.

## Stable owners

| Owner | Interface | Change this when… |
|---|---|---|
| `src/investment_panel/api/main.py` | `create_app()` | app wiring or router registration changes |
| `src/investment_panel/api/contracts.py` / `response_contracts.py` | named Pydantic HTTP models | an HTTP request or response contract changes |
| `src/investment_panel/api/dependencies.py` | typed config, runtime, repository, and authorization providers | a route needs a new dependency |
| `src/investment_panel/application/read_models/panel_snapshot.py` | panel scopes, pagination, freshness, and last-good cache | a panel read changes |
| `src/investment_panel/application/read_models/loaders.py` | bounded panel query composition | a Read Model scope needs bounded loading |
| `src/investment_panel/api/job_control.py` | refresh start, heartbeat, and subprocess boundary | refresh control changes |
| `src/investment_panel/workflows/options.py` | option workflow sequencing and fail-closed gates | options actions change |
| `src/investment_panel/workflows/strategies.py` | persisted strategy resolution, requirement-driven evaluation, run completion, replay, and research evidence | strategy workflow sequencing changes |
| `src/investment_panel/workflows/market.py` | market cutoff, input loading, computation, and publication sequencing | MarketState publication behavior changes |
| `src/investment_panel/workflows/market_data.py` | normalized market-data ingestion and scoped refresh policy | source refresh behavior changes |
| `src/investment_panel/workflows/ticker_decisions.py` | ticker decision loading, ranking, publication, and paper-only sequencing | ticker decision publication changes |
| `src/investment_panel/workflows/today.py` | bounded Today queue and brief composition | Today workflow changes |
| `src/investment_panel/workflows/event_scout.py` | Event Scout packet, cooldown, and replay workflow | Event Scout mutation changes |
| `src/investment_panel/domain/factors/catalog.py` | typed factor definitions, dependency checks, and memoized evaluation | a factor or factor parameter changes |
| `src/investment_panel/domain/signals/catalog.py` | reusable typed signal definitions and factor requests | a reusable interpretation changes |
| `src/investment_panel/domain/strategies/implementations.py` | pure concrete strategy calculations and input normalization | a strategy calculation changes |
| `src/investment_panel/domain/strategies/catalog.py` | immutable strategy definitions and explicit implementation bindings | a strategy revision or binding changes |
| `src/investment_panel/domain/market/publication.py` | pure Market publication calculation and contract shaping | MarketState calculation or evidence semantics change |
| `src/investment_panel/infrastructure/postgres/strategy_inputs.py` | exact-session and declared-requirement loading | a supported strategy dataset requirement changes |
| `src/investment_panel/domain/portfolio/contracts.py` | account-aware portfolio and decision contracts | portfolio valuation or policy meaning changes |
| `src/investment_panel/domain/panel/` | panel contract and payload rules | a canonical panel shape changes |
| `src/investment_panel/core/event_scout.py` | Event Scout public rules and packet interface | signal normalization changes |
| `src/investment_panel/core/event_scout_runtime.py` | runtime packet processing | Event Scout runtime sequencing changes |
| `src/investment_panel/infrastructure/providers/advisory.py` | `StructuredProviderRequest`, result, and `invoke_structured` | provider behavior changes |
| `src/investment_panel/infrastructure/postgres/panel_models.py` | PostgreSQL model catalog and retrieval | a named Read Model is added or moved |
| `src/investment_panel/infrastructure/postgres/panel_queries.py` | panel query policies | a canonical panel query changes |
| `src/investment_panel/infrastructure/postgres/options_history.py` | Option History capture, history policy, health, and retention | historical option evidence changes |
| `src/investment_panel/infrastructure/postgres/options_research.py` | research candidates, event studies, and learning | research-only option reads change |
| `src/investment_panel/infrastructure/postgres/options_decision_system.py` | Decision Truth and readiness | option decision publication changes |
| `src/investment_panel/infrastructure/postgres/options_execution.py` | Option Ticket and paper execution | ticket or execution gates change |
| `src/investment_panel/infrastructure/postgres/options_recovery_read.py` | recovery research Read Models | recovery evidence changes |
| `src/investment_panel/infrastructure/postgres/ingestion.py` | managed ingestion lifecycle | collector lifecycle changes |
| `src/investment_panel/infrastructure/postgres/strategy_factory.py` | persisted strategy revision resolution and research evidence writes | strategy identity or evidence persistence changes |
| `src/investment_panel/infrastructure/scheduler.py` | one fixed-capacity scheduler and process boundary | scheduling or worker lifetime changes |
| `src/investment_panel/infrastructure/postgres/portfolio_ledger.py` | transaction, reversal, and position projection | portfolio accounting changes |
| `src/investment_panel/infrastructure/postgres/source_facts.py` | source facts and publication inputs | source facts change |
| `src/investment_panel/infrastructure/postgres/jobs.py` | canonical job allowlist and identity | a scheduled job changes |
| `migrations/versions/` | Alembic migrations | PostgreSQL schema changes |
| `frontend/src/apiTransport.ts` | browser transport and HTTP errors | transport behavior changes |
| `frontend/src/api/<domain>.ts` | one domain's request functions | a frontend request path changes |
| `frontend/src/generated/` | generated contract artifacts | backend schemas change; regenerate, do not hand-edit |

Package interfaces use explicit imports and `__all__`. Internal implementation
modules do not become public seams. A short forwarding module still fails the
deletion test; a deep coherent module may exceed 700 lines.

## Configuration

`investment_panel.settings` is the one typed configuration owner. Internal
callers use `AppConfig` or a narrow typed subsection. Only the redacted
settings HTTP response may become a dictionary. No action or database owner
accepts both `AppConfig` and arbitrary dictionaries.

## Frontend contracts

Backend Pydantic response models own direct domain contracts. The generated
OpenAPI files are reproducible build outputs. `frontend/src/apiTransport.ts`
owns transport behavior; domain request modules own URL and request shaping;
views import domain modules directly. Panel requests return bounded snapshots.
`MarketDataProvider` merges each snapshot once into current state, and its
in-flight map owns loading status. Scope generations reject late responses
across query options; accepted-data freshness is separate from attempted
refresh. An owning successful empty portfolio snapshot emits explicit
empty/null fields, while failures retain only labeled last-good state. Never
return captured application state from a request helper. `RowRecord` is kept only at the dynamic
panel-table seam. Do not hand-edit or routinely inspect generated schemas,
bundles, or full build logs. Use the contract checks, TypeScript check, and
production build to verify them.

## Focused change recipes

Each recipe starts with no more than three owner interfaces:

1. HTTP endpoint: `src/investment_panel/api/routers/<domain>.py`,
   `src/investment_panel/api/response_contracts.py`, and the domain owner.
   Run `make test-api` and `make check`.
2. Panel Read Model: `domain/panel/`, `infrastructure/postgres/panel_models.py`,
   and the owning database query module. Run `make test-postgres` and
   `make check`.
3. Options behavior: a typed read dependency or `workflows/options.py` for
   coordinated workflows, one deep options owner, and its
   public test interface. Run `make test-options` and `make check`.
4. Provider behavior: `infrastructure/providers/advisory.py`, the option-agent workflow, and
   its adapter tests. Run `make test-unit` and `make check`.
5. Configuration: `settings.py`, `api/dependencies.py`, and the settings
   route. Run `make test-api`, `make check`, and the config-focused tests.
6. Frontend request: `frontend/src/api/<domain>.ts`, the backend response
   owner, and the affected view. Run Vitest, TypeScript, and
   `npm --prefix frontend run build`.

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
uv run python scripts/architecture_inventory.py --area factors
uv run python scripts/architecture_inventory.py --area signals
uv run python scripts/architecture_inventory.py --area strategies
uv run python scripts/architecture_inventory.py --area workflows
```

The full output stays below 200 lines. Area output stays below 120 lines. The
inventory reports subsystem lines, route categories, explicit owner exports,
cycles, private imports, router boundaries, re-export-only modules, console
entry points, generated-contract presence, and compatibility markers. It does
not print the complete import graph.

The final architecture invariants are also checked by the inventory: compact
availability authority, implemented storage phases, seven-day/seven-hundred-
and-thirty-day option lifecycle, fixed scheduler capacity two, no current-price
confirmation fallback, and no retired backend TradingView provider or
configuration markers. The frontend TradingView chart embed remains a
presentation feature.

Scheduled work has one fixed in-process capacity of two. Fast deterministic
database ticks use worker threads inside the scheduler; long collectors and
provider or agent work use isolated subprocesses. No external queue is part of
the runtime contract.

## Fast iteration

- `make typecheck` runs TypeScript only; `make frontend` also runs Vitest.
- Use the existing focused backend target for the changed owner during edits.
- Run `make check` before commit and the full release gate once on the final
  integrated candidate. Do not repeat a full gate for unchanged code.
- Preserve tests through public interfaces; update old doubles instead of
  adding callback bags, signature inspection, or alternate production paths.

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
npm --prefix frontend run build
```

The storage archive tests require at least the configured free-space reserve.
If the host cannot satisfy that precondition, report the environment failure;
do not weaken the storage safety gate.

Storage recovery procedures and destructive-command gates are recorded in
[`docs/storage-operations.md`](docs/storage-operations.md) and
[`docs/adr/20260821-final-architecture-scale.md`](docs/adr/20260821-final-architecture-scale.md).
The ADRs preserve storage, execution, and deployment obligations. Current
ownership is defined here: retain deep coherent owners, remove forwarding
layers when callers can use the existing typed owner directly, and keep
routers on typed dependencies rather than database imports. Do not add
interfaces for a single implementation or divide modules to meet a line-count
target. Dated reviews and campaigns are historical inputs, not competing
architecture rules.

Checkout development serves `frontend/dist` when it exists. An installed wheel
is API-only unless `MARKET_FRONTEND_DIST` points to a built distribution and
`MARKET_MIGRATIONS_ROOT` points to a checkout or deployment bundle containing
`alembic.ini` and `migrations/`. The wheel never guesses those external assets.

For live checks, bind API and Vite to `0.0.0.0`, probe `/api/status` and the
changed routes, and compare the served frontend asset between `:5173` and
canonical `:8000`. Paper execution, strategy promotion, and Telegram remain
fail-closed unless their independent deterministic gates pass.
