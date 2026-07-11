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
| PG-02 | PostgreSQL pool/runtime and configuration are primary | open | Bounded pool, read-only transactions, advisory locks, revision checks, and deterministic shutdown implemented; remaining callers pending |
| PG-03 | Alembic owns six-schema PostgreSQL DDL | done | `20260711_0001`; upgrade/downgrade test on PostgreSQL 18.4 |
| PG-04 | Real PostgreSQL test harness and coverage gates | open | PostgreSQL 18 fixtures; full fast suite 555 passed, 3 skipped; job/publication concurrency covered; final coverage thresholds pending |
| PG-05 | Durable app/user workflows ported | open | Portfolio, watchlist, and versioned thesis CRUD/read models ported; remaining settings/journal/orders pending |
| PG-06 | Ingestion and normalized raw facts ported | open | Idempotent run/payload manifests, quotes, monthly narrow option partitions, Robinhood and IBKR job persistence ported; remaining providers pending |
| PG-07 | Analysis decisions/outcomes and atomic publications ported | open | Versioned option features/decisions and concurrent atomic publications ported; outcome/learning retention pending |
| PG-08 | Jobs, agents, brokers, retention, and backups ported | open | ops orchestration, reference-safe option/analysis retention, verified PostgreSQL backups ported; agents/brokers pending |
| PG-09 | Selective DuckDB importer and reconciliation report | open | Idempotent durable-state/agent-evidence importer and exclusion report tested; canonical import pending cutover |
| PG-10 | All DuckDB runtime dependencies and vocabulary removed | open | |
| PG-11 | Full tests, coverage, concurrency, and performance gates pass | open | |
| PG-12 | Canonical runtime cut over and live NVDA probe verified | open | |
| PG-13 | Adversarial review has no accepted findings | open | |
| PG-14 | Migration committed, landed, and worktree cleaned | open | |

## Authority model

PostgreSQL is the only live authority. The final DuckDB file is an explicitly
named legacy import/recovery artifact retained for 30 days after cutover. Raw
facts point to one archived provider payload manifest. Analytical rows point to
raw identifiers and analytical runs; they never embed provider payloads.
