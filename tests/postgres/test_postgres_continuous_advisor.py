from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from investment_panel.core.continuous_advisor import build_evidence_packet, packet_fingerprint
from investment_panel.infrastructure.postgres.authority import runtime_for_url
from investment_panel.infrastructure.postgres.continuous_advisor import ContinuousAdvisorRepository
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.migrations import upgrade_database
from investment_panel.infrastructure.postgres.thesis import save_thesis
from conftest import typed_config


def _packet(
    symbol: str = "ACME",
    *,
    prompt_version: str = "continuous_v1",
    cutoff: datetime = datetime(2026, 9, 8, 15, tzinfo=UTC),
) -> dict:
    return build_evidence_packet(
        {"symbol": symbol},
        {
            "context_status": {"cutoff_available": True, "cutoff": cutoff.isoformat()},
            "portfolio": {"price": 100, "quote_observed_at": cutoff, "owned": True},
            "source_evidence": [{"reference": "fixture:1", "source_type": "fundamental", "observed_at": cutoff}],
        },
        cutoff=cutoff,
        prompt_version=prompt_version,
    )


def test_continuous_records_are_idempotent_and_claims_are_immutable(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    runtime = runtime_for_url(postgres_dsn)
    repository = ContinuousAdvisorRepository(runtime)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('ACME', 'ACME', 'equity') ON CONFLICT (symbol) DO NOTHING")
    packet = repository.store_packet(_packet())
    same_packet = repository.store_packet(_packet())
    assert same_packet["id"] == packet["id"]
    retry = dict(_packet())
    retry["cutoff"] = datetime(2026, 9, 8, 15, 5, tzinfo=UTC).isoformat()
    assert retry["fingerprint"] == packet["fingerprint"]
    assert packet_fingerprint(retry) != packet["fingerprint"]
    with pytest.raises(psycopg.Error, match="identity or fingerprint"):
        repository.store_packet(retry)
    claim = repository.claim_review(
        packet,
        request={"workflow": "continuous_advisor", "symbol": "ACME", "packet_id": packet["id"], "packet_fingerprint": packet["fingerprint"], "cutoff": packet["cutoff"], "slot_start": packet["slot_start"], "prompt_version": packet["prompt_version"]},
        provider="codex",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    duplicate = repository.claim_review(
        packet,
        request={"workflow": "continuous_advisor", "symbol": "ACME", "packet_id": packet["id"], "packet_fingerprint": packet["fingerprint"], "cutoff": packet["cutoff"], "slot_start": packet["slot_start"], "prompt_version": packet["prompt_version"]},
        provider="codex",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    assert claim["created"] is True
    assert duplicate["created"] is False
    response = {
        "symbol": "ACME",
        "thesis": {"core_thesis": "Fixture thesis"},
        "countercase": "Fixture countercase",
        "forecasts": [{"claim_key": "f1", "statement": "up", "horizon": "1d", "direction": "up", "probability": 0.6, "evidence_refs": ["fixture:1"]}],
        "invalidations": [{"claim_key": "i1", "condition": "breaks", "horizon": "1m", "probability": 0.2, "evidence_refs": ["fixture:1"]}],
        "evidence_refs": ["fixture:1"],
        "next_review_trigger": "fixture event",
        "change_since_prior": "none",
    }
    finished = repository.finish_review(claim["task_id"], status="succeeded", response=response, validation={"schema_valid": True, "evidence_valid": True}, usage={"input_tokens": 10, "output_tokens": 20}, cost_usd=0.01)
    assert finished["status"] == "succeeded"
    runs = repository.list_runs(symbol="ACME")
    assert runs[0]["response_status"] == "succeeded"
    assert len(repository.ticker_briefs(symbols=["ACME"])) == 1
    assert repository.replay_scorecards()[0]["matched_outcomes"] == 0
    detail = repository.run_detail(claim["task_id"])
    assert detail is not None
    assert detail["packet"]["fingerprint"] == packet["fingerprint"]
    assert len(detail["claims"]) == 2

    later_packet = repository.store_packet(_packet(cutoff=datetime(2026, 9, 8, 15, 5, tzinfo=UTC)))
    later_claim = repository.claim_review(
        later_packet,
        request={"workflow": "continuous_advisor", "symbol": "ACME", "packet_id": later_packet["id"], "packet_fingerprint": later_packet["fingerprint"], "cutoff": later_packet["cutoff"], "slot_start": later_packet["slot_start"], "prompt_version": later_packet["prompt_version"]},
        provider="codex",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    repository.finish_review(later_claim["task_id"], status="skipped", validation={"reason": "evidence_blocked"})
    brief = repository.ticker_briefs(symbols=["ACME"])[0]
    assert brief["verdict"]["thesis"] == "Fixture thesis"
    assert brief["provenance"]["response_id"] == finished["response_id"]
    assert brief["provenance"]["latest_response_status"] == "skipped"
    assert "latest_run_skipped" in brief["verdict"]["blockers"]
    with pytest.raises(psycopg.Error):
        with psycopg.connect(postgres_dsn) as connection:
            connection.execute("UPDATE analysis.continuous_advisor_packet SET blockers = '[]' WHERE id = %s", [packet["id"]])


def test_ticker_brief_keeps_failed_run_provenance_without_success(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    repository = ContinuousAdvisorRepository(runtime_for_url(postgres_dsn))
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('FAILED', 'FAILED', 'equity') ON CONFLICT (symbol) DO NOTHING")
    packet = repository.store_packet(_packet("FAILED"))
    task = repository.claim_review(
        packet,
        request={"workflow": "continuous_advisor", "symbol": "FAILED", "packet_id": packet["id"], "packet_fingerprint": packet["fingerprint"], "cutoff": packet["cutoff"], "slot_start": packet["slot_start"], "prompt_version": packet["prompt_version"]},
        provider="codex", model="gpt-5.6-luna", reasoning_effort="high",
    )
    finished = repository.finish_review(
        task["task_id"], status="failed", validation={"reason": "fixture failure"}, error="fixture failure",
    )

    brief = repository.ticker_briefs(symbols=["FAILED"])[0]
    assert brief["provenance"]["response_id"] == finished["response_id"]
    assert brief["provenance"]["latest_response_id"] == finished["response_id"]
    assert brief["provenance"]["latest_finished_at"] is not None


def test_active_and_challenger_are_scored_on_the_same_frozen_packet(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    runtime = runtime_for_url(postgres_dsn)
    repository = ContinuousAdvisorRepository(runtime)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('MATCH', 'MATCH', 'equity') ON CONFLICT (symbol) DO NOTHING")
    repository.ensure_prompt_version({"version": "active_v1", "template": {}, "approved_change_set": []})
    repository.ensure_prompt_version({"version": "candidate_v1", "parent_version": "active_v1", "template": {"forecast_instruction": "calibrated"}, "approved_change_set": ["forecast_instruction"]})
    response = {
        "symbol": "MATCH",
        "thesis": {"core_thesis": "Fixture thesis"},
        "countercase": "Fixture countercase",
        "forecasts": [{"claim_key": "f1", "statement": "up", "horizon": "1d", "direction": "up", "probability": 0.6, "evidence_refs": ["fixture:1"]}],
        "invalidations": [{"claim_key": "i1", "condition": "breaks", "horizon": "1m", "probability": 0.2, "evidence_refs": ["fixture:1"]}],
        "evidence_refs": ["fixture:1"],
        "next_review_trigger": "fixture event",
        "change_since_prior": "none",
    }
    for version in ("active_v1", "candidate_v1"):
        packet = repository.store_packet(_packet("MATCH", prompt_version=version))
        task = repository.claim_review(
            packet,
            request={"workflow": "continuous_advisor", "symbol": "MATCH", "packet_id": packet["id"], "packet_fingerprint": packet["fingerprint"], "cutoff": packet["cutoff"], "slot_start": packet["slot_start"], "prompt_version": version},
            provider="codex",
            model="gpt-5.6-luna",
            reasoning_effort="high",
        )
        repository.finish_review(task["task_id"], status="succeeded", response=response, validation={"schema_valid": True, "evidence_valid": True})
        claim_id = repository.run_detail(task["task_id"])["claims"][0]["claim_id"]
        repository.record_outcome(claim_id, {"status": "resolved", "correct": True, "evidence_valid": True, "actual_return": 0.01, "excess_return": 0.01})

    cohort = repository.matched_cohort_scorecards("active_v1", "candidate_v1")
    assert cohort["common_frozen_packets"] == 1
    assert cohort["active"]["full"]["matched_outcomes"] == 1
    assert cohort["candidate"]["full"]["matched_outcomes"] == 1
    assert cohort["active"]["full"]["excess_return_accuracy"] == 1
    assert cohort["candidate"]["full"]["excess_return_accuracy"] == 1
    repository.record_promotion(
        {
            "candidate_prompt_version": "candidate_v1",
            "previous_active_prompt_version": "active_v1",
            "decision": "reject",
            "reason": "fixture rejection",
        }
    )
    assert repository.prompt_status("active_v1")["challenger"] is None


def test_prompt_activation_is_scoped_to_the_configured_lineage(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    repository = ContinuousAdvisorRepository(runtime_for_url(postgres_dsn))
    for version, parent in (("root_a", None), ("candidate_a", "root_a"), ("root_b", None), ("candidate_b", "root_b")):
        repository.ensure_prompt_version({"version": version, "parent_version": parent, "template": {}, "approved_change_set": []})
    repository.record_promotion({
        "candidate_prompt_version": "candidate_b",
        "previous_active_prompt_version": "root_b",
        "decision": "activate",
        "reason": "fixture",
        "scorecard": {},
    })

    assert repository.active_prompt_version("root_a") == "root_a"
    assert repository.active_prompt_version("root_b") == "candidate_b"


def test_prompt_template_inherits_parent_directives(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    repository = ContinuousAdvisorRepository(runtime_for_url(postgres_dsn))
    repository.ensure_prompt_version({
        "version": "inherit_root",
        "template": {"evidence_order": ["source_evidence"], "max_packet_tokens": 512},
        "approved_change_set": [],
    })
    repository.ensure_prompt_version({
        "version": "inherit_child",
        "parent_version": "inherit_root",
        "template": {"forecast_instruction": "Use calibrated probabilities."},
        "approved_change_set": ["forecast_instruction"],
    })

    assert repository.prompt_template("inherit_child") == {
        "evidence_order": ["source_evidence"],
        "max_packet_tokens": 512,
        "forecast_instruction": "Use calibrated probabilities.",
    }


def test_application_role_can_persist_advisor_thesis_revision(application_postgres_dsn: str):
    saved = save_thesis(
        typed_config(application_postgres_dsn),
        "ADVISORAPP",
        {
            "thesis": "Application-role thesis update",
            "why_owned_watched": "Application-role fixture",
            "author_kind": "ai",
            "countercase": "Application-role countercase",
        },
    )
    assert saved["symbol"] == "ADVISORAPP"


def test_application_role_uses_advisor_security_definer_writers(application_postgres_dsn: str):
    repository = ContinuousAdvisorRepository(runtime_for_url(application_postgres_dsn))
    with psycopg.connect(application_postgres_dsn) as connection:
        connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('ADVISORWRITE', 'ADVISORWRITE', 'equity') ON CONFLICT (symbol) DO NOTHING"
        )
    packet = repository.store_packet(_packet("ADVISORWRITE"))
    assert packet["symbol"] == "ADVISORWRITE"
    assert repository.ensure_prompt_version({"version": "advisor_writer_v1", "template": {}, "approved_change_set": []}) == "advisor_writer_v1"


def test_ai_thesis_write_respects_manual_lock_and_expected_revision(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    config = typed_config(postgres_dsn)
    locked = save_thesis(
        config,
        "LOCKADVISOR",
        {"thesis": "Human decision", "automation_policy": "manual_lock"},
    )
    with pytest.raises(ValueError, match="manually locked"):
        save_thesis(
            config,
            "LOCKADVISOR",
            {"thesis": "AI decision", "author_kind": "ai"},
            expected_revision=locked["revision"],
        )
    with pytest.raises(ValueError, match="revision changed"):
        save_thesis(
            config,
            "LOCKADVISOR",
            {"thesis": "AI decision", "author_kind": "ai"},
            expected_revision=locked["revision"] - 1,
        )


def test_advisor_lineage_rolls_back_with_a_rejected_thesis_update(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    config = typed_config(postgres_dsn)
    locked = save_thesis(
        config,
        "ATOMICADVISOR",
        {"thesis": "Human decision", "automation_policy": "manual_lock"},
    )
    repository = ContinuousAdvisorRepository(runtime_for_url(postgres_dsn))
    packet = repository.store_packet(_packet("ATOMICADVISOR"))
    claim = repository.claim_review(
        packet,
        request={"workflow": "continuous_advisor", "symbol": "ATOMICADVISOR", "packet_id": packet["id"], "packet_fingerprint": packet["fingerprint"], "cutoff": packet["cutoff"], "slot_start": packet["slot_start"], "prompt_version": packet["prompt_version"]},
        provider="codex",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    with pytest.raises(ValueError, match="manually locked"):
        repository.finish_review(
            claim["task_id"],
            status="succeeded",
            response={"symbol": "ATOMICADVISOR", "thesis": {"core_thesis": "AI decision"}},
            thesis_update={"thesis": "AI decision", "author_kind": "ai"},
            expected_thesis_revision=locked["revision"],
        )
    with psycopg.connect(postgres_dsn) as connection:
        response_count = connection.execute(
            "SELECT count(*) FROM analysis.continuous_advisor_response WHERE task_id = %s",
            [claim["task_id"]],
        ).fetchone()[0]
        task_status = connection.execute(
            "SELECT status FROM analysis.agent_task WHERE id = %s",
            [claim["task_id"]],
        ).fetchone()[0]
    assert response_count == 0
    assert task_status == "running"


def test_expired_advisor_lease_cannot_finish_a_replaced_task(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    runtime = runtime_for_url(postgres_dsn)
    repository = ContinuousAdvisorRepository(runtime)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('LEASEADVISOR', 'LEASEADVISOR', 'equity') ON CONFLICT (symbol) DO NOTHING")
    packet = repository.store_packet(_packet("LEASEADVISOR"))
    claim = repository.claim_review(
        packet,
        request={"workflow": "continuous_advisor", "symbol": "LEASEADVISOR", "packet_id": packet["id"], "packet_fingerprint": packet["fingerprint"], "cutoff": packet["cutoff"], "slot_start": packet["slot_start"], "prompt_version": packet["prompt_version"]},
        provider="codex",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute(
            "UPDATE analysis.agent_task SET updated_at = now() - interval '16 minutes' WHERE id = %s",
            [claim["task_id"]],
        )
    ignored = repository.finish_review(claim["task_id"], status="succeeded", response={})
    assert ignored["ignored"] is True
    assert ignored["reason"] == "lease_expired"
    with psycopg.connect(postgres_dsn) as connection:
        task = connection.execute("SELECT status FROM analysis.agent_task WHERE id = %s", [claim["task_id"]]).fetchone()
        response_count = connection.execute("SELECT count(*) FROM analysis.continuous_advisor_response WHERE task_id = %s", [claim["task_id"]]).fetchone()[0]
    assert task[0] == "failed"
    assert response_count == 0


def test_replay_quote_lookup_does_not_cross_the_claim_horizon(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    runtime = runtime_for_url(postgres_dsn)
    ingestion = IngestionRepository(runtime)
    ingestion.register_source(
        "advisor-replay-quotes",
        name="Advisor replay quotes",
        family="market",
        kind="quote",
        operational_state="active",
        health_owner="test",
        freshness_seconds=3_600,
    )
    target = datetime.now(UTC) - timedelta(days=2)
    observed = target + timedelta(days=1)
    run = ingestion.start_run("advisor-replay-quotes", "quotes", started_at=observed)
    ingestion.store_quotes(
        run,
        "advisor-replay-quotes",
        [{"symbol": "BOUNDADVISOR", "observed_at": observed, "price": 101}],
    )
    ingestion.finish_run(run, "succeeded")
    repository = ContinuousAdvisorRepository(runtime)
    available_by = datetime.now(UTC) + timedelta(minutes=1)
    assert repository.quote_at_or_after("BOUNDADVISOR", target, available_by=available_by, observed_to=target) is None
    bounded = repository.quote_at_or_after(
        "BOUNDADVISOR", target, available_by=available_by, observed_to=observed
    )
    assert bounded is not None
    assert datetime.fromisoformat(str(bounded["observed_at"])).astimezone(UTC) == observed


def test_replay_invalidation_finds_a_crossing_before_endpoint_recovery(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    runtime = runtime_for_url(postgres_dsn)
    ingestion = IngestionRepository(runtime)
    ingestion.register_source(
        "advisor-invalidation-quotes",
        name="Advisor invalidation quotes",
        family="market",
        kind="quote",
        operational_state="active",
        health_owner="test",
        freshness_seconds=3_600,
    )
    target = datetime.now(UTC) - timedelta(days=2)
    crossed = target + timedelta(days=1)
    recovered = crossed + timedelta(hours=1)
    run = ingestion.start_run("advisor-invalidation-quotes", "quotes", started_at=crossed)
    ingestion.store_quotes(
        run,
        "advisor-invalidation-quotes",
        [
            {"symbol": "CROSSADVISOR", "observed_at": crossed, "price": 90},
            {"symbol": "CROSSADVISOR", "observed_at": recovered, "price": 110},
        ],
    )
    ingestion.finish_run(run, "succeeded")
    repository = ContinuousAdvisorRepository(runtime)

    crossing = repository.quote_crossing_at_or_after(
        "CROSSADVISOR", target, observed_to=recovered,
        available_by=datetime.now(UTC) + timedelta(minutes=1), below=True, threshold=100,
    )
    assert crossing is not None
    assert datetime.fromisoformat(str(crossing["observed_at"])).astimezone(UTC) == crossed
