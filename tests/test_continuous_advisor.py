from datetime import UTC, datetime, timedelta
import json

import pytest

from investment_panel.jobs import codex_thesis_monitor
from investment_panel.jobs import evolve_continuous_advisor
from investment_panel.jobs import replay_continuous_advisor
from investment_panel.jobs import run_continuous_advisor
from investment_panel.core.continuous_advisor import (
    ContinuousAdvisorValidationError,
    build_evidence_packet,
    compare_scorecards,
    mutate_prompt_template,
    packet_is_replay_safe,
    packet_fingerprint,
    promotion_gate,
    resolve_claim,
    score_claims,
    validate_continuous_response,
)


def _packet():
    cutoff = datetime(2026, 9, 8, 15, tzinfo=UTC)
    return build_evidence_packet(
        {"symbol": "ACME", "latest_price": 100, "latest_quote_at": cutoff},
        {
            "context_status": {"cutoff_available": True, "cutoff": cutoff.isoformat()},
            "portfolio": {"price": 100, "quote_observed_at": cutoff, "owned": True},
            "source_evidence": [{"reference": "source:1", "source_type": "fundamental", "observed_at": cutoff, "title": "Results"}],
            "published_models": {},
            "catalysts": [],
            "option_opportunity": {},
        },
        cutoff=cutoff,
        prompt_version="continuous_v1",
    )


def test_packet_is_deterministic_and_replay_safe():
    packet = _packet()
    assert packet_is_replay_safe(packet)
    assert packet["fingerprint"] == build_evidence_packet(
        {"symbol": "ACME", "latest_price": 100},
        {
            "context_status": {"cutoff_available": True, "cutoff": packet["cutoff"]},
            "portfolio": {"price": 100, "quote_observed_at": packet["cutoff"], "owned": True},
            "source_evidence": [{"reference": "source:1", "source_type": "fundamental", "observed_at": packet["cutoff"], "title": "Results"}],
        },
        cutoff=datetime.fromisoformat(packet["cutoff"]),
        prompt_version="continuous_v1",
    )["fingerprint"]
    changed = {**packet, "evidence": {**packet["evidence"], "prices": {"price": 101}}}
    assert not packet_is_replay_safe(changed)
    timestamp_changed = {**packet, "evidence": {**packet["evidence"], "source_evidence": [{"reference": "source:1", "source_type": "fundamental", "observed_at": "2026-09-08T14:59:00+00:00", "title": "Results"}]}}
    assert packet_fingerprint(timestamp_changed) != packet["fingerprint"]


def test_packet_rejects_future_and_stale_facts_without_using_them():
    cutoff = datetime(2026, 9, 8, 15, tzinfo=UTC)
    packet = build_evidence_packet(
        {"symbol": "ACME"},
        {
            "context_status": {"cutoff_available": True, "cutoff": cutoff.isoformat()},
            "portfolio": {"price": 100, "quote_observed_at": cutoff},
            "source_evidence": [
                {"reference": "future", "source_type": "news", "observed_at": cutoff + timedelta(minutes=1)},
                {"reference": "old", "source_type": "news", "observed_at": cutoff - timedelta(days=2)},
            ],
        },
        cutoff=cutoff,
        prompt_version="continuous_v1",
    )
    assert packet["source_references"] == []
    assert "evidence_future" in packet["blockers"]
    assert "evidence_stale" in packet["blockers"]


def test_packet_rejects_quote_available_after_cutoff():
    cutoff = datetime(2026, 9, 8, 15, tzinfo=UTC)
    packet = build_evidence_packet(
        {"symbol": "ACME", "latest_price": 100, "latest_quote_at": cutoff, "latest_quote_available_at": cutoff + timedelta(minutes=1)},
        {
            "context_status": {"cutoff_available": True, "cutoff": cutoff.isoformat()},
            "source_evidence": [{"reference": "source:1", "source_type": "fundamental", "observed_at": cutoff}],
        },
        cutoff=cutoff,
        prompt_version="continuous_v1",
    )
    assert packet["evidence"]["prices"] == {}
    assert "price_future" in packet["blockers"]


def test_packet_keeps_bounded_options_context():
    packet = build_evidence_packet(
        {"symbol": "ACME"},
        {
            "context_status": {"cutoff_available": True},
            "portfolio": {"price": 100, "quote_observed_at": datetime(2026, 9, 8, 15, tzinfo=UTC)},
            "option_opportunities": [{"structure": "CALL", "strike": 110, "entry_price": 2.5, "available_at": datetime(2026, 9, 8, 15, tzinfo=UTC)}],
            "source_evidence": [{"reference": "source:1", "source_type": "fundamental", "observed_at": datetime(2026, 9, 8, 15, tzinfo=UTC)}],
        },
        cutoff=datetime(2026, 9, 8, 15, tzinfo=UTC),
        prompt_version="continuous_v1",
    )
    assert packet["evidence"]["options"] == [{
        "structure": "CALL", "strike": 110, "entry_price": 2.5,
        "available_at": "2026-09-08T15:00:00+00:00",
    }]


def test_packet_drops_stale_thesis_and_unstamped_evidence():
    cutoff = datetime(2026, 9, 8, 15, tzinfo=UTC)
    packet = build_evidence_packet(
        {"symbol": "ACME", "updated_at": cutoff - timedelta(days=46)},
        {
            "context_status": {"cutoff_available": True, "cutoff": cutoff.isoformat()},
            "portfolio": {
                "price": 100,
                "quote_observed_at": cutoff,
                "thesis": {"core_thesis": "old"},
                "thesis_updated_at": cutoff - timedelta(days=46),
            },
            "source_evidence": [{"reference": "unstamped", "source_family": "news"}],
        },
        cutoff=cutoff,
        prompt_version="continuous_v1",
    )
    assert packet["evidence"]["thesis"] == {}
    assert packet["evidence"]["source_evidence"] == []
    assert "thesis_stale" in packet["blockers"]
    assert "evidence_unknown" in packet["blockers"]


def test_packet_does_not_fallback_to_an_untimestamped_current_thesis():
    cutoff = datetime(2026, 9, 8, 15, tzinfo=UTC)
    packet = build_evidence_packet(
        {"symbol": "ACME", "raw_thesis": {"core_thesis": "newer thesis"}},
        {
            "context_status": {"cutoff_available": True, "cutoff": cutoff.isoformat()},
            "portfolio": {"price": 100, "quote_observed_at": cutoff},
            "source_evidence": [{"reference": "source:1", "source_type": "fundamental", "observed_at": cutoff}],
        },
        cutoff=cutoff,
        prompt_version="continuous_v1",
    )
    assert packet["evidence"]["thesis"] == {}
    assert "thesis_unknown" in packet["blockers"]


def test_scorecard_uses_excess_return_direction():
    claim = {"claim_key": "f1", "claim_kind": "forecast", "direction": "up", "probability": 0.7}
    outcome = resolve_claim(claim, actual_return=0.05, excess_return=-0.02)
    scorecard = score_claims([claim], [{"claim_key": "f1", **outcome}])
    assert scorecard["directional_accuracy"] == 1
    assert scorecard["excess_return_accuracy"] == 0


def test_response_claims_are_packet_bound_and_scorecard_is_stable():
    packet = _packet()
    response = {
        "symbol": "ACME",
        "thesis": {"core_thesis": "Demand remains durable."},
        "countercase": "Multiple compression.",
        "evidence_refs": ["source:1"],
        "forecasts": [{"claim_key": "f1", "statement": "Price rises", "horizon": "1d", "direction": "up", "probability": 0.7, "evidence_refs": ["source:1"]}],
        "invalidations": [{"claim_key": "i1", "condition": "Demand breaks", "horizon": "1m", "probability": 0.2, "evidence_refs": ["source:1"]}],
        "next_review_trigger": "New earnings evidence",
        "change_since_prior": "No material change.",
    }
    validated = validate_continuous_response(response, packet)
    outcome = resolve_claim(validated["forecasts"][0], actual_return=0.05, evidence_valid=True)
    scorecard = score_claims(validated["forecasts"], [{"claim_key": "f1", **outcome}])
    assert scorecard["matched_outcomes"] == 1
    assert scorecard["directional_accuracy"] == 1
    assert compare_scorecards(scorecard, scorecard)["lower_confidence_bound"] == 0
    with pytest.raises(ContinuousAdvisorValidationError, match="outside packet"):
        validate_continuous_response({**response, "evidence_refs": ["foreign"]}, packet)
    with pytest.raises(ContinuousAdvisorValidationError, match="outside packet"):
        validate_continuous_response({**response, "thesis": {"core_thesis": "x", "evidence_links": ["foreign"]}}, packet)


def test_response_cannot_mutate_human_thesis_controls():
    packet = _packet()
    response = {
        "symbol": "ACME",
        "thesis": {"core_thesis": "Demand remains durable.", "automation_policy": "manual_lock"},
        "countercase": "Multiple compression.",
        "evidence_refs": ["source:1"],
        "forecasts": [{"claim_key": "f1", "statement": "Price rises", "horizon": "1d", "direction": "up", "probability": 0.7, "evidence_refs": ["source:1"]}],
        "invalidations": [{"claim_key": "i1", "condition": "Demand breaks", "horizon": "1m", "probability": 0.2, "evidence_refs": ["source:1"]}],
        "next_review_trigger": "New earnings evidence",
        "change_since_prior": "No material change.",
    }
    with pytest.raises(ContinuousAdvisorValidationError, match="human-owned control fields"):
        validate_continuous_response(response, packet)


def test_response_horizons_match_the_replay_grammar():
    packet = _packet()
    response = {
        "symbol": "ACME",
        "thesis": {"core_thesis": "Demand remains durable."},
        "countercase": "Multiple compression.",
        "evidence_refs": ["source:1"],
        "forecasts": [{"claim_key": "f1", "statement": "Price rises", "horizon": "intraday", "direction": "up", "probability": 0.7, "evidence_refs": ["source:1"]}],
        "invalidations": [{"claim_key": "i1", "condition": "Demand breaks", "horizon": "1m", "probability": 0.2, "evidence_refs": ["source:1"]}],
        "next_review_trigger": "New earnings evidence",
        "change_since_prior": "No material change.",
    }
    with pytest.raises(ContinuousAdvisorValidationError, match="incomplete"):
        validate_continuous_response(response, packet)


def test_forecast_and_invalidation_claim_keys_must_be_unique_together():
    packet = _packet()
    response = {
        "symbol": "ACME",
        "thesis": {"core_thesis": "Demand remains durable."},
        "countercase": "Multiple compression.",
        "evidence_refs": ["source:1"],
        "forecasts": [{"claim_key": "same", "statement": "Price rises", "horizon": "1d", "direction": "up", "probability": 0.7, "evidence_refs": ["source:1"]}],
        "invalidations": [{"claim_key": "same", "condition": "Demand breaks", "horizon": "1m", "probability": 0.2, "evidence_refs": ["source:1"]}],
        "next_review_trigger": "New earnings evidence",
        "change_since_prior": "No material change.",
    }
    with pytest.raises(ContinuousAdvisorValidationError, match="duplicate invalidation claim key"):
        validate_continuous_response(response, packet)


def test_claim_keys_must_not_be_blank_after_normalization():
    packet = _packet()
    response = {
        "symbol": "ACME",
        "thesis": {"core_thesis": "Demand remains durable."},
        "countercase": "Multiple compression.",
        "evidence_refs": ["source:1"],
        "forecasts": [{"claim_key": "   ", "statement": "Price rises", "horizon": "1d", "direction": "up", "probability": 0.7, "evidence_refs": ["source:1"]}],
        "invalidations": [{"claim_key": "i1", "condition": "Demand breaks", "horizon": "1m", "probability": 0.2, "evidence_refs": ["source:1"]}],
        "next_review_trigger": "New earnings evidence",
        "change_since_prior": "No material change.",
    }
    with pytest.raises(ContinuousAdvisorValidationError, match="empty claim key"):
        validate_continuous_response(response, packet)


def test_invalidation_claims_resolve_without_becoming_directional_accuracy():
    claim = {"claim_kind": "invalidation", "probability": 0.8, "claim_key": "i1"}
    outcome = resolve_claim(claim, actual_return=0.05, invalidated=True, evidence_valid=True)
    scorecard = score_claims([claim], [{"claim_key": "i1", **outcome}])
    assert outcome["status"] == "resolved"
    assert outcome["invalidation_correct"] is True
    assert scorecard["invalidation_accuracy"] == 1
    assert scorecard["directional_accuracy"] is None


def test_promotion_requires_production_floor_and_walk_forward():
    candidate = {"matched_outcomes": 3, "schema_validity_rate": 1, "evidence_validity_rate": 1, "quality_score": 0.9}
    gate = promotion_gate(candidate, walk_forward={"status": "pass"}, forward_session={"status": "pass"}, safety_checks={"passed": True})
    assert not gate["eligible"]
    assert "matched_outcomes_below_promotion_floor" in gate["blockers"]
    assert "non_positive_lower_confidence_bound" in gate["blockers"]


def test_prompt_mutation_rejects_execution_fields():
    mutation = mutate_prompt_template("continuous_v1", {"forecast_instruction": "Use calibrated probabilities."}, "Improve horizon wording.")
    assert mutation["parent_version"] == "continuous_v1"
    repeat = mutate_prompt_template("continuous_v1", {"forecast_instruction": "Use calibrated probabilities."}, "Retry after rejection.")
    assert repeat["version"] != mutation["version"]
    with pytest.raises(ContinuousAdvisorValidationError, match="not approved"):
        mutate_prompt_template("continuous_v1", {"risk_override": True}, "Unsafe")
    with pytest.raises(ContinuousAdvisorValidationError, match="max_packet_tokens"):
        mutate_prompt_template("continuous_v1", {"max_packet_tokens": "large"}, "Unsafe type")


def test_prompt_mutation_reaches_provider_and_orders_packet_copy(monkeypatch):
    captured = {}

    class Result:
        payload = {"ok": True}

        @staticmethod
        def metadata():
            return {}

    def fake_invoke(request):
        captured["request"] = request
        return Result()

    monkeypatch.setattr(codex_thesis_monitor, "invoke_structured", fake_invoke)
    evidence = {
        "prices": {"price": 100},
        "source_evidence": [{"reference": "source:1", "text": "x" * 2_000}],
        "thesis": {"core_thesis": "Demand remains durable."},
    }
    request = {
        "symbol": "ACME",
        "prompt_template": {
            "system_focus": "Compare the countercase first.",
            "forecast_instruction": "Use calibrated probabilities.",
            "evidence_order": ["source_evidence", "prices"],
            "max_packet_tokens": 256,
        },
        "evidence_packet": {"symbol": "ACME", "evidence": evidence},
    }

    codex_thesis_monitor.generate_deepseek_continuous_advisor(
        request,
        model="deepseek-v4-flash",
    )

    provider_request = captured["request"]
    assert "Focus: Compare the countercase first." in provider_request.system_prompt
    assert "Forecast instruction: Use calibrated probabilities." in provider_request.system_prompt
    assert provider_request.max_output_tokens == 24_000
    assert list(provider_request.payload["evidence_packet"]["evidence"]) == ["source_evidence", "prices", "thesis"]
    assert len(provider_request.payload["evidence_packet"]["evidence"]["source_evidence"]) == 1
    assert provider_request.payload["evidence_packet"]["evidence"]["source_evidence"][0]["reference"] == "source:1"
    assert len(json.dumps(provider_request.payload["evidence_packet"], default=str, separators=(",", ":"))) <= 256 * 4
    assert evidence["source_evidence"]


def test_provider_validation_uses_the_trimmed_evidence_copy():
    packet = _packet()
    exact = run_continuous_advisor._provider_validation_packet(
        packet,
        {"_meta": {"provider_evidence_references": []}},
    )
    assert exact["source_references"] == []
    assert exact["evidence"]["source_evidence"] == []
    response = {
        "symbol": "ACME",
        "thesis": {"core_thesis": "Demand remains durable."},
        "countercase": "Multiple compression.",
        "evidence_refs": ["source:1"],
        "forecasts": [{"claim_key": "f1", "statement": "Price rises", "horizon": "1d", "direction": "up", "probability": 0.7, "evidence_refs": ["source:1"]}],
        "invalidations": [{"claim_key": "i1", "condition": "Demand breaks", "horizon": "1m", "probability": 0.2, "evidence_refs": ["source:1"]}],
        "next_review_trigger": "New earnings evidence",
        "change_since_prior": "No material change.",
    }
    with pytest.raises(ContinuousAdvisorValidationError, match="outside packet"):
        validate_continuous_response(response, exact)


def test_scheduled_due_is_used_as_the_point_in_time_cutoff(monkeypatch):
    monkeypatch.setenv("MARKET_SCHEDULED_DUE_AT", "2026-09-09T13:30:00+00:00")
    assert run_continuous_advisor._scheduled_due_at() == datetime(2026, 9, 9, 13, 30, tzinfo=UTC)


def test_delayed_scheduled_cutoff_cannot_cross_a_market_session():
    due = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
    assert run_continuous_advisor._scheduled_cutoff_is_current(due, due + timedelta(hours=1))
    assert not run_continuous_advisor._scheduled_cutoff_is_current(due, due - timedelta(seconds=1))
    assert not run_continuous_advisor._scheduled_cutoff_is_current(due, due + timedelta(days=1))


def test_replay_uses_a_bounded_market_session_resolution_window():
    target = datetime(2026, 9, 8, 15, tzinfo=UTC)
    window_end = replay_continuous_advisor._observation_window_end(target)
    assert window_end is not None
    assert target < window_end <= datetime(2026, 9, 8, 21, tzinfo=UTC)
    assert replay_continuous_advisor._observation_window_end(target + timedelta(days=30)) is not None


def test_replay_horizon_units_are_unambiguous():
    assert replay_continuous_advisor._horizon_delta("1m") == timedelta(minutes=1)
    assert replay_continuous_advisor._horizon_delta("1h") == timedelta(hours=1)
    assert replay_continuous_advisor._horizon_delta("1mo") == timedelta(days=30)
    assert replay_continuous_advisor._horizon_delta("0h") is None


def test_cohort_evaluation_keeps_response_evidence_safety_rate():
    result = evolve_continuous_advisor._cohort_evaluation(
        {"evidence_validity_rate": 0.0, "response_evidence_validity_rate": 0.0},
        {"full": {"evidence_validity_rate": 1.0}, "holdout": {}},
        {"common_frozen_packets": 1, "holdout_cutoff": "2026-09-08T15:00:00+00:00"},
    )
    assert result["evidence_validity_rate"] == 0.0
    assert result["outcome_evidence_validity_rate"] == 1.0


def test_estimated_call_cost_is_available_for_registered_provider():
    from investment_panel.settings import AppConfig

    assert run_continuous_advisor._estimated_call_cost({"evidence_packet": {}}, AppConfig()) is not None


def test_provider_failure_telemetry_is_priced_from_direct_metadata():
    from investment_panel.settings import AppConfig

    usage, cost = run_continuous_advisor._telemetry(
        {
            "provider": "codex",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "high",
            "estimated": True,
            "usage": {"input_tokens": 1_000, "output_tokens": 500},
        },
        AppConfig(),
    )
    assert usage["output_tokens"] == 500
    assert cost is not None and cost > 0
