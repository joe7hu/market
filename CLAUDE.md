# Market

Personal investment panel: FastAPI backend (`app/`, `src/investment_panel/`) +
React/TypeScript frontend (`frontend/`), DuckDB storage.

## Start here

**Read [ARCHITECTURE.md](ARCHITECTURE.md) before navigating or adding code** — it maps
where each responsibility lives and the conventions to follow.

Key rule: the backend's former god-modules are now **facade packages**
(`core/panel/`, `app/data_access/`, `core/decision/`, `core/brokers/`,
`core/free_sources/`). Import from the package; add new responsibility submodules and
re-export them from the package `__init__.py` rather than growing a single large file.

## Verify

- Backend tests: `.venv/bin/python -m pytest tests/<file>.py -q` (run focused suites first).
- Frontend typecheck: `node_modules/.bin/tsc --noEmit`.
- See ARCHITECTURE.md → "Verify changes" for the shared-DuckDB lock caveat on full runs.

See also `AGENTS.md` for brain-wiring and project-boundary rules.
