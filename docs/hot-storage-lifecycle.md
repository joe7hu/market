# Hot PostgreSQL, cold NAS: lifecycle and deployment

## Why 60 GB is not a serving requirement

The September 23 inventory is a production report, not a measurement made by
this PR. Twenty-six thousand ticker decisions occupying roughly 23 GB is
primarily a representation/lifecycle problem, not a need to store that much
unique trading information. A snapshot embeds whole input groups repeatedly;
context-only sharing in revision 0035 did not normalize these groups. One-row
publication archives also amplified NAS metadata work. General retention could
cascade-delete derived history and execute unbounded quote deletes/rewrites.

PostgreSQL remains the only online authority. NAS contains verified immutable
archives, not PGDATA and not an additional database on the request path.
Separate the data's usefulness from the original provider transport envelope.

## Implemented policy

| Class | Local PostgreSQL | NAS lifecycle |
|---|---|---|
| Current publications, latest captures per source/profile/symbol/universe | Always retained, including an old last valid capture | Never removed solely because markets are closed or a provider is late |
| Ticker decisions, plans, point-in-time inputs | IDs, hashes, plans and lineage remain local; identical input groups >=1 KiB are immutable content-addressed payloads | No cold read dependency or lossy historical projection |
| Raw option provider envelopes | Recent 7 calendar days, latest captures and exact feature/decision/arbitrage references | Original PostgreSQL row archived and verified before the envelope is cleared |
| Typed option history/event-strip quotes | Existing 730/365-day history windows, plus protected references and latest captures | Only eligible expired rows leave after verified archival |
| Other radar quotes | 7 days, plus exact references and latest captures | Bounded archive-before-delete, preserving snapshot/capture metadata |
| Relative-value calculations | 30 days; decision-linked, verified/arbitrage, publication and strategy-evaluation references retained | Unreferenced old calculations archived before removal |
| Superseded publication payloads | Rolling UI scopes 7 trading days; other ordinary scopes 7 calendar days; market keeps latest 48 superseded generations | Self-describing packed archives before deletion; all current, shared and decision-linked generations protected |
| Ranking / outcome-attribution publications | Existing protection retained | No unreviewed removal of point-in-time research history |
| Decisions, option decisions, evidence, features, executions, outcomes, P&L | Retained; a parent run cannot cascade-delete them | Only genuinely empty run metadata can be archived/deleted |
| Operational job logs | Successful/skipped 7 days; failed/partial 30 days | Bounded operational expiry, not investment evidence |

These policies bound disposable history; they do **not** promise a fixed-size
entire database. Unique decision/audit evidence still grows. Do not claim that
all 60 GB, or all 23 GB of ticker decisions, can be removed. Measure the shared
input hit rate and protected-row mix on the host. A future cold decision/replay
API must be implemented and verified before removing that remaining history.

## New writes and backward-compatible reads

Revision `20260924_0036` adds `analysis.decision_input_payload`, compact manifest
references, and a pending-backfill index. It does not rewrite old history during
the upgrade. New ticker publications intern large input groups in the same
transaction. Exact PostgreSQL JSON text owns hashing and equality; decimal
precision, nulls, timestamps, source revisions and the original input hash are
not changed. Collisions/missing references fail closed. Payloads cannot be
updated or deleted by the application. `ticker_decision_read` expands old and
new formats without NAS, and Research's thesis reader uses this same view.

Backfill is explicit, at most 100 decisions and 64 MiB of original input JSON
per transaction. The default is 25. Empty/small manifests are marked processed
so they cannot repeatedly block progress. The actual PostgreSQL volume must
have at least 2 GiB free before a backfill transaction.

## Bounded archival, restart and failure semantics

`postgres_retention` now runs hourly, with a one-interval startup delay and
180-second launcher timeout. `MARKET_STORAGE_RETENTION_SECONDS=0` disables the
periodic invocation (the full-refresh entrypoint still runs retention).
Hot phases alternate, share a 60-second between-batch budget, and perform at
most 20 batches per phase in a scheduled invocation. Each database statement
has a 30-second timeout and a 2-second lock timeout. A slow NAS syscall may
still last until the enclosing launcher timeout; these are not hard real-time
latency guarantees. Heavy one-off maintenance must not run alongside trading
workers on a nearly full machine.

Each source batch scans at most 500 rows by default (hard maximum 1,000), with
4 MiB of selected original row text. PostgreSQL locks the selected rows. NAS
packs contain at most 500 rows / 8 MiB of serialized JSON each, are compressed,
content-addressed, fsynced and verified using the existing archive service.
The NAS reserves 10 GiB plus the object size. Missing mounts, capacity problems,
oversized individual rows and corruption abort source mutation.

A source mutation and its checkpoint commit together. Checkpoints advance past
protected rows, resume within a snapshot, and reset after a completed sweep to
revisit rows that age into eligibility. New quote timestamps need not equal
the snapshot timestamp. A crashed batch can leave an unreferenced but valid NAS
pack; it cannot leave deleted rows with an advanced-but-uncommitted cursor.
Retries verify reused bytes. A failed phase records its old cursor and error.
One maintenance job lock excludes competing hot-retention workers.

The transaction also locks capture generations before archiving old quotes and
checks the complete source row count before option-history output is written.
It locks parent analysis runs before deleting relative values, so concurrent
publication and strategy-evaluation foreign-key writes finish before the final
pin check. The empty-run delete helper applies its age cutoff again, even when
called directly with IDs that were not selected by the normal retention query.

Publication packs amortize what used to be one file and manifest per row.
A generation remains atomic: a failure during any pack retains its publication
and payloads. Large historical generations may take several packs; generation
count and per-pack memory are bounded, not a promised constant total byte count
for an arbitrary publication graph. Never turn off its reference protections
to force a target size. Shared publication payloads are rechecked before GC.

## Runtime permission boundary

Lifecycle integration tests use the configured non-owner application login.
Revision 0036 grants only the extra derived/cache deletion and RV row-lock
permissions used by retention. It does not grant parent `analysis.run` DELETE:
a bounded, fixed-search-path database helper rechecks every incoming FK before
removing empty metadata. The ordinary application cannot cascade-delete a run.

## Deployment and initial backlog

1. Verify a current full NAS backup and an independent copy/snapshot. Keep the
   existing backup receipt and checksum. Stop old API/scheduler writers for the
   schema upgrade. Use the separate migration-owner DSN, never app credentials
   in shell history.
2. Set `MARKET_STORAGE_DATABASE_PATH` to the real host filesystem backing
   PGDATA/tablespaces. A path inside Docker is not necessarily the host volume;
   do not substitute the checkout or NAS. Confirm the configured NAS archive
   root is mounted. Existing archives and backups remain in their current paths.
3. Upgrade and inventory. Do not run backfill blindly with insufficient free
   space; updates produce WAL and dead tuples even when the final live model is
   smaller.

```sh
uv run market-storage plan
uv run market-db-migrate
uv run market-storage compact --phase decision-inputs --state plan
uv run market-storage compact --phase hot-options --state plan
uv run market-storage compact --phase relative-values --state plan
```

Restart the upgraded API/scheduler, or keep writers stopped during the initial
maintenance window. Drain explicit bounded batches while recording counts and
free space. One input command is deliberately one transaction; repeat until
`compacted` is zero. Hot phases stop at the time budget even with a larger
`--max-batches`; repeat until the checkpoint records a completed sweep.

```sh
uv run market-storage compact --phase decision-inputs --state backfill --execute --batch-size 25
uv run market-storage compact --phase hot-options --state backfill --execute --batch-size 500 --max-batches 100
uv run market-storage compact --phase relative-values --state backfill --execute --batch-size 500 --max-batches 100
uv run market-storage compact --phase publications --state cutover --execute --batch-size 10 --backup-token "$BACKUP_SHA"
uv run market-storage verify --manifest-id ID
```

Inspect `/api/health/storage`: all nine reported large relations plus shared
input payloads appear in the inventory (partition children and TOAST included).
`database_volume` is explicitly measured, unavailable or unconfigured. Checkout
free space is labelled separately. Old partition age is an inventory metric,
not proof of missing archives: compact typed history intentionally stays local.
A forecast remains unavailable until real daily accounting samples exist.

## Restore and compatibility

Each `postgres-row-pack.v1` object decompresses to an ordered JSON array. Every
entry names its source relation, original column names/types, source database
identity, and `row_json`, the original PostgreSQL-rendered row as **text**.
Do not decode that inner text through Python floats. Recover selected packs to
an empty staging file with `market-storage restore --manifest-id ID
--destination /empty/staging/path`. On a scratch cluster at the recorded schema,
use `jsonb_populate_record(NULL::<relation>, <row_json>::jsonb)` with the recorded
column order, then restore in FK dependency order. Reconcile conflicts rather
than overwriting live rows. Tests round-trip typed rows, exact numeric values,
retries and corrupted files. This is an operator-controlled cold restore,
not a new transparent online historical query API.

Existing `publication-row.v1/v2` objects and COPY/pg_dump archives are still
valid and retain their documented restore contracts. Full backup restoration
is a separate disaster-recovery rehearsal; checksum verification alone is not
proof of a full-cluster restore.

## Physical space and acceptance

Normal VACUUM makes deleted/rewritten space reusable inside PostgreSQL; it
usually does not shrink relation files on the Mac. This PR never auto-runs
VACUUM FULL, REINDEX, a whole-cluster copy, or a non-empty partition DROP.
After logical cleanup, measure dead/live bytes and WAL/free space again. Use a
separately planned rewrite/verified restore onto a sufficiently sized local
volume only when physical reclamation is needed. NAS remains cold storage.

Before/after validation: unchanged decision IDs/input hashes/plans, historical
view JSON equivalence, unchanged executions/outcomes/P&L, latest capture and
publication readability with NAS unavailable, successful archive verification,
checkpoint advancement, and decreasing eligible payload/derived-row counts.
Run `make guards`, `make check`, `make release-gate`, then `make release-smoke`
and actual Today/Research/Learning/Paper UI checks on the deployed host.
Source/fixture validation does not establish NAS durability, production savings,
or runtime behavior on the Mac.

Rollback before backfill can downgrade revision 0036. Once input references
exist, downgrade refuses; first restore inline manifests through the expanded
read view in a separately budgeted maintenance operation. Once eligible rows
have been archived/deleted, rolling back code alone does not restore them.
Use the preserved packs/full backup and retain the newer archive readers.
