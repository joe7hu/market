from __future__ import annotations

from contextlib import closing, nullcontext
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import CheckViolation, RaiseException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from investment_panel.core.portfolio import (
    allocation_id_for_snapshot,
    build_execution_model_snapshot,
    canonical_content_hash,
    cash_only_allocation,
)
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
            "allocation:" + "a" * 64, AS_OF + timedelta(seconds=1), [],
        )
        PortfolioLoopRepository.store_execution_model(connection, model)
        stored = connection.execute(
            "SELECT content_hash FROM analysis.execution_model_snapshot "
            "WHERE execution_model_snapshot_id = %s",
            [model.execution_model_snapshot_id],
        ).fetchone()
        assert stored is not None
        assert str(stored["content_hash"]).strip() == canonical_content_hash(model)


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
