"""The non-owner paper producer receives only its existing write columns."""

import os

import psycopg
import pytest
from sqlalchemy.engine import make_url

from investment_panel.database.migrations import upgrade_database
from investment_panel.database.runtime import DatabaseRuntime


WRITER_COLUMNS = {
    ("analysis.shadow_trade", "UPDATE"): {
        "status", "pending_entry_reason", "entry_at", "entry_price",
        "exit_at", "exit_price", "fill_basis", "metrics",
    },
    ("app.paper_order", "INSERT"): {
        "decision_id", "instrument_id", "side", "quantity", "limit_price", "status",
        "policy_result", "policy_snapshot", "lane", "structure", "reserved_collateral",
        "idempotency_key", "ticket_version", "ticket_snapshot", "intended_limit_price",
    },
    ("app.paper_order", "UPDATE"): {
        "status", "actual_fill_price", "filled_at", "fill_evidence_at", "execution_quote",
        "contract_multiplier", "filled_quantity", "fees", "entry_fees", "entry_slippage",
        "updated_at", "unfilled_reason", "submitted_at", "exited_quantity", "exit_price",
        "exit_at", "exit_fees", "exit_slippage",
    },
    ("app.paper_order_leg", "INSERT"): {
        "paper_order_id", "leg_index", "contract_id", "option_type", "side", "strike",
        "bid", "ask", "bid_size", "ask_size", "quote_time", "open_interest", "volume",
    },
}


def _writer_columns(connection):
    return {
        (table, privilege): {row[0] for row in connection.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attnum > 0 AND NOT attisdropped AND has_column_privilege('market_app', attrelid, attnum, %s)",
            [table, privilege],
        ).fetchall()}
        for table, privilege in WRITER_COLUMNS
    }


def test_option_paper_writer_grants_are_narrow(postgres_dsn):
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        assert _writer_columns(connection) == WRITER_COLUMNS
        assert all(not connection.execute("SELECT has_table_privilege('market_app', %s, %s)", [table, privilege]).fetchone()[0]
                   for table, privilege in WRITER_COLUMNS)
        for table in {table for table, _privilege in WRITER_COLUMNS}:
            assert not connection.execute("SELECT has_table_privilege('market_app', %s, 'DELETE')", [table]).fetchone()[0]
        assert not connection.execute("SELECT has_table_privilege('market_app', 'analysis.research_evaluator_signing_secret', 'SELECT')").fetchone()[0]
        assert not connection.execute("SELECT has_table_privilege('market_app', 'analysis.portfolio_allocation_snapshot', 'INSERT')").fetchone()[0]

    application_dsn = make_url(postgres_dsn).set(
        username=os.environ["MARKET_APP_LOGIN_ROLE"], password=os.environ["MARKET_APP_DATABASE_PASSWORD"],
    ).render_as_string(hide_password=False)
    application = DatabaseRuntime(application_dsn)
    application.open()
    try:
        with application.transaction() as connection:
            assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "3s"
            for statement in (
                "UPDATE analysis.shadow_trade SET decision_id = decision_id WHERE false",
                "UPDATE analysis.shadow_trade SET source_kind = source_kind WHERE false",
                "UPDATE app.paper_order SET ticket_snapshot = ticket_snapshot WHERE false",
                "UPDATE app.paper_order SET policy_result = policy_result WHERE false",
                "UPDATE app.paper_order SET quantity = quantity WHERE false",
                "UPDATE app.paper_order_leg SET bid = bid WHERE false",
                "DELETE FROM analysis.shadow_trade WHERE false",
                "DELETE FROM app.paper_order WHERE false",
                "DELETE FROM app.paper_order_leg WHERE false",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with connection.transaction():
                        connection.execute(statement)
    finally:
        application.close()
