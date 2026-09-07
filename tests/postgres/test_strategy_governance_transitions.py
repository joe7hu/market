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


@pytest.mark.parametrize("earlier_attempt", [False, True])
def test_later_paper_attempt_keeps_old_shadow_episode_inside_rollback_window(
    migrated_postgres_dsn: str, earlier_attempt: bool,
) -> None:
    from investment_panel.database.strategy_learning import OUTCOME_QUERY, PAPER_EPISODE_ORDERS_SQL

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        clock = datetime(2026, 9, 4, 18, tzinfo=UTC)
        old_as_of, old_close = clock - timedelta(hours=3), clock - timedelta(hours=2)
        later_as_of, later_attempt = clock - timedelta(minutes=5), clock - timedelta(minutes=4)
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('LATE', 'Late attempt', 'equity') RETURNING id",
            ).fetchone()["id"]
            parent = connection.execute(
                """INSERT INTO analysis.strategy_revision (strategy_key, revision, name, status, authority_group, parameters)
                   VALUES ('late-parent', 1, 'Parent', 'superseded', 'options-radar-core', %s) RETURNING id""",
                [Jsonb({"contract_version": 3, "gates": {"max_spread_pct": .25}})],
            ).fetchone()["id"]
            active = connection.execute(
                """INSERT INTO analysis.strategy_revision
                   (strategy_key, revision, name, status, authority_group, supersedes_id, promoted_at, parameters)
                   VALUES ('late-active', 1, 'Active', 'active', 'options-radar-core', %s, %s, %s) RETURNING id""",
                [parent, old_as_of - timedelta(minutes=1), Jsonb({"contract_version": 3, "gates": {"max_spread_pct": .20}})],
            ).fetchone()["id"]

            def decision(key, episode, as_of, closed_at=None):
                run_id = connection.execute(
                    """INSERT INTO analysis.run
                       (run_type, input_cutoff, code_version, input_hash, started_at, status, strategy_revision_id)
                       VALUES ('options-radar', %s, 'clock-regression', %s, %s, 'succeeded', %s) RETURNING id""",
                    [as_of, "0" * 64, as_of, active],
                ).fetchone()["id"]
                decision_id = connection.execute(
                    """INSERT INTO analysis.decision
                       (run_id, instrument_id, decision_key, kind, state, as_of, input_hash,
                        strategy_revision_id, lane, episode_key, sample_eligible, calibration_cohort)
                       VALUES (%s, %s, %s, 'option', 'READY', %s, %s, %s, 'radar', %s, true,
                               'option-scorecard-truth-v1:radar') RETURNING id""",
                    [run_id, instrument_id, key, as_of, "1" * 64, active, episode],
                ).fetchone()["id"]
                if closed_at is not None:
                    shadow_id = connection.execute(
                        """INSERT INTO analysis.shadow_trade
                           (decision_id, status, source_kind, structure, entry_at, entry_price, exit_at, exit_price)
                           VALUES (%s, 'closed', 'system', 'long_call', %s, .5, %s, .45) RETURNING id""",
                        [decision_id, as_of + timedelta(seconds=1), closed_at],
                    ).fetchone()["id"]
                    connection.execute(
                        """INSERT INTO analysis.option_outcome
                           (decision_id, shadow_trade_id, maturity_state, observed_through, current_return,
                            promotion_eligible, outcome_classification, lane, episode_key, sample_eligible, calibration_cohort)
                           VALUES (%s, %s, 'mature', %s, -.10, true, 'captured', 'radar', %s,
                                   true, 'option-scorecard-truth-v1:radar')""",
                        [decision_id, shadow_id, closed_at, episode],
                    )
                return decision_id

            old = decision("old-shadow", "old-episode", old_as_of, old_close)
            for index in range(20):
                decision(f"intervening-{index}", f"episode-{index}",
                         clock - timedelta(minutes=100 - index), clock - timedelta(minutes=60 - index))
            late = decision("later-publication", "old-episode", later_as_of)
            if earlier_attempt:
                connection.execute(
                    """INSERT INTO app.paper_order
                       (decision_id, instrument_id, side, quantity, limit_price, status, paper_only, lane, created_at)
                       VALUES (%s, %s, 'buy', 1, .5, 'unfilled', true, 'radar', %s)""",
                    [old, instrument_id, old_as_of + timedelta(minutes=1)],
                )
            paper_id = connection.execute(
                """INSERT INTO app.paper_order
                   (decision_id, instrument_id, side, quantity, limit_price, status, paper_only, lane,
                    created_at, filled_at, actual_fill_price, filled_quantity, fees, entry_slippage, contract_multiplier)
                   VALUES (%s, %s, 'buy', 1, .5, 'entered', true, 'radar', %s, %s, .5, 1, .65, .01, 100) RETURNING id""",
                [late, instrument_id, later_attempt, later_attempt + timedelta(seconds=30)],
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO app.trade_journal
                   (decision_id, instrument_id, action, quantity, price, rationale, details, created_at)
                   VALUES (%s, %s, 'paper_entry', 1, .5, 'deterministic_options_paper_execution', %s, %s)""",
                [late, instrument_id, Jsonb({"paper_order_id": str(paper_id)}), later_attempt + timedelta(seconds=30)],
            )
        with runtime.read() as connection:
            # The open later decision has neither a completed paper outcome nor
            # a closed shadow. The old episode must carry its attempt clock.
            assert all(row["decision_id"] != str(late) for row in connection.execute(OUTCOME_QUERY, [active, active]).fetchall())
            counts = connection.execute(
                f"SELECT episode.* FROM analysis.decision decision CROSS JOIN LATERAL ({PAPER_EPISODE_ORDERS_SQL}) episode WHERE decision.id = %s",
                [old],
            ).fetchone()
            assert counts["paper_order_count"] == 1 + int(earlier_attempt)
            assert counts["first_paper_at"] == (old_as_of + timedelta(minutes=1) if earlier_attempt else later_attempt)
            assert counts["last_paper_at"] == later_attempt
            historical_sql = PAPER_EPISODE_ORDERS_SQL.replace("now()", "%s::timestamptz")
            before = connection.execute(
                f"SELECT episode.* FROM analysis.decision decision CROSS JOIN LATERAL ({historical_sql}) episode WHERE decision.id = %s",
                [later_attempt - timedelta(seconds=1), old],
            ).fetchone()
            assert before["paper_order_count"] == int(earlier_attempt)
            assert before["last_paper_at"] == (old_as_of + timedelta(minutes=1) if earlier_attempt else None)
        governance = StrategyGovernanceRepository(runtime)
        assert governance.rollback_regressing_active() == 0
        assert governance.rollback_regressing_active() == 0
        with runtime.read() as connection:
            assert connection.execute("SELECT status FROM analysis.strategy_revision WHERE id = %s", [active]).fetchone()["status"] == "active"
    finally:
        runtime.close()
