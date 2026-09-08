# Database maintenance

Market uses one PostgreSQL baseline (`20260907_0006`). It is a current-state,
data-free schema snapshot. It does not replay historical migrations and it does
not delete data. Older migration files remain in Git history for audit only.

## Apply a schema change

1. Make and verify a database backup. Stop the API and scheduled writers for the
   maintenance window.
2. Set `MARKET_APP_LOGIN_ROLE` to the application login. Set
   `MARKET_DATABASE_URL` to the separate migration-owner connection.
3. Run `uv run market-db-migrate` from the intended checkout.
4. Check the schema revision and affected API paths, then restart writers.

The runner takes one database-wide migration lock and sets bounded lock and
statement timeouts. Do not run Alembic directly. A failed transaction leaves the
previous schema intact and can be retried after its cause is fixed.

An existing database already at `20260907_0006` is left unchanged when the
runner is repeated. Its rows, secrets, roles, and grants are not rewritten.
Databases that still record an archived revision fail closed; use the matching
old checkout to reach `20260907_0006` first. Never stamp an unknown schema or
reset a database that contains needed records.

For an empty database, the baseline creates the schema and small configuration
seed. Provision the application login first; a `market_app` login can instead
be created with `MARKET_APP_DATABASE_PASSWORD` (at least 16 characters).
A separate login must have safe role attributes and membership in `market_app`.
Configure signing keys through the existing secret settings when required.

## Keep future snapshots small

When the schema changes, apply the required data-preserving maintenance SQL to
existing databases, then regenerate the one snapshot from a verified current
schema and update the revision marker. Do not add historical `ALTER` statements
to the snapshot. The snapshot is for empty databases and cannot safely update
an older nonempty schema by itself. Keep one definition of each fact; reference
it from derived records. Preserve history needed to explain decisions.

Review lists use bounded keyset pages with a creation cutoff and expiring
cursors. Values can change between pages. Options reads select the latest
eligible capture before reading its quotes; an empty latest capture stays empty.
New provider archives use deterministic content-addressed blobs, so retries reuse
the same bytes. Existing archives stay intact for provenance.

Validate schema changes on an empty database and, when needed, an upgrade
fixture. Use bounded `EXPLAIN (ANALYZE, BUFFERS)` checks for changed read paths;
save plans to disk and report timing and spill counts instead of fact rows.
