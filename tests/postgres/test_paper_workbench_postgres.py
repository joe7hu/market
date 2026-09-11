from uuid import uuid4

from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.paper_workbench import (
    PaperWorkbenchRepository,
)
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


def test_paper_workbench_empty_scope_has_truthful_empty_state(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = PaperWorkbenchRepository(runtime)
        page = repository.trades(limit=100)
        performance = repository.performance()
    finally:
        runtime.close()

    assert page["rows"] == []
    assert page["counts"]["total"] == 0
    assert performance["counts"]["total_orders"] == 0
    assert performance["net_pnl"] == 0.0
    assert performance["nav"] is None
    assert "opening_capital_unavailable" in performance["missing_evidence_reasons"]


def test_paper_workbench_uses_journal_fills_and_keeps_staged_limits_out_of_pnl(
    migrated_postgres_dsn,
):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    order_ids = []
    try:
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, f"WB{uuid4().hex[:8]}")
            staged_id = connection.execute(
                """INSERT INTO app.paper_order
                   (instrument_id, side, quantity, limit_price, status, paper_only, structure)
                   VALUES (%s, 'buy', 1, 99, 'staged', true, 'equity') RETURNING id""",
                [instrument_id],
            ).fetchone()["id"]
            filled_id = connection.execute(
                """INSERT INTO app.paper_order
                   (instrument_id, side, quantity, limit_price, status, paper_only, structure)
                   VALUES (%s, 'buy', 1, 10, 'exited', true, 'equity') RETURNING id""",
                [instrument_id],
            ).fetchone()["id"]
            order_ids = [str(staged_id), str(filled_id)]
            connection.cursor().executemany(
                """INSERT INTO app.trade_journal
                   (instrument_id, action, quantity, price, rationale, details)
                   VALUES (%s, %s, 1, %s, 'deterministic_options_paper_execution', %s)""",
                [
                    (
                        instrument_id,
                        "paper_entry",
                        10,
                        Jsonb({"paper_order_id": str(filled_id), "fees": 0.5}),
                    ),
                    (
                        instrument_id,
                        "paper_exit:take_profit",
                        12,
                        Jsonb({"paper_order_id": str(filled_id), "fees": 0.5}),
                    ),
                ],
            )
        repository = PaperWorkbenchRepository(runtime)
        rows = repository.trades(limit=100)["rows"]
        by_id = {row["paper_order_id"]: row for row in rows}
        first_page = repository.trades(limit=1)
        next_page = repository.trades(
            limit=1,
            cursor=(
                first_page["rows"][0]["staged_at"],
                first_page["rows"][0]["paper_order_id"],
            ),
        )
        assert first_page["counts"]["total"] == 2
        assert next_page["counts"]["total"] == 2
        assert next_page["counts"]["eligible"] == 2
        assert by_id[order_ids[0]]["entry_price"] is None
        assert by_id[order_ids[0]]["net_pnl"] is None
        assert by_id[order_ids[1]]["entry_price"] == 10
        assert by_id[order_ids[1]]["realized_pnl"] == 1.0
        assert by_id[order_ids[1]]["net_pnl"] == 1.0
    finally:
        with runtime.transaction() as connection:
            if order_ids:
                connection.execute(
                    "DELETE FROM app.trade_journal WHERE details->>'paper_order_id' = ANY(%s)",
                    [order_ids],
                )
                connection.execute(
                    "DELETE FROM app.paper_order WHERE id = ANY(%s::uuid[])",
                    [order_ids],
                )
        runtime.close()
