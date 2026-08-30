from __future__ import annotations

from uuid import uuid4

from psycopg.types.json import Jsonb

from investment_panel.core.decision.governance import TRACKED_METRICS
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.strategy_governance import StrategyGovernanceRepository


def test_promotion_requires_walk_forward_shadow_and_execution_grade_paper(
    migrated_postgres_dsn: str,
) -> None:
    """BIG-A15: real paper governance evidence is required before promotion."""

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        key = f"phase7-{uuid4()}"
        with runtime.transaction() as connection:
            strategy_id = connection.execute(
                """
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, authority_group)
                VALUES (%s, 1, %s, 'candidate', %s, %s)
                RETURNING id
                """,
                [key, key, Jsonb({"phase": 7}), key],
            ).fetchone()["id"]
            metrics = {
                name: ({"risk_on": 0.5} if name == "regime_performance" else 0.1)
                for name in TRACKED_METRICS
            }
            for stage in ("walk_forward", "shadow", "execution_grade_paper"):
                connection.execute(
                    """
                    INSERT INTO analysis.strategy_evaluation
                        (strategy_revision_id, evaluation_type, evaluated_at,
                         period_start, period_end, verdict, metrics, evidence)
                    VALUES (%s, %s, clock_timestamp(), now() - interval '30 days',
                            now(), 'pass', %s, %s)
                    """,
                    [
                        strategy_id, stage, Jsonb(metrics),
                        Jsonb({"sample_size": 30, "source": "realized_paper_outcomes"}),
                    ],
                )
        result = StrategyGovernanceRepository(runtime).promotion_readiness(strategy_id)
        assert result["promotion_eligible"] is True
        assert result["paper_only"] is True
        assert result["live_eligibility"] == "unavailable"
    finally:
        runtime.close()
