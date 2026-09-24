# Hot storage failure matrix (written before implementation)

The integration contract is PostgreSQL -> verified NAS pack -> bounded mutation
-> committed cursor, with ordinary readers never accessing the NAS.

1. Normalize repeated large manifest input groups without changing original
   JSONB, numerical precision, nulls, ordering, timestamps, input hashes, IDs,
   trade plans, outcomes, or context. Repeat/restart without creating duplicates.
   Reject an invalid content hash/reference and immutable-object mutation.
2. Process only a bounded prefix by both source rows and uncompressed bytes.
   Advance past protected rows; resume partial snapshots and reset completed
   sweeps. A row larger than the byte budget fails without advancing a cursor.
3. Archive complete original PostgreSQL row text, typed column metadata, schema
   revision and relation in deterministic multi-row packs. Reuse requires actual
   checksum/read-back verification. Restore with jsonb_populate_record on a
   scratch database, never a floating-point JSON round trip.
4. Missing/full NAS, corrupt reused packs, failed source mutation, failed cursor
   commit, and concurrent maintenance retain source data. Previously written
   verified packs may remain after rollback; retries can safely reuse them.
5. Preserve latest option captures, decision-referenced quotes, executable
   arbitrage evidence, verified relative values, published models, and all
   decision/execution/outcome/P&L records. Retain compact snapshot metadata.
6. Shorten only superseded/unlinked publication history, not current or
   decision-linked publications. Batch archive metadata instead of creating
   millions of one-row NAS objects.
7. Do not cascade-delete an entire analysis run merely because its age exceeds
   a threshold. Only empty run metadata is eligible for run deletion.
8. Register periodic bounded maintenance independently of full-market refresh.
   Expose progress/failure and measured database-volume headroom honestly.
9. A dry run does not create archive files, mutate source rows or advance
   checkpoints. Reclamation reports logical changes separately from OS bytes.

Run `tests/postgres/test_hot_storage_lifecycle.py`, existing storage/retention
and decision tests, architecture guards, and the full release gate. Production
NAS durability, real-size EXPLAIN plans and post-restart UI smoke remain separate
host-side acceptance checks; CI cannot establish them.
