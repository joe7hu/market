# PostgreSQL Authority Migration

## Goal packet

- User request: replace DuckDB with PostgreSQL 18 and simplify the schema into
  catalog, ingest, raw, analysis, app, and ops layers.
- Success criteria: PostgreSQL is authoritative for reads, writes, jobs,
  backups, and recovery; existing API/UI behavior is preserved; selective
  import retains durable data; coverage, concurrency, cutover, and live ticker
  verification pass.
- Non-negotiable constraints: no DuckDB runtime fallback, no dual-write final
  state, raw provider facts stay separate from analytical decisions, and exact
  source payloads are archived once rather than copied through derived tables.
- Non-goals: retaining full reject/mark history or preserving DuckDB SQL and
  lock semantics through a compatibility translator.
- External dependencies: PostgreSQL 18 on the canonical `mini1.local` runtime
  and the NAS archive path for payloads/backups.
- Stop condition: every ledger item below is `done` with current evidence and
  no accepted review findings remain.

## Completion ledger

| ID | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| PG-01 | Isolated migration branch/worktree from current Market main | done | `codex/postgres-revamp` at `7c3aa11` |
| PG-02 | PostgreSQL pool/runtime and configuration are primary | done | Bounded pool, read-only transactions, advisory locks, revision checks, deterministic shutdown, PostgreSQL app cache identity, and PostgreSQL-only live job allowlist |
| PG-03 | Alembic owns six-schema PostgreSQL DDL | done | `20260711_0001`; upgrade/downgrade test on PostgreSQL 18.4 |
| PG-04 | Real PostgreSQL test harness and coverage gates | done | PostgreSQL 18 fixtures; all 563 tests including slow tier pass; enforced live app/database coverage is 80.87% (minimum 80%); job/publication concurrency covered |
| PG-05 | Durable app/user workflows ported | open | Portfolio, watchlist, versioned thesis, journal, alert acknowledgement, and paper-order workflows ported; settings persistence pending |
| PG-06 | Ingestion and normalized raw facts ported | open | Idempotent run/payload manifests, quotes, monthly narrow option partitions, Robinhood and IBKR job persistence ported; remaining providers pending |
| PG-07 | Analysis decisions/outcomes and atomic publications ported | open | Versioned option features/decisions, PostgreSQL-native `/today` brief/risk/review publication, and concurrent atomic publications ported; outcome/learning retention pending |
| PG-08 | Jobs, agents, brokers, retention, and backups ported | open | ops orchestration, external agent queue, broker snapshots/recommendations, guarded strategy promotion, reference-safe retention, and verified PostgreSQL backups ported; legacy collector allowlist cleanup pending |
| PG-09 | Selective DuckDB importer and reconciliation report | open | Local canonical rehearsal imported 2 positions, 13 watchlist items, 2 current theses, 7 strategies, and 84 historical agent artifacts; rerun imported zero duplicate agent/thesis artifacts; reports in `data/migration/`; mini1 import pending |
| PG-10 | All DuckDB runtime dependencies and vocabulary removed | open | DuckDB removed from production dependencies and FastAPI imports successfully with DuckDB blocked; legacy importer/test modules and configuration compatibility vocabulary remain |
| PG-11 | Full tests, coverage, concurrency, and performance gates pass | done | 563 tests including slow tier; 80.87% enforced coverage; Ruff/architecture/TypeScript/build pass; 100 concurrent requests per route all 200 with p95 212ms status, 220ms `/today`, 349ms NVDA; restored DB counts/revision match; local DB is 11MB |
| PG-12 | Canonical runtime cut over and live NVDA probe verified | open | Local PostgreSQL 18 rehearsal migrated/imported, `/api/status`, `/today`, portfolio, watchlist, options, sources, and `/api/tickers/NVDA` returned 200; mini1 SSH currently rejects configured key |
| PG-13 | Adversarial review has no accepted findings | open | |
| PG-14 | Migration committed, landed, and worktree cleaned | open | |

## Authority model

PostgreSQL is the only live authority. The final DuckDB file is an explicitly
named legacy import/recovery artifact retained for 30 days after cutover. Raw
facts point to one archived provider payload manifest. Analytical rows point to
raw identifiers and analytical runs; they never embed provider payloads.
