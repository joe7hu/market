from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import CheckViolation, RaiseException


AS_OF = datetime(2026, 9, 2, 15, tzinfo=UTC)


def insert_cash_allocation(connection: psycopg.Connection) -> None:
    allocation_id = "allocation:" + "a" * 64
    connection.execute(
        """INSERT INTO analysis.portfolio_allocation_snapshot
           (allocation_id, as_of, input_cutoff, status, cash_hurdle, input_hash, content_hash)
           VALUES (%s, %s, %s, 'cash_only', 0, %s, %s)""",
        [allocation_id, AS_OF, AS_OF, "a" * 64, "b" * 64],
    )
    connection.execute(
        """INSERT INTO analysis.portfolio_allocation_item
           (allocation_item_id, allocation_id, ticker, disposition, target_weight,
            marginal_book_utility, trace, content_hash)
           VALUES (%s, %s, 'CASH', 'selected', 1, 0, '{}'::jsonb, %s)""",
        ["allocation-item:" + "b" * 64, allocation_id, "c" * 64],
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
            "INSERT INTO app.paper_order (id, instrument_id, side, quantity, limit_price, status, created_at) VALUES (%s, %s, 'buy', 1, 100, 'staged', %s)",
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
                   (paper_execution_observation_id, paper_order_id, execution_mode, paper_only, status,
                    requested_quantity, filled_quantity, observed_at)
                   VALUES ('observation:bad', %s, 'live', false, 'submitted', 1, 0, %s)""",
                [paper_order_id, AS_OF],
            )
        connection.rollback()
        connection.execute(
            """INSERT INTO app.paper_execution_observation
               (paper_execution_observation_id, paper_order_id, execution_mode, paper_only, status,
                requested_quantity, filled_quantity, observed_at)
               VALUES ('observation:good', %s, 'paper', true, 'submitted', 1, 0, %s)""",
            [paper_order_id, AS_OF],
        )
        connection.commit()
        assert connection.execute(
            "SELECT paper_only, execution_mode FROM app.paper_execution_observation WHERE paper_execution_observation_id = 'observation:good'"
        ).fetchone() == (True, "paper")


def test_phase4_database_requires_pit_equal_cutoff_and_positive_funded_utility(migrated_postgres_dsn: str) -> None:
    with closing(psycopg.connect(migrated_postgres_dsn)) as connection:
        with pytest.raises(CheckViolation):
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
