# Storage operations

Storage commands are PostgreSQL-native and fail closed. For the large decision/JSON
histories and the 1 GB free-space incident, follow the
[storage-efficiency migration](storage-efficiency-migration.md).
No command in that plan silently deletes linked decisions or their evidence.

```sh
market-storage plan
market-storage compact --phase price-confirmations --state plan
market-storage compact --phase price-confirmations --state backfill --execute
market-storage compact --phase price-confirmations --state verify
market-storage compact --phase price-confirmations --state cutover \
  --execute --backup-token <verified-backup-sha256>
market-storage archive --phase options
market-storage archive --phase options --state backfill --execute
market-storage archive --phase options --state cutover --execute \
  --backup-token <verified-backup-sha256>
market-storage archive --phase options --expire
market-storage verify --manifest-id <id>
market-storage restore --manifest-id <id> --destination <empty-staging-file>
```

The availability workflow is resumable. Each backfill batch persists a
compound fact cursor and is idempotent. `verify` must report 100% coverage for
all eligible current and historical facts before `cutover` can recreate the
empty confirmation staging tables.

The option workflow writes custom-format dumps for immutable partitions. A
manifest is verified with `pg_restore --list`, checksum comparison, row-count
comparison, and a scratch-database restore. A failed NAS write, checksum,
capacity check, or restore leaves the partition attached. Normal APIs use
local hot PostgreSQL rows only.

Derived retention selects eligible `analysis.run` IDs first and deletes at
most 1,000 runs per transaction. It reports protected publication, outcome,
shadow-trade, journal, event-study, and verification reasons before mutation.
It uses normal `VACUUM (ANALYZE)` only; `VACUUM FULL` is prohibited.

The `/api/health/storage` payload reports storage size, archive lag, hot
partition age, retention backlog, and projected free space. Scheduler health
reports active work, fixed capacity two, job names, oldest runtime, and
deferred due work.

Publication retention now archives original rows on the configured NAS before
deletion. Missing/unconfigured storage retains source rows. Ticker ranking and
outcome-attribution publication scopes remain online. The retired duplicate
manifest table has its own bounded COPY export and separately verified cutover.
