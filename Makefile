# Quality gates for the Market codebase. See ARCHITECTURE.md → "Guardrails".
#
#   make check   - fast, deterministic pre-commit gate (guards + lint + typecheck)
#   make test    - full backend suite with ephemeral PostgreSQL fixtures
#   make coverage - migration-critical app/database coverage gate (80% minimum)
#   make guards  - architecture-convention tests only (interfaces + imports)
#   make lint    - high-signal ruff rules (config in pyproject.toml [tool.ruff])
#   make test-unit/test-api/test-options/test-postgres - focused behavior gates
#   make test-all - complete backend suite
#   make test-workflow - focused production-role and fixture-isolation checks
#   make test-migrations - raw-database migration/recovery checks
#   make release-gate - full release gate
#
# `check` is intentionally green-or-bust and quick so it can run on every commit.
# The full backend suite uses ephemeral PostgreSQL fixtures. Storage archive
# tests also enforce the configured free-space reserve.

PY := uv run --extra test python
RUFF := uvx ruff

.PHONY: check contracts guards lint frontend typecheck test test-unit test-api test-options test-postgres test-all coverage build test-workflow test-migrations release-gate

check: contracts guards lint frontend
	@echo "✓ check passed"

contracts:
	@echo "→ generated panel contract"
	@$(PY) scripts/generate_panel_contract.py --check
	@echo "→ generated OpenAPI contract"
	@npm --prefix frontend run check:api

guards:
	@echo "→ architecture guards (interfaces + facade imports)"
	@$(PY) -m pytest tests/contracts/test_architecture_guards.py tests/contracts/test_postgres_runtime_boundary.py -q

lint:
	@echo "→ ruff (high-signal rules)"
	@$(RUFF) check src tests

frontend:
	@echo "→ frontend Vitest"
	@npm --prefix frontend run test:frontend
	@echo "→ frontend typecheck"
	@npm --prefix frontend run typecheck

typecheck:
	@npm --prefix frontend run typecheck

test: test-all

test-unit:
	@$(PY) -m pytest tests/contracts tests/providers tests/test_*.py -q

test-api:
	@$(PY) -m pytest tests/application_api tests/contracts/test_openapi_contract.py tests/contracts/test_postgres_runtime_boundary.py -q

test-options:
	@$(PY) -m pytest tests/options tests/test_option*.py tests/test_options*.py tests/test_strategy_parameters.py -q

test-postgres:
	@$(PY) -m pytest tests/postgres tests/options -q

test-workflow:
	@$(PY) -m pytest -q tests/postgres/test_application_workflows.py tests/postgres/test_fixture_isolation.py tests/postgres/test_phase2_stock_alpha.py::test_distinct_noinherit_login_is_the_only_runtime_activation_boundary tests/postgres/test_postgres_phase4_portfolio.py::test_manual_funding_capacity_replays_cash_after_snapshot_and_reversal tests/test_wait_for_job.py

test-migrations:
	@$(PY) -m pytest tests/postgres/test_postgres_foundation.py -q

test-all:
	@$(PY) -m pytest tests -q

coverage:
	@$(PY) -m pytest tests -q --cov=src/investment_panel/infrastructure/postgres --cov=src/investment_panel/api --cov-fail-under=80

build:
	@npm --prefix frontend run build

release-gate: check build
	@$(PY) -m pytest tests -q --run-slow --cov=src/investment_panel/infrastructure/postgres --cov=src/investment_panel/api --cov-fail-under=80
	@git diff --check
