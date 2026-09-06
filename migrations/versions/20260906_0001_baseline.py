"""Verified personal-app baseline; older migration history is preserved in Git."""

from pathlib import Path

from alembic import op

from migrations.baseline_support import install_secrets, prepare_roles
from migrations.baseline_contract import BASELINE_SCHEMA_HASHES
from migrations.schema_contract import schema_hash


revision = "20260906_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    prepare_roles(connection)
    root = Path(__file__).resolve().parents[1]
    connection.connection.driver_connection.execute((root / "baseline.sql").read_text())
    connection.connection.driver_connection.execute((root / "baseline_seed.sql").read_text())
    connection.connection.driver_connection.execute("GRANT SELECT ON public.alembic_version TO market_app")
    connection.connection.driver_connection.execute("""
        REVOKE ALL ON analysis.research_evaluator_signing_secret,
            analysis.phase4_allocation_signing_secret FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON FUNCTION analysis.research_evaluator_signing_key(),
            analysis.phase4_allocation_signing_key() FROM PUBLIC, market_app, market_migrator;
    """)
    if schema_hash(connection.connection.driver_connection) not in BASELINE_SCHEMA_HASHES:
        raise RuntimeError("fresh schema failed baseline verification; check owner default privileges")
    install_secrets(connection)


def downgrade() -> None:
    op.execute("DROP SCHEMA app, analysis, raw, ingest, catalog, ops CASCADE")
