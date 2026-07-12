from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4
import psycopg
from psycopg.types.json import Jsonb
import pytest

from investment_panel.database.actions import ActionRepository
from investment_panel.database.agents import AgentRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.strategy_learning import StrategyLearningRepository


def _postmortem_task(runtime: DatabaseRuntime, decision_id=None) -> str:
    with runtime.transaction() as connection:
        row = connection.execute(
            "INSERT INTO analysis.agent_task (decision_id, task_kind, status, request) "
            "VALUES (%s, 'option_postmortem', 'completed', %s) RETURNING id",
            [decision_id, Jsonb({"source": "test"})],
        ).fetchone()
    return str(row["id"])


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


def test_agent_repository_requeues_tasks_from_expired_worker_lease(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        task = repository.queue_thesis("NVDA", trigger="recovery")
        with runtime.transaction() as connection:
            run = connection.execute(
                "INSERT INTO analysis.agent_run (provider, model, trigger, started_at, status) "
                "VALUES ('test', 'test', 'recovery', now(), 'running') RETURNING id"
            ).fetchone()
            connection.execute(
                "UPDATE analysis.agent_task SET status = 'running', agent_run_id = %s, updated_at = %s WHERE id = %s",
                [run["id"], datetime.now(UTC) - timedelta(minutes=30), task["request_id"]],
            )

        assert repository.recover_stale_tasks(stale_after=timedelta(minutes=10)) == 1
        with runtime.read() as connection:
            recovered = connection.execute(
                "SELECT status, agent_run_id, validation FROM analysis.agent_task WHERE id = %s",
                [task["request_id"]],
            ).fetchone()
            failed_run = connection.execute(
                "SELECT status, summary FROM analysis.agent_run WHERE id = %s", [run["id"]]
            ).fetchone()
        assert recovered["status"] == "queued"
        assert recovered["agent_run_id"] is None
        assert recovered["validation"]["reason"] == "stale_running_lease"
        assert failed_run["status"] == "failed"
    finally:
        runtime.close()


def test_agent_repository_claims_sequential_tasks_only_when_execution_starts(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        repository.queue_thesis("NVDA", trigger="sequential")
        repository.queue_thesis("MSFT", trigger="sequential")
        observed_statuses: list[list[str]] = []

        def fake_run(*_args, input: str, **_kwargs):
            with runtime.read() as connection:
                observed_statuses.append(
                    [
                        row["status"]
                        for row in connection.execute(
                            "SELECT status FROM analysis.agent_task ORDER BY created_at"
                        ).fetchall()
                    ]
                )
            request = json.loads(input)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"core_thesis": f"{request['ticker']} thesis"}),
                stderr="",
            )

        monkeypatch.setattr("investment_panel.database.agents.subprocess.run", fake_run)
        result = repository.run_queued(
            "agent-command", trigger="sequential", limit=2, task_kinds=("option_thesis",)
        )

        assert result["completed"] == 2
        assert observed_statuses[0] == ["running", "queued"]
        assert observed_statuses[1] == ["completed", "running"]
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
            base_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, promoted_at) "
                "VALUES ('options-radar-core', 1, 'core', 'active', %s, now()) RETURNING id",
                [Jsonb({"gates": {"max_spread_pct": 0.25}})],
            ).fetchone()["id"]
            candidate_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, supersedes_id) "
                "VALUES ('new-v2', 1, 'new-v2', 'candidate', %s, %s) RETURNING id",
                [Jsonb({"max_spread_pct": 0.2}), base_id],
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
        assert StrategyLearningRepository(runtime).refresh_evaluations() == {
            "strategy_backtests": 0,
            "strategy_forward_tests": 0,
        }
        with runtime.read() as connection:
            promotion = connection.execute(
                "SELECT validation FROM analysis.agent_task WHERE id = %s", [proposal_id]
            ).fetchone()
        assert promotion["validation"] == {"status": "promoted", "approved_by": "joe"}
        with runtime.transaction() as connection:
            sibling_proposal = connection.execute(
                "INSERT INTO analysis.agent_task (task_kind, status, request, result) "
                "VALUES ('strategy_mutation_proposal', 'completed', %s, %s) RETURNING id",
                [Jsonb({"source": "sibling"}), Jsonb({"status": "approved", "proposed_strategy_version": "sibling-v2"})],
            ).fetchone()["id"]
            sibling_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, supersedes_id) "
                "VALUES ('sibling-v2', 1, 'sibling', 'candidate', %s, %s) RETURNING id",
                [Jsonb({}), base_id],
            ).fetchone()["id"]
            for evaluation_type in ("backtest", "forward_shadow_test"):
                connection.execute(
                    "INSERT INTO analysis.strategy_evaluation "
                    "(strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics) "
                    "VALUES (%s, %s, now(), 'pass', %s)",
                    [sibling_id, evaluation_type, Jsonb({"sample_size": 100})],
                )
        with pytest.raises(ValueError, match="base is no longer active"):
            actions.promote_strategy_proposal(str(sibling_proposal), approved_by="joe")
    finally:
        runtime.close()

    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT count(*) FROM app.trade_journal WHERE id = %s", [journal_id]).fetchone()[0] == 1
        assert connection.execute("SELECT acknowledged_at IS NOT NULL FROM app.alert WHERE id = %s", [alert_id]).fetchone()[0] is True
        strategy = connection.execute("SELECT status, parameters FROM analysis.strategy_revision WHERE strategy_key = 'new-v2'").fetchone()
        base_status = connection.execute(
            "SELECT status FROM analysis.strategy_revision WHERE id = %s", [base_id]
        ).fetchone()[0]
    assert strategy == ("active", {"max_spread_pct": 0.2})
    assert base_status == "superseded"


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


def test_strategy_learning_normalizes_dte_and_blocks_unsupported_changes(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        repository = StrategyLearningRepository(runtime)
        tightened = repository.materialize_postmortem(
            _postmortem_task(runtime),
            {"strategy_version": "ignored-agent-base", "proposed_parameter_changes": {"dte_min": 30}},
        )
        unsupported = repository.materialize_postmortem(
            _postmortem_task(runtime),
            {
                "strategy_version": "learning-base",
                "proposed_parameter_changes": {"require_rs_improving": True},
            },
        )
        with runtime.read() as connection:
            candidates = connection.execute(
                "SELECT parameters FROM analysis.strategy_revision WHERE status = 'candidate' ORDER BY id"
            ).fetchall()
            verdicts = connection.execute(
                "SELECT verdict FROM analysis.strategy_evaluation "
                "WHERE evaluation_type = 'backtest' ORDER BY evaluated_at"
            ).fetchall()
        assert tightened["strategy_backtests"] == 1
        assert unsupported["strategy_backtests"] == 1
        assert candidates[0]["parameters"]["gates"]["min_dte"] == 30
        assert [row["verdict"] for row in verdicts] == ["insufficient_data", "unsupported_parameters"]
    finally:
        runtime.close()


def test_strategy_learning_does_not_create_agent_named_active_base(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        repository = StrategyLearningRepository(runtime)
        empty = repository.materialize_postmortem(
            str(uuid4()),
            {"proposed_parameter_changes": {"delta_min": None, "candidate_note": ""}},
        )
        assert empty == {
            "strategy_proposals": 0,
            "strategy_backtests": 0,
            "strategy_forward_tests": 0,
        }
        repository.materialize_postmortem(
            _postmortem_task(runtime),
            {
                "strategy_version": "agent-controlled-active-key",
                "proposed_strategy_version": "agent-controlled-active-key",
                "proposed_parameter_changes": {"dte_min": 30},
            },
        )
        with runtime.read() as connection:
            keys = connection.execute(
                "SELECT strategy_key, status FROM analysis.strategy_revision ORDER BY id"
            ).fetchall()
        assert keys[0] == {"strategy_key": "options-radar-core", "status": "active"}
        assert keys[1]["strategy_key"].startswith("options-radar-core__agent_")
        assert all(row["strategy_key"] != "agent-controlled-active-key" for row in keys)
    finally:
        runtime.close()


def test_postmortem_submission_rolls_back_when_strategy_materialization_fails(
    postgres_dsn: str,
) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        task_id = _postmortem_task(runtime)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.agent_task SET status = 'queued' WHERE id = %s", [task_id]
            )
        with pytest.raises((TypeError, ValueError)):
            AgentRepository(runtime).submit_postmortem(
                {
                    "request_id": task_id,
                    "failure_type": "invalid_change",
                    "proposed_parameter_changes": {"dte_min": "not-a-number"},
                }
            )
        with runtime.read() as connection:
            task = connection.execute(
                "SELECT status, result FROM analysis.agent_task WHERE id = %s", [task_id]
            ).fetchone()
            proposal_count = connection.execute(
                "SELECT count(*) AS count FROM analysis.agent_task "
                "WHERE task_kind = 'strategy_mutation_proposal'"
            ).fetchone()["count"]
        assert task["status"] == "queued"
        assert task["result"] is None
        assert proposal_count == 0
    finally:
        runtime.close()


def test_strategy_learning_rejects_source_decisions_outside_core_lineage(
    postgres_dsn: str,
) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "INSERT INTO catalog.instrument (symbol, asset_class) "
                "VALUES ('NVDA', 'equity') RETURNING id"
            ).fetchone()["id"]
            strategy_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, promoted_at) "
                "VALUES ('unrelated-strategy', 1, 'unrelated', 'active', %s, now()) RETURNING id",
                [Jsonb({"gates": {"min_dte": 14}})],
            ).fetchone()["id"]
            run_id = connection.execute(
                "INSERT INTO analysis.run "
                "(run_type, input_cutoff, code_version, input_hash, started_at, status) "
                "VALUES ('test', now(), 'test', %s, now(), 'running') RETURNING id",
                ["a" * 64],
            ).fetchone()["id"]
            decision_id = connection.execute(
                "INSERT INTO analysis.decision "
                "(run_id, decision_key, kind, instrument_id, as_of, state, strategy_revision_id, input_hash) "
                "VALUES (%s, 'outside', 'option', %s, now(), 'WATCH', %s, %s) RETURNING id",
                [run_id, instrument_id, strategy_id, "b" * 64],
            ).fetchone()["id"]
        task_id = _postmortem_task(runtime, decision_id)

        with pytest.raises(ValueError, match="outside the options-radar-core lineage"):
            StrategyLearningRepository(runtime).materialize_postmortem(
                task_id, {"proposed_parameter_changes": {"min_dte": 30}}
            )
    finally:
        runtime.close()
