# Database maintenance

Market uses one PostgreSQL baseline (`20260906_0001`). The 131 older migrations
remain in Git history. The baseline keeps account records, research evidence,
signing keys, and paper-only controls. It removes only the obsolete review-page
cache and two indexes already covered by unique constraints.

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

For the existing `20260906_0131` database, the runner checks the exact audited
catalog, roles, owners, and grants before adoption. Unknown drift is refused.
Adoption repairs grants, adds two query indexes, restores the funding guard,
removes the obsolete cache and duplicate indexes, and records the baseline in
one transaction. Earlier revisions need the old checkout to reach `0131` first.
Never stamp an unknown schema or reset a database that contains needed records.

For an empty database, the baseline creates the schema and small configuration
seed. Provision the application login first; a `market_app` login can instead
be created with `MARKET_APP_DATABASE_PASSWORD` (at least 16 characters).
A separate login must have safe role attributes and membership in `market_app`.
Configure signing keys through the existing secret settings when required.

## Keep future changes small

Add short forward Alembic revisions after the baseline and update
`HEAD_REVISION` in the migration runner. Keep one definition of each fact;
reference it from derived records. Preserve history needed to explain decisions.
Do not remove constraints or privilege checks to simplify a migration.

Review lists use bounded keyset pages with a creation cutoff and expiring
cursors. Values can change between pages. Options reads select the latest
eligible capture before reading its quotes; an empty latest capture stays empty.
New provider archives use deterministic content-addressed blobs, so retries reuse
the same bytes. Existing archives stay intact for provenance.

Validate schema changes on an empty database and, when needed, an upgrade
fixture. Use bounded `EXPLAIN (ANALYZE, BUFFERS)` checks for changed read paths;
save plans to disk and report timing and spill counts instead of fact rows.
