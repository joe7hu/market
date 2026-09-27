# Decision evidence lifecycle acceptance record

Status: **in progress; production cutover and compaction have not run**.

## Identity and baseline

- Candidate branch: `codex/market-evidence-lifecycle-20260926`, based on `1ebefecbe9de8cec2a5dbb915f822a0f6149eca7`.
- Additive schema: Alembic `20260926_0040`, from `20260924_0039`.
- Tested code identity: `69bb937acf739af64b83e3c29d420cc22555b40d`. The checks below ran on this frozen code and schema revision.
- Baseline capture: `/tmp/market-storage-baseline-20260926.json` (read only). PostgreSQL: 57,782,732,479 bytes; PostgreSQL filesystem free: 36,693,741,568 bytes. `app.publication_payload`: 12,869,959,680 bytes; `analysis.ticker_decision`: 10,839,572,480 bytes; `raw.option_quote`: about 9.93 GiB.
- The 100-decision sample showed duplicate resolution/plan impact, decision/episode expressions, and selected expression in nearly every applicable row. The resolution impact alone added 17,433,334 JSON bytes in the sample.
- No production schema migration, backfill, archive, or physical rewrite has run. Production after-size, observed reclamation, and production archive and restore receipts are **unmeasured**. The disposable restore test generates a manifest ID, deletes source scan evidence, restores typed rows in staging, and compares the exact JSON representation before and after.

## Repeat the local proof

Run from the branch checkout with a disposable PostgreSQL test instance:

```sh
uv run pytest -q tests/postgres/test_hot_storage_lifecycle.py tests/postgres/test_decision_storage.py tests/postgres/test_today_action_queue.py tests/postgres/test_ticker_paper_execution.py tests/postgres/test_postgres_retention_backup.py tests/postgres/test_postgres_analysis.py tests/postgres/test_storage_guard.py
make guards
make check
make release-gate
```

The E2E cases cover exact referenced/legacy reads, numeric JSON precision, unchanged checkpoints, same-cutoff semantic changes, source revisions and freshness, runtime-role grants, 30-day option eligibility, typed restore after deleting source scan rows from a disposable database, an unavailable NAS during compact reads, missing quote dependencies, archive corruption and interruption, active and concurrent paper references, and large typed COPY rows. The restore test compares `to_jsonb(analysis.option_decision)::text` before archive with the typed staging row after restoration. Archive manifest IDs are test-generated and change on each run.

Operator commands after a fresh verified backup and a successful dry run:

```sh
market-storage account
market-storage archive --phase option-scans
market-storage archive --phase option-scans --execute --backup-token <verified-backup-sha256>
market-storage restore --phase option-scans --scan-id <decision-uuid>
```

The restore destination must be a migrated staging PostgreSQL database set through `MARKET_STORAGE_RESTORE_DATABASE_URL`. The normal API reads local compact rows and does not require NAS access.

## Local results to date

- `uv run pytest -q` on the seven files above: **145 passed, one dependency deprecation warning**, `/tmp/market-evidence-e2e-22.log`.
- `make guards`: **31 passed**, `/tmp/market-evidence-guards-22.log`.
- `make check`: **passed**, including 53 frontend test files / 235 tests and typecheck, `/tmp/market-evidence-check-23.log`.
- `make release-gate`: **passed**, 2,487 tests, 84.30% coverage, `/tmp/market-evidence-release-gate-19.log`.
- Independent branch review completed with no accepted finding in `/tmp/market-evidence-autoreview-21.log`. Its excluded-scope starvation concern was checked against `_publication_candidates`: the SQL excludes those scopes before `LIMIT`. Earlier reviews found and repaired archive, replay, legacy paper lookup, action fingerprint, and optional-history status defects.

## Open acceptance gates

- Ticker evidence older than 30 days needs a verified archive, explicit full-evidence state on reads, typed restore, and transactional shared-payload reference collection.
- Decision-backed publications still persist repeated model objects. They need canonical ID references and joined read reconstruction across Today, ticker evidence, Research, Learning, attribution, and paper execution.
- The safe ranking and outcome publication scope pin retains unreferenced old publications. It needs indexed decision-to-publication references and bounded legacy backfill before those scopes can be pruned. This is an acknowledged source of continuing local growth.
- Option detail and feature writers still retain repeated typed facts; old quote pins remain until their complete consumer graph can use compact records or verified archives.
- The option archive holds scans with a thesis or primary-decision link locally until their full dependency closure is supported. This limits 30-day coverage for linked scans.
- A representative busy-day replay, before/after production write measurement, three distinct daily production forecast samples, a fresh production backup/restore receipt, final independent review, final frozen release gate, managed-runtime smoke, and UI checks remain.
- The forecast is provisional. The measured baseline PostgreSQL-volume free space was 36,693,741,568 bytes (34.17 GiB). At the provisional 0.7 GiB/day rate, the illustrative 30-day free space is about 13.17 GiB, below the approved 15 GiB reserve. The accounting path reports degraded health and defers optional historical collection. Three distinct daily production samples, protected-byte accounting, and measured reclamation are still missing. Reusable PostgreSQL pages are not counted as filesystem reclamation.

Do not use this record as approval for production compaction or a physical rewrite.
