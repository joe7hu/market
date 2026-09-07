from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.options_paper_ledger import shared_sleeve_blockers, shared_sleeve_loss_state
from investment_panel.database.runtime import DatabaseRuntime


NOW = datetime(2026, 9, 8, 4, 30, tzinfo=UTC)  # 00:30 in New York.


@pytest.fixture
def ledger(migrated_postgres_dsn):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    with runtime.transaction() as connection:
        instrument = reconcile_instrument(connection, "NVDA")
    try:
        yield runtime, instrument
    finally:
        runtime.close()


def _journal(connection, instrument, *, at, details, action="paper_exit:stop_loss", decision=None, price=2):
    return connection.execute(
        "INSERT INTO app.trade_journal (decision_id, instrument_id, created_at, action, quantity, price, rationale, details) "
        "VALUES (%s, %s, %s, %s, 1, %s, 'deterministic_options_paper_execution', %s) RETURNING id",
        [decision, instrument, at, action, price, Jsonb(details)],
    ).fetchone()["id"]


@pytest.mark.parametrize("details", [{}, {"net_pnl": None}, {"net_pnl": "bad"}, {"net_pnl": "NaN"},
                                    {"net_pnl": "Infinity"}, {"net_pnl": "-Infinity"}, {"net_pnl": True}])
def test_unknown_options_exit_blocks_every_shared_lane_until_reconciled(ledger, details):
    runtime, instrument = ledger
    with runtime.transaction() as connection:
        _journal(connection, instrument, at=NOW, details={"paper_order_id": "malformed-order-id", **details})
    with runtime.read() as connection:
        assert shared_sleeve_loss_state(connection, now=NOW - timedelta(seconds=1)) == {
            "value": 0, "unresolved_exits": 0, "reconciled_exits": 0,
        }
        # Unknown accounting does not expire at midnight or disappear when the
        # optional numeric daily-loss threshold is not configured.
        for cutoff in (NOW, NOW + timedelta(days=1)):
            assert shared_sleeve_loss_state(connection, now=cutoff)["value"] is None
            for lane in ("radar", "qqq", "recovery"):
                assert shared_sleeve_blockers(connection, now=cutoff, lane=lane, sleeve_capital=25000,
                    daily_loss_halt_pct=None, max_open_positions=None) == ["shared_exit_accounting_unreconciled"]


def test_known_loss_uses_new_york_day_and_cutoff_with_existing_numeric_scope(ledger):
    runtime, instrument = ledger
    with runtime.transaction() as connection:
        _journal(connection, instrument, at=NOW - timedelta(minutes=31), details={"net_pnl": -1000})
        _journal(connection, instrument, at=NOW - timedelta(minutes=29), details={"net_pnl": -20})
        _journal(connection, instrument, at=NOW, details={"net_pnl": "-5"}, action="manual_exit")
        _journal(connection, instrument, at=NOW + timedelta(seconds=1), details={"net_pnl": -1000})
    with runtime.read() as connection:
        assert shared_sleeve_loss_state(connection, now=NOW) == {
            "value": -25, "unresolved_exits": 0, "reconciled_exits": 0,
        }
        assert shared_sleeve_blockers(connection, now=NOW, lane="radar", sleeve_capital=1000,
            daily_loss_halt_pct=.02, max_open_positions=None) == ["shared_daily_loss_halt"]
        assert shared_sleeve_loss_state(connection, now=NOW + timedelta(days=1))["value"] == 0


@pytest.mark.parametrize("gap", ["entry_fee", "exit_decision", "exit_quantity", "multiplier",
                                 "missing_entry_multiplier", "missing_exit_multiplier", "conflicting_exit_multiplier",
                                 "nonfinite_exit_multiplier", "boolean_exit_multiplier"])
def test_unknown_assignment_loss_reconciles_only_complete_order_evidence(ledger, gap):
    runtime, instrument = ledger
    run = AnalysisRepository(runtime).start_run("ledger-regression", input_cutoff=NOW, code_version="test", inputs={})
    with runtime.transaction() as connection:
        decision = connection.execute(
            "INSERT INTO analysis.decision (run_id, decision_key, kind, instrument_id, as_of, state, input_hash) "
            "VALUES (%s, 'ledger', 'options-radar', %s, %s, 'READY', %s) RETURNING id",
            [run, instrument, NOW - timedelta(hours=4), "a" * 64],
        ).fetchone()["id"]
        paper = connection.execute(
            "INSERT INTO app.paper_order (decision_id, instrument_id, created_at, updated_at, side, quantity, status, "
            "lane, structure, actual_fill_price, filled_at, filled_quantity, exited_quantity, entry_fees, exit_fees, fees, contract_multiplier) "
            "VALUES (%s, %s, %s, %s, 'sell', 1, 'exited', 'radar', 'cash_secured_put', .5, %s, 1, 1, .65, .65, 1.3, 10) RETURNING id",
            [decision, instrument, NOW - timedelta(hours=3), NOW - timedelta(hours=1), NOW - timedelta(hours=2)],
        ).fetchone()["id"]
        entry = _journal(connection, instrument, at=NOW - timedelta(hours=2), decision=decision,
            action="paper_entry", price=.5, details={"paper_order_id": str(paper), "fees": .65, "contract_multiplier": 10})
        exit_id = _journal(connection, instrument, at=NOW, decision=decision,
            action="paper_exit:assignment", details={"paper_order_id": str(paper), "fees": .65, "net_pnl": None,
                                                    "entry_contract_multiplier": 10, "exit_contract_multiplier": 10})
        if gap == "entry_fee":
            connection.execute("UPDATE app.trade_journal SET details = details - 'fees' WHERE id = %s", [entry])
        elif gap == "exit_decision":
            connection.execute("UPDATE app.trade_journal SET decision_id = NULL WHERE id = %s", [exit_id])
        elif gap == "exit_quantity":
            connection.execute("UPDATE app.trade_journal SET quantity = .5 WHERE id = %s", [exit_id])
        elif gap == "multiplier":
            connection.execute("UPDATE app.paper_order SET contract_multiplier = NULL WHERE id = %s", [paper])
        elif gap == "missing_entry_multiplier":
            connection.execute("UPDATE app.trade_journal SET details = details - 'entry_contract_multiplier' WHERE id = %s", [exit_id])
        elif gap == "missing_exit_multiplier":
            connection.execute("UPDATE app.trade_journal SET details = details - 'exit_contract_multiplier' WHERE id = %s", [exit_id])
        else:
            value = {"conflicting_exit_multiplier": 100, "nonfinite_exit_multiplier": "Infinity", "boolean_exit_multiplier": True}[gap]
            connection.execute("UPDATE app.trade_journal SET details = details || %s WHERE id = %s", [Jsonb({"exit_contract_multiplier": value}), exit_id])
    with runtime.read() as connection:
        assert shared_sleeve_loss_state(connection, now=NOW) == {
            "value": None, "unresolved_exits": 1, "reconciled_exits": 0,
        }
    if gap not in {"entry_fee", "exit_decision", "exit_quantity", "multiplier"}:
        # Later order state is not corrected evidence for the immutable exit.
        with runtime.transaction() as connection:
            connection.execute("UPDATE app.paper_order SET contract_multiplier = 100 WHERE id = %s", [paper])
        with runtime.read() as connection:
            assert shared_sleeve_loss_state(connection, now=NOW + timedelta(days=1)) == {
                "value": None, "unresolved_exits": 1, "reconciled_exits": 0,
            }
        return
    # Restored evidence is sufficient; no journal P&L rewrite or new state is required.
    with runtime.transaction() as connection:
        connection.execute("UPDATE app.trade_journal SET details = details || %s WHERE id = %s", [Jsonb({"fees": .65}), entry])
        connection.execute("UPDATE app.trade_journal SET decision_id = %s, quantity = 1 WHERE id = %s", [decision, exit_id])
        connection.execute("UPDATE app.paper_order SET contract_multiplier = 10 WHERE id = %s", [paper])
    with runtime.read() as connection:
        assert shared_sleeve_loss_state(connection, now=NOW) == {
            "value": pytest.approx(-16.3), "unresolved_exits": 0, "reconciled_exits": 1,
        }
        assert shared_sleeve_blockers(connection, now=NOW, lane="radar", sleeve_capital=1000,
            daily_loss_halt_pct=.02, max_open_positions=None) == []
        assert shared_sleeve_loss_state(connection, now=NOW + timedelta(days=1)) == {
            "value": 0, "unresolved_exits": 0, "reconciled_exits": 1,
        }
        assert connection.execute("SELECT details->'net_pnl' AS value FROM app.trade_journal WHERE id = %s", [exit_id]).fetchone()["value"] is None
