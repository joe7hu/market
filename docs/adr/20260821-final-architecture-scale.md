# ADR: Final architecture and scale lifecycle

Date: 2026-08-21  
Status: accepted

## Decision

Market remains a single-owner PostgreSQL 18 application. PostgreSQL is the
only runtime authority. Compact fact-availability projections are the only
point-in-time selector seam for price bars and quotes. Confirmation tables are
bounded ingestion staging: successful and partial runs are projected, terminal
staging rows are removed, and failed rows remain for a 30-day audit window.

Raw option quotes use a seven-day hot window. New partitions are daily from
the first non-overlapping boundary after the legacy monthly partition. Older
immutable partitions are custom-format `pg_dump` artifacts on the NAS. A
manifest records bounds, row count, byte count, checksums, dump-list hash,
and scratch-restore status. Detach and drop require a verified backup token,
verified manifest, capacity, and no conflicting database activity. NAS data
is never read by normal APIs.

Analytical detail is recomputable and owned by `analysis.run`. Unreferenced
runs and their strict derived children are retained for 30 days. Publications,
outcomes, calibration observations, paper evidence, journals, and pinned
research evidence protect their source rows. Option detail retains quote
identity values without a raw-partition foreign key so 30-day evidence can
outlive seven-day raw hot storage.

The scheduler has one fixed capacity budget of two. Fast deterministic ticks
run in-process through `asyncio.to_thread`; long collectors and provider or
agent work remain isolated subprocesses. Duplicate due ticks are not queued.

## Recovery and gates

Every destructive storage command is dry-run by default. Execution requires a
verified backup or archive token. Cutover stops when coverage is below 100%
for eligible facts, a writer is active, a manifest or restore check fails, or
the local/NAS capacity reserve is not available. A failed gate leaves the
source partition or staging relation attached.

The rollback path is explicit: restore a verified NAS custom dump into a
scratch database first, then use a forward migration or controlled recovery
operation. Alembic does not copy or rewrite the large confirmation relations.

Architecture work remains frozen for 90 days. Reopen it only for measured
archive lag over 24 hours, more than eight complete hot option days, local
free space below 30 GiB, availability below 100%, scheduler work above two,
repeated API statement timeouts, or a real second implementation that earns a
new seam.
