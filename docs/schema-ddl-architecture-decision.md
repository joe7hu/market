# Schema DDL Architecture Decision

> Superseded on 2026-07-12 by the PostgreSQL authority migration. The live
> schema is defined only by Alembic revisions under `migrations/versions/`.
> This file is retained as historical migration evidence; it is not an active
> runtime or restore instruction. See [postgresql-migration.md](postgresql-migration.md).

## Decision

Use PostgreSQL schemas and Alembic revisions as the only application DDL owner.

## Context

The old embedded schema was removed. A second DDL assembly layer would create
ambiguity about the runtime authority.

The current codebase has a stronger need for one obvious migration source than
for a new DDL module graph. Read-model locality should be improved in the
accessor layer first, where table usage and product behavior live.

## Consequences

- Table definitions are discoverable through Alembic revision history.
- Fresh and upgrade-path migration tests are the schema contract.
- New tables belong to the PostgreSQL schema owner for their domain.
