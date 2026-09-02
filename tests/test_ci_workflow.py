"""CI workflow contracts that keep the PostgreSQL test authority initialized."""

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
TCP_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


def test_market_ci_migrates_the_tcp_database_before_the_phase_zero_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    migration_step = "      - name: Migrate CI PostgreSQL\n        run: uv run market-db-migrate"
    gate_step = "      - name: Run Phase 0 gate\n        run: make phase0-gate"

    assert f"MARKET_DATABASE_URL: {TCP_DSN}" in workflow
    assert migration_step in workflow
    assert workflow.index(migration_step) < workflow.index(gate_step)
