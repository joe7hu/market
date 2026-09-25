"""Postmortems receive stored evidence and re-open only on evidence changes."""
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.agents import AgentRepository
from investment_panel.infrastructure.postgres import agents
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


def test_postmortem_evidence_survives_queue_batch_and_new_evidence_reopens_review(migrated_postgres_dsn, monkeypatch):
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        now = datetime.now(UTC)
        old = now - timedelta(days=70)
        with runtime.transaction() as connection:
            instrument = connection.execute("INSERT INTO catalog.instrument (symbol, asset_class) VALUES ('PM-EVIDENCE', 'equity') RETURNING id").fetchone()["id"]
            run = connection.execute("INSERT INTO analysis.run (run_type, input_cutoff, code_version, input_hash, started_at, status) VALUES ('test', %s, 'test', %s, %s, 'succeeded') RETURNING id", [old, "a" * 64, old]).fetchone()["id"]
            decision = connection.execute("INSERT INTO analysis.decision (run_id, instrument_id, decision_key, kind, state, as_of, input_hash) VALUES (%s, %s, 'postmortem-evidence', 'option', 'WATCH', %s, %s) RETURNING id", [run, instrument, old, "b" * 64]).fetchone()["id"]
            connection.execute("INSERT INTO analysis.option_outcome (decision_id, maturity_state, observed_through, realized_exit_return, realized_exit_basis, stock_move_effect, iv_effect, theta_effect, spread_effect, unexplained_effect) VALUES (%s, 'expired', %s, .1, 'historical_estimate', .1, .02, -.01, -.01, 0)", [decision, now - timedelta(days=1)])
            paper = connection.execute("INSERT INTO app.paper_order (decision_id, instrument_id, side, quantity, status, paper_only, contract_multiplier, thesis_snapshot, execution_quote) VALUES (%s, %s, 'buy', 1, 'exited', true, 100, %s, %s) RETURNING id", [decision, instrument, Jsonb({"core_thesis": "Original entry thesis"}), Jsonb({"assignment": {"status": "pending_settlement", "reason": "provider_mark_missing"}})]).fetchone()["id"]
            connection.execute("INSERT INTO app.trade_journal (decision_id, instrument_id, action, quantity, price, rationale, details) VALUES (%s, %s, 'paper_entry', 1, 2, 'deterministic_options_paper_execution', %s)", [decision, instrument, Jsonb({"paper_order_id": str(paper), "fees": .65, "contract_multiplier": 100})])
            # Completed legacy reviews must not permanently suppress repaired requests.
            legacy = connection.execute("INSERT INTO analysis.agent_task (decision_id, task_kind, status, request) VALUES (%s, 'option_postmortem', 'completed', %s) RETURNING id", [decision, Jsonb({"decision_id": str(decision), "decision": {"maturity_state": "expired"}})]).fetchone()["id"]
        repository = AgentRepository(runtime)
        assert repository.queue_current_postmortems(limit=4) == 1
        with runtime.read() as connection:
            task = connection.execute("SELECT * FROM analysis.agent_task WHERE task_kind = 'option_postmortem' AND id <> %s", [legacy]).fetchone()
        assert repository.queue_current_postmortems(limit=4) == 0
        captured = []
        def review(*args, **kwargs):
            captured.append(json.loads(kwargs["input"]))
            # Deliberately omit an agent result. Request production must succeed
            # independently of an external model accepting the evidence.
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')
        monkeypatch.setattr(agents.subprocess, "run", review)
        repository.run_queued("evidence-test", consolidated=True, task_kinds=("option_postmortem",))
        context = captured[0]["postmortem"][0]["context"]
        assert context["decision"]["id"] == str(decision)
        assert context["outcome"]["realized_exit_basis"] == "historical_estimate"
        assert context["attribution"]["stock_move_effect"] == .1
        assert context["paper_executions"][0]["thesis_snapshot"]["core_thesis"] == "Original entry thesis"
        assert context["paper_executions"][0]["settlement"]["status"] == "pending_settlement"
        assert context["paper_executions"][0]["fill_evidence"]["entry_quantity"] == 1
        assert context["paper_executions"][0]["realized_payoff_status"] == "unreconciled"
        assert "realized_payoff_unreconciled" in context["evidence_gaps"]
        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.agent_task SET status = 'completed', result = %s WHERE id = %s", [Jsonb({"proposed_parameter_changes": {}}), task["id"]])
            connection.execute("UPDATE analysis.option_outcome SET updated_at = clock_timestamp() WHERE decision_id = %s", [decision])
        assert repository.queue_current_postmortems(limit=4) == 0  # Timestamp-only refresh is not new evidence.
        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.option_outcome SET realized_exit_return = .2, updated_at = clock_timestamp() WHERE decision_id = %s", [decision])
        assert repository.queue_current_postmortems(limit=4) == 1
        with runtime.read() as connection:
            tasks = connection.execute("SELECT * FROM analysis.agent_task WHERE task_kind = 'option_postmortem' ORDER BY created_at").fetchall()
            assert len(tasks) == 3
            assert tasks[1]["request"]["context"]["outcome"]["realized_exit_return"] == .1
            assert tasks[2]["request"]["context"]["outcome"]["realized_exit_return"] == .2
            assert connection.execute("SELECT count(*) AS n FROM analysis.strategy_revision WHERE status = 'candidate'").fetchone()["n"] == 0
    finally:
        runtime.close()
