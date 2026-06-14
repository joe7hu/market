# Quality gates for the Market codebase. See ARCHITECTURE.md → "Guardrails".
#
#   make check   - fast, deterministic pre-commit gate (guards + lint + typecheck)
#   make test    - full backend test suite on a throwaway DuckDB
#   make guards  - architecture-convention tests only (size + facade imports)
#   make lint    - high-signal ruff rules (config in pyproject.toml [tool.ruff])
#
# `check` is intentionally green-or-bust and quick so it can run on every commit.
# The full backend suite lives in `test` because it needs a throwaway DuckDB and
# carries a few path-sensitive failures (see the target below).

PY := .venv/bin/python
# Throwaway DB: the real DuckDB is single-writer, so a live dev `uvicorn --reload`
# would otherwise deadlock the suite (see ARCHITECTURE.md → "Verify changes").
CHECK_DB := /tmp/market-check.duckdb
RUFF := uvx ruff

.PHONY: check guards lint typecheck test

check: guards lint typecheck
	@echo "✓ check passed"

guards:
	@echo "→ architecture guards (module size + facade imports)"
	@MARKET_DUCKDB_PATH=$(CHECK_DB) $(PY) -m pytest tests/test_architecture_guards.py -q

lint:
	@echo "→ ruff (high-signal rules)"
	@$(RUFF) check app src

typecheck:
	@if [ -x frontend/node_modules/.bin/tsc ]; then \
		echo "→ frontend typecheck"; \
		cd frontend && node_modules/.bin/tsc --noEmit; \
	else \
		echo "→ frontend typecheck skipped (run 'npm ci' in frontend/ to enable)"; \
	fi

# Full backend suite. Known: ~4 tests assert the default DuckDB path / fixture
# ordering and fail under the throwaway DB — pre-existing, not a regression gate.
test:
	@MARKET_DUCKDB_PATH=$(CHECK_DB) $(PY) -m pytest tests -q
