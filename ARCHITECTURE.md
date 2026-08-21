# Market architecture

Market has one runtime data seam: PostgreSQL 18. Alembic owns schema changes.
The NAS keeps historical migration evidence only. The repository has no archive
reader, restore path, fallback store, or dual-write path for that evidence.

## Request flow

```text
Browser
  -> frontend/src/apiTransport.ts
  -> frontend/src/api.ts and domain request modules
  -> app/routers/
  -> app/dependencies.py / app/panel_snapshot.py / app/job_control.py
  -> app/actions or app/data_access query owners
  -> src/investment_panel/database/
  -> PostgreSQL 18
```

Writes use an action or workflow owner. Read-only requests use a query/read-model
owner. Routers do not construct database adapters.

## Stable owners

| Owner | Interface | Change this when… |
|---|---|---|
| `app/main.py` | `create_app()` | app wiring or router registration changes |
| `app/contracts.py` | Pydantic HTTP input models | an HTTP request contract changes |
| `app/dependencies.py` | typed config and runtime providers | a dependency needs a new provider |
| `app/panel_snapshot.py` | scope snapshots, pagination, freshness, last-good cache | a panel read needs snapshot behavior |
| `app/job_control.py` | refresh start, heartbeat, subprocess boundary | a refresh workflow changes |
| `app/request_security.py` | local/private-network request gate | mutation access policy changes |
| `app/actions/options.py` | option decision, ticket, paper gate workflow | options sequencing or fail-closed policy changes |
| `app/actions/event_scout.py` | Event Scout packet, cooldown, replay workflow | Event Scout mutation or replay changes |
| `app/data_access/loaders.py` | panel query adapters | a read-model query needs a new API shape |
| `database/panel_models.py` | model catalog and read-model ownership | a named panel model is added or moved |
| `database/panel_queries.py` | PostgreSQL panel query implementation | a canonical panel table query changes |
| `database/event_scout.py` | PostgreSQL Event Scout repository | Event Scout persistence or truth linkage changes |
| `database/options_decision_system.py` | option decision repository | options decision publication changes |
| `database/options_history_v3.py` | capture, evidence, materialization owner | option history evidence changes |
| `database/portfolio_ledger.py` | transaction, reversal, and position projection | portfolio accounting changes |
| `database/source_facts.py` | source facts and publication inputs | source ingestion facts change |
| `database/ingestion.py` | managed ingestion run lifecycle | a collector lifecycle changes |
| `core/refresh_jobs.py` | canonical job allowlist and job identity | a scheduled job changes |
| `migrations/versions/` | Alembic migrations | a PostgreSQL schema changes |
| `frontend/src/generated/apiSchema.ts` | generated OpenAPI TypeScript types | backend HTTP schemas change |
| `frontend/src/apiTransport.ts` | GET/POST/PATCH transport and errors | browser transport behavior changes |
| `frontend/src/api.ts` | thin domain request wrappers and view adapters | a frontend request path changes |

Pure rules stay in the smallest domain module that owns them. A package facade
may expose a documented public API, but it must use explicit imports and
`__all__`. New code must not add lazy exports, dynamic module loading, or a
second compatibility layer.

## Configuration rule

`investment_panel.core.config.load_config()` is the internal typed boundary.
Application owners receive `AppConfig` or a narrow typed config object. The
settings endpoint may call `public_config_payload()` to make a redacted dict;
that public dict must not flow back into internal owners.

## Frontend contract rule

`scripts/generate_openapi.py` writes the stable backend contract to
`frontend/src/generated/openapi.json`. The pinned `openapi-typescript` version
generates `frontend/src/generated/apiSchema.ts`. Run `npm run generate:api` after
an intentional HTTP schema change. CI and `make check` run both generators in
check mode and fail when either file is stale.

`frontend/src/api.ts` keeps existing request function names for UI stability.
It contains thin domain wrappers and frontend-only adapters. Transport logic is
in `apiTransport.ts`; backend-owned request and response shapes come from the
generated schema whenever the route has a named OpenAPI model.

## Five change recipes

1. Add an endpoint: add a Pydantic response model beside the router contract,
   call an action or query owner, run `npm run generate:api`,
   `make check`, and the focused route tests.
2. Add a read model: add the SQL and normalization to the owning `database/`
   read module, register its catalog contract, then verify the scope route and
   its PostgreSQL interface test. Do not add SQL to a router.
3. Add a job: add one canonical owner under `jobs/`, register its identity in
   `core/refresh_jobs.py`, add lifecycle tests, then run the job-control tests.
4. Add a migration: add one Alembic revision, test fresh `base -> head` and
   previous revision `-> head` on PostgreSQL 18, then run the affected owner
   tests and `uv lock --check`.
5. Add a frontend field: update the response model first, regenerate the
   OpenAPI TypeScript files, update the domain adapter/view, then run Vitest,
   `npm run typecheck`, and the production build.

## Guardrails

`make check` runs:

- generated panel and OpenAPI contract checks;
- architecture tests for module size, local imports, explicit facades,
  PostgreSQL-only runtime markers, router boundaries, console entry points,
  and OpenAPI baseline/schema rules;
- Ruff;
- all frontend Vitest tests and TypeScript checking.

Run the compact inventory with `uv run python scripts/architecture_inventory.py`.
It prints entry points, owner modules, forbidden dependency checks, and only a
failure summary. It does not print the complete import graph.

## Verification

```sh
make check
uv run --extra test pytest tests/<focused_test>.py -q
uv lock --check
uv build --wheel
npm run build
```

For a live check, bind the API and Vite server to `0.0.0.0`, probe `/api/status`
and the changed routes, and compare the served frontend asset between `:5173`
and canonical `:8000`. Paper trading, promotion, and Telegram remain
fail-closed unless their existing readiness gates pass.
