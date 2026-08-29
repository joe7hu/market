from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
import shlex
import sys
from types import SimpleNamespace
from uuid import uuid4
import psycopg
from psycopg.types.json import Jsonb
import pytest

from investment_panel.database.actions import ActionRepository
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.agents import AgentRepository, command_args
from investment_panel.database.agent_process import market_day_start_utc, validate_result
from investment_panel.database.agent_context import option_opportunity_context, ticker_context
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.strategy_learning import StrategyLearningRepository
from investment_panel.database.strategy_governance import StrategyGovernanceRepository
from investment_panel.database.thesis_evidence import thesis_source_evidence
from investment_panel.jobs.option_agent_workflow import compact_agent_batch


def _option_thesis_result(ticker: str, *, request_id: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "ticker": ticker,
        "direction": "long",
        "bull_target_price": 240.0,
        "bull_target_date": "2027-01-15",
        "base_target_price": 210.0,
        "bear_target_price": 150.0,
        "scenario_probabilities": {"base": 0.55, "bull": 0.25, "bear": 0.20},
        "preferred_structures": ["long_call", "call_debit_spread"],
        "core_thesis": f"{ticker} thesis",
        "required_proofs": ["Revenue growth remains durable."],
        "catalysts": [{"type": "earnings", "expected_window": "next quarter", "what_to_watch": "guidance"}],
        "invalidation": ["Revenue growth breaks below the underwriting range."],
        "bear_case": "Demand weakens materially.",
        "confidence": 0.7,
        "evidence_refs": [],
    }
    if request_id:
        result["request_id"] = request_id
    return result


def _postmortem_task(runtime: DatabaseRuntime, decision_id=None) -> str:
    with runtime.transaction() as connection:
        row = connection.execute(
            "INSERT INTO analysis.agent_task (decision_id, task_kind, status, request) "
            "VALUES (%s, 'option_postmortem', 'completed', %s) RETURNING id",
            [decision_id, Jsonb({"source": "test"})],
        ).fetchone()
    return str(row["id"])


def test_agent_today_window_uses_new_york_calendar_day() -> None:
    assert market_day_start_utc(datetime(2026, 8, 2, 0, 5, tzinfo=UTC)) == datetime(
        2026, 8, 1, 4, 0, tzinfo=UTC,
    )


def test_strategy_governance_automatically_promotes_only_complete_evidence(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            base_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, authority_group, promoted_at) "
                "VALUES ('options-radar-core', 2, 'core', 'active', %s, 'options-radar-core', now()) RETURNING id",
                [Jsonb({"contract_version": 2, "gates": {"max_spread_pct": .25}})],
            ).fetchone()["id"]
            candidate_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, supersedes_id, authority_group) "
                "VALUES ('options-radar-core__agent_auto', 1, 'auto', 'candidate', %s, %s, 'options-radar-core') RETURNING id",
                [Jsonb({"contract_version": 2, "gates": {"max_spread_pct": .20}}), base_id],
            ).fetchone()["id"]
            task_id = connection.execute(
                "INSERT INTO analysis.agent_task (task_kind, status, request, result, validation) "
                "VALUES ('strategy_mutation_proposal', 'completed', %s, %s, %s) RETURNING id",
                [
                    Jsonb({"source": "test"}),
                    Jsonb({"candidate_revision_id": candidate_id, "proposed_parameter_changes": {"max_spread_pct": .20}}),
                    Jsonb({"status": "ready"}),
                ],
            ).fetchone()["id"]
            baseline = {"net_expectancy": .10, "precision_at_5": .50, "max_drawdown": -.20, "calibration_error": .10}
            for evaluation_type, sample, span in (
                ("backtest", 100, 120),
                ("forward_shadow_test", 30, 30),
                ("canary", 20, 20),
            ):
                proposed = {
                    "sample_size": sample,
                    "net_expectancy": .12,
                    "lower_95_expectancy": .02,
                    "precision_at_5": .50,
                    "max_drawdown": -.19,
                    "calibration_error": .10,
                    "max_ticker_contribution": .10,
                }
                connection.execute(
                    "INSERT INTO analysis.strategy_evaluation "
                    "(strategy_revision_id, evaluation_type, evaluated_at, period_start, period_end, verdict, metrics) "
                    "VALUES (%s, %s, now(), now() - make_interval(days => %s), now(), 'pass', %s)",
                    [candidate_id, evaluation_type, span, Jsonb({"baseline": baseline, "proposed": proposed, "observation_span_days": span})],
                )

        governance = StrategyGovernanceRepository(runtime)
        assert governance.automatic_promote_eligible(enabled=False) == 0
        with runtime.read() as connection:
            assert connection.execute(
                "SELECT status FROM analysis.strategy_revision WHERE id = %s", [candidate_id]
            ).fetchone()["status"] == "candidate"
        assert governance.automatic_promote_eligible() == 1
        with runtime.read() as connection:
            statuses = connection.execute(
                "SELECT id, status FROM analysis.strategy_revision WHERE id IN (%s, %s) ORDER BY id",
                [base_id, candidate_id],
            ).fetchall()
            validation = connection.execute(
                "SELECT validation FROM analysis.agent_task WHERE id = %s", [task_id]
            ).fetchone()["validation"]
        assert [row["status"] for row in statuses] == ["superseded", "active"]
        assert validation["authority"] == "automatic_deterministic_governance"
    finally:
        runtime.close()


def test_agent_queue_external_execution_and_manual_submission(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        queued = repository.queue_thesis("NVDA", prompt="focus on invalidation", trigger="ondemand")
        duplicate = repository.queue_thesis("NVDA", prompt="duplicate", trigger="ondemand")
        assert duplicate["request_id"] == queued["request_id"]
        assert duplicate["request_envelope"] == queued["request_envelope"]

        template = _option_thesis_result("NVDA")
        command_code = (
            "import json,sys; request=json.load(sys.stdin); "
            f"result={template!r}; result['ticker']=request['ticker']; "
            "result['request_id']=request['request_id']; "
            "result['core_thesis']=request['ticker'] + ' thesis'; print(json.dumps(result))"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(command_code)}"
        result = repository.run_queued(command, trigger="ondemand", task_kinds=("option_thesis",))
        assert result["completed"] == 1
        thesis = repository.rows("agent_thesis")[0]
        assert thesis["core_thesis"] == "NVDA thesis"
        with runtime.read() as connection:
            materialized = connection.execute(
                """
                SELECT thesis.id, thesis.thesis, expression.structure, task.validation
                FROM app.thesis thesis
                JOIN catalog.instrument instrument ON instrument.id = thesis.instrument_id
                JOIN app.thesis_expression expression ON expression.thesis_revision_id = thesis.id
                JOIN analysis.agent_task task ON task.id = (thesis.thesis->'provenance'->>'option_agent_task_id')::uuid
                WHERE instrument.symbol = 'NVDA' AND thesis.status = 'current'
                  AND expression.expression_kind = 'option' AND expression.status = 'active'
                """
            ).fetchone()
        assert materialized is not None
        assert materialized["thesis"]["scenarios"]["base"]["target"] == 210.0
        assert materialized["structure"]["preferred_structures"] == ["long_call", "call_debit_spread"]
        assert materialized["validation"]["materialization"]["status"] == "materialized"

        second = repository.queue_thesis("MSFT", trigger="manual")
        submitted_id = repository.submit(
            "option_thesis",
            {
                **_option_thesis_result("MSFT", request_id=second["request_id"]),
                "core_thesis": "MSFT cloud thesis",
            },
        )
        assert submitted_id == second["request_id"]
        overview = repository.overview()
        assert overview["queue"]["total_open"] == 0
        assert overview["runs"][0]["status"] == "succeeded"
        assert overview["materialization"]["materialized"] == 2
        assert overview["materialization"]["historical_unmaterialized"] == 0
    finally:
        runtime.close()


def test_agent_queue_uses_the_exact_published_decision(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "INSERT INTO catalog.instrument (symbol, asset_class) VALUES ('EDEC', 'equity') RETURNING id"
            ).fetchone()["id"]
            run_ids = [
                connection.execute(
                    "INSERT INTO analysis.run (run_type, input_cutoff, code_version, input_hash, started_at, status) "
                    "VALUES ('test', now(), 'test', %s, now(), 'succeeded') RETURNING id",
                    [character * 64],
                ).fetchone()["id"]
                for character in ("a", "b")
            ]
            decision_ids = [
                connection.execute(
                    "INSERT INTO analysis.decision "
                    "(run_id, decision_key, kind, instrument_id, as_of, state, input_hash) "
                    "VALUES (%s, %s, 'option', %s, now() + make_interval(secs => %s), 'WATCH', %s) RETURNING id",
                    [run_id, f"decision-{index}", instrument_id, index, character * 64],
                ).fetchone()["id"]
                for index, (run_id, character) in enumerate(zip(run_ids, ("c", "d")))
            ]

        queued = AgentRepository(runtime).queue_thesis(
            "EDEC", trigger="scheduled", context={"decision_id": str(decision_ids[0])},
        )
        with runtime.read() as connection:
            stored = connection.execute(
                "SELECT decision_id, request->'decision'->>'id' AS request_decision_id "
                "FROM analysis.agent_task WHERE id = %s",
                [queued["request_id"]],
            ).fetchone()
        assert stored["decision_id"] == decision_ids[0]
        assert stored["request_decision_id"] == str(decision_ids[0])

        without_decision = AgentRepository(runtime).queue_thesis(
            "EDEC",
            trigger="manual",
            context={"decision_id": str(decision_ids[0])},
            context_sources={"decision": False},
        )
        with runtime.read() as connection:
            stored_without_decision = connection.execute(
                "SELECT decision_id, request->'decision' AS request_decision, "
                "request->'request_envelope' AS envelope "
                "FROM analysis.agent_task WHERE id = %s",
                [without_decision["request_id"]],
            ).fetchone()
        assert stored_without_decision["decision_id"] == decision_ids[0]
        assert stored_without_decision["request_decision"] == {}
        assert stored_without_decision["envelope"]["decision_id"] == str(decision_ids[0])
    finally:
        runtime.close()


def test_agent_queue_tied_decisions_use_latest_identity(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "INSERT INTO catalog.instrument (symbol, asset_class) VALUES ('TIED', 'equity') RETURNING id"
            ).fetchone()["id"]
            run_id = connection.execute(
                """
                INSERT INTO analysis.run
                    (run_type, input_cutoff, code_version, input_hash, started_at, status)
                VALUES ('tied-decision-test', %s, 'test', %s, %s, 'succeeded')
                RETURNING id
                """,
                [datetime(2026, 8, 28, 15, tzinfo=UTC), "a" * 64, datetime(2026, 8, 28, 14, tzinfo=UTC)],
            ).fetchone()["id"]
            decision_ids = [
                connection.execute(
                    """
                    INSERT INTO analysis.decision
                        (id, run_id, decision_key, kind, instrument_id, as_of, state, score, input_hash)
                    VALUES (%s, %s, %s, 'option', %s, %s, 'WATCH', 0.5, %s)
                    RETURNING id
                    """,
                    [
                        f"00000000-0000-0000-0000-{index + 1:012d}", run_id, f"tied-{index}", instrument_id,
                        datetime(2026, 8, 28, 15, tzinfo=UTC), character * 64,
                    ],
                ).fetchone()["id"]
                for index, character in enumerate(("a", "b"))
            ]
        queued = AgentRepository(runtime).queue_thesis("TIED", trigger="tied-decision")
        with runtime.read() as connection:
            stored = connection.execute(
                "SELECT decision_id FROM analysis.agent_task WHERE id = %s", [queued["request_id"]]
            ).fetchone()
        assert stored["decision_id"] == decision_ids[1]
    finally:
        runtime.close()


def test_current_candidate_queue_counts_unique_symbols_not_structure_rows(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        analysis = AnalysisRepository(runtime)
        run_id = analysis.start_run(
            "queue-test", input_cutoff=datetime.now(UTC), code_version="test", inputs={},
        )
        analysis.publish(
            run_id,
            "options-radar",
            {"option_radar_opportunity": [
                {"ticker": "NVDA", "structure": "long_call", "ticket": {"legs": [{"large": "payload" * 1000}]}},
                {"ticker": "NVDA", "structure": "call_debit_spread"},
                {"ticker": "MSFT", "structure": "long_call"},
                {"ticker": "AAPL", "structure": "long_call"},
            ]},
        )

        queued = AgentRepository(runtime).queue_current_candidates(limit=3, trigger="manual")

        with runtime.read() as connection:
            symbols = connection.execute(
                "SELECT request->>'ticker' AS ticker FROM analysis.agent_task ORDER BY request->>'ticker'"
            ).fetchall()
            ticket_copied = connection.execute(
                "SELECT request->'context'->'option_opportunity' ? 'ticket' AS copied "
                "FROM analysis.agent_task WHERE request->>'ticker' = 'NVDA'"
            ).fetchone()["copied"]
        assert queued == 3
        assert [row["ticker"] for row in symbols] == ["AAPL", "MSFT", "NVDA"]
        assert ticket_copied is False
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


def test_legacy_recovered_task_uses_dispatch_fallback_envelope(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        with runtime.transaction() as connection:
            task = connection.execute(
                """
                INSERT INTO analysis.agent_task (task_kind, status, request, updated_at)
                VALUES ('option_thesis', 'running', %s, %s) RETURNING id
                """,
                [
                    Jsonb({"ticker": "NVDA", "trigger": "legacy-recovery", "authority": "hypothesis_only"}),
                    datetime.now(UTC) - timedelta(minutes=30),
                ],
            ).fetchone()
        task_id = str(task["id"])
        assert repository.recover_stale_tasks(stale_after=timedelta(minutes=10)) == 1

        def fake_run(*_args, input, **_kwargs):
            request = json.loads(input)
            envelope = request["request"]
            result = _option_thesis_result(envelope["ticker"], request_id=envelope["request_id"])
            result["evidence_refs"] = [{"type": "agent_request", "id": envelope["request_id"]}]
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

        monkeypatch.setattr("investment_panel.database.agents.subprocess.run", fake_run)
        outcome = repository.run_queued(
            "legacy-agent", limit=1, trigger="legacy-recovery", task_kinds=("option_thesis",),
        )
        assert outcome["completed"] == 1
        with runtime.read() as connection:
            recovered = connection.execute(
                "SELECT status, result FROM analysis.agent_task WHERE id = %s", [task_id]
            ).fetchone()
        assert recovered["status"] == "completed"
        assert recovered["result"]["request_id"] == task_id
    finally:
        runtime.close()


def test_legacy_manual_submission_uses_fallback_envelope(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        with runtime.transaction() as connection:
            task = connection.execute(
                """
                INSERT INTO analysis.agent_task (task_kind, status, request)
                VALUES ('option_thesis', 'queued', %s) RETURNING id
                """,
                [Jsonb({"ticker": "NVDA", "authority": "hypothesis_only"})],
            ).fetchone()
        task_id = str(task["id"])
        result = _option_thesis_result("NVDA", request_id=task_id)
        result["evidence_refs"] = [{"type": "agent_request", "id": task_id}]
        assert repository.submit("option_thesis", result) == task_id
        with runtime.read() as connection:
            status = connection.execute(
                "SELECT status FROM analysis.agent_task WHERE id = %s", [task_id]
            ).fetchone()["status"]
        assert status == "completed"
    finally:
        runtime.close()


def test_completed_agent_submission_rejects_conflicting_replay(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        queued = repository.queue_thesis("NVDA", trigger="completed-replay")
        first = _option_thesis_result("NVDA", request_id=queued["request_id"])
        first["evidence_refs"] = [{"type": "agent_request", "id": queued["request_id"]}]
        assert repository.submit("option_thesis", first) == queued["request_id"]
        assert repository.submit("option_thesis", first) == queued["request_id"]
        conflicting = {**first, "core_thesis": "conflicting replay"}
        with pytest.raises(ValueError, match="conflicting replay"):
            repository.submit("option_thesis", conflicting)
        with runtime.read() as connection:
            stored = connection.execute(
                "SELECT result FROM analysis.agent_task WHERE id = %s", [queued["request_id"]]
            ).fetchone()
            current_count = connection.execute(
                """
                SELECT count(*) AS count
                FROM app.thesis thesis
                JOIN catalog.instrument instrument ON instrument.id = thesis.instrument_id
                WHERE instrument.symbol = 'NVDA' AND thesis.status = 'current'
                """
            ).fetchone()["count"]
        assert stored["result"] == first
        assert current_count == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confidence", float("nan")),
        ("confidence", float("inf")),
        ("bull_target_price", float("nan")),
        ("bull_target_price", float("inf")),
        ("scenario_probabilities", {"base": float("nan"), "bull": 0.25, "bear": 0.20}),
        ("scenario_probabilities", {"base": float("inf"), "bull": 0.25, "bear": 0.20}),
    ],
)
def test_option_agent_rejects_non_finite_numeric_fields(field: str, value: object) -> None:
    result = _option_thesis_result("NVDA")
    result[field] = value
    with pytest.raises(ValueError):
        validate_result("option_thesis", result)


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
                stdout=json.dumps(_option_thesis_result(request["ticker"], request_id=request["request_id"])),
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


def test_agent_repository_runs_one_configured_batch_and_propagates_model(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        first = repository.queue_thesis("NVDA", trigger="scheduled")
        repository.queue_thesis("MSFT", trigger="scheduled")
        calls: list[dict[str, object]] = []

        def fake_run(*_args, input: str, env: dict[str, str], **_kwargs):
            payload = json.loads(input)
            calls.append({"payload": payload, "env": env})
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "thesis": [
                        _option_thesis_result(
                            item["request"]["ticker"], request_id=item["request"]["request_id"],
                        )
                        for item in payload["thesis"]
                    ],
                    "postmortem": [],
                    "_meta": {"usage": {"input_tokens": 120, "output_tokens": 40}},
                }),
                stderr="",
            )

        monkeypatch.setattr("investment_panel.database.agents.subprocess.run", fake_run)
        result = repository.run_queued(
            "market-run-option-agent --provider codex --task batch", consolidated=True, limit=8,
            provider="codex", model="gpt-5.6-luna", reasoning_effort="high",
            kind_limits={"option_thesis": 8, "option_postmortem": 4},
        )

        assert result["completed"] == 2
        assert len(calls) == 1
        assert len(calls[0]["payload"]["thesis"]) == 2
        assert compact_agent_batch(calls[0]["payload"])["thesis"][0]["request"]["ticker"] in {"NVDA", "MSFT"}
        assert calls[0]["env"]["MARKET_CODEX_MODEL"] == "gpt-5.6-luna"
        assert calls[0]["env"]["MARKET_CODEX_REASONING_EFFORT"] == "high"
        assert calls[0]["env"]["MARKET_CODEX_TIMEOUT_SECONDS"] == "165"
        overview = repository.overview()
        option_run = next(run for run in overview["runs"] if run["workflow"] == "option_agent")
        assert option_run["input_tokens"] == 120
        assert option_run["output_tokens"] == 40
        assert option_run["thesis_accepted"] == 2
        duplicate = repository.queue_thesis("NVDA", trigger="scheduled")
        assert duplicate["request_id"] == first["request_id"]
        assert duplicate["status"] == "completed"
    finally:
        runtime.close()


def test_consolidated_agent_does_not_record_a_run_without_tasks(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        result = AgentRepository(runtime).run_queued("unused", consolidated=True)
        with runtime.read() as connection:
            count = connection.execute("SELECT count(*) AS count FROM analysis.agent_run").fetchone()["count"]
        assert result["status"] == "skipped"
        assert result["reason"] == "no_open_tasks"
        assert count == 0
    finally:
        runtime.close()


def test_consolidated_agent_enforces_scheduled_daily_run_cap(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = AgentRepository(runtime)
    try:
        repository.queue_thesis("NVDA", trigger="scheduled")
        monkeypatch.setattr(
            "investment_panel.database.agents.subprocess.run",
            lambda *_args, input, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "thesis": [_option_thesis_result(
                        json.loads(input)["thesis"][0]["request"]["ticker"],
                        request_id=json.loads(input)["thesis"][0]["request"]["request_id"],
                    )],
                    "postmortem": [],
                }),
                stderr="",
            ),
        )
        first = repository.run_queued(
            "agent", consolidated=True, run_trigger="scheduled", max_runs_per_day=1,
            task_kinds=("option_thesis",), kind_limits={"option_thesis": 1},
        )
        repository.queue_thesis("MSFT", trigger="scheduled")
        capped = repository.run_queued(
            "agent", consolidated=True, run_trigger="scheduled", max_runs_per_day=1,
            task_kinds=("option_thesis",), kind_limits={"option_thesis": 1},
        )
        assert first["status"] == "ok"
        assert capped == {"status": "skipped", "reason": "daily_run_cap", "completed": 0, "failed": 0}
    finally:
        runtime.close()


def test_agent_command_resolves_from_active_virtualenv(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    command = bin_dir / "market-run-option-agent"
    python.write_text("")
    command.write_text("")
    monkeypatch.setattr("investment_panel.database.agent_process.shutil.which", lambda _name: None)
    monkeypatch.setattr("investment_panel.database.agent_process.sys.executable", str(python))
    resolved = command_args("market-run-option-agent")
    assert resolved[0] == str(command)


def test_option_agent_context_keeps_decision_fields_without_copying_the_full_ticket() -> None:
    compact = option_opportunity_context({
        "ticker": "NVDA",
        "decision_id": "decision-1",
        "structure": "long_call",
        "expected_value": 42,
        "blockers": ["calibrated_probability_required"],
        "ticket": {"legs": [{"large": "payload" * 1000}]},
        "raw": {"provider": "payload" * 1000},
    })

    assert compact == {
        "ticker": "NVDA",
        "decision_id": "decision-1",
        "structure": "long_call",
        "expected_value": 42,
        "blockers": ["calibrated_probability_required"],
    }


def test_agent_thesis_request_envelope_is_bounded_and_hypothesis_only(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        queued = AgentRepository(runtime).queue_thesis("NVDA", trigger="envelope")
        with runtime.read() as connection:
            request = connection.execute(
                "SELECT request FROM analysis.agent_task WHERE id = %s", [queued["request_id"]]
            ).fetchone()["request"]
        envelope = request["request_envelope"]
        assert envelope["request"] == envelope["request_id"] == queued["request_id"]
        assert envelope["task"] == "option_thesis"
        assert envelope["role"] == "expression-specialist"
        assert envelope["objective"] == "falsifiable_option_thesis"
        assert envelope["ticker"] == "NVDA"
        assert envelope["cutoff"] is None
        assert envelope["authority"] == "hypothesis_only"
        assert envelope["evidence_refs"] == [{"type": "agent_request", "id": queued["request_id"]}]
    finally:
        runtime.close()


def test_option_agent_rejects_unknown_reference_before_materialization(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        queued = AgentRepository(runtime).queue_thesis("NVDA", trigger="forged-reference")
        result = _option_thesis_result("NVDA", request_id=queued["request_id"])
        result["evidence_refs"] = [{"type": "source_signal", "id": "forged"}]
        with pytest.raises(ValueError, match="unknown or unavailable evidence"):
            AgentRepository(runtime).submit("option_thesis", result)
        with runtime.read() as connection:
            task = connection.execute(
                "SELECT status, result FROM analysis.agent_task WHERE id = %s", [queued["request_id"]]
            ).fetchone()
        assert task["status"] == "queued"
        assert task["result"] is None
    finally:
        runtime.close()


def test_agent_context_excludes_source_from_future_ingest_run(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
    source_id = f"agent-cutoff-{uuid4().hex}"
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('PITX', 'PITX', 'equity') "
                "RETURNING id"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO ingest.source
                    (id, name, family, kind, operational_state, health_owner, freshness_seconds)
                VALUES (%s, 'PITX test source', 'news', 'news', 'active', 'test', 3600)
                """,
                [source_id],
            )
            ingest_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'news', %s, %s, 'succeeded') RETURNING id
                """,
                [source_id, cutoff - timedelta(hours=2), cutoff + timedelta(minutes=1)],
            ).fetchone()
            item = connection.execute(
                """
                INSERT INTO raw.content_item
                    (source_id, ingest_run_id, source_key, kind, title, published_at, observed_at)
                VALUES (%s, %s, 'pitx-before-cutoff', 'article', 'PITX before cutoff', %s, %s)
                RETURNING id
                """,
                [source_id, ingest_run["id"], cutoff - timedelta(hours=3), cutoff - timedelta(hours=3)],
            ).fetchone()
            connection.execute(
                "INSERT INTO raw.content_item_instrument (content_item_id, instrument_id) VALUES (%s, %s)",
                [item["id"], instrument["id"]],
            )

        with runtime.read() as connection:
            evidence = thesis_source_evidence(connection, ["PITX"], cutoff=cutoff)
        assert evidence.get("PITX", []) == []
    finally:
        runtime.close()


def test_agent_source_evidence_duplicate_urls_have_stable_order(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
    source_id = f"agent-duplicate-url-{uuid4().hex}"
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('PITD', 'PITD', 'equity') "
                "RETURNING id"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO ingest.source
                    (id, name, family, kind, operational_state, health_owner, freshness_seconds)
                VALUES (%s, 'PITD test source', 'news', 'news', 'active', 'test', 3600)
                """,
                [source_id],
            )
            ingest_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'news', %s, %s, 'succeeded') RETURNING id
                """,
                [source_id, cutoff - timedelta(hours=3), cutoff - timedelta(hours=2)],
            ).fetchone()
            for title in ("PITD first", "PITD second"):
                item = connection.execute(
                    """
                    INSERT INTO raw.content_item
                        (source_id, ingest_run_id, source_key, kind, title, url, published_at, observed_at, summary)
                    VALUES (%s, %s, %s, 'article', %s, 'https://example.test/duplicate', %s, %s, %s)
                    RETURNING id
                    """,
                    [
                        source_id, ingest_run["id"], title.lower().replace(" ", "-"), title,
                        cutoff - timedelta(hours=1), cutoff - timedelta(hours=1), title,
                    ],
                ).fetchone()
                connection.execute(
                    "INSERT INTO raw.content_item_instrument (content_item_id, instrument_id) VALUES (%s, %s)",
                    [item["id"], instrument["id"]],
                )
        with runtime.read() as connection:
            first = thesis_source_evidence(connection, ["PITD"], max_per_symbol=2, cutoff=cutoff)
            second = thesis_source_evidence(connection, ["PITD"], max_per_symbol=2, cutoff=cutoff)
        first_titles = [item["title"] for item in first["PITD"]]
        assert first_titles == [item["title"] for item in second["PITD"]]
        assert first_titles == ["PITD first", "PITD second"]
    finally:
        runtime.close()


def test_agent_context_rejects_unavailable_or_unfinished_signal_runs(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
    source_id = f"agent-signal-{uuid4().hex}"
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('PITS', 'PITS', 'equity') "
                "RETURNING id"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO ingest.source
                    (id, name, family, kind, operational_state, health_owner, freshness_seconds)
                VALUES (%s, 'PITS test source', 'news', 'news', 'active', 'test', 3600)
                """,
                [source_id],
            )
            ingest_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'news', %s, %s, 'succeeded') RETURNING id
                """,
                [source_id, cutoff - timedelta(hours=3), cutoff - timedelta(hours=2)],
            ).fetchone()
            item = connection.execute(
                """
                INSERT INTO raw.content_item
                    (source_id, ingest_run_id, source_key, kind, title, published_at, observed_at, summary)
                VALUES (%s, %s, 'pits-signal-provenance', 'article', 'PITS raw title', %s, %s, 'PITS raw truth')
                RETURNING id
                """,
                [source_id, ingest_run["id"], cutoff - timedelta(hours=3), cutoff - timedelta(hours=3)],
            ).fetchone()
            connection.execute(
                "INSERT INTO raw.content_item_instrument (content_item_id, instrument_id) VALUES (%s, %s)",
                [item["id"], instrument["id"]],
            )
            analysis_runs = []
            for status, finished_at in (
                ("failed", cutoff - timedelta(hours=1)),
                ("succeeded", cutoff + timedelta(minutes=1)),
                ("succeeded", cutoff - timedelta(hours=1)),
            ):
                analysis_runs.append(connection.execute(
                    """
                    INSERT INTO analysis.run
                        (run_type, input_cutoff, code_version, input_hash, started_at, finished_at, status)
                    VALUES ('agent-signal-test', %s, 'test', %s, %s, %s, %s) RETURNING id
                    """,
                    [cutoff, "a" * 64, finished_at - timedelta(minutes=1), finished_at, status],
                ).fetchone()["id"])
            signal_specs = [
                (analysis_runs[0], cutoff - timedelta(hours=2), cutoff - timedelta(hours=2), cutoff - timedelta(hours=2), "failed-run"),
                (analysis_runs[1], cutoff - timedelta(hours=2), cutoff - timedelta(hours=2), cutoff - timedelta(hours=2), "future-run"),
                (analysis_runs[2], None, cutoff - timedelta(hours=2), cutoff - timedelta(hours=2), "missing-availability"),
                (analysis_runs[2], cutoff - timedelta(hours=2), cutoff + timedelta(hours=1), cutoff - timedelta(hours=2), "future-event"),
                (analysis_runs[2], cutoff - timedelta(hours=2), cutoff - timedelta(hours=2), cutoff + timedelta(hours=1), "future-publication"),
            ]
            for index, (run_id, available_at, event_at, published_at, signal_name) in enumerate(signal_specs):
                connection.execute(
                    """
                    INSERT INTO analysis.source_signal
                        (run_id, content_item_id, instrument_id, observed_at, available_at, event_at,
                         published_at, signal_type, sentiment, thesis)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'positive', %s)
                    """,
                    [
                        run_id, item["id"], instrument["id"], cutoff - timedelta(minutes=index + 1),
                        available_at, event_at, published_at, f"invalid-signal-{signal_name}",
                        f"invalid signal {signal_name}",
                    ],
                )

        with runtime.read() as connection:
            evidence = thesis_source_evidence(connection, ["PITS"], cutoff=cutoff)
        assert evidence["PITS"][0]["summary"] == "PITS raw truth"
        assert evidence["PITS"][0]["sentiment"] == "neutral"
    finally:
        runtime.close()


def test_agent_context_applies_cutoff_to_quote_catalyst_and_publication(
    postgres_dsn: str,
) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    cutoff = datetime(2026, 8, 28, 15, tzinfo=UTC)
    source_id = f"agent-context-{uuid4().hex}"
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('PITC', 'PITC', 'equity') "
                "RETURNING id"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO ingest.source
                    (id, name, family, kind, operational_state, health_owner, freshness_seconds)
                VALUES (%s, 'PITC test source', 'price', 'price', 'active', 'test', 3600)
                """,
                [source_id],
            )
            ingest_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'prices', %s, %s, 'succeeded') RETURNING id
                """,
                [source_id, cutoff - timedelta(hours=4), cutoff - timedelta(hours=3)],
            ).fetchone()
            old_quote = connection.execute(
                """
                INSERT INTO raw.quote
                    (instrument_id, source_id, ingest_run_id, observed_at, available_at, price)
                VALUES
                    (%s, %s, %s, %s, %s, 100)
                RETURNING id, available_at
                """,
                [
                    instrument["id"], source_id, ingest_run["id"], cutoff - timedelta(hours=2),
                    cutoff - timedelta(hours=2),
                ],
            ).fetchone()
            future_quote = connection.execute(
                """
                INSERT INTO raw.quote
                    (instrument_id, source_id, ingest_run_id, observed_at, available_at, price)
                VALUES (%s, %s, %s, %s, %s, 200)
                RETURNING id, available_at
                """,
                [
                    instrument["id"], source_id, ingest_run["id"], cutoff + timedelta(hours=1),
                    cutoff + timedelta(hours=1),
                ],
            ).fetchone()
            for quote in (old_quote, future_quote):
                connection.execute(
                    """
                    INSERT INTO raw.quote_fact_availability (fact_id, fact_available_at, ingest_run_id)
                    VALUES (%s, %s, %s)
                    """,
                    [quote["id"], quote["available_at"], ingest_run["id"]],
                )
            event = connection.execute(
                """
                INSERT INTO raw.market_event
                    (instrument_id, source_id, ingest_run_id, source_key, event_scope, event_kind,
                     title, starts_at, verification_status, available_at)
                VALUES (%s, %s, %s, 'pitc-event', 'ticker', 'earnings', 'valid catalyst', %s, 'confirmed', %s)
                RETURNING id
                """,
                [instrument["id"], source_id, ingest_run["id"], cutoff + timedelta(hours=1), cutoff - timedelta(hours=2)],
            ).fetchone()
            connection.execute(
                """
                INSERT INTO raw.market_event_version
                    (market_event_id, instrument_id, source_id, ingest_run_id, source_key,
                     event_scope, event_kind, title, starts_at, verification_status, available_at)
                VALUES (%s, %s, %s, %s, 'pitc-event-v1', 'ticker', 'earnings',
                        'valid catalyst', %s, 'confirmed', %s)
                """,
                [event["id"], instrument["id"], source_id, ingest_run["id"],
                 cutoff + timedelta(hours=1), cutoff - timedelta(hours=2)],
            )
            historical_catalyst = connection.execute(
                """
                INSERT INTO app.catalyst
                    (instrument_id, market_event_id, event_key, source_id, starts_at, title, created_at,
                     version, status, superseded_at)
                VALUES (%s, %s, 'pitc-valid', %s, %s, 'valid catalyst', %s, 1, 'superseded', %s)
                RETURNING id
                """,
                [
                    instrument["id"], event["id"], source_id, cutoff + timedelta(hours=1),
                    cutoff - timedelta(hours=1), cutoff + timedelta(minutes=30),
                ],
            ).fetchone()
            connection.execute(
                """
                INSERT INTO app.catalyst
                    (instrument_id, market_event_id, event_key, source_id, starts_at, title, created_at,
                     version, status, supersedes_id)
                VALUES (%s, %s, 'pitc-valid', %s, %s, 'changed later', %s, 2, 'current', %s)
                """,
                [
                    instrument["id"], event["id"], source_id, cutoff + timedelta(hours=2),
                    cutoff + timedelta(hours=1), historical_catalyst["id"],
                ],
            )
            connection.execute(
                """
                INSERT INTO app.catalyst
                    (instrument_id, event_key, source_id, starts_at, title, created_at)
                VALUES
                    (%s, 'pitc-past', %s, %s, 'past catalyst', %s),
                    (%s, 'pitc-mismatch', %s, %s, 'mismatched catalyst', %s),
                    (%s, 'pitc-future', %s, %s, 'future-created catalyst', %s)
                """,
                [
                    instrument["id"], source_id, cutoff - timedelta(hours=1), cutoff - timedelta(hours=1),
                    instrument["id"], source_id, cutoff + timedelta(hours=2), cutoff - timedelta(hours=1),
                    instrument["id"], source_id, cutoff + timedelta(hours=2), cutoff + timedelta(hours=1),
                ],
            )
            publication_runs = []
            for suffix, finished_at in (("old", cutoff - timedelta(hours=1)), ("future", cutoff + timedelta(hours=1))):
                publication_runs.append(connection.execute(
                    """
                    INSERT INTO analysis.run
                        (run_type, input_cutoff, code_version, input_hash, started_at, finished_at, status)
                    VALUES ('agent-context-test', %s, 'test', %s, %s, %s, 'succeeded') RETURNING id
                    """,
                    [cutoff, ("a" if suffix == "old" else "b") * 64,
                     finished_at - timedelta(minutes=1), finished_at],
                ).fetchone()["id"])
            for run_id, published_at, scope, stable_key, value in (
                (publication_runs[0], cutoff - timedelta(hours=1), "agent-context-test-old", "old", "old"),
                (publication_runs[1], cutoff + timedelta(hours=1), "agent-context-test-future", "future", "future"),
            ):
                publication = connection.execute(
                    """
                    INSERT INTO app.publication (scope, analysis_run_id, status, published_at)
                    VALUES (%s, %s, 'published', %s) RETURNING id
                    """,
                    [scope, run_id, published_at],
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO app.publication_item
                        (publication_id, model_name, stable_key, rank, instrument_id, payload)
                    VALUES (%s, 'agent_context_model', %s, 1, %s, %s)
                    """,
                    [
                        publication["id"], stable_key, instrument["id"],
                        Jsonb({"ticker": "PITC", "value": value}),
                    ],
                )

        with runtime.read() as connection:
            context = ticker_context(connection, "PITC", cutoff=cutoff)
        assert context["portfolio"]["price"] == 100.0
        assert context["published_models"]["agent_context_model"]["value"] == "old"
        assert [item["title"] for item in context["catalysts"]] == ["valid catalyst"]
    finally:
        runtime.close()


def test_agent_context_without_cutoff_is_unavailable(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('MISSING-CUTOFF', 'Mutable', 'equity') "
                "RETURNING id"
            ).fetchone()
            connection.execute(
                "INSERT INTO app.portfolio_position (instrument_id, quantity, average_cost, notes) VALUES (%s, 5, 10, 'mutable')",
                [instrument["id"]],
            )
            connection.execute(
                "INSERT INTO app.thesis (instrument_id, revision, status, thesis) VALUES (%s, 1, 'current', %s)",
                [instrument["id"], Jsonb({"core_thesis": "mutable thesis"})],
            )
        with runtime.read() as connection:
            context = ticker_context(connection, "MISSING-CUTOFF", cutoff=None)
        assert context["context_status"] == {"cutoff": None, "cutoff_available": False}
        assert context["portfolio"] == {}
        assert context["option_opportunity"] == {}
        assert context["published_models"] == {}
        assert context["catalysts"] == []
        assert context["source_evidence"] == []
    finally:
        runtime.close()


def test_agent_thesis_identity_mismatches_fail_closed() -> None:
    request = {
        "ticker": "NVDA",
        "request_id": "request-1",
        "request_envelope": {
            "request": "request-1",
            "request_id": "request-1",
            "task": "option_thesis",
            "ticker": "NVDA",
            "decision_id": "decision-1",
            "evidence_refs": [
                {"type": "agent_request", "id": "request-1"},
                {"type": "decision", "id": "decision-1"},
            ],
        },
    }
    valid = _option_thesis_result("NVDA", request_id="request-1")
    valid["decision_id"] = "decision-1"
    valid["evidence_refs"] = [
        {"type": "agent_request", "id": "request-1"},
        {"type": "decision", "id": "decision-1"},
    ]
    validate_result("option_thesis", valid, request=request, task_id="request-1")
    for field, value in (
        ("ticker", "MSFT"),
        ("request_id", "request-2"),
        ("decision_id", "decision-2"),
    ):
        invalid = dict(valid)
        invalid[field] = value
        with pytest.raises(ValueError):
            validate_result("option_thesis", invalid, request=request, task_id="request-1")


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
                "(strategy_key, revision, name, status, parameters, authority_group, promoted_at) "
                "VALUES ('options-radar-core', 1, 'core', 'active', %s, "
                "'options-radar-core', now()) RETURNING id",
                [Jsonb({"gates": {"max_spread_pct": 0.25}})],
            ).fetchone()["id"]
            candidate_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, supersedes_id, authority_group) "
                "VALUES ('new-v2', 1, 'new-v2', 'candidate', %s, %s, "
                "'options-radar-core') RETURNING id",
                [Jsonb({"max_spread_pct": 0.2}), base_id],
            ).fetchone()["id"]
            for evaluation_type in ("backtest", "forward_shadow_test"):
                connection.execute(
                    "INSERT INTO analysis.strategy_evaluation "
                    "(strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics) "
                    "VALUES (%s, %s, now(), 'pass', %s)",
                    [candidate_id, evaluation_type, Jsonb({"sample_size": 100})],
                )
            analysis_run_id = connection.execute(
                "INSERT INTO analysis.run "
                "(run_type, input_cutoff, code_version, strategy_revision_id, input_hash, "
                "started_at, finished_at, status) "
                "VALUES ('options-radar', now(), 'old-strategy', %s, %s, now(), now(), "
                "'succeeded') RETURNING id",
                [base_id, "c" * 64],
            ).fetchone()["id"]
            publication_id = connection.execute(
                "INSERT INTO app.publication (scope, analysis_run_id, status, published_at) "
                "VALUES ('options-radar', %s, 'published', now()) RETURNING id",
                [analysis_run_id],
            ).fetchone()["id"]
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
            publication_status = connection.execute(
                "SELECT status FROM app.publication WHERE id = %s", [publication_id]
            ).fetchone()["status"]
        assert promotion["validation"] == {"status": "promoted", "approved_by": "joe"}
        assert publication_status == "superseded"
        with runtime.transaction() as connection:
            sibling_proposal = connection.execute(
                "INSERT INTO analysis.agent_task (task_kind, status, request, result) "
                "VALUES ('strategy_mutation_proposal', 'completed', %s, %s) RETURNING id",
                [Jsonb({"source": "sibling"}), Jsonb({"status": "approved", "proposed_strategy_version": "sibling-v2"})],
            ).fetchone()["id"]
            sibling_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, supersedes_id, authority_group) "
                "VALUES ('sibling-v2', 1, 'sibling', 'candidate', %s, %s, "
                "'options-radar-core') RETURNING id",
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
        alias = repository.materialize_postmortem(
            _postmortem_task(runtime),
            {"proposed_parameter_changes": {"reject_spread_pct": 0.05}},
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
        assert alias["strategy_backtests"] == 1
        assert candidates[0]["parameters"]["gates"]["min_dte"] == 30
        assert candidates[2]["parameters"]["gates"]["max_spread_pct"] == 0.05
        assert "reject_spread_pct" not in candidates[2]["parameters"]["gates"]
        assert [row["verdict"] for row in verdicts] == [
            "collecting_data", "unsupported_parameters", "collecting_data"
        ]
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
                "VALUES ('OUTL', 'equity') RETURNING id"
            ).fetchone()["id"]
            strategy_id = connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, authority_group, promoted_at) "
                "VALUES ('unrelated-strategy', 1, 'unrelated', 'active', %s, "
                "'unrelated-strategy', now()) RETURNING id",
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
