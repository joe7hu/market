from __future__ import annotations

from contextlib import closing, nullcontext
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import CheckViolation, RaiseException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from investment_panel.core.decision.alpha import build_strategy_forecast
from investment_panel.core.portfolio import (
    allocation_id_for_snapshot,
    build_execution_model_snapshot,
    canonical_content_hash,
    cash_only_allocation,
    execution_model_id_for_snapshot,
    PaperExecutionObservation,
)
from investment_panel.database.migrations import downgrade_database, upgrade_database
from investment_panel.database.portfolio import PortfolioLoopRepository


AS_OF = datetime(2026, 9, 2, 15, tzinfo=UTC)


def insert_cash_allocation(connection: psycopg.Connection) -> None:
    allocation_id = "allocation:" + "a" * 64
    connection.execute(
        """INSERT INTO ingest.source (id, name, family, kind) VALUES ('phase4-cash', 'cash', 'test', 'test')
           ON CONFLICT (id) DO NOTHING"""
    )
    run_id = connection.execute(
        """INSERT INTO ingest.run (source_id, capability, started_at, status)
           VALUES ('phase4-cash', 'test', %s, 'succeeded') RETURNING id""", [AS_OF]
    ).fetchone()
    run_id = run_id["id"] if isinstance(run_id, dict) else run_id[0]
    account_id = connection.execute(
        """INSERT INTO raw.broker_account_snapshot
           (source_id, ingest_run_id, account_key, observed_at, net_liquidation, cash_balance)
           VALUES ('phase4-cash', %s, %s, %s, 100, 100) RETURNING id""",
        [run_id, f"paper-{run_id}", AS_OF],
    ).fetchone()
    account_id = account_id["id"] if isinstance(account_id, dict) else account_id[0]
    connection.execute(
        """INSERT INTO analysis.portfolio_allocation_snapshot
           (allocation_id, as_of, input_cutoff, status, cash_hurdle, input_hash, content_hash, metadata)
           VALUES (%s, %s, %s, 'cash_only', 0, %s, %s,
                   %s)""",
        [allocation_id, AS_OF, AS_OF, "a" * 64, "b" * 64,
         Jsonb({"authority": "postgresql", "authority_snapshot_id": f"broker-account:{account_id}", "source_hashes": ["a" * 64]})],
    )
    connection.execute(
        """INSERT INTO analysis.portfolio_allocation_item
           (allocation_item_id, allocation_id, ticker, action_id, disposition, target_weight,
            marginal_book_utility, trace, input_hash, content_hash)
           VALUES (%s, %s, 'CASH', 'action:compatibility', 'selected', 1, 0, '{}'::jsonb, %s, %s)""",
        ["allocation-item:" + "b" * 64, allocation_id, "b" * 64, "c" * 64],
    )
    connection.commit()


def test_phase4_artifacts_are_immutable_and_paper_only(migrated_postgres_dsn: str) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn)) as connection:
        insert_cash_allocation(connection)
        instrument_id = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('P4TEST', 'P4TEST', 'equity') RETURNING id"
        ).fetchone()[0]
        paper_order_id = uuid4()
        connection.execute(
            """INSERT INTO app.paper_order
               (id, instrument_id, side, quantity, limit_price, status, created_at, policy_result)
               VALUES (%s, %s, 'buy', 1, 100, 'staged', %s, '{\"trade_plan_id\": \"action:compatibility\"}'::jsonb)""",
            [paper_order_id, instrument_id, AS_OF - timedelta(seconds=1)],
        )
        connection.commit()
        with pytest.raises(RaiseException):
            connection.execute("UPDATE analysis.portfolio_allocation_snapshot SET status = 'unavailable'")
        connection.rollback()
        with pytest.raises(RaiseException):
            connection.execute("DELETE FROM analysis.portfolio_allocation_item")
        connection.rollback()
        with pytest.raises(RaiseException):
            connection.execute(
                """INSERT INTO analysis.probabilistic_portfolio_scenario_artifact
                   (scenario_artifact_id, allocation_id, model_version, probability_semantics,
                    scenarios, tail_dependence, simultaneous_unwind, input_cutoff, input_hash)
                   VALUES ('scenario:bad', %s, 'v1', 'normalized', '[{"probability": 1}]', '{}', '{}', %s, %s)""",
                ["allocation:" + "a" * 64, AS_OF.replace(hour=14), "d" * 64],
            )
        connection.rollback()
        with pytest.raises((CheckViolation, RaiseException)):
                connection.execute(
                    """INSERT INTO app.paper_execution_observation
                       (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id,
                        execution_mode, paper_only, status,
                        requested_quantity, filled_quantity, observed_at)
                       VALUES ('observation:bad', %s, %s, %s, 'live', false, 'submitted', 1, 0, %s)""",
                    ["allocation-item:" + "b" * 64, "action:compatibility", paper_order_id, AS_OF],
                )
        connection.rollback()
        with pytest.raises(RaiseException, match="genuine paper fill"):
            connection.execute(
                """INSERT INTO app.paper_execution_observation
                   (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id,
                    execution_mode, paper_only, status, requested_quantity, filled_quantity,
                    fill_price, observed_at)
                   VALUES ('observation:unfilled', %s, %s, %s, 'paper', true, 'filled', 1, 1, 100, %s)""",
                ["allocation-item:" + "b" * 64, "action:compatibility", paper_order_id, AS_OF],
            )
        connection.rollback()
        connection.execute(
            """INSERT INTO app.paper_execution_observation
               (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id,
                execution_mode, paper_only, status,
                requested_quantity, filled_quantity, observed_at)
               VALUES ('observation:good', %s, %s, %s, 'paper', true, 'submitted', 1, 0, %s)""",
            ["allocation-item:" + "b" * 64, "action:compatibility", paper_order_id, AS_OF],
        )
        connection.commit()
        assert connection.execute(
            "SELECT paper_only, execution_mode FROM app.paper_execution_observation WHERE paper_execution_observation_id = 'observation:good'"
        ).fetchone() == (True, "paper")


def test_phase4_database_requires_pit_equal_cutoff_and_positive_funded_utility(migrated_postgres_dsn: str) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn)) as connection:
        with pytest.raises((CheckViolation, RaiseException)):
            connection.execute(
                """INSERT INTO analysis.portfolio_allocation_snapshot
                       (allocation_id, as_of, input_cutoff, status, cash_hurdle, input_hash, content_hash)
                       VALUES ('allocation:bad', %s, %s, 'available', 0, %s, %s)""",
                [AS_OF, AS_OF.replace(hour=14), "c" * 64, "d" * 64],
            )
        connection.rollback()

        insert_cash_allocation(connection)
        with pytest.raises((CheckViolation, RaiseException)):
            connection.execute(
                """INSERT INTO analysis.portfolio_allocation_item
                   (allocation_item_id, allocation_id, ticker, disposition, target_weight,
                    marginal_book_utility, trace)
                   VALUES ('allocation-item:bad', %s, 'XYZ', 'selected', 0.1, 0, '{}'::jsonb)""",
                ["allocation:" + "a" * 64],
            )
        connection.rollback()


def test_execution_model_store_matches_postgresql_canonical_digest(migrated_postgres_dsn: str) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        insert_cash_allocation(connection)
        model = build_execution_model_snapshot(
            "allocation:" + "a" * 64, AS_OF + timedelta(seconds=1, microseconds=10), [],
        )
        PortfolioLoopRepository.store_execution_model(connection, model)
        stored = connection.execute(
            "SELECT content_hash FROM analysis.execution_model_snapshot "
            "WHERE execution_model_snapshot_id = %s",
            [model.execution_model_snapshot_id],
        ).fetchone()
        assert stored is not None
        assert str(stored["content_hash"]).strip() == canonical_content_hash(model)


def test_phase4_execution_writer_rejects_forged_self_hashed_metrics(migrated_postgres_dsn: str) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        insert_cash_allocation(connection)
        model = build_execution_model_snapshot(
            "allocation:" + "a" * 64, AS_OF + timedelta(seconds=1), [],
        )
        forged_id = execution_model_id_for_snapshot(model.model_copy(update={"fill_probability": 0.99}))
        forged = model.model_copy(update={"fill_probability": 0.99, "execution_model_snapshot_id": forged_id})
        payload = forged.model_dump(mode="json") | {
            "input_hash": forged_id.split(":", 1)[1],
            "content_hash": canonical_content_hash(forged),
        }
        with pytest.raises(RaiseException, match="metrics|derived|canonical"):
            connection.execute(
                "SELECT analysis.write_phase4_execution(%s, %s)",
                [Jsonb(payload), PortfolioLoopRepository._telemetry_signature(connection, "phase4-execution.v1", payload)],
            )
        connection.rollback()


def test_repository_persists_genuine_paper_fill_and_database_calibrates_it(
    migrated_postgres_dsn: str,
) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        insert_cash_allocation(connection)
        instrument_id = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('P4FILL', 'P4FILL', 'equity') RETURNING id"
        ).fetchone()["id"]
        order_id = uuid4()
        submitted_at = AS_OF + timedelta(seconds=1)
        filled_at = AS_OF + timedelta(seconds=61)
        fill_evidence_at = AS_OF + timedelta(seconds=91)
        quote = {"bid": 99.0, "ask": 101.0, "spread": 2.0}
        connection.execute(
            """INSERT INTO app.paper_order
               (id, instrument_id, created_at, side, quantity, limit_price, intended_limit_price, status,
                paper_only, submitted_at, filled_quantity, filled_at, actual_fill_price,
                fill_evidence_at, execution_quote, fees, entry_fees, entry_slippage,
                contract_multiplier, policy_result)
               VALUES (%s, %s, %s, 'buy', 1, 100, 100, 'entered', true, %s, 1, %s, 100.5,
                       %s, %s, .25, .25, .5, 100, %s)""",
            [order_id, instrument_id, AS_OF, submitted_at, filled_at, fill_evidence_at, Jsonb(quote),
             Jsonb({"trade_plan_id": "action:compatibility"})],
        )
        observation = PaperExecutionObservation(
            paper_execution_observation_id="observation:genuine-fill",
            allocation_item_id="allocation-item:" + "b" * 64,
            action_id="action:compatibility", paper_order_id=str(order_id),
            status="filled", requested_quantity=1, filled_quantity=1,
            requested_price=100, fill_price=100.5, spread_bps=999, latency_ms=999,
            impact_bps=999, observed_at=filled_at, available_at=fill_evidence_at,
            metadata={"fees": .25, "paper_order_id": str(order_id), "contract_multiplier": 100,
                      "submitted_at": submitted_at, "filled_at": filled_at, "quote": quote},
        )

        class Runtime:
            def transaction(self):
                return nullcontext(connection)

        repository = PortfolioLoopRepository(Runtime())
        assert repository.record_paper_execution(observation) == observation.paper_execution_observation_id
        stored_observation = connection.execute(
            """SELECT filled_quantity, fill_price, spread_bps, latency_ms, impact_bps,
                              event_fee, contract_multiplier, available_at
               FROM app.paper_execution_observation
               WHERE paper_execution_observation_id = %s""",
            [observation.paper_execution_observation_id],
        ).fetchone()
        assert stored_observation["filled_quantity"] == 1
        assert float(stored_observation["fill_price"]) == pytest.approx(100.5)
        assert float(stored_observation["spread_bps"]) == pytest.approx(200)
        assert float(stored_observation["latency_ms"]) == pytest.approx(60_000)
        assert float(stored_observation["impact_bps"]) == pytest.approx(50)
        assert float(stored_observation["event_fee"]) == pytest.approx(.25)
        model = connection.execute(
            """SELECT calibration_status, sample_count, fill_probability, spread_bps,
                              latency_ms, impact_bps, input_cutoff, metadata
               FROM analysis.execution_model_snapshot
               WHERE allocation_id = %s""",
            ["allocation:" + "a" * 64],
        ).fetchone()
        assert model["calibration_status"] == "calibrated"
        assert model["sample_count"] == 1
        assert float(model["fill_probability"]) == pytest.approx(1)
        assert float(model["spread_bps"]) == pytest.approx(200)
        assert float(model["latency_ms"]) == pytest.approx(60_000)
        assert float(model["impact_bps"]) == pytest.approx(50)
        assert model["input_cutoff"] == fill_evidence_at
        assert model["metadata"]["paper_observation_ids"] == [observation.paper_execution_observation_id]


def test_repository_calibrates_from_every_item_in_the_persisted_allocation(
    migrated_postgres_dsn: str,
) -> None:
    """The production recording path must use the complete allocation denominator."""

    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        insert_cash_allocation(connection)
        allocation_id = "allocation:" + "a" * 64
        second_item_id = "allocation-item:" + "c" * 64
        connection.execute(
            """INSERT INTO analysis.portfolio_allocation_item
               (allocation_item_id, allocation_id, candidate_id, ticker, action_id,
                disposition, target_weight, current_weight, marginal_book_utility,
                trace, input_hash, content_hash)
               VALUES (%s, %s, 'CASH-SECOND', 'CASH', 'action:compatibility',
                       'selected', .01, 0, 0, '{}'::jsonb, %s, %s)""",
            [second_item_id, allocation_id, "c" * 64, "d" * 64],
        )

        observations: list[PaperExecutionObservation] = []
        for index, item_id in enumerate(("allocation-item:" + "b" * 64, second_item_id)):
            instrument_id = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity') RETURNING id",
                [f"P4MULTI{index}", f"P4MULTI{index}"],
            ).fetchone()["id"]
            order_id = uuid4()
            submitted_at = AS_OF + timedelta(seconds=1 + index)
            filled_at = AS_OF + timedelta(seconds=61 + index * 60)
            fill_evidence_at = AS_OF + timedelta(seconds=91 + index * 60)
            requested_price = 100 + index
            quote = {"bid": requested_price - 1, "ask": requested_price + 1, "spread": 2.0}
            connection.execute(
                """INSERT INTO app.paper_order
                   (id, instrument_id, created_at, side, quantity, limit_price, intended_limit_price, status,
                    paper_only, submitted_at, filled_quantity, filled_at, actual_fill_price,
                    fill_evidence_at, execution_quote, fees, entry_fees, entry_slippage,
                    contract_multiplier, policy_result)
                   VALUES (%s, %s, %s, 'buy', 1, %s, %s, 'entered', true, %s, 1, %s, %s,
                           %s, %s, .25, .25, .5, 100, %s)""",
                [
                    order_id, instrument_id, AS_OF, requested_price, requested_price,
                    submitted_at, filled_at, requested_price + 0.5, fill_evidence_at,
                    Jsonb(quote), Jsonb({"trade_plan_id": "action:compatibility"}),
                ],
            )
            observations.append(PaperExecutionObservation(
                paper_execution_observation_id=f"observation:multi-{index}",
                allocation_item_id=item_id,
                action_id="action:compatibility",
                paper_order_id=str(order_id),
                status="filled",
                requested_quantity=1,
                filled_quantity=1,
                requested_price=requested_price,
                fill_price=requested_price + 0.5,
                spread_bps=999,
                latency_ms=999,
                impact_bps=999,
                observed_at=filled_at,
                available_at=fill_evidence_at,
                metadata={
                    "fees": .25,
                    "paper_order_id": str(order_id),
                    "contract_multiplier": 100,
                    "submitted_at": submitted_at,
                    "filled_at": filled_at,
                    "quote": quote,
                },
            ))
        connection.commit()

        class Runtime:
            def transaction(self):
                return nullcontext(connection)

        repository = PortfolioLoopRepository(Runtime())
        assert repository.record_paper_execution(observations[0]) == observations[0].paper_execution_observation_id
        assert repository.record_paper_execution(observations[1]) == observations[1].paper_execution_observation_id
        model = connection.execute(
            """SELECT calibration_status, sample_count, spread_bps, latency_ms, impact_bps,
                              input_cutoff, metadata
               FROM analysis.execution_model_snapshot
               WHERE allocation_id = %s
               ORDER BY input_cutoff DESC, execution_model_snapshot_id DESC
               LIMIT 1""",
            [allocation_id],
        ).fetchone()
        assert model["calibration_status"] == "calibrated"
        assert model["sample_count"] == 2
        assert float(model["spread_bps"]) == pytest.approx((200 + (2 / 101 * 10_000)) / 2)
        assert float(model["latency_ms"]) == pytest.approx(89_500)
        assert float(model["impact_bps"]) == pytest.approx((50 + (0.5 / 101 * 10_000)) / 2)
        assert model["input_cutoff"] == AS_OF + timedelta(seconds=151)
        assert model["metadata"]["paper_observation_ids"] == [
            observations[0].paper_execution_observation_id,
            observations[1].paper_execution_observation_id,
        ]
        connection.commit()

        stale_cutoff = build_execution_model_snapshot(
            allocation_id, AS_OF + timedelta(seconds=151), observations,
        )
        stale_cutoff = stale_cutoff.model_copy(update={"input_cutoff": AS_OF + timedelta(seconds=91)})
        stale_cutoff = stale_cutoff.model_copy(update={
            "execution_model_snapshot_id": execution_model_id_for_snapshot(stale_cutoff),
        })
        with pytest.raises(RaiseException, match="maximum allocation observation availability"):
            repository.store_execution_model(connection, stale_cutoff)
        connection.rollback()

        incomplete = build_execution_model_snapshot(
            allocation_id, AS_OF + timedelta(seconds=151), [observations[1]],
        )
        with pytest.raises(RaiseException, match="complete eligible allocation fill set"):
            repository.store_execution_model(connection, incomplete)
        connection.rollback()

        forged = build_execution_model_snapshot(
            "allocation:" + "f" * 64, AS_OF + timedelta(seconds=151), observations,
        )
        with pytest.raises(RaiseException, match="not persisted"):
            repository.store_execution_model(connection, forged)
        connection.rollback()


def test_phase4_application_role_cannot_directly_write_forged_authority(migrated_postgres_dsn: str) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn)) as connection:
        assert connection.execute(
            "SELECT has_table_privilege('market_app', 'analysis.portfolio_allocation_snapshot', 'INSERT')"
        ).fetchone()[0] is False
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.insert_phase4_allocation_snapshot(jsonb)', 'EXECUTE')"
        ).fetchone()[0] is False
        assert connection.execute(
            "SELECT has_function_privilege('public', 'analysis.insert_phase4_allocation_snapshot(jsonb)', 'EXECUTE')"
        ).fetchone()[0] is False
        with pytest.raises(RaiseException, match="signature"):
            connection.execute(
                "SELECT analysis.write_phase4_allocation(%s, '[]'::jsonb, 'forged')",
                [Jsonb({"allocation_id": "allocation:" + "a" * 64})],
            )
        connection.rollback()
        with pytest.raises(RaiseException, match="authority"):
            connection.execute(
                """INSERT INTO analysis.portfolio_allocation_snapshot
                   (allocation_id, as_of, input_cutoff, status, cash_hurdle, input_hash, content_hash, metadata)
                   VALUES (%s, %s, %s, 'available', .01, %s, %s,
                           '{"authority":"postgresql","authority_snapshot_id":"broker-account:999999","source_hashes":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}'::jsonb)""",
                ["allocation:" + "c" * 64, AS_OF, AS_OF, "c" * 64, "d" * 64],
            )
        connection.rollback()


def test_repository_persists_and_replays_a_postgresql_owned_cash_allocation(migrated_postgres_dsn: str) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        connection.execute("INSERT INTO ingest.source (id, name, family, kind) VALUES ('phase4-test', 'test', 'test', 'test')")
        run_id = connection.execute(
            """INSERT INTO ingest.run (source_id, capability, started_at, status)
               VALUES ('phase4-test', 'test', %s, 'succeeded') RETURNING id""", [AS_OF]
        ).fetchone()["id"]
        account_id = connection.execute(
            """INSERT INTO raw.broker_account_snapshot
               (source_id, ingest_run_id, account_key, observed_at, net_liquidation, cash_balance)
               VALUES ('phase4-test', %s, 'paper', %s, 100, 100) RETURNING id""", [run_id, AS_OF]
        ).fetchone()["id"]
        allocation = cash_only_allocation(AS_OF, 0.01, "test")
        allocation = allocation.model_copy(update={"metadata": {
            "authority": "postgresql", "authority_snapshot_id": f"broker-account:{account_id}",
            "source_hashes": [],
        }})
        allocation = allocation.model_copy(update={"allocation_id": allocation_id_for_snapshot(allocation)})

        class Runtime:
            def transaction(self): return nullcontext(connection)

        repository = PortfolioLoopRepository(Runtime())
        assert repository.store_allocation(allocation) == allocation.allocation_id
        assert repository.store_allocation(allocation) == allocation.allocation_id
        connection.commit()
        assert connection.execute(
            "SELECT count(*) AS count FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = %s",
            [allocation.allocation_id],
        ).fetchone()["count"] == 1


def test_phase4_source_and_calibration_migration_round_trip_restores_permissions(
    migrated_postgres_dsn: str,
) -> None:
    """The repair migration must return to the exact 0076 writer boundary."""

    downgrade_database(migrated_postgres_dsn, "20260904_0076")
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260904_0076"
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.insert_phase4_execution(text,text,text,text,integer,double precision,double precision,double precision,double precision,timestamptz,text,text,jsonb)', 'EXECUTE')"
        ).fetchone()["has_function_privilege"] is False
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.write_phase4_execution(jsonb,text)', 'EXECUTE')"
        ).fetchone()["has_function_privilege"] is True
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.insert_phase4_paper_execution_observation(jsonb)', 'EXECUTE')"
        ).fetchone()["has_function_privilege"] is False
        assert connection.execute(
            "SELECT has_column_privilege('market_app', 'analysis.portfolio_allocation_item', 'funding_sources', 'SELECT')"
        ).fetchone()["has_column_privilege"] is True
        assert connection.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'app.paper_execution_observation'::regclass AND conname = 'paper_execution_observation_status_check'"
        ).fetchone()["pg_get_constraintdef"].find("partial_exited") == -1

    upgrade_database(migrated_postgres_dsn)
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260905_0078"
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.write_phase4_execution(jsonb,text)', 'EXECUTE')"
        ).fetchone()["has_function_privilege"] is True
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.write_phase4_execution_0077(jsonb,text)', 'EXECUTE')"
        ).fetchone()["has_function_privilege"] is False
        assert connection.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'app.paper_execution_observation'::regclass AND conname = 'phase4_paper_observation_status'"
        ).fetchone()["pg_get_constraintdef"].find("partial_exited") >= 0

    downgrade_database(migrated_postgres_dsn, "20260904_0075")
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260904_0075"
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.insert_phase4_execution(text,text,text,text,integer,double precision,double precision,double precision,double precision,timestamptz,text,text,jsonb)', 'EXECUTE')"
        ).fetchone()["has_function_privilege"] is True
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.insert_phase4_paper_execution_observation(jsonb)', 'EXECUTE')"
        ).fetchone()["has_function_privilege"] is False

    upgrade_database(migrated_postgres_dsn)
    downgrade_database(migrated_postgres_dsn, "20260904_0074")
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260904_0074"
        assert connection.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'analysis' AND table_name = 'portfolio_allocation_item' AND column_name = 'funding_sources'"
        ).fetchone()["count"] == 0
        assert connection.execute(
            "SELECT has_function_privilege('market_app', 'analysis.insert_phase4_allocation_item(jsonb)', 'EXECUTE')"
        ).fetchone()["has_function_privilege"] is True

    upgrade_database(migrated_postgres_dsn)


def test_repository_persists_and_replays_cash_plus_two_trim_sources_with_conservation(
    migrated_postgres_dsn: str,
) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn, row_factory=dict_row)) as connection:
        source_id = "phase4-funding"
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES (%s, 'funding test', 'test', 'test')",
            [source_id],
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) VALUES (%s, 'test', %s, 'succeeded') RETURNING id",
            [source_id, AS_OF - timedelta(minutes=5)],
        ).fetchone()["id"]
        account_id = connection.execute(
            """INSERT INTO raw.broker_account_snapshot
               (source_id, ingest_run_id, account_key, observed_at, net_liquidation, cash_balance)
               VALUES (%s, %s, 'paper-funding', %s, 1, .001) RETURNING id""",
            [source_id, run_id, AS_OF],
        ).fetchone()["id"]
        allocation_id = "allocation:" + "a" * 64
        connection.execute(
            """INSERT INTO analysis.portfolio_allocation_snapshot
               (allocation_id, as_of, input_cutoff, status, cash_hurdle, input_hash, content_hash, metadata)
               VALUES (%s, %s, %s, 'cash_only', 0, %s, %s, %s)""",
            [allocation_id, AS_OF, AS_OF, "a" * 64, "b" * 64,
             Jsonb({"authority": "postgresql", "authority_snapshot_id": f"broker-account:{account_id}",
                    "source_hashes": ["a" * 64]})],
        )
        connection.execute(
            """INSERT INTO analysis.portfolio_allocation_item
               (allocation_item_id, allocation_id, ticker, action_id, disposition, target_weight,
                marginal_book_utility, trace, input_hash, content_hash)
               VALUES (%s, %s, 'CASH', 'action:compatibility', 'selected', 1, 0, '{}'::jsonb, %s, %s)""",
            ["allocation-item:" + "b" * 64, allocation_id, "b" * 64, "c" * 64],
        )
        instrument_ids = {}
        for ticker in ("TRIMA", "TRIMB", "TRIMC", "FUNDED"):
            instrument_ids[ticker] = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity') RETURNING id",
                [ticker, ticker],
            ).fetchone()["id"]
        position_ids = {}
        for ticker in ("TRIMA", "TRIMB", "TRIMC"):
            position_ids[ticker] = connection.execute(
                """INSERT INTO raw.broker_position_snapshot
                   (account_snapshot_id, instrument_id, quantity, market_value)
                   VALUES (%s, %s, 1, .1) RETURNING id""",
                [account_id, instrument_ids[ticker]],
            ).fetchone()["id"]
        hypothesis_id = connection.execute(
            """INSERT INTO analysis.hypothesis
               (hypothesis_key, statement, mechanism_class, falsification, input_hash, created_at, available_at)
               VALUES ('phase4-funding', 'funding test', 'test', 'test', %s, %s, %s) RETURNING id""",
            ["1" * 64, AS_OF - timedelta(minutes=5), AS_OF - timedelta(minutes=5)],
        ).fetchone()["id"]
        revision_id = connection.execute(
            """INSERT INTO analysis.strategy_revision
               (strategy_key, revision, name, status, parameters, authority_group, hypothesis_id)
               VALUES ('phase4-funding', 1, 'funding test', 'draft', '{}'::jsonb, 'phase4-funding', %s) RETURNING id""",
            [hypothesis_id],
        ).fetchone()["id"]
        # The research authority trigger requires generated and available
        # timestamps to be actual, same-day observations.  Keep the source
        # forecast inside the target allocation's PIT window instead of
        # fabricating a historical availability timestamp.
        forecast_time = datetime.now(UTC) - timedelta(seconds=1)
        forecast = build_strategy_forecast(
            ticker="FUNDED", opportunity_episode_id="episode:phase4-funding",
            strategy_revision_id=revision_id, strategy_evaluation_id=None,
            target="expected_return", horizon="TACTICAL", forecast_value=.15,
            model_artifact_id="artifact:phase4-funding", artifact_hash="3" * 64,
            input_hash="4" * 64, as_of=forecast_time, generated_at=forecast_time,
            available_at=forecast_time,
        )
        forecast_id = forecast.strategy_forecast_id
        action_id = "action:phase4-funding"
        rank_id = "rank:phase4-funding"
        decision_hash = "2" * 64
        connection.execute(
            """INSERT INTO analysis.strategy_forecast
               (id, strategy_revision_id, instrument_id, opportunity_episode_id, target, horizon,
                forecast_value, model_artifact_id, artifact_hash, input_hash, as_of, input_cutoff,
                generated_at, available_at)
               VALUES (%s, %s, %s, 'episode:phase4-funding', 'expected_return', 'TACTICAL', .15,
                       'artifact:phase4-funding', %s, %s, %s, %s, %s, %s)""",
            [forecast_id, revision_id, instrument_ids["FUNDED"], "3" * 64, "4" * 64,
             forecast_time, forecast_time, forecast_time, forecast_time],
        )
        decision_id = connection.execute(
            """INSERT INTO analysis.ticker_decision
               (instrument_id, decision_revision, contract_version, as_of, published_at,
                input_hash, code_version, experiment_id, tactical, fundamental, capital_action,
                risk_policy, input_manifest, status)
               VALUES (%s, 'phase4-funding', 'test.v1', %s, %s, %s, 'test', 'experiment:phase4-funding',
                       '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, %s, 'published') RETURNING id""",
            [instrument_ids["FUNDED"], AS_OF - timedelta(minutes=2), AS_OF - timedelta(minutes=2),
             decision_hash, Jsonb({"trade_plan": {
                 "trade_plan_id": action_id, "rank_id": rank_id, "strategy_forecast_id": forecast_id,
             }})],
        ).fetchone()["id"]

        calibration_order_id = uuid4()
        submitted_at = AS_OF + timedelta(seconds=1)
        filled_at = AS_OF + timedelta(seconds=61)
        fill_evidence_at = AS_OF + timedelta(seconds=91)
        calibration_quote = {"bid": 99.0, "ask": 101.0, "spread": 2.0}
        connection.execute(
            """INSERT INTO app.paper_order
               (id, instrument_id, created_at, side, quantity, limit_price, intended_limit_price, status,
                paper_only, submitted_at, filled_quantity, filled_at, actual_fill_price,
                fill_evidence_at, execution_quote, fees, entry_fees, entry_slippage,
                contract_multiplier, policy_result)
               VALUES (%s, %s, %s, 'buy', 1, 100, 100, 'entered', true, %s, 1, %s, 100.5,
                       %s, %s, .25, .25, .5, 100, %s)""",
            [calibration_order_id, instrument_ids["FUNDED"], AS_OF, submitted_at, filled_at,
             fill_evidence_at, Jsonb(calibration_quote), Jsonb({"trade_plan_id": "action:compatibility"})],
        )
        calibration_observation = PaperExecutionObservation(
            paper_execution_observation_id="observation:phase4-funding-calibration",
            allocation_item_id="allocation-item:" + "b" * 64,
            action_id="action:compatibility", paper_order_id=str(calibration_order_id), status="filled",
            requested_quantity=1, filled_quantity=1, requested_price=100, fill_price=100.5,
            observed_at=filled_at, available_at=fill_evidence_at,
            metadata={"fees": .25, "paper_order_id": str(calibration_order_id),
                      "contract_multiplier": 100, "submitted_at": submitted_at, "filled_at": filled_at,
                      "quote": calibration_quote},
        )

        class CalibrationRuntime:
            def transaction(self):
                return nullcontext(connection)

        repository = PortfolioLoopRepository(CalibrationRuntime())
        assert repository.record_paper_execution(calibration_observation) == calibration_observation.paper_execution_observation_id
        connection.commit()
        execution_model_id = connection.execute(
            "SELECT execution_model_snapshot_id FROM analysis.execution_model_snapshot WHERE allocation_id = %s",
            ["allocation:" + "a" * 64],
        ).fetchone()["execution_model_snapshot_id"]
        target_as_of = datetime.now(UTC) + timedelta(minutes=1)

        def make_item(
            item_id: str, ticker: str, disposition: str, target: float, current: float,
            utility: float, funding_source: str | None, funding_amount: float | None,
            funding_sources: dict[str, float], trace_extra: dict[str, object],
        ) -> object:
            from investment_panel.core.portfolio import PortfolioAllocationItem

            return PortfolioAllocationItem(
                allocation_item_id=item_id, candidate_id=ticker, ticker=ticker,
                strategy_forecast_id=forecast_id, action_id=action_id, rank_id=rank_id,
                hypothesis_id=str(hypothesis_id), disposition=disposition,
                target_weight=target, current_weight=current, marginal_book_utility=utility,
                funding_source=funding_source, funding_amount=funding_amount,
                funding_sources=funding_sources,
                trace={
                    "source_decision_id": str(decision_id),
                    "source_decision_input_hash": decision_hash,
                    "source_input_hash": decision_hash,
                    "trim_position_id": trace_extra.get("trim_position_id"),
                    "expression": {"kind": "stock", "ticker": ticker},
                    **trace_extra,
                },
            )

        trim_a_source = f"TRIM:broker-position:{position_ids['TRIMA']}"
        trim_b_source = f"TRIM:broker-position:{position_ids['TRIMB']}"
        cash_source = f"CASH:broker-account:{account_id}"
        items = (
            make_item("allocation-item:" + "a" * 64, "TRIMA", "rollback", 0, .1, .02,
                      trim_a_source, .1, {trim_a_source: .1}, {"trim_position_id": f"broker-position:{position_ids['TRIMA']}"}),
            make_item("allocation-item:" + "7" * 64, "TRIMB", "rollback", 0, .1, .03,
                      trim_b_source, .1, {trim_b_source: .1}, {"trim_position_id": f"broker-position:{position_ids['TRIMB']}"}),
            make_item("allocation-item:" + "8" * 64, "FUNDED", "selected", .15, 0, .13,
                      "MULTI_SOURCE", .15, {cash_source: .001, trim_a_source: .0745, trim_b_source: .0745}, {}),
            make_item("allocation-item:" + "9" * 64, "CASH", "selected", .85, 0, 0,
                      "CASH", .85, {}, {}),
        )
        base = {
            "as_of": target_as_of, "input_cutoff": target_as_of, "cash_hurdle": .01, "status": "available",
            "items": items, "forecast_ids": (forecast_id,), "action_ids": (action_id,),
            "strategy_registry_ids": (),
            "metadata": {
                "authority": "postgresql", "authority_snapshot_id": f"broker-account:{account_id}",
                "authority_content_hash": "5" * 64, "constraint_hash": "6" * 64,
                "execution_status": "calibrated", "execution_model_snapshot_id": execution_model_id,
                "source_hashes": [decision_hash],
            },
        }
        allocation_id = allocation_id_for_snapshot(base)
        from investment_panel.core.portfolio import PortfolioAllocationSnapshot

        allocation = PortfolioAllocationSnapshot.model_validate(base | {"allocation_id": allocation_id})

        assert repository.store_allocation(allocation) == allocation_id
        connection.commit()
        stored = connection.execute(
            "SELECT funding_source, funding_amount, funding_sources FROM analysis.portfolio_allocation_item WHERE allocation_item_id = %s",
            ["allocation-item:" + "8" * 64],
        ).fetchone()
        assert stored["funding_source"] == "MULTI_SOURCE"
        assert stored["funding_sources"] == {cash_source: 0.001, trim_a_source: 0.0745, trim_b_source: 0.0745}
        assert float(stored["funding_amount"]) == pytest.approx(.15)
        assert repository.store_allocation(allocation) == allocation_id
        connection.commit()
        assert connection.execute(
            "SELECT count(*) AS count FROM analysis.portfolio_allocation_item WHERE allocation_id = %s",
            [allocation_id],
        ).fetchone()["count"] == 4

        with pytest.raises(RaiseException, match="over|allocated|released"):
            connection.execute(
                """INSERT INTO analysis.portfolio_allocation_item
                   (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id,
                    action_id, rank_id, hypothesis_id, disposition, target_weight, current_weight,
                    marginal_book_utility, trace, funding_source, funding_amount, funding_sources,
                    input_hash, content_hash)
                   VALUES (%s, %s, 'OVERDRAW', 'FUNDED', %s, %s, %s, %s, 'selected', .15, 0,
                           .13, %s, %s, .001, %s, %s, %s)""",
                ["allocation-item:" + "e" * 64, allocation_id, forecast_id, action_id, rank_id,
                 hypothesis_id, Jsonb({"source_decision_id": str(decision_id),
                                       "source_decision_input_hash": decision_hash,
                                       "trim_position_id": None, "expression": {"kind": "stock"}}),
                 cash_source, Jsonb({cash_source: .001}), "e" * 64, "f" * 64],
            )
            connection.commit()
        connection.rollback()

        trim_c_source = f"TRIM:broker-position:{position_ids['TRIMC']}"
        with pytest.raises(RaiseException, match="over|released"):
            connection.execute(
                """INSERT INTO analysis.portfolio_allocation_item
                   (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id,
                    action_id, rank_id, hypothesis_id, disposition, target_weight, current_weight,
                    marginal_book_utility, trace, funding_source, funding_amount, funding_sources,
                    input_hash, content_hash)
                   VALUES (%s, %s, 'TRIMC', 'TRIMC', %s, %s, %s, %s, 'rollback', 0, .1,
                           .01, %s, %s, .11, %s, %s, %s)""",
                ["allocation-item:" + ("0" * 63 + "1"), allocation_id, forecast_id, action_id, rank_id,
                 hypothesis_id, Jsonb({"source_decision_id": str(decision_id),
                                       "source_decision_input_hash": decision_hash,
                                       "trim_position_id": f"broker-position:{position_ids['TRIMC']}"}),
                 trim_c_source, Jsonb({trim_c_source: .11}), "0" * 63 + "1", "2" * 64],
            )
            connection.commit()
        connection.rollback()

        with pytest.raises(RaiseException, match="conserve|funding_amount"):
            connection.execute(
                """INSERT INTO analysis.portfolio_allocation_item
                   (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id,
                    action_id, rank_id, hypothesis_id, disposition, target_weight, current_weight,
                    marginal_book_utility, trace, funding_source, funding_amount, funding_sources,
                    input_hash, content_hash)
                   VALUES (%s, %s, 'PARTIAL', 'FUNDED', %s, %s, %s, %s, 'selected', .15, 0,
                           .13, %s, %s, .15, %s, %s, %s)""",
                ["allocation-item:" + "f" * 64, allocation_id, forecast_id, action_id, rank_id,
                 hypothesis_id, Jsonb({"source_decision_id": str(decision_id),
                                       "source_decision_input_hash": decision_hash,
                                       "trim_position_id": None, "expression": {"kind": "stock"}}),
                 cash_source, Jsonb({cash_source: .1}), "f" * 64, "1" * 64],
            )
        connection.rollback()
