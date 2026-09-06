# Migration and repository review — 2026-09-06

**Verdict: changes are required.** The PostgreSQL foundation is sound, but migration recovery, application-role coverage, and several read models have concrete defects. Adding indexes alone will not resolve them.

Reviewed commit: `5841a51aeeae2c543427ea0d6264f5a8ceb9dc72` on clean, current `main`. The live database reported revision `20260906_0131` and PostgreSQL 18.4. Scope: migration runner and history, runtime transactions and role activation, registered panel queries and callers, pagination, and selected ingestion paths. Inventory: 131 migrations and 116 database modules. This was a risk-led review, not a line-by-line audit of all 63,080 lines.

Production access was read-only. No application result rows were printed into the investigation context. Query plans and tests were saved locally; only compact metadata and measurements were returned. No application code or production schema was changed.

1. **P1 — Application permissions remain incomplete at head.**

   Evidence: [role grants](../../migrations/versions/20260902_0067_application_role_privilege_hardening.py#L170), [event queries](../../src/investment_panel/database/event_panel_models.py#L6), [journal and alerts](../../src/investment_panel/database/panel_models.py#L934), [snapshot paging](../../src/investment_panel/database/panel_pagination.py#L54).

   Of 76 registered direct queries planned under `SET ROLE market_app`, five failed: `trade_journal`, `radar_alert`, `event_decision_packets`, `decision_truth`, and `event_scout_events`. The same role also lacks SELECT, INSERT, and DELETE on `app.review_page_snapshot`. The latter blocks the non-agent learning collections before their paging logic can work.

   The event queries feed `/api/event-scout` and `/api/event-scout/packets`. Their loader converts database errors into unavailable panels. Learning-page database errors are not covered by that route's ValueError handler. An isolated, newly migrated test database reproduced the missing privileges, so this is not only live database drift. There are already 43 migration filenames containing `privilege`; isolated grant repairs have not established complete workflow coverage.

   Repair: add explicit grants for the required reads, snapshot writes, and any required sequences. Test the registered query set and representative API/job workflows with the actual application login. Keep protected evaluator tables and functions outside broad grants. A head-revision check does not verify this contract.

2. **P1 — Migration 0037 cannot restart after interruption at its concurrent index step.**

   Evidence: [0037 upgrade](../../migrations/versions/20260815_0037_storage_archive_metadata.py#L21), especially the [autocommit block](../../migrations/versions/20260815_0037_storage_archive_metadata.py#L72).

   This migration creates tables and alters policy columns, then enters an autocommit block to create an index. Entering the block commits the preceding DDL. A failure at the index step leaves those objects present but leaves Alembic at revision 0036. Re-running then fails at the first CREATE TABLE with “already exists.”

   An isolated test injected an interruption at the index statement, verified the old revision and committed table, and reproduced the retry failure. This can block a fresh install or recovery even though normal upgrade/downgrade tests pass. The new 0129/0131 invalid-index handling does not repair this earlier point in the chain.

   Repair: provide verified recovery for this partial state. For new changes, separate transactional schema changes from a restartable concurrent-index revision, including invalid-index recovery. Do not blindly stamp a failed revision. Alembic documents that an [autocommit block commits prior work](https://alembic.sqlalchemy.org/en/latest/api/runtime.html#alembic.runtime.migration.MigrationContext.autocommit_block); PostgreSQL documents [invalid indexes after failed concurrent builds](https://www.postgresql.org/docs/18/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY).

3. **P1 — The option-chain read selects latest snapshots by scanning quote history for the full universe.**

   Evidence: [options_chain](../../src/investment_panel/database/panel_models.py#L761), [symbol-scope policy](../../src/investment_panel/database/panel_queries.py#L24).

   The latest-snapshot CTE joins historical quotes to contracts, then sorts and deduplicates by instrument. `options_chain` is absent from the symbol-scoped policy. Thus a caller's symbol filter does not constrain this query. The live plan includes an estimated 4.93 million rows at its largest node; a 50-row request exceeded the three-second server limit. This query belongs to the deeper ticker table contract, not the initial ticker bundle.

   Repair: pass instrument IDs into the source query, select eligible current snapshots before joining their quote rows, and bound the requested contracts. Reuse existing current-snapshot selectors where their eligibility rules match. Verify the resulting plan on populated history; an outer LIMIT does not bound the inner history scan.

4. **P1 — Volatility surface values mix all historical observations.**

   Evidence: [vol_surface_features](../../src/investment_panel/database/panel_models.py#L1025).

   The query averages IV across every quote row for each symbol and expiry. It does not select a current snapshot, restrict a time interval, or verify a completed eligible ingest run. Repeated captures therefore change the weight of a contract. `count(*) AS contracts` counts observations, while `max(observed_at) AS as_of` gives this historical aggregate the latest observation time. This is a correctness defect as well as a performance defect; the 50-row read also exceeded three seconds.

   Repair: define one eligible snapshot per symbol/source policy, then compute the surface from that snapshot's contracts. If a historical average is wanted, expose it as a separate, explicitly time-bounded measure.

5. **P2 — Event symbol filtering happens after a global LIMIT.**

   Evidence: [event query limits](../../src/investment_panel/database/event_panel_models.py#L6), [outer symbol filter](../../src/investment_panel/database/panel_models.py#L2114).

   Event queries select the latest 200 or 500 rows across all symbols. The shared loader wraps the query and applies the requested symbol afterward. A valid event outside that global prefix disappears from a symbol request. An isolated test inserted 201 events with the target symbol in the oldest event: the stored target count was one, but the wrapped query returned zero.

   Repair: apply symbol and availability filters before ordering and limiting. Use the existing `(symbol, as_of DESC)` access path where applicable. Test a target symbol outside the global prefix and stable ordering for equal timestamps.

6. **P2 — Learning pagination transfers and decodes the whole snapshot on every page.**

   Evidence: [snapshot read](../../src/investment_panel/database/panel_pagination.py#L54), [snapshot creation](../../src/investment_panel/database/panel_pagination.py#L65), [Python slicing](../../src/investment_panel/database/panel_pagination.py#L92).

   The first page loads up to 50,001 rows, converts them to JSON, and stores the full array. Each next page selects and decodes that full array before slicing it in Python. A 25-row page can therefore transfer 50,000 rows internally. Once a collection exceeds 50,000 rows, even its first page fails. This remains a design defect after the permission repair in finding 1.

   Repair: use database keyset pagination with stable ordering where the source is immutable. Where frozen results are required, store snapshot rows by ordinal and select only the requested range. Keep an explicit expiry policy. A row limit applied after decoding does not bound database-to-application traffic.

7. **P2 — Transition pages compute and sort the full history before returning a small page.**

   Evidence: [transition window](../../src/investment_panel/database/panel_models.py#L1070), [unconditional total-count window for limited reads](../../src/investment_panel/database/panel_models.py#L2090).

   A 50-row request took 1,930.9 ms, processed about 821,545 rows at its largest node, and wrote 48,307 temporary blocks, about 377 MiB. The query computes LAG by contract across history, sorts again by descending time, and the wrapper requests an exact full count. The page limit reduces output, not this work.

   Repair: retrieve the requested recent decisions and look up their actual predecessor per contract, with deterministic ordering, or maintain transitions at write time if warranted. Do not truncate history before LAG and silently lose predecessor correctness. Remove exact total counts where the UI only needs a next-page indicator; otherwise compute or cache them separately.

8. **P2 — URL-encoded database passwords break migration configuration.**

   Evidence: [alembic_config](../../src/investment_panel/database/migrations.py#L20).

   A valid DSN containing a password such as `example%40password` raises ValueError before connecting because `set_main_option` uses ConfigParser interpolation. A plain synthetic password succeeds. The isolated reproducer confirms this without accessing credentials.

   Repair: escape percent signs for ConfigParser or pass the URL/connection outside its interpolated string settings. Preserve correct driver conversion. Add one encoded-password regression check. See [Alembic set_main_option](https://alembic.sqlalchemy.org/en/latest/api/config.html#alembic.config.Config.set_main_option).

**Validation and limits**

- Five existing foundation tests passed in 3.23 seconds: normal migration round trip, forward upgrade from 0001, runtime commit/read-only behavior, job lock behavior, and the current read-path privilege assertions.
- Four audit reproductions passed in 2.58 seconds. They assert the observed defects: interrupted migration retry, encoded DSN failure, filter-after-limit data loss, and missing grants on a fresh head database. They are evidence scripts, not acceptance tests for the fixed behavior.
- Live database: zero invalid indexes; zero lock-wait sessions and idle transactions at the initial check. Query monitoring confirmed no audit query remained active after the server timeouts.
- The five measured reads used `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` with a read-only role context, three-second statement timeout, and 500 ms lock timeout. Measurements are individual observations, not latency percentiles or a load test.
- `sources` took 138.9 ms and `source_health` 47.1 ms in this run. Their estimated cost alone is not a defect. `pg_stat_statements` is not installed, so this review cannot rank queries by total production workload.
- Live role checks used an owner connection with `SET ROLE market_app`, not the production login handshake. Fresh-database grant checks corroborate the missing privileges. Existing application-login tests cover only part of the workflow set.
- The full suite, populated backup restore, concurrent migration runners, and HTTP/browser journeys were not run. Disk headroom was about 25 GiB, below the recorded release reserve. No live migration or restart was needed for this review.

**Keep the existing foundation.** Alembic is the sole DDL owner. Runtime pools are bounded; read and write contexts set statement and lock limits; related portfolio reads have a repeatable snapshot interface. Normal migrations use per-revision transactions, and recent index repairs check index validity. Parameterized queries and database constraints are already common. No ORM replacement or new repository framework is justified by these findings.

The migration runner itself has no migration-wide lock or explicit timeout policy in `migrations/env.py:30-39`. Add a single-runner lock that survives autocommit boundaries and explicit lock/build time budgets as operational hardening; concurrent migration failure was not reproduced in this review. In ingestion, SQL-in-loop inventory found 64 functions. That is a profiling lead, not 64 defects: preserve per-fact locks and provenance, and batch only independent work. Psycopg already supports [pipelined executemany](https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html).

**Repair order:** (1) complete application-role contracts and migration recovery; (2) fix current-snapshot semantics and filter placement; (3) bound paging and transition work; (4) add populated-data plan checks and workflow tests. Keep one small reproducer per defect. Do not raise API timeouts to conceal unbounded history reads.

Local audit evidence: `/tmp/market-db-review/` contains `revision`, `plans.py`, `plans.json`, `measure.py`, `measured.json`, `test_audit_repros.py`, `focused-tests.log`, `repros.log`, and the compact checkpoint. These scratch artifacts are not committed and can be removed by system cleanup. No GBrain skill or routing update was made.

All eight findings were repaired and verified. See [completion ledger](db-simplification-ledger.md).
