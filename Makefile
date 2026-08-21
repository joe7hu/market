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
# use the ephemeral PostgreSQL fixture for storage-bound verification.

PY := uv run python
RUFF := uvx ruff

.PHONY: check contracts guards lint frontend typecheck test coverage build

check: contracts guards lint frontend
	@echo "✓ check passed"

contracts:
	@echo "→ generated panel contract"
	@$(PY) scripts/generate_panel_contract.py --check
	@echo "→ generated OpenAPI contract"
	@npm run check:api

guards:
	@echo "→ architecture guards (module size + facade imports)"
	@$(PY) -m pytest tests/test_architecture_guards.py tests/test_postgres_runtime_boundary.py -q

lint:
	@echo "→ ruff (high-signal rules)"
	@$(RUFF) check app src

frontend:
	@echo "→ frontend Vitest"
	@npm run test:frontend
	@echo "→ frontend typecheck"
	@npm run typecheck

typecheck: frontend

test:
	@$(PY) -m pytest tests -q

coverage:
	@$(PY) -m pytest tests -q --cov=src/investment_panel/database --cov=app --cov-fail-under=80

build:
	@npm run build
