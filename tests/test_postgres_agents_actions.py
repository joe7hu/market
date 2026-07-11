from __future__ import annotations

from contextlib import closing
import psycopg
from psycopg.types.json import Jsonb
import pytest

from investment_panel.database.actions import ActionRepository
from investment_panel.database.agents import AgentRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.runtime import DatabaseRuntime


def test_agent_queue_external_execution_and_manual_submission(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        queued = repository.queue_thesis("NVDA", prompt="focus on invalidation", trigger="ondemand")
        duplicate = repository.queue_thesis("NVDA", prompt="duplicate", trigger="ondemand")
        assert duplicate["request_id"] == queued["request_id"]

        command = "python -c 'import json,sys; request=json.load(sys.stdin); print(json.dumps({\"core_thesis\": request[\"ticker\"] + \" thesis\", \"confidence\": 0.8}))'"
        result = repository.run_queued(command, trigger="ondemand", task_kinds=("option_thesis",))
        assert result["completed"] == 1
        thesis = repository.rows("agent_thesis")[0]
        assert thesis["core_thesis"] == "NVDA thesis"

        second = repository.queue_thesis("MSFT", trigger="manual")
        submitted_id = repository.submit(
            "option_thesis",
            {"request_id": second["request_id"], "core_thesis": "MSFT cloud thesis", "confidence": 0.7},
        )
        assert submitted_id == second["request_id"]
        overview = repository.overview()
        assert overview["queue"]["total_open"] == 0
        assert overview["runs"][0]["status"] == "succeeded"
    finally:
        runtime.close()


def test_actions_persist_journal_acknowledgement_and_guarded_promotion(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    actions = ActionRepository(runtime)
    try:
        journal_id = actions.record_trade_journal(
            ticker="NVDA",
            contract_id="contract-1",
            event_id=None,
            strategy_version="v1",
            opportunity={"premium_mid": 5},
            notes="paper review",
        )
        with runtime.transaction() as connection:
            instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'").fetchone()["id"]
            alert_id = connection.execute(
                "INSERT INTO app.alert (instrument_id, alert_type, severity, title) "
                "VALUES (%s, 'review', 'high', 'Review NVDA') RETURNING id",
                [instrument_id],
            ).fetchone()["id"]
            proposal_id = connection.execute(
                """
                INSERT INTO analysis.agent_task (task_kind, status, request, result)
                VALUES ('strategy_mutation_proposal', 'completed', %s, %s) RETURNING id
                """,
                [Jsonb({"source": "test"}), Jsonb({"status": "approved", "proposed_strategy_version": "new-v2", "proposed_parameter_changes": {"max_spread_pct": 0.2}})],
            ).fetchone()["id"]
            candidate_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters) "
                "VALUES ('new-v2', 1, 'new-v2', 'candidate', %s) RETURNING id",
                [Jsonb({"max_spread_pct": 0.2})],
            ).fetchone()["id"]
            for evaluation_type in ("backtest", "forward_shadow_test"):
                connection.execute(
                    "INSERT INTO analysis.strategy_evaluation "
                    "(strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics) "
                    "VALUES (%s, %s, now(), 'pass', %s)",
                    [candidate_id, evaluation_type, Jsonb({"sample_size": 100})],
                )
        assert actions.acknowledge_alert(str(alert_id)) is True
        assert actions.acknowledge_alert(str(alert_id)) is False
        assert actions.promote_strategy_proposal(str(proposal_id), approved_by="joe") == "new-v2"
    finally:
        runtime.close()

    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT count(*) FROM app.trade_journal WHERE id = %s", [journal_id]).fetchone()[0] == 1
        assert connection.execute("SELECT acknowledged_at IS NOT NULL FROM app.alert WHERE id = %s", [alert_id]).fetchone()[0] is True
        strategy = connection.execute("SELECT status, parameters FROM analysis.strategy_revision WHERE strategy_key = 'new-v2'").fetchone()
    assert strategy == ("active", {"max_spread_pct": 0.2})


def test_strategy_promotion_rejects_agent_approval_without_deterministic_evaluations(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            proposal_id = connection.execute(
                "INSERT INTO analysis.agent_task (task_kind, status, request, result) "
                "VALUES ('strategy_mutation_proposal', 'completed', %s, %s) RETURNING id",
                [Jsonb({"source": "test"}), Jsonb({"status": "approved", "proposed_strategy_version": "unsafe-v1", "proposed_parameter_changes": {"delta_min": 0.01}})],
            ).fetchone()["id"]
        with pytest.raises(ValueError, match="candidate revision"):
            ActionRepository(runtime).promote_strategy_proposal(str(proposal_id), approved_by="joe")
    finally:
        runtime.close()
