from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.strategy_governance import StrategyGovernanceRepository


def test_rollback_counts_independent_episodes_and_preserves_publication_history(
    migrated_postgres_dsn: str, application_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    try:
        repository = AnalysisRepository(runtime)
        governance = StrategyGovernanceRepository(runtime)
        now = datetime.now(UTC)
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('ROLL', 'Rollback', 'equity') RETURNING id",
            ).fetchone()["id"]
            parent_id = connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, authority_group, parameters)
                   VALUES ('rollback-parent', 1, 'Parent', 'active', 'options-radar-core', %s) RETURNING id""",
                [Jsonb({"contract_version": 3, "gates": {"max_spread_pct": .25}})],
            ).fetchone()["id"]
        parent_run = repository.start_run(
            "options-radar", input_cutoff=now - timedelta(days=2), code_version="test",
            inputs={"policy": "parent"}, strategy_revision_id=parent_id,
        )
        parent_publication = repository.publish(
            parent_run, "options-radar", {"option_radar_opportunity": [{"stable_key": "ROLL", "value": "parent"}]},
            strategy_root_key="options-radar-core",
        )
        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s", [parent_id])
            candidate_id = connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, authority_group, parameters, supersedes_id, promoted_at)
                   VALUES ('rollback-candidate', 1, 'Candidate', 'active', 'options-radar-core', %s, %s, %s) RETURNING id""",
                [Jsonb({"contract_version": 3, "gates": {"max_spread_pct": .20}}), parent_id, now - timedelta(days=1, seconds=1)],
            ).fetchone()["id"]
        candidate_run = repository.start_run(
            "options-radar", input_cutoff=now - timedelta(days=1), code_version="test",
            inputs={"policy": "candidate"}, strategy_revision_id=candidate_id,
        )
        candidate_publication = repository.publish(
            candidate_run, "options-radar", {"option_radar_opportunity": [{"stable_key": "ROLL", "value": "candidate"}]},
            strategy_root_key="options-radar-core",
        )

        def add_outcome(index: int, episode: str, *, outcome_episode: str | None = None):
            with runtime.transaction() as connection:
                decision_id = connection.execute(
                    """INSERT INTO analysis.decision
                       (run_id, instrument_id, decision_key, kind, state, as_of, input_hash,
                        strategy_revision_id, lane, episode_key, sample_eligible, calibration_cohort)
                       VALUES (%s, %s, %s, 'option', 'resolved', %s, %s, %s,
                               'radar', %s, true, 'option-scorecard-truth-v1:radar') RETURNING id""",
                    [candidate_run, instrument_id, f"rollback-{index}", now - timedelta(days=1) + timedelta(seconds=index),
                     "1" * 64, candidate_id, episode],
                ).fetchone()["id"]
                connection.execute(
                    """INSERT INTO analysis.option_outcome
                       (decision_id, maturity_state, observed_through, current_return,
                        promotion_eligible, outcome_classification, lane, episode_key,
                        sample_eligible, calibration_cohort)
                       VALUES (%s, 'mature', %s, -.10, true, 'captured', 'radar', %s,
                               true, 'option-scorecard-truth-v1:radar')""",
                    [decision_id, now - timedelta(hours=1), outcome_episode or episode],
                )

        for index in range(20):
            add_outcome(index, "episode-0")
        assert governance.rollback_regressing_active() == 0
        for index in range(1, 19):
            add_outcome(index + 20, f"episode-{index}")
        add_outcome(40, "episode-19", outcome_episode="different-episode")
        assert governance.rollback_regressing_active() == 0
        add_outcome(41, "episode-19")

        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.strategy_revision SET promoted_at = %s WHERE id = %s", [now, candidate_id])
        assert governance.rollback_regressing_active() == 0
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.strategy_revision SET promoted_at = %s WHERE id = %s",
                [now - timedelta(days=1, seconds=1), candidate_id],
            )

        def snapshot():
            with runtime.read() as connection:
                return {
                    "strategies": connection.execute(
                        "SELECT id, status FROM analysis.strategy_revision WHERE id IN (%s, %s) ORDER BY id",
                        [parent_id, candidate_id],
                    ).fetchall(),
                    "publications": connection.execute(
                        "SELECT id, status, published_at FROM app.publication WHERE id IN (%s, %s) ORDER BY id",
                        [parent_publication, candidate_publication],
                    ).fetchall(),
                    "current": connection.execute(
                        "SELECT publication_id FROM app.current_publication_item WHERE scope = 'options-radar'",
                    ).fetchall(),
                }

        before = snapshot()
        # An alert write failure occurs after policy/pointer changes. The whole
        # transaction must still roll back under the production application role.
        with psycopg.connect(migrated_postgres_dsn, autocommit=True) as connection:
            connection.execute("""
                CREATE FUNCTION public.reject_test_rollback_alert() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'forced rollback audit failure'; END; $$;
                CREATE TRIGGER reject_test_rollback_alert BEFORE INSERT ON app.alert
                FOR EACH ROW WHEN (NEW.alert_type = 'strategy_rollback')
                EXECUTE FUNCTION public.reject_test_rollback_alert();
            """)
        with pytest.raises(psycopg.errors.RaiseException, match="forced rollback audit failure"):
            governance.rollback_regressing_active()
        assert snapshot() == before
        with psycopg.connect(migrated_postgres_dsn, autocommit=True) as connection:
            connection.execute("DROP TRIGGER reject_test_rollback_alert ON app.alert; DROP FUNCTION public.reject_test_rollback_alert()")

        assert governance.rollback_regressing_active() == 1
        after = snapshot()
        assert [(row["id"], row["status"]) for row in after["strategies"]] == [(parent_id, "active"), (candidate_id, "rolled_back")]
        assert after["current"] == []
        assert all(row["status"] == "superseded" for row in after["publications"])
        assert {row["id"]: row["published_at"] for row in after["publications"]} == {
            row["id"]: row["published_at"] for row in before["publications"]
        }
        assert governance.rollback_regressing_active() == 0
        assert snapshot() == after
        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM app.alert WHERE alert_type = 'strategy_rollback'").fetchone()["count"] == 1
    finally:
        runtime.close()
