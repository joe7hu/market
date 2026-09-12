from datetime import UTC, date, datetime, timedelta
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


def test_paper_workbench_uses_confirmed_stock_mark_for_open_pnl(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    order_id = None
    source_id = f"wb-mark-{uuid4().hex[:10]}"
    now = datetime.now(UTC)
    try:
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, f"WB{uuid4().hex[:8]}")
            connection.execute(
                """INSERT INTO ingest.source
                   (id, name, family, kind, operational_state, health_owner, freshness_seconds)
                   VALUES (%s, %s, 'test', 'quote', 'active', 'test', 3600)""",
                [
                    source_id,
                    source_id,
                ],
            )
            run_id = connection.execute(
                """INSERT INTO ingest.run
                   (source_id, capability, started_at, finished_at, status)
                   VALUES (%s, 'quotes', %s, %s, 'succeeded') RETURNING id""",
                [source_id, now - timedelta(hours=2), now - timedelta(hours=1)],
            ).fetchone()["id"]
            quote = connection.execute(
                """INSERT INTO raw.quote
                   (instrument_id, source_id, ingest_run_id, observed_at, price, available_at)
                   VALUES (%s, %s, %s, %s, 12, %s) RETURNING id, available_at""",
                [
                    instrument_id,
                    source_id,
                    run_id,
                    now - timedelta(hours=1),
                    now - timedelta(minutes=50),
                ],
            ).fetchone()
            connection.execute(
                """INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id)
                   VALUES (%s, %s, %s)""",
                [quote["id"], quote["available_at"], run_id],
            )
            order_id = connection.execute(
                """INSERT INTO app.paper_order
                   (instrument_id, side, quantity, limit_price, status, paper_only, structure)
                   VALUES (%s, 'buy', 2, 10, 'entered', true, 'equity') RETURNING id""",
                [instrument_id],
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO app.trade_journal
                   (instrument_id, action, quantity, price, rationale, details)
                   VALUES (%s, 'paper_entry', 2, 10, 'deterministic_options_paper_execution', %s)""",
                [instrument_id, Jsonb({"paper_order_id": str(order_id), "fees": 1})],
            )

        repository = PaperWorkbenchRepository(runtime)
        row = repository.trade(str(order_id))
        performance = repository.performance()
        assert row is not None
        assert row["mark_status"] == "verified"
        assert row["mark_price"] == 12.0
        assert row["mark_value"] == 24.0
        assert row["unrealized_pnl"] == 3.0
        assert performance["unrealized_pnl"] == 3.0
        assert performance["evidence_coverage"]["mark_coverage"] == 1.0

        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.source SET enabled = false WHERE id = %s",
                [source_id],
            )
        disabled_row = repository.trade(str(order_id))
        assert disabled_row is not None
        assert disabled_row["mark_status"] == "unavailable"
        assert disabled_row["mark_value"] is None
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.source SET enabled = true WHERE id = %s",
                [source_id],
            )
    finally:
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.source SET enabled = true WHERE id = %s",
                [source_id],
            )
            if order_id is not None:
                connection.execute(
                    "DELETE FROM app.trade_journal WHERE details->>'paper_order_id' = %s",
                    [str(order_id)],
                )
                connection.execute(
                    "DELETE FROM app.paper_order WHERE id = %s::uuid", [str(order_id)]
                )
        runtime.close()


def test_paper_workbench_keeps_unresolved_realized_pnl_unknown(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    order_id = None
    try:
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, f"WB{uuid4().hex[:8]}")
            order_id = connection.execute(
                """INSERT INTO app.paper_order
                   (instrument_id, side, quantity, limit_price, status, paper_only, structure)
                   VALUES (%s, 'buy', 1, 10, 'exited', true, 'equity') RETURNING id""",
                [instrument_id],
            ).fetchone()["id"]
            connection.cursor().executemany(
                """INSERT INTO app.trade_journal
                   (instrument_id, action, quantity, price, rationale, details)
                   VALUES (%s, %s, 1, %s, 'deterministic_options_paper_execution', %s)""",
                [
                    (
                        instrument_id,
                        "paper_entry",
                        10,
                        Jsonb({"paper_order_id": str(order_id)}),
                    ),
                    (
                        instrument_id,
                        "paper_exit:take_profit",
                        12,
                        Jsonb({"paper_order_id": str(order_id)}),
                    ),
                ],
            )

        performance = PaperWorkbenchRepository(runtime).performance()
        assert performance["realized_pnl"] is None
        assert performance["realized_pnl_status"] == "partial"
        assert performance["net_pnl"] is None
    finally:
        with runtime.transaction() as connection:
            if order_id is not None:
                connection.execute(
                    "DELETE FROM app.trade_journal WHERE details->>'paper_order_id' = %s",
                    [str(order_id)],
                )
                connection.execute(
                    "DELETE FROM app.paper_order WHERE id = %s::uuid", [str(order_id)]
                )
        runtime.close()


def test_paper_workbench_filters_use_book_scope_and_exact_evidence_counts(
    migrated_postgres_dsn,
):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    order_ids: list[str] = []
    try:
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, f"WB{uuid4().hex[:8]}")
            created_at = datetime(2026, 9, 10, 14, tzinfo=UTC)
            verified_id = connection.execute(
                """INSERT INTO app.paper_order
                   (instrument_id, side, quantity, limit_price, status, paper_only,
                    structure, book, sleeve, lane, created_at)
                   VALUES (%s, 'buy', 1, 10, 'exited', true, 'equity',
                           'paper', 'growth', 'ticker', %s) RETURNING id""",
                [instrument_id, created_at],
            ).fetchone()["id"]
            partial_id = connection.execute(
                """INSERT INTO app.paper_order
                   (instrument_id, side, quantity, limit_price, status, paper_only,
                    structure, book, sleeve, lane, created_at)
                   VALUES (%s, 'buy', 1, 10, 'exited', true, 'equity',
                           'paper', 'growth', 'ticker', %s) RETURNING id""",
                [instrument_id, created_at],
            ).fetchone()["id"]
            staged_id = connection.execute(
                """INSERT INTO app.paper_order
                   (instrument_id, side, quantity, limit_price, status, paper_only,
                    structure, book, sleeve, lane, created_at)
                   VALUES (%s, 'buy', 1, 10, 'staged', true, 'equity',
                           'paper', 'income', 'radar', %s) RETURNING id""",
                [instrument_id, created_at + timedelta(days=1)],
            ).fetchone()["id"]
            order_ids = [str(verified_id), str(partial_id), str(staged_id)]
            connection.cursor().executemany(
                """INSERT INTO app.trade_journal
                   (instrument_id, action, quantity, price, rationale, details)
                   VALUES (%s, %s, 1, %s, 'deterministic_options_paper_execution', %s)""",
                [
                    (instrument_id, "paper_entry", 10, Jsonb({"paper_order_id": str(verified_id), "fees": 0.1})),
                    (instrument_id, "paper_exit:take_profit", 12, Jsonb({"paper_order_id": str(verified_id), "fees": 0.1})),
                    (instrument_id, "paper_entry", 10, Jsonb({"paper_order_id": str(partial_id)})),
                    (instrument_id, "paper_exit:take_profit", 12, Jsonb({"paper_order_id": str(partial_id)})),
                ],
            )

        repository = PaperWorkbenchRepository(runtime)
        filters = {
            "sleeve": "growth",
            "instrument_kind": "equity",
            "date_from": date(2026, 9, 10),
            "date_to": date(2026, 9, 10),
            "lane": "ticker",
            "structure": "equity",
            "evidence_class": "verified",
        }
        page = repository.trades(**filters)
        exported = repository.export_rows(**filters)
        performance = repository.performance(**filters)
        assert [row["paper_order_id"] for row in page["rows"]] == [str(verified_id)]
        assert page["scope"]["book"] == "paper"
        assert page["scope"]["sleeve"] == "growth"
        assert page["counts"]["total"] == 1
        assert page["counts"]["reconciled_orders"] == 1
        assert exported["scope"]["scope_id"] == page["scope"]["scope_id"]
        assert performance["counts"]["total_orders"] == 1
        assert performance["counts"]["reconciled_orders"] == 1
        assert performance["evidence_coverage"]["reconciled_orders"] == 1

        partial = repository.trades(sleeve="growth", evidence_class="partial")
        unavailable = repository.trades(sleeve="income", evidence_class="unavailable")
        assert [row["paper_order_id"] for row in partial["rows"]] == [str(partial_id)]
        assert [row["paper_order_id"] for row in unavailable["rows"]] == [str(staged_id)]
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
