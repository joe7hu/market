# Quality gates for the Market codebase. See ARCHITECTURE.md → "Guardrails".
#
#   make check   - fast, deterministic pre-commit gate (guards + lint + typecheck)
#   make test    - full backend suite with ephemeral PostgreSQL fixtures
#   make coverage - migration-critical app/database coverage gate (80% minimum)
#   make guards  - architecture-convention tests only (interfaces + imports)
#   make lint    - high-signal ruff rules (config in pyproject.toml [tool.ruff])
#   make test-unit/test-api/test-options/test-postgres - focused behavior gates
#   make test-all - complete backend suite
#
# `check` is intentionally green-or-bust and quick so it can run on every commit.
# The full backend suite uses ephemeral PostgreSQL fixtures. Storage archive
# tests also enforce the configured free-space reserve.

PY := uv run python
RUFF := uvx ruff

.PHONY: check contracts guards lint frontend typecheck test test-unit test-api test-options test-postgres test-all coverage build

check: contracts guards lint frontend
	@echo "✓ check passed"

contracts:
	@echo "→ generated panel contract"
	@$(PY) scripts/generate_panel_contract.py --check
	@echo "→ generated OpenAPI contract"
	@npm run check:api

guards:
	@echo "→ architecture guards (interfaces + facade imports)"
	@$(PY) -m pytest tests/contracts/test_architecture_guards.py tests/contracts/test_postgres_runtime_boundary.py -q

lint:
	@echo "→ ruff (high-signal rules)"
	@$(RUFF) check app src tests

frontend:
	@echo "→ frontend Vitest"
	@npm run test:frontend
	@echo "→ frontend typecheck"
	@npm run typecheck

typecheck: frontend

test: test-all

test-unit:
	@$(PY) -m pytest tests/contracts tests/providers tests/test_*.py -q

test-api:
	@$(PY) -m pytest tests/application_api tests/contracts/test_openapi_contract.py tests/contracts/test_postgres_runtime_boundary.py -q

test-options:
	@$(PY) -m pytest tests/options tests/test_option*.py tests/test_options*.py tests/test_strategy_parameters.py -q

test-postgres:
	@$(PY) -m pytest tests/postgres tests/options -q

test-all:
	@$(PY) -m pytest tests -q

coverage:
	@$(PY) -m pytest tests -q --cov=src/investment_panel/database --cov=app --cov-fail-under=80

build:
	@npm run build
