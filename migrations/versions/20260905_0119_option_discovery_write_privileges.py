"""Allow the options publisher to materialize discovery candidates and gates."""

from __future__ import annotations

from alembic import op


revision = "20260905_0119"
down_revision = "20260905_0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON analysis.option_discovery_run, "
        "analysis.option_discovery_candidate, analysis.option_gate_result TO market_app;"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON analysis.option_discovery_run, "
        "analysis.option_discovery_candidate, analysis.option_gate_result FROM market_app;"
    )
