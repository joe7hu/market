# ADR: PostgreSQL-only runtime

Date: 2026-08-20

## Decision

PostgreSQL 18 is the only Market runtime and development-test data authority.
The main repository does not support an embedded database import, fallback,
dual-write, restore, or comparison path. Alembic owns all live schema changes.

Historical migration snapshots and reports remain on the NAS as immutable
evidence. They are outside the application runtime and are not deleted by this
decision.

## Consequences

- New persistence and tests use PostgreSQL owners and the ephemeral PostgreSQL
  fixture.
- Storage configuration, package extras, console scripts, and implementation
  tests cannot add a second store.
- Evidence retention is an operational archive concern, not a repository API.
- The architecture guard and production-wheel import check prevent the old seam
  from returning.
