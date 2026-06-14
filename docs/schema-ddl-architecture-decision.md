# Schema DDL Architecture Decision

## Decision

Keep the canonical DuckDB schema in `src/investment_panel/core/schema.py` as one
DDL string applied by `src/investment_panel/core/db.py:init_db`.

## Context

`core/schema.py` is large, and per-table files would improve table-local
navigation. That split would also create a second schema assembly layer and
contradict the repository convention documented in `ARCHITECTURE.md`.

The current codebase has a stronger need for one obvious migration source than
for a new DDL module graph. Read-model locality should be improved in the
accessor layer first, where table usage and product behavior live.

## Consequences

- Table definitions remain discoverable through one canonical file.
- `init_db` continues to apply a single schema artifact.
- Reviews should not request a schema split for file-size reasons alone.
- Revisit this only when schema changes need table-level migration ordering,
  generated schemas, or isolated ownership by domain.

