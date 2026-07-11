# Quality gates for the Market codebase. See ARCHITECTURE.md → "Guardrails".
#
#   make check   - fast, deterministic pre-commit gate (guards + lint + typecheck)
#   make test    - full backend suite with ephemeral PostgreSQL fixtures
#   make coverage - migration-critical app/database coverage gate (80% minimum)
#   make guards  - architecture-convention tests only (size + facade imports)
#   make lint    - high-signal ruff rules (config in pyproject.toml [tool.ruff])
#
# `check` is intentionally green-or-bust and quick so it can run on every commit.
# The full backend suite uses ephemeral PostgreSQL fixtures; legacy-import tests
# open throwaway DuckDB files only to verify the one-time importer.

PY := uv run python
RUFF := uvx ruff

.PHONY: check guards lint typecheck test coverage build

check: guards lint typecheck
	@echo "✓ check passed"

guards:
	@echo "→ architecture guards (module size + facade imports)"
	@$(PY) -m pytest tests/test_architecture_guards.py tests/test_postgres_runtime_boundary.py -q

lint:
	@echo "→ ruff (high-signal rules)"
	@$(RUFF) check app src

typecheck:
	@if [ -x node_modules/.bin/tsc ]; then \
		echo "→ frontend typecheck"; \
		node_modules/.bin/tsc --noEmit; \
	else \
		echo "→ frontend typecheck skipped (run 'npm ci' to enable)"; \
	fi

test:
	@$(PY) -m pytest tests -q

coverage:
	@$(PY) -m pytest tests -q --cov=src/investment_panel/database --cov=app --cov-fail-under=80

build:
	@npm run build
