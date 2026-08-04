from __future__ import annotations

import pytest
from datetime import UTC, datetime, timedelta

from investment_panel.core.options_recovery_agents import (
    MUTATION_DRAFTER,
    normalize_recovery_agent_output,
    recovery_agent_schema,
    validate_evidence,
)
from investment_panel.core.options_event_tape import EventObservation
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.option_events import OptionEventRepository
from investment_panel.database.options_recovery_agents import (
    RecoveryEventAgentRepository,
    _agent_trigger,
    _validate_output_for_persistence,
)
from investment_panel.database.runtime import DatabaseRuntime


def _output(*, mutation: object = None) -> dict[str, object]:
    return {
        "task_id": "task-1",
        "role": MUTATION_DRAFTER,
        "thesis": "The primary thesis remains intact.",
        "countercase": "Demand could soften.",
        "catalyst": "A sourced catalyst is pending.",
        "invalidation": "Evidence contradicts the thesis.",
        "evidence": [{"source": "Example", "url": "https://example.test/source", "claim": "A supported claim."}],
        "mutation": mutation,
    }


def test_agent_contract_rejects_authority_bearing_output_fields() -> None:
    row = _output()
    row["ticket_quantity"] = 99
    with pytest.raises(ValueError, match="unsupported output fields"):
        normalize_recovery_agent_output({"outputs": [row]}, expected_tasks=[{"id": "task-1", "role": MUTATION_DRAFTER}])


def test_agent_schema_expresses_nullable_mutation_as_distinct_strict_branches() -> None:
    mutation = recovery_agent_schema()["properties"]["outputs"]["items"]["properties"]["mutation"]

    assert "type" not in mutation
    object_branch, null_branch = mutation["anyOf"]
    assert object_branch["required"] == ["strategy_key", "changes"]
    assert object_branch["additionalProperties"] is False
    change = object_branch["properties"]["changes"]
    assert change["type"] == "array"
    assert change["items"]["required"] == ["key", "value"]
    assert change["items"]["additionalProperties"] is False
    assert null_branch == {"type": "null"}


def test_unknown_agent_mutation_is_rejected_before_proposal_persistence() -> None:
    normalized = normalize_recovery_agent_output(
        {"outputs": [_output(mutation={"strategy_key": "shock_reversal_call_v1", "changes": {"made_up_edge": 99}})]},
        expected_tasks=[{"id": "task-1", "role": MUTATION_DRAFTER}],
    )
    persisted, validation = _validate_output_for_persistence(normalized["task-1"])

    assert persisted["mutation"] is None
    assert validation["mutation_status"] == "rejected_unsupported_mutation"


def test_agent_evidence_ids_are_the_only_validated_evidence() -> None:
    accepted, proposals, valid = validate_evidence(
        [
            {"evidence_id": "source_signal:42", "source": "Persisted", "url": "https://persisted.test", "claim": "Bound fact"},
            {"evidence_id": "https://arbitrary.test", "source": "Arbitrary", "url": "https://arbitrary.test", "claim": "Unverified proposal"},
        ],
        evidence_bundle=[
            {"evidence_id": "source_signal:42", "source": "Persisted", "url": "https://persisted.test", "claim": "Bound fact"},
        ],
    )

    assert [row["evidence_id"] for row in accepted] == ["source_signal:42"]
    assert [row["url"] for row in proposals] == ["https://arbitrary.test"]
    assert valid is False


def test_agent_evidence_id_cannot_rewrite_a_bundled_record() -> None:
    accepted, proposals, valid = validate_evidence(
        [{
            "evidence_id": "source_signal:42",
            "source": "Forged source",
            "url": "https://arbitrary.test",
            "claim": "Forged claim",
        }],
        evidence_bundle=[{
            "evidence_id": "source_signal:42",
            "source": "Persisted",
            "url": "https://persisted.test",
            "claim": "Bound fact",
        }],
    )

    assert accepted == []
    assert proposals[0]["url"] == "https://arbitrary.test"
    assert valid is False


def test_material_trigger_requires_a_real_event_fingerprint_change() -> None:
    current = {
        "underlying_price": 102.1,
        "avg_iv": 0.42,
        "material_evidence_count": 3,
        "signal_families": ["shock_reversal_call_v1:shadow"],
    }
    previous = {
        "underlying_price": 100.0,
        "avg_iv": 0.40,
        "material_evidence_count": 2,
        "signal_families": ["shock_reversal_call_v1:shadow"],
    }

    trigger, reasons = _agent_trigger(current, previous, preopen=False)
    assert trigger == "underlying_move_2pct"
    assert set(reasons) == {"underlying_move_2pct", "new_material_evidence"}
    assert _agent_trigger(current, current, preopen=False) == (None, [])


class _AgentContextResult:
    def __init__(self, row: dict[str, object] | None = None, rows: list[dict[str, object]] | None = None) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self) -> dict[str, object] | None:
        return self.row

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _AgentContextConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, statement: str, params: object = None) -> _AgentContextResult:
        self.calls.append((statement, params))
        if "FROM analysis.option_event event" in statement:
            return _AgentContextResult({
                "id": "event-1", "instrument_id": 1, "started_at": datetime(2026, 8, 3, tzinfo=UTC),
                "reference_price": 100.0, "event_low": 90.0, "cohort_id": "cohort-1",
                "material_evidence_count": 0, "provenance": {}, "symbol": "NVDA",
            })
        if "count(*) AS count FROM analysis.option_event_capture" in statement:
            return _AgentContextResult({"count": 2})
        if "FROM analysis.option_event_capture" in statement:
            return _AgentContextResult({
                "id": "capture-1", "snapshot_id": "snapshot-1", "capture_generation_id": "generation-1",
                "finished_at": datetime(2026, 8, 3, 15, tzinfo=UTC),
            })
        if "FROM analysis.option_event_spot" in statement:
            return _AgentContextResult({"price": 95.0, "available_at": datetime(2026, 8, 3, 15, tzinfo=UTC)})
        if "avg(quote.provider_iv)" in statement:
            return _AgentContextResult({"avg_iv": 0.42})
        if "FROM analysis.option_event_signal" in statement:
            return _AgentContextResult(rows=[])
        if "FROM analysis.source_signal" in statement:
            return _AgentContextResult(rows=[])
        raise AssertionError(f"unexpected query: {statement}")


def test_agent_context_uses_the_capture_generation_for_iv_evidence() -> None:
    repository = RecoveryEventAgentRepository(runtime=object())
    connection = _AgentContextConnection()

    context = repository._event_context(connection, "event-1", None)

    assert context is not None
    assert context["avg_iv"] == pytest.approx(0.42)
    iv_params = next(params for statement, params in connection.calls if "avg(quote.provider_iv)" in statement)
    assert iv_params == ["event-1", "snapshot-1", "generation-1"]


def test_agent_failure_stays_isolated_from_event_capture(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        started = datetime(2026, 8, 3, 14, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
        event_id = OptionEventRepository(runtime).detect_events(
            [EventObservation("NVDA", started, 100.0, one_day_pct=-0.08, instrument_id=instrument_id)],
            now=started,
        )["active_events"][0]["event_id"]
        with runtime.transaction() as connection:
            for offset, price in ((15, 99.0), (30, 98.0)):
                slot = started + timedelta(minutes=offset)
                connection.execute(
                    """
                    INSERT INTO analysis.option_event_capture
                        (event_id, scheduled_at, finished_at, status, expected_contract_count, received_contract_count, completeness)
                    VALUES (%s, %s, %s, 'complete', 1, 1, 1.0)
                    """,
                    [event_id, slot, slot + timedelta(seconds=1)],
                )
                connection.execute(
                    """INSERT INTO analysis.option_event_spot (event_id, observed_at, available_at, price)
                       VALUES (%s, %s, %s, %s)""",
                    [event_id, slot, slot, price],
                )
        repository = RecoveryEventAgentRepository(runtime)
        queued = repository.queue_if_material(event_id, now=started + timedelta(minutes=31))
        assert queued["status"] == "queued"
        assert len(queued["tasks"]) == 4
        claim = repository.claim_next()
        assert claim is not None
        failed = repository.fail(claim, "synthetic Codex timeout")
        assert failed["status"] == "retrying"
        retry_one = repository.claim_next()
        assert retry_one is not None
        assert retry_one["batch"]["id"] == claim["batch"]["id"]
        assert repository.fail(retry_one, "synthetic Codex timeout")["status"] == "retrying"
        retry_two = repository.claim_next()
        assert retry_two is not None
        terminal = repository.fail(retry_two, "synthetic Codex timeout")
        assert terminal["status"] == "failed"
        assert terminal["attempt"] == 3
        with runtime.read() as connection:
            captures = connection.execute(
                "SELECT count(*) AS count FROM analysis.option_event_capture WHERE event_id = %s", [event_id]
            ).fetchone()
        assert captures["count"] == 2
    finally:
        runtime.close()
