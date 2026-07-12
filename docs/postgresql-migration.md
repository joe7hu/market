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
| PG-01 | Isolated migration branch/worktree from current Market main | done | `codex/postgres-revamp` in `/Users/joehu/proj/market-postgres-revamp` |
| PG-02 | PostgreSQL pool/runtime and configuration are primary | done | Bounded pool, read-only transactions, advisory locks, revision checks, deterministic shutdown, PostgreSQL app cache identity, and PostgreSQL-only live job allowlist |
| PG-03 | Alembic owns six-schema PostgreSQL DDL | done | `20260711_0001` foundation plus forward `0002`, heartbeat lease migration `0003`, and strategy-authority migration `0004`; fresh, `0001 -> head`, reconciliation, uniqueness, and downgrade tests on PostgreSQL 18.4 |
| PG-04 | Real PostgreSQL test harness and coverage gates | done | PostgreSQL 18 fixtures; 597 passed and 2 skipped; migration-critical coverage 83.59% (minimum 80%); job/publication concurrency covered |
| PG-05 | Durable app/user workflows ported | done | Portfolio, watchlist, versioned thesis, journal, alerts, paper orders, and PostgreSQL-authoritative agent/research settings |
| PG-06 | Ingestion and normalized raw facts ported | done | Idempotent payload manifests plus market bars, content/X/RSS, Arco, official events, House/13F/CSV disclosures, broker/options collectors, and narrow monthly option partitions |
| PG-07 | Analysis decisions/outcomes and atomic publications ported | done | Actionable-only option features/decisions, aggregated rejects, one-row incremental outcomes, calibration/cohort read models, `/today`/market publications, and atomic publication swaps |
| PG-08 | Jobs, agents, brokers, retention, and backups ported | done | PostgreSQL-only API and CLI orchestration, external agent queue, broker snapshots/recommendations, guarded strategy promotion, reference-safe retention, and streamed verified backups |
| PG-09 | Selective DuckDB importer and reconciliation report | done | Canonical mini1 import and idempotent rerun retained 2 positions, 13 watchlist items, 2 theses, 7 strategies, 84 agent artifacts, 370,945 bars after existing-row reconciliation, 24,522 content items, 1,319 disclosures, and 34 events; excluded ~10.1M derived rows |
| PG-10 | All DuckDB runtime dependencies and vocabulary removed | done | DuckDB exists only in test/legacy-import extras; FastAPI plus full/hourly/premarket installed entrypoints import with DuckDB blocked; retired DuckDB collectors are absent from live allowlist and console scripts |
| PG-11 | Full tests, coverage, concurrency, and performance gates pass | done | 597 passed, 2 skipped; 83.59% coverage; Ruff/architecture/TypeScript/build pass; restored DB revision/counts match. Live revision-0004 concurrent 50-request p95: status 304ms, `/today` 270ms, NVDA 463ms, portfolio 224ms; all responses 200. Local DB is 139MB versus 32GB while retaining compact durable raw facts |
| PG-12 | Canonical runtime cut over and live NVDA probe verified | done | `mini1.local` runs PostgreSQL revision `20260711_0004`; API and Vite bind all interfaces; `/api/status`, `/today`, portfolio, options radar, and `/api/tickers/NVDA` returned 200. Live custom backup `market-20260712T114339Z.dump` is 12.9MB with SHA-256 `df219d1b04be04d9b72c9df7023fd3e4048a10dad8c53fd40f77bf5292f91c6a` |
| PG-13 | Adversarial review has no accepted findings | done | Joe approved the final three findings. Canonical alias normalization, promotion/publication coordination, stale-run rejection, authority reconciliation, and a unique active-authority constraint were implemented with regression tests; the finding-originating independent reviewer returned `CLEAN` on commit `5c4f7ed` |
| PG-14 | Migration committed, landed, and worktree cleaned | done | PostgreSQL migration and review remediation commits are on `main`; the temporary migration worktree was removed after live cutover |

## Authority model

PostgreSQL is the only live authority. The final DuckDB file is an explicitly
named legacy import/recovery artifact retained for 30 days after cutover. Raw
facts point to one archived provider payload manifest. Analytical rows point to
raw identifiers and analytical runs; they never embed provider payloads.

Options default to the daily/premarket cadence. Intraday option pulls require an
explicit environment interval. Full chains remain available as the latest
interactive snapshot; unreferenced historical chains retain seven days, while
actionable decision inputs and one incremental outcome row remain durable.
