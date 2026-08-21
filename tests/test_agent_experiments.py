from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from investment_panel.database.agent_experiments import AgentExperimentRepository, _arm_summary
from investment_panel.core.agent_providers import provider_cost, resolve_provider_selection
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs import run_agent_experiment
from investment_panel.providers.advisory import ProviderTokenMetadata, StructuredProviderResult


def test_paired_packet_extracts_explicit_refs_from_radar_thesis_payload() -> None:
    packet = run_agent_experiment._evidence_packet({
        "ticker": "TSLA",
        "ticket": {"legs": []},
        "thesis_payload": {
            "pillars": [
                {
                    "id": "proof-1",
                    "claim": "This prose must not become a reference.",
                    "evidence_refs": [
                        "https://data.sec.gov/api/xbrl/companyfacts/CIK0001318605.json",
                    ],
                },
            ],
            "provenance": {"evidence_refs": ["request:stable-agent-task"]},
            "invalidation_rules": [{"type": "event", "id": "not-source-evidence"}],
        },
    })

    refs = {(row["type"], row["id"]) for row in packet["evidence_refs"]}
    assert ("evidence_ref", "https://data.sec.gov/api/xbrl/companyfacts/CIK0001318605.json") in refs
    assert ("evidence_ref", "request:stable-agent-task") in refs
    assert ("event", "not-source-evidence") not in refs
    assert all("This prose" not in reference_id for _type, reference_id in refs)


def test_paired_experiment_freezes_evidence_and_enforces_daily_cap(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    reference = datetime(2026, 8, 12, 14, tzinfo=UTC)
    repository = AgentExperimentRepository(runtime)
    try:
        first = repository.queue_pair(
            role="thesis_survival",
            evidence_packet={"symbol": "NVDA", "price": 180, "sources": ["sec:1"]},
            prompt_version="v1",
            schema_version="v1",
            baseline_version="deterministic-v1",
            now=reference,
        )
        for index in range(11):
            repository.queue_pair(
                role="red_team",
                evidence_packet={"symbol": "NVDA", "ordinal": index},
                prompt_version="v1",
                schema_version="v1",
                baseline_version="deterministic-v1",
                now=reference,
            )
        with pytest.raises(ValueError, match="daily paired-task cap"):
            repository.queue_pair(
                role="postmortem",
                evidence_packet={"symbol": "NVDA"},
                prompt_version="v1",
                schema_version="v1",
                baseline_version="deterministic-v1",
                now=reference,
            )
        repository.record_result(
            task_id=UUID(first["champion_task_id"]),
            status="completed",
            validation_status="passed",
            validation_detail={"evidence_valid": True, "useful_advice": True},
            latency_ms=10_000,
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.10,
        )
        repository.record_result(
            task_id=UUID(first["challenger_task_id"]),
            status="completed",
            validation_status="passed",
            validation_detail={"evidence_valid": True, "useful_advice": True},
            latency_ms=9_000,
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.01,
        )
        summary = repository.current()
        with pytest.raises(ValueError, match="cannot be sealed"):
            repository.seal_report()
        with runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT arm, paired_task_id, evidence_fingerprint, request->'evidence_packet' AS evidence_packet
                FROM analysis.agent_task
                WHERE id = ANY(%s)
                ORDER BY arm
                """,
                [[UUID(first["champion_task_id"]), UUID(first["challenger_task_id"])]],
            ).fetchall()
    finally:
        runtime.close()

    assert summary is not None
    assert summary["advisory_only"] is True
    assert summary["routing_changed"] is False
    assert summary["progress"]["paired_tasks"] == 12
    assert len(rows) == 2
    assert rows[0]["evidence_fingerprint"] == rows[1]["evidence_fingerprint"] == first["evidence_fingerprint"]
    assert rows[0]["evidence_packet"] == rows[1]["evidence_packet"]
    assert str(rows[0]["paired_task_id"]) == first["champion_task_id"]
    assert str(rows[1]["paired_task_id"]) == first["challenger_task_id"]


def test_paired_worker_dispatches_both_providers_against_the_same_frozen_packet(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    reference = datetime(2026, 8, 12, 14, tzinfo=UTC)
    repository = AgentExperimentRepository(runtime)
    invocations: list[tuple[str, str, dict[str, object]]] = []

    def fake_invoke(request):
        invocations.append((request.provider, request.model, request.payload))
        return StructuredProviderResult(
            payload={
            "ticker": "NVDA",
            "direction": "long",
            "bull_target_price": 210,
            "bull_target_date": "2026-12-31",
            "base_target_price": 190,
            "bear_target_price": 160,
            "scenario_probabilities": {"base": 0.5, "bull": 0.3, "bear": 0.2},
            "preferred_structures": ["long_call"],
            "core_thesis": "Demand evidence remains above the deterministic baseline.",
            "required_proofs": ["source confirmation"],
            "catalysts": [{"type": "earnings", "expected_window": "next report", "what_to_watch": "guidance"}],
            "invalidation": ["guidance reverses"],
            "bear_case": "Demand falls.",
            "confidence": 0.6,
            "evidence_refs": [{"type": "filing", "id": "sec:1"}],
            },
            provider=request.provider,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
            token_metadata=ProviderTokenMetadata(input_tokens=120, output_tokens=80),
        )

    monkeypatch.setattr(run_agent_experiment, "invoke_structured", fake_invoke)
    try:
        repository.queue_pair(
            role="thesis_survival",
            evidence_packet={
                "symbol": "NVDA",
                "evidence_refs": [{"type": "filing", "id": "sec:1"}],
            },
            prompt_version="v1",
            schema_version="v1",
            baseline_version="deterministic-v1",
            now=reference,
        )

        result = run_agent_experiment._process_pairs(
            repository,
            pricing={"default": {"input_per_1m": 1.0, "output_per_1m": 2.0}},
            timeout_seconds=90,
            now=reference,
            limit=1,
        )
        with runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT task.provider, task.model, task.status, task.validation_detail,
                       task.result, run.status AS run_status
                FROM analysis.agent_task task
                JOIN analysis.agent_run run ON run.id = task.agent_run_id
                WHERE task.experiment_id IS NOT NULL
                ORDER BY task.arm
                """
            ).fetchall()
    finally:
        runtime.close()

    assert result == {"processed_pairs": 1, "completed": 2, "failed": 0, "errors": []}
    assert [(provider, model) for provider, model, _ in invocations] == [
        ("codex", "gpt-5.6-luna"),
        ("deepseek", "deepseek-v4-flash"),
    ]
    assert invocations[0][2]["evidence_packet"] == invocations[1][2]["evidence_packet"]
    assert {row["status"] for row in rows} == {"completed"}
    assert {row["run_status"] for row in rows} == {"succeeded"}
    assert all(row["validation_detail"]["evidence_valid"] is True for row in rows)
    assert {row["provider"] for row in rows} == {"codex", "deepseek"}
    assert all(row["validation_detail"]["provider_telemetry"]["pricing"]["pricing_status"] == "provider_rate" for row in rows)
    assert all(row["validation_detail"]["provider_telemetry"]["reasoning_effort"] == "high" for row in rows)


def test_provider_registry_uses_distinct_verified_rate_cards() -> None:
    luna = resolve_provider_selection("codex", "gpt-5.6-luna", "high")
    deepseek = resolve_provider_selection("deepseek", "deepseek-v4-flash", "high")
    luna_cost, luna_detail = provider_cost(
        {"provider": "codex", "model": "gpt-5.6-luna", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
        selection=luna,
    )
    deepseek_cost, deepseek_detail = provider_cost(
        {"provider": "deepseek", "model": "deepseek-v4-flash", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
        selection=deepseek,
    )
    assert luna_detail["pricing_status"] == deepseek_detail["pricing_status"] == "provider_rate"
    assert luna_cost == 0.1
    assert deepseek_cost == 0.14
    assert luna_detail["rate"]["source"] != deepseek_detail["rate"]["source"]


def test_failure_and_validation_rates_keep_scheduled_tasks_in_the_denominator() -> None:
    rows = [
        {"status": "completed", "validation_status": "passed", "validation_detail": {"evidence_valid": True, "useful_advice": True}, "latency_ms": 1, "cost_usd": 0.01},
        {"status": "failed", "validation_status": "failed", "validation_detail": {}, "latency_ms": 2, "cost_usd": None},
        {"status": "queued", "validation_status": None, "validation_detail": {}, "latency_ms": None, "cost_usd": None},
    ]
    summary = _arm_summary(rows)
    assert summary["failure_rate"] == pytest.approx(1 / 3)
    assert summary["schema_validation_rate"] == pytest.approx(1 / 3)
    assert summary["evidence_validation_rate"] == pytest.approx(1 / 3)
    assert summary["unresolved"] == 1
    assert summary["all_scheduled_tasks_resolved"] is False
