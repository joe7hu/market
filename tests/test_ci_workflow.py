"""CI workflow contracts that keep the PostgreSQL test authority initialized."""

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
TCP_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/postgres"


def test_market_ci_migrates_the_tcp_database_before_the_phase_zero_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job_environment = workflow.split("    steps:", maxsplit=1)[0]
    bootstrap_step = (
        "      - name: Bootstrap and migrate CI PostgreSQL\n"
        "        # CI has no deployment-provided application login. Create one with\n"
        "        # an ephemeral password, then let the role-boundary migrations verify\n"
        "        # its attributes, membership, protected-role reachability, and ACLs.\n"
        "        env:\n"
        f"          MARKET_DATABASE_URL: {TCP_DSN}\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        "          ci_password=\"$(openssl rand -hex 32)\"\n"
        "          PGPASSWORD=postgres psql \"$MARKET_DATABASE_URL\" -v ON_ERROR_STOP=1 \\\n"
        "            -c \"CREATE ROLE market_ci_login LOGIN PASSWORD '$ci_password' NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION\"\n"
        "          export MARKET_APP_LOGIN_ROLE=market_ci_login\n"
        "          export MARKET_APP_DATABASE_PASSWORD=\"$ci_password\"\n"
        "          uv run market-db-migrate\n"
    )
    gate_step = "      - name: Run Phase 0 gate\n        run: make phase0-gate"

    assert "MARKET_DATABASE_URL" not in job_environment
    assert bootstrap_step in workflow
    assert workflow.index(bootstrap_step) < workflow.index(gate_step)
