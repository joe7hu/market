# Market

Personal investment panel: FastAPI backend (`app/`, `src/investment_panel/`) and
React/TypeScript frontend (`frontend/`). PostgreSQL 18 is the only runtime and
development-test data authority.

## Start here

**Read [ARCHITECTURE.md](ARCHITECTURE.md) before navigating or adding code** — it maps
where each responsibility lives and the conventions to follow.

Use the owner modules and change recipes in ARCHITECTURE.md. New code must not
add a storage fallback, dual-write path, dynamic compatibility export, or
direct router-to-database adapter import.

## Verify

- Backend focused tests: `uv run --extra test pytest tests/<file>.py -q`.
- Fast gate: `make check`.
- Frontend: `npm run test:frontend`, `npm run typecheck`, `npm run build`.
- Migration: run the affected Alembic tests on PostgreSQL 18.

The NAS retains historical migration evidence outside the application runtime.
The repository does not read, restore, or compare that evidence.

See also `AGENTS.md` for brain-wiring and project-boundary rules.
