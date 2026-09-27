# Decision evidence lifecycle acceptance record

Status: **in progress; production cutover and compaction have not run**.

## Identity and baseline

- Candidate branch: `codex/market-evidence-lifecycle-20260926`, based on `1ebefecbe9de8cec2a5dbb915f822a0f6149eca7`.
- Additive schema: Alembic `20260927_0041`, from `20260926_0040` (which follows production `20260924_0039`).
- Prior frozen candidate: `b73c02f099ef6e1a8f0594a67593e0ba2f29f57d`. The new `0041` branch candidate needs full gates and review before production rollout.
- Baseline capture: `/tmp/market-storage-baseline-20260926.json` (read only). PostgreSQL: 57,782,732,479 bytes; PostgreSQL filesystem free: 36,693,741,568 bytes. `app.publication_payload`: 12,869,959,680 bytes; `analysis.ticker_decision`: 10,839,572,480 bytes; `raw.option_quote`: about 9.93 GiB.
- The 100-decision sample showed duplicate resolution/plan impact, decision/episode expressions, and selected expression in nearly every applicable row. The resolution impact alone added 17,433,334 JSON bytes in the sample.
- No production schema migration, backfill, archive, or physical rewrite has run. Production after-size, observed reclamation, and production archive and restore receipts are **unmeasured**. The disposable restore test generates a manifest ID, deletes source scan evidence, restores typed rows in staging, and compares the exact JSON representation before and after.
- Second read-only production sample (2026-09-27 09:20 UTC): `pg_database_size(current_database())` = 58,238,109,375 bytes; measured PostgreSQL data-volume free = 37,496,332,288 bytes. Database bytes rose 455,376,896 since the 2026-09-26 sample. Volume free also rose, so its change cannot be attributed to database compaction. These are two distinct dates; forecast confidence remains provisional until at least a third daily sample.
- Read-only ranking publication inventory on 2026-09-27: 1,541 publications with 247,674 bundle items and 247,629 unique payload hashes; referenced payload size is 5,420,028,174 bytes. Only eight superseded generations and 671,082 referenced payload bytes are older than 30 days; 1,532 superseded generations and 5,415,729,389 referenced payload bytes are still inside the approved window. An outcome-attribution run separately held 1,050,151 bytes of repeated model input JSON; new runs store the six content-derived IDs there.

## Repeat the local proof

Run from the branch checkout with a disposable PostgreSQL test instance:

```sh
uv run pytest -q tests/postgres/test_hot_storage_lifecycle.py tests/postgres/test_decision_storage.py tests/postgres/test_postgres_panel_catalog.py tests/postgres/test_ticker_paper_execution.py tests/postgres/test_postgres_retention_backup.py tests/postgres/test_outcome_publication_recovery.py tests/application_api/test_api.py
make guards
make check
make release-gate
```

The E2E cases cover exact referenced/legacy reads, numeric JSON precision, unchanged checkpoints, same-cutoff semantic changes, source revisions and freshness, runtime-role grants, 30-day option and ticker eligibility, typed restoration after deleting source rows in a disposable database, an unavailable NAS during compact API reads, missing dependencies, archive corruption and interruption, concurrent paper and outcome references, and large typed COPY rows. Restore tests compare original and staged `to_jsonb(... )::text` values. Archive manifest IDs are test-generated and change on each run.

Operator commands after a fresh verified backup and a successful dry run:

```sh
market-storage account
market-storage compact --phase ranking-refs --state plan
market-storage compact --phase ranking-refs --state backfill --execute --batch-size 25
market-storage archive --phase ticker-decisions
market-storage archive --phase ticker-decisions --execute --backup-token <verified-backup-sha256>
market-storage compact --phase ticker-decisions --state gc --decision-id <ticker-decision-uuid> --execute --backup-token <verified-backup-sha256>
market-storage restore --phase ticker-decisions --decision-id <ticker-decision-uuid>
market-storage archive --phase option-scans
market-storage archive --phase option-scans --execute --backup-token <verified-backup-sha256>
market-storage restore --phase option-scans --scan-id <decision-uuid>
```

The restore destination must be a migrated staging PostgreSQL database set through `MARKET_STORAGE_RESTORE_DATABASE_URL`. The normal API reads local compact rows and does not require NAS access.

## Local results to date

- `uv run pytest -q` on the seven files above: **219 passed, one dependency deprecation warning**, `/tmp/market-evidence-focused-29.log`; the API contract correction then passed its five targeted checks, `/tmp/market-contract-31.log`.
- `make guards`: **31 passed**, `/tmp/market-evidence-guards-b73c02f.log`.
- `make check`: **passed**, including 53 frontend test files / 235 tests and typecheck, `/tmp/market-evidence-check-b73c02f.log`.
- `make release-gate`: **passed**, 2,503 tests, 84.31% coverage in 8m48s on frozen commit `b73c02f`, `/tmp/market-evidence-release-gate-b73c02f.log`.
- Independent branch review: **clean**, with no actionable finding on frozen commit `b73c02f`, `/tmp/market-evidence-autoreview-b73c02f.log`.
- Subsequent option-link extension: `tests/postgres/test_hot_storage_lifecycle.py` **58 passed** in `/tmp/market-hot-storage-lifecycle-after-links.log`. Further thesis-agent and protected-byte changes require a fresh frozen-candidate run. These changes are not covered by the frozen `b73c02f` full gates or review yet.
- Canonical ranking publication and linked option archive focused suite: **226 passed** in `/tmp/market-canonical-publication-focused-4.log`; migration foundation: **21 passed** in `/tmp/market-publication-foundation.log`. These runs cover the current `0041` candidate but are not full gates.
- Deterministic 100-symbol, three-evaluation busy-day replay: **300 evaluations, 110 decision writes, 190 unchanged checks, 2 publication generations, 200 bundle references, zero duplicate `app.publication_payload` rows, 441 unique decision payloads totaling 430,377 measured PostgreSQL payload bytes**. Command: `uv run --extra test python -m pytest -q -s tests/postgres/test_hot_storage_lifecycle.py::test_representative_busy_day_replay_measures_unique_writes`; output `/tmp/market-busy-day-replay.log`. This is a synthetic workload; production before/after write rates remain to be measured.

Disposable ticker restore receipt from `test_completed_ticker_evidence_archives_exact_inputs_and_restores_typed_rows`: `status=restored`, `analysis.ticker_decision=1`, `analysis.decision_input_payload=1`, `analysis.decision_context=1`, `catalog.instrument=1`, `analysis.strategy_forecast=0`. The test removes the source decision from its disposable database, restores into a typed staging schema, and asserts exact `to_jsonb` text equality with the original. The manifest ID is generated by that test run; the repeat command above creates a new verified receipt.

## Open acceptance gates

- Completed ticker evidence older than 30 days now has a verified typed archive, explicit full-evidence state and API read, staging restore, and separate guarded shared-payload and context collection. Production execution and a measured after-state remain open.
- New ticker ranking publications reference the existing immutable decision payload table instead of storing full model objects again in `app.publication_payload`. The SQL read views reconstruct rows for Today, ticker evidence, Research, Learning, attribution, and paper execution; mixed legacy and new readers pass the focused suite. Legacy publication payloads still require a bounded exact backfill. Option decision publications still require the same ownership audit.
- Ranking publications have an indexed decision reference and a bounded exact legacy backfill. Retention archives unreferenced superseded ranking generations after every legacy decision reference is checked, archives their decision payload dependencies, and collects an orphan only after verified pack identity and a transactional reference check. Superseded outcome-attribution generations are now archive eligible after 30 days; current outcomes remain local.
- Option detail and feature writers still retain repeated typed facts; old quote pins remain until their complete consumer graph can use compact records or verified archives.
- The option archive now verifies and restores linked thesis rows, superseded thesis revision ancestry, completed thesis automation and agent-task chains, and local primary option-scan ancestry. It compacts only the eligible completed scan. A scan with an unfinished task or experiment, or a scan serving a newer local dependent, stays local.
- A representative busy-day replay, before/after production write measurement, three distinct daily production forecast samples, a fresh production backup/restore receipt, managed-runtime smoke, and UI checks remain.
- The forecast is provisional. The measured baseline PostgreSQL-volume free space was 36,693,741,568 bytes (34.17 GiB). At the provisional 0.7 GiB/day rate, the illustrative 30-day free space is about 13.17 GiB. Joe waived the 15 GiB reserve as a completion gate on 2026-09-27. The existing runtime guard still reports degraded health and defers optional historical collection until a separate code change changes that policy. Accounting now reports a measured lower bound for old local evidence row bytes; it excludes shared dependencies, current work, and indexes. Three distinct daily production samples, complete protected-byte accounting, and measured reclamation are still missing. Reusable PostgreSQL pages are not counted as filesystem reclamation.

Do not use this record as approval for production compaction or a physical rewrite.
