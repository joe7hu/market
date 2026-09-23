# Decision-history storage repair and NAS migration

## What was growing

This investigation starts from the reported sizes, not a measurement of the
production database: 24 GB of input-manifest rows, 22 GB of ticker decisions,
9 GB of publication payloads, and about 19 GB of options. Re-run
`uv run market-storage plan` on the actual host before setting a space budget.
The inventory includes TOAST and sums option partition children. Its local
filesystem metric describes the CLI working directory, **not necessarily the
PostgreSQL data volume**. It does not scan all historical JSON just to report
capacity.

The old ticker writer copied the same input records into both the canonical
`analysis.ticker_decision.input_manifest` and a separate manifest table. That
second table has no production reader. Its original/revised columns usually
both contained the same complete input row. The writer ran on retries too;
`revision` was nullable in its ordinary unique constraint, so null revisions
were not deduplicated. A smaller compression setting would not fix this model.

Publication payloads already have content hashes. Adding another hash would
not address their growth: retained generations contain genuinely different
payloads. Existing retention also deleted historical publications without a
NAS export. Market context was repeated inside separate ticker decisions even
when the immutable context was identical.

## New ownership and retention rules

| Data | Online authority | Cold copy and reclamation |
| --- | --- | --- |
| Decisions, canonical input manifests, plans, outcomes, paper executions and P&L | Keep original IDs, values and point-in-time lineage in PostgreSQL | No age-based deletion introduced |
| Shared market/risk-policy context | Immutable `analysis.decision_context`, addressed by SHA-256; `ticker_decision_read` expands old inline and new referenced rows | Explicit resumable context backfill; no NAS dependency for app reads |
| Duplicate manifest rows | Retired as `analysis.ticker_input_manifest_legacy`; app writes revoked | Export every original column and row ID, verify complete coverage and typed restore, then owner-only `DROP ... RESTRICT` |
| Superseded publications eligible under existing age/count rules | Keep live generations, decision-linked market publications, and all ticker-ranking/outcome-attribution scopes | Verified, self-describing row archives before any retention deletion; missing NAS aborts deletion |
| Unreferenced publication payloads | Delete only after rechecking both bundle and current-projection references | Archive original hash, payload and creation timestamp first |
| Options | Existing hot/read authority unchanged | Export verified custom dumps; non-empty partitions require a separately reviewed owner maintenance operation |

The schema upgrade `20260923_0035` changes metadata and creates an empty context
table/view. It neither rewrites nor deletes the large histories. New decisions
stop writing duplicate manifests immediately. Context insertion is immutable,
checks equality on hash conflicts, and never rewrites an existing TOAST blob.
The read view preserves complete original JSON, not a lossy projection.

Publication exports are one original row per content-addressed JSON/gzip
object, including publication, bundle, bundle-item, legacy-item, payload and
analysis-run records. Sharing is retained, rather than exporting an entire
large bundle into one in-memory object. Scheduled production retention is wired
to the configured NAS directory. Direct `RetentionRepository` callers without
an archive directory retain publications/payloads instead of deleting them;
`MARKET_STORAGE_ARCHIVE_DIR` can configure those standalone callers. Default
publication deletion batches are reduced to 25 generations.

## Stage 1 — stop growth and create a recoverable starting point

Stop the API, scheduler and other database writers for the upgrade/reclamation
window. Do not deploy the renamed schema while an old writer is running: the
old writer must fail rather than recreate the redundant history.

Use the existing deployment's **migration-owner** connection as
`MARKET_DATABASE_URL`, not the app login. Keep credentials out of shell history
and documents. Confirm the intended database, version, tablespaces, actual
volume free space, and that `/Volumes/agent` is mounted. The configured paths
are:

- Archives: `/Volumes/agent/data-sources/market-mini/storage-archive/v1`.
- Backups: `/Volumes/agent/data-sources/market-mini/postgres-backups`.

The archive writer reserves 10 GiB on the NAS in addition to the next object.
Take a new full custom-format backup directly to the NAS:

```sh
uv run market-snapshot-database --config config.yaml
uv run market-db-migrate
uv run market-storage plan
```

Save the backup's returned `sha256` as `BACKUP_SHA`. The cutover rehashes the
actual dump file; a stale `verified` receipt alone is insufficient. Keep a
second independent copy/snapshot of the backup and cold archives. Rehearse a
full backup restore on a separate cluster with adequate space; the existing
backup command checks its dump listing and checksum, not a full restore.

For maintenance verification/backfill, set `MARKET_STORAGE_DATABASE_PATH` to
the actual local mount backing PostgreSQL data/tablespaces. On a local native
PostgreSQL installation, `SHOW data_directory` can identify it. A Docker path
inside a container is not automatically the corresponding host volume. For a
remote database, run these maintenance steps on its storage host or use a
correctly mounted representation; do not point this variable at the NAS or at
an unrelated free disk to bypass the check.

## Stage 2 — reclaim the retired manifest table first

This is a better first candidate than copying/restoring a non-empty 10 GB
options partition on a nearly full source cluster. It needs no full-table local
staging file or replacement database. Exports stream directly to the NAS in
500-row chunks, capped at 64 MiB **uncompressed COPY bytes per chunk**. The
verification phase checks at least 256 MiB free on the identified data volume
for its bounded temporary restore and overhead. This is not a promise that all
other concurrent workloads fit in that reserve; keep writers stopped.

```sh
uv run market-storage archive --phase decision-manifests --state plan
uv run market-storage archive --phase decision-manifests --state backfill \
  --execute --batch-size 500 --max-batches 20000
uv run market-storage compact --phase decision-manifests --state verify
uv run market-storage compact --phase decision-manifests --state cutover \
  --execute --backup-token "$BACKUP_SHA"
```

Repeat backfill if its result is `batch_complete`, until `export_complete`.
The command is restartable. Checkpoints advance only after durable file and
manifest writes. A crash before checkpoint commit reuses identical bytes.
Oversized chunks abort with instructions to lower the batch size. Sidecars
record column names/types, source identity, ID bounds, row counts, schema
revision, compressed checksum and uncompressed checksum. They remain useful
when restoring without access to the original manifest database.

Cutover locks only the retired table and independently repeats all checks:
contiguous coverage, source identity/schema, full source COPY hashes, actual
archive checksums, row counts, a typed PostgreSQL restore per chunk, and exact
restore round-trip hashes. It checks for an unexported tail. Corruption,
changed rows, missing chunks, missing mounts and new dependencies abort the
transaction. Only then does it issue `DROP TABLE ... RESTRICT`, never CASCADE.
No decision, outcome, execution, publication, or canonical manifest is deleted.
The result reports the actual relation bytes released on commit. The reported
24 GB is a planning estimate until this is executed on the real database.

## Stage 3 — compact retained decision context and old publications

With headroom recovered, backfill shared context in small transactions:

```sh
uv run market-storage compact --phase decision-context --state plan
uv run market-storage compact --phase decision-context --state backfill \
  --execute --batch-size 25
```

Repeat until `compacted` is zero. Each batch requires at least 2 GiB free on the
identified PostgreSQL volume and leaves full input manifests/plans inline.
The hot read view must return the same context JSON before/after backfill.
Context sharing savings depend on actual repeated values; do not budget the
entire 22 GB as reclaimable. No timestamps, raw inputs, revisions or evidence
are dropped to make hashes match.

Publication cleanup can be rehearsed as export-only before bounded cutover:

```sh
uv run market-storage archive --phase publications --state plan --batch-size 10
uv run market-storage archive --phase publications --state backfill \
  --execute --batch-size 10
uv run market-storage compact --phase publications --state cutover \
  --execute --batch-size 10 --backup-token "$BACKUP_SHA"
```

Repeat bounded cutover while eligible publications/orphan payloads remain.
Cutover uses the same archive-before-delete path as scheduled retention; it
re-verifies reused objects. It cooperates with publisher scope locks and
rechecks superseded status, retaining current/linked/shared records. It does
not promise to remove every historical publication: protected histories are
intentional. Do not disable those protections to force a target disk size.

## Stage 4 — separate logical reduction from filesystem reclamation

Ordinary `VACUUM (ANALYZE)` after context/publication batches makes dead space
reusable inside PostgreSQL. It usually does not return these TOAST/table files
to the operating system. The retired-table DROP is different: that removes
whole relation files after commit.

Do **not** launch VACUUM FULL, a whole-table rewrite, global REINDEX or a second
full cluster restore with only 1 GB free. Once logical compaction is complete,
measure remaining relation/index sizes and available space again. For physical
compaction, either provision a separate sufficiently sized local disk/cluster
and perform a verified backup/restore cutover, or schedule a single-table
rewrite with adequate replacement-table, index and WAL headroom. Leave the
live PostgreSQL data directory on suitable database storage; the attached NAS
is cold storage, not an automatic destination for PGDATA.

## Options staging and the non-empty partition boundary

```sh
uv run market-storage archive --phase options --state plan
# Optional: point to a disposable verification cluster with sufficient storage.
# Set MARKET_ARCHIVE_VERIFY_DATABASE_URL securely, never commit its credentials.
uv run market-storage archive --phase options --state backfill --execute
```

Without `--execute`, planning does not dump files or create scratch databases.
With an external verification DSN, scratch CREATE/restore/count/DROP all retain
its host, port, user and connection options. The source pg_dump still reads the
source database. Merely placing a dump file on the NAS does NOT move the
scratch database or its WAL off the source cluster. Ensure the selected scratch
cluster has space for restored data and overhead and uses a compatible version.
Dumps are published atomically after fsync; reused dumps are reverified.

The current app-facing `raw.detach_option_quote_partition` deliberately only
removes **empty** partitions. `--state cutover --execute --backup-token ...`
retains non-empty ones. Do not bypass this privilege boundary or claim August
is reclaimed after an archive-only run. A non-empty owner cutover additionally
needs an inventory of live and JSON-embedded evidence references, validated
cold-read/research needs, and a source-equality check while ingestion is frozen.
The provided 10 GB August estimate is therefore not an automatic first win.

## Restore and acceptance checks

`market-storage verify --manifest-id ID` rechecks an archive object;
`market-storage restore --manifest-id ID --destination /empty/staging/file`
restores a verified object to a new file, not into live tables. COPY chunks use
PostgreSQL text COPY, not CSV. Use their sidecar column order/types in a scratch
table with `COPY ... FROM STDIN WITH (FORMAT text)`. Publication JSON objects
contain `relation` and the full original `row`; restore in dependency order
using matching schemas and original IDs/hashes, first on a scratch database.
Never substitute a newly computed decision for an archived original.

Record before/after decision, outcome and execution counts; compare sampled
canonical input manifests, context JSON, plan hashes and P&L. Check that Today,
ticker evidence, Learning, Research and Paper Trading render without new
missing-data errors. Confirm storage health shows the actual large relations
and NAS failures rather than a healthy source with hidden archive failures.
Run focused storage/ticker tests and the release gate before deployment, then
`make release-smoke` against the running local app. The PR's CI does not prove
production data, NAS durability, free-space measurements or browser behavior.

Before legacy DROP, rollback means returning to the stopped pre-upgrade
checkout/schema only after restoring inline context. After legacy DROP,
rollback requires the archived rows or full backup as well. Migration downgrade
fails closed when normalized references exist or the legacy table has been
dropped; it must not silently discard compacted evidence.

## PostgreSQL references

- https://www.postgresql.org/docs/current/indexes-unique.html — ordinary unique
  indexes treat nulls as distinct unless configured otherwise.
- https://www.postgresql.org/docs/current/sql-vacuum.html — normal vacuum versus
  rewrite-based physical reclamation and extra-space requirements.
- https://www.postgresql.org/docs/current/storage-toast.html — out-of-line value
  storage and compression; not a substitute for avoiding duplicate facts.
