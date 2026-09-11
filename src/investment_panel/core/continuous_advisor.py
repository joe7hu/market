"""Pure contracts for the continuous, replay-learned Market advisor.

This module deliberately has no provider, database, or execution dependency.
It owns the point-in-time packet boundary, response validation, scoring, and
the small deterministic promotion gate used by the persistence/job adapters.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import math
import re
from typing import Any, Mapping
from uuid import UUID

from investment_panel.domain.portfolio.contracts import canonical_content_hash
from investment_panel.infrastructure.postgres.instruments import canonical_symbol


PACKET_SCHEMA_VERSION = "continuous-advisor-packet.v1"
RESPONSE_SCHEMA_VERSION = "continuous-advisor-response.v1"
DEFAULT_CADENCE_MINUTES = 120
MIN_CADENCE_MINUTES = 5
MAX_CADENCE_MINUTES = 720
MIN_TEST_MATCHES = 3
MIN_PROMOTION_MATCHES = 30
PROMOTION_Z = 1.96
CONTINUOUS_MAX_OUTPUT_TOKENS = 24_000
SCORING_VERSION = "continuous-advisor-score.v2"

_TIMESTAMP_KEYS = frozenset({
    "observed_at", "available_at", "published_at", "event_at", "finished_at",
    "started_at", "created_at", "updated_at", "quote_observed_at",
    "thesis_updated_at", "latest_quote_available_at", "as_of",
})
_VOLATILE_PACKET_KEYS = frozenset({
    "fingerprint", "slot_start", "created_at", "ingest_run_id",
    "ingested_at", "last_seen_at", "payload_id", "run_id", "updated_at",
})
_UNTRUSTED_STATES = frozenset({
    "failed", "stale", "unknown", "unavailable", "untrusted", "invalid",
    "quarantined", "archived", "disabled",
})
_FORBIDDEN_RESPONSE_KEYS = frozenset({
    "order", "orders", "trade", "trades", "trade_plan", "paper_order",
    "execution", "execution_ready", "quantity", "position_size", "broker",
    "clear_gate", "risk_override",
})
_SUPPORTED_HORIZON_RE = re.compile(
    r"\s*(\d+)\s*(m|min|mins|minute|minutes|h|hour|hours|mo|month|months|d|day|days|w|week|weeks)\s*\Z",
    re.IGNORECASE,
)


def _supported_horizon(value: Any) -> bool:
    match = _SUPPORTED_HORIZON_RE.fullmatch(str(value))
    return bool(match and int(match.group(1)) > 0)
APPROVED_PROMPT_FIELDS = frozenset({
    "system_focus", "evidence_order", "forecast_instruction",
    "countercase_instruction", "review_trigger_instruction", "max_packet_tokens",
})


class ContinuousAdvisorValidationError(ValueError):
    """Raised when packet, response, or prompt data fails a hard boundary."""


def build_evidence_packet(
    row: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    cutoff: datetime,
    prompt_version: str,
    slot_start: datetime | None = None,
    cadence_minutes: int = DEFAULT_CADENCE_MINUTES,
) -> dict[str, Any]:
    """Build one bounded, cutoff-correct packet from existing ticker context."""

    cutoff = _aware_utc(cutoff, "cutoff")
    symbol = canonical_symbol(row.get("symbol") or context.get("symbol"))
    prompt_version = str(prompt_version or "").strip()
    if not prompt_version:
        raise ContinuousAdvisorValidationError("prompt_version is required")
    cadence = max(MIN_CADENCE_MINUTES, min(MAX_CADENCE_MINUTES, int(cadence_minutes)))
    slot = slot_start or _slot_start(cutoff, cadence)
    slot = _aware_utc(slot, "slot_start")

    status = dict(context.get("context_status") or {})
    blockers: set[str] = set()
    if status.get("cutoff_available") is not True:
        blockers.add("cutoff_unavailable")
    context_cutoff = _parse_datetime(status.get("cutoff"))
    if context_cutoff is not None and context_cutoff > cutoff:
        blockers.add("context_cutoff_after_packet_cutoff")

    portfolio = dict(context.get("portfolio") or {})
    price = _first_present(portfolio, "price", row.get("latest_price"))
    quote_observed = _first_present(portfolio, "quote_observed_at", row.get("latest_quote_at"))
    quote_available = _first_present(portfolio, "quote_available_at", row.get("latest_quote_available_at"))
    price_status = _fact_status(
        {"price": price, "quote_observed_at": quote_observed, "available_at": quote_available},
        cutoff,
        max_age=timedelta(hours=4),
    )
    if price_status != "available":
        blockers.add(f"price_{price_status}")

    portfolio_fact = {
        key: value for key, value in portfolio.items()
        if key not in {"thesis_created_at", "thesis_updated_at"}
    }
    portfolio_status = _fact_status(portfolio_fact, cutoff, max_age=timedelta(hours=4)) if portfolio_fact else "unavailable"
    if portfolio_status != "available" and portfolio:
        blockers.add(f"portfolio_{portfolio_status}")
        portfolio = {}
    thesis = portfolio.get("thesis")
    if not isinstance(thesis, dict):
        row_updated = _parse_datetime(row.get("updated_at"))
        thesis = dict(row.get("raw_thesis") or {}) if row_updated is not None and row_updated <= cutoff else {}
    thesis_timestamp = _parse_datetime(portfolio.get("thesis_updated_at"))
    if thesis_timestamp is None or thesis_timestamp > cutoff:
        thesis_timestamp = _parse_datetime(portfolio.get("thesis_created_at")) or _parse_datetime(row.get("updated_at"))
    thesis_status = _fact_status({"updated_at": thesis_timestamp}, cutoff, max_age=timedelta(days=45))
    if thesis_status != "available":
        blockers.add(f"thesis_{thesis_status}")
        thesis = {}
    if not str(thesis.get("core_thesis") or thesis.get("thesis") or "").strip():
        blockers.add("thesis_unavailable")

    source_evidence, evidence_states = _usable_evidence(context.get("source_evidence"), cutoff)
    blockers.update(f"evidence_{state}" for state in evidence_states if state != "available")
    if not source_evidence:
        blockers.add("source_evidence_unavailable")

    catalysts, catalyst_states = _usable_items(
        context.get("catalysts"), cutoff, max_age=timedelta(days=30)
    )
    blockers.update(f"catalysts_{state}" for state in catalyst_states if state != "available")

    published = dict(context.get("published_models") or {})
    option_values = context.get("option_opportunities")
    if option_values is None:
        option_values = [context.get("option_opportunity")] if context.get("option_opportunity") else []
    options, option_states = _usable_items(option_values, cutoff, max_age=timedelta(hours=4))
    blockers.update(f"options_{state}" for state in option_states if state != "available")
    categories: dict[str, Any] = {
        "fundamentals": {},
        "technicals": {},
        "options": options,
        "macro_regime": {},
    }
    for model_name, value in published.items():
        name = str(model_name).lower()
        if any(term in name for term in ("fundamental", "valuation", "earnings", "dcf")):
            category = "fundamentals"
        elif any(term in name for term in ("technical", "sepa", "relative_strength", "momentum")):
            category = "technicals"
        elif any(term in name for term in ("macro", "regime", "market_state", "rates")):
            category = "macro_regime"
        else:
            continue
        if isinstance(value, dict):
            model_status = _fact_status(value, cutoff, max_age=_evidence_max_age(value))
            if model_status == "available":
                categories[category][model_name] = value
            else:
                blockers.add(f"{category}_{model_status}")

    freshness = {
        "prices": price_status,
        "portfolio_exposure": portfolio_status,
        "thesis": "available" if str(thesis.get("core_thesis") or thesis.get("thesis") or "").strip() else "unavailable",
        "catalysts": "available" if catalysts else "unavailable",
        "fundamentals": "available" if categories["fundamentals"] else "unavailable",
        "technicals": "available" if categories["technicals"] else "unavailable",
        "options": "available" if categories["options"] else "unavailable",
        "macro_regime": "available" if categories["macro_regime"] else "unavailable",
        "source_evidence": "available" if source_evidence else "unavailable",
    }
    source_refs = sorted({str(item["reference"]) for item in source_evidence if item.get("reference")})
    packet = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "symbol": symbol,
        "cutoff": cutoff.isoformat(),
        "slot_start": slot.isoformat(),
        "prompt_version": prompt_version,
        "source_references": source_refs,
        "freshness": freshness,
        "blockers": sorted(blockers),
        "evidence": {
            "prices": {
                "price": _jsonable(price),
                "observed_at": _jsonable(quote_observed),
                "available_at": _jsonable(quote_available),
            } if price_status == "available" else {},
            "portfolio_exposure": _bounded(portfolio),
            "thesis": _bounded(thesis),
            "catalysts": _bounded(catalysts),
            "fundamentals": _bounded(categories["fundamentals"]),
            "technicals": _bounded(categories["technicals"]),
            "options": _bounded(categories["options"]),
            "macro_regime": _bounded(categories["macro_regime"]),
            "source_evidence": _bounded(source_evidence),
        },
        "authority": {
            "advisory_only": True,
            "research_ranking_only": True,
            "paper_only": True,
            "execution_authority": False,
        },
    }
    packet["fingerprint"] = packet_fingerprint(packet)
    return packet


def packet_fingerprint(packet: Mapping[str, Any]) -> str:
    """Hash stable packet facts while excluding clock/ingestion bookkeeping."""

    return canonical_content_hash(_stable(packet))


def packet_is_replay_safe(packet: Mapping[str, Any], cutoff: datetime | None = None) -> bool:
    """Return false when any fact timestamp could have been learned after cutoff."""

    packet_cutoff = _parse_datetime(cutoff or packet.get("cutoff"))
    if packet_cutoff is None:
        return False
    if str(packet.get("fingerprint") or "") != packet_fingerprint(packet):
        return False
    return not _has_future_timestamp(packet, packet_cutoff)


def validate_continuous_response(
    output: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the model contract and restrict references to packet evidence."""

    if not isinstance(output, Mapping):
        raise ContinuousAdvisorValidationError("response must be an object")
    symbol = str(packet.get("symbol") or "").upper()
    if str(output.get("symbol") or "").upper() != symbol:
        raise ContinuousAdvisorValidationError("response symbol does not match packet")
    if not packet_is_replay_safe(packet):
        raise ContinuousAdvisorValidationError("packet is not replay-safe")
    _reject_forbidden_keys(output)
    thesis = output.get("thesis")
    if not isinstance(thesis, dict) or not str(thesis.get("core_thesis") or "").strip():
        raise ContinuousAdvisorValidationError("response missing thesis.core_thesis")
    forbidden_thesis_fields = sorted({"automation_policy", "lifecycle_status"}.intersection(thesis))
    if forbidden_thesis_fields:
        raise ContinuousAdvisorValidationError(
            "response thesis contains human-owned control fields: " + ", ".join(forbidden_thesis_fields)
        )
    countercase = str(output.get("countercase") or "").strip()
    if not countercase:
        raise ContinuousAdvisorValidationError("response missing countercase")
    allowed_refs = {str(ref) for ref in packet.get("source_references") or [] if str(ref).strip()}
    top_refs = _refs(output.get("evidence_refs"), "evidence_refs")
    _check_refs(top_refs, allowed_refs)
    _check_nested_refs(output, allowed_refs)
    forecasts = output.get("forecasts")
    if not isinstance(forecasts, list) or not forecasts:
        raise ContinuousAdvisorValidationError("response must include forecasts")
    normalized_forecasts = []
    forecast_keys: set[str] = set()
    for index, forecast in enumerate(forecasts):
        if not isinstance(forecast, dict):
            raise ContinuousAdvisorValidationError(f"forecast {index} must be an object")
        claim_key = str(forecast.get("claim_key") or forecast.get("claim_id") or f"forecast_{index + 1}").strip()
        if not claim_key:
            raise ContinuousAdvisorValidationError(f"forecast {index} has an empty claim key")
        if claim_key in forecast_keys:
            raise ContinuousAdvisorValidationError(f"duplicate forecast claim key: {claim_key}")
        forecast_keys.add(claim_key)
        statement = str(forecast.get("statement") or "").strip()
        horizon = str(forecast.get("horizon") or "").strip()
        direction = str(forecast.get("direction") or "").strip().lower()
        probability = _probability(forecast.get("probability"), f"forecast {claim_key} probability")
        refs = _refs(forecast.get("evidence_refs"), f"forecast {claim_key} evidence_refs")
        _check_refs(refs, allowed_refs)
        if not statement or not _supported_horizon(horizon) or direction not in {"up", "down", "flat", "bullish", "bearish", "neutral"}:
            raise ContinuousAdvisorValidationError(f"forecast {claim_key} is incomplete")
        normalized_forecasts.append({**forecast, "claim_key": claim_key, "statement": statement, "horizon": horizon, "direction": direction, "probability": probability, "evidence_refs": refs})
    invalidations = output.get("invalidations")
    if not isinstance(invalidations, list) or not invalidations:
        raise ContinuousAdvisorValidationError("response must include invalidations")
    normalized_invalidations = []
    invalidation_keys: set[str] = set()
    for index, invalidation in enumerate(invalidations):
        if not isinstance(invalidation, dict):
            raise ContinuousAdvisorValidationError(f"invalidation {index} must be an object")
        claim_key = str(invalidation.get("claim_key") or invalidation.get("rule_id") or f"invalidation_{index + 1}").strip()
        if not claim_key:
            raise ContinuousAdvisorValidationError(f"invalidation {index} has an empty claim key")
        if claim_key in forecast_keys or claim_key in invalidation_keys:
            raise ContinuousAdvisorValidationError(f"duplicate invalidation claim key: {claim_key}")
        invalidation_keys.add(claim_key)
        condition = str(invalidation.get("condition") or invalidation.get("statement") or "").strip()
        horizon = str(invalidation.get("horizon") or "").strip()
        probability = _probability(invalidation.get("probability"), f"invalidation {claim_key} probability")
        refs = _refs(invalidation.get("evidence_refs"), f"invalidation {claim_key} evidence_refs")
        _check_refs(refs, allowed_refs)
        if not condition or not _supported_horizon(horizon):
            raise ContinuousAdvisorValidationError(f"invalidation {claim_key} is incomplete")
        normalized_invalidations.append({**invalidation, "claim_key": claim_key, "condition": condition, "horizon": horizon, "probability": probability, "evidence_refs": refs})
    next_trigger = str(output.get("next_review_trigger") or "").strip()
    if not next_trigger:
        raise ContinuousAdvisorValidationError("response missing next_review_trigger")
    next_review_at = output.get("next_review_at")
    packet_cutoff = _parse_datetime(packet.get("cutoff"))
    if next_review_at is not None and packet_cutoff is not None:
        parsed_next = _parse_datetime(next_review_at)
        if parsed_next is None or parsed_next < packet_cutoff:
            raise ContinuousAdvisorValidationError("next_review_at must not precede packet cutoff")
    return {
        **dict(output),
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "symbol": symbol,
        "countercase": countercase,
        "evidence_refs": top_refs,
        "forecasts": normalized_forecasts,
        "invalidations": normalized_invalidations,
        "next_review_trigger": next_trigger,
    }


def response_claims(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten forecast and invalidation claims for immutable persistence."""

    claims: list[dict[str, Any]] = []
    for claim in response.get("forecasts") or []:
        if isinstance(claim, dict):
            claims.append({**claim, "claim_kind": "forecast"})
    for claim in response.get("invalidations") or []:
        if isinstance(claim, dict):
            claims.append({**claim, "claim_kind": "invalidation", "statement": claim.get("condition") or claim.get("statement")})
    return claims


def resolve_claim(
    claim: Mapping[str, Any],
    *,
    actual_return: float | None,
    excess_return: float | None = None,
    invalidated: bool | None = None,
    evidence_valid: bool = True,
    measured_through: datetime | date | str | None = None,
) -> dict[str, Any]:
    """Resolve one declared claim without changing the original claim."""

    direction = str(claim.get("direction") or "neutral").lower()
    actual_direction = "unresolved"
    correct: bool | None = None
    if actual_return is not None and math.isfinite(float(actual_return)):
        value = float(actual_return)
        actual_direction = "up" if value > 0 else "down" if value < 0 else "flat"
        expected = {"bullish": "up", "bearish": "down"}.get(direction, direction)
        correct = expected == "neutral" and actual_direction == "flat" or expected == actual_direction
    is_invalidation = str(claim.get("claim_kind") or "") == "invalidation"
    invalidation_correct = None
    event_truth: float | None = None
    if is_invalidation and invalidated is not None:
        expected_probability = float(claim.get("probability") or 0)
        invalidation_correct = bool(invalidated) == (expected_probability >= 0.5)
        event_truth = 1.0 if invalidated else 0.0
        correct = invalidation_correct
    probability = float(claim.get("probability") or 0)
    actual = 1.0 if correct else 0.0 if correct is False else None
    calibration_truth = event_truth if is_invalidation else actual
    return {
        "scoring_version": SCORING_VERSION,
        "status": "resolved" if actual is not None and evidence_valid else "quarantined" if actual is not None else "unresolvable",
        "actual_return": float(actual_return) if actual_return is not None else None,
        "excess_return": float(excess_return) if excess_return is not None else None,
        "actual_direction": actual_direction,
        "correct": correct,
        "calibration_error": (probability - calibration_truth) ** 2 if calibration_truth is not None else None,
        "event_truth": event_truth,
        "invalidation_actual": invalidated,
        "invalidation_correct": invalidation_correct,
        "evidence_valid": bool(evidence_valid),
        "measured_through": _jsonable(measured_through),
    }


def score_claims(
    claims: list[Mapping[str, Any]],
    outcomes: list[Mapping[str, Any]],
    *,
    latency_ms: list[float] | None = None,
    token_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Produce the stable scorecard used for active/candidate comparisons."""

    by_id = {str(item.get("claim_id")): item for item in claims if item.get("claim_id")}
    by_key = {str(item.get("claim_key") or item.get("claim_id")): item for item in claims}
    matched = []
    for item in outcomes:
        if str(item.get("status") or "") != "resolved":
            continue
        identity = str(item.get("claim_id") or "")
        if identity in by_id or str(item.get("claim_key") or "") in by_key:
            matched.append(item)
    brier_values: list[float] = []
    directional: list[float] = []
    excess: list[float] = []
    invalidation: list[float] = []
    evidence: list[float] = []
    quality_samples: list[float] = []
    for outcome in matched:
        claim = by_id.get(str(outcome.get("claim_id") or "")) or by_key[str(outcome.get("claim_key") or outcome.get("claim_id"))]
        probability = _safe_probability(claim.get("probability"))
        classification_actual = 1.0 if outcome.get("correct") is True else 0.0 if outcome.get("correct") is False else None
        event_truth = outcome.get("event_truth")
        if event_truth is None and claim.get("claim_kind") == "invalidation":
            invalidation_actual = outcome.get("invalidation_actual")
            event_truth = 1.0 if invalidation_actual is True else 0.0 if invalidation_actual is False else None
        if event_truth is None:
            event_truth = classification_actual
        brier = (probability - float(event_truth)) ** 2 if event_truth is not None else None
        if brier is None:
            continue
        brier_values.append(brier)
        if classification_actual is not None and claim.get("claim_kind") != "invalidation":
            directional.append(classification_actual)
        if outcome.get("excess_return") is not None and claim.get("claim_kind") != "invalidation":
            excess_direction = _direction(float(outcome["excess_return"]))
            expected_excess = {
                "bullish": "up", "bearish": "down",
            }.get(str(claim.get("direction") or "").lower(), str(claim.get("direction") or "").lower())
            excess.append(1.0 if expected_excess == excess_direction else 0.0)
        if outcome.get("invalidation_correct") is not None:
            invalidation.append(1.0 if outcome["invalidation_correct"] else 0.0)
        evidence.append(1.0 if outcome.get("evidence_valid", True) else 0.0)
        quality_samples.append((1.0 - brier) * 0.55 + (classification_actual or 0.0) * 0.25 + (1.0 if outcome.get("evidence_valid", True) else 0.0) * 0.20)
    brier_score = sum(brier_values) / len(brier_values) if brier_values else None
    calibration = 1.0 - brier_score if brier_score is not None else None
    directional_accuracy = sum(directional) / len(directional) if directional else None
    excess_accuracy = sum(excess) / len(excess) if excess else None
    invalidation_accuracy = sum(invalidation) / len(invalidation) if invalidation else None
    evidence_validity = sum(evidence) / len(evidence) if evidence else None
    components = [value for value in (calibration, directional_accuracy, excess_accuracy, invalidation_accuracy, evidence_validity) if value is not None]
    quality_score = sum(components) / len(components) if components else None
    promotion_quality_score = sum(quality_samples) / len(quality_samples) if quality_samples else None
    return {
        "scoring_version": SCORING_VERSION,
        "matched_outcomes": len(brier_values),
        "resolved_claims": len(brier_values),
        "brier_score": brier_score,
        "calibration_score": calibration,
        "directional_accuracy": directional_accuracy,
        "excess_return_accuracy": excess_accuracy,
        "invalidation_accuracy": invalidation_accuracy,
        "evidence_validity_rate": evidence_validity,
        "avg_latency_ms": sum(latency_ms or []) / len(latency_ms) if latency_ms else None,
        "token_cost_usd": round(float(token_cost_usd or 0), 6),
        "quality_score": quality_score,
        "quality_score_basis": "mean_available_metric_components",
        "promotion_quality_score": promotion_quality_score,
        "promotion_quality_score_basis": "weighted_resolved_claim_samples",
        "quality_samples": quality_samples,
    }


def compare_scorecards(active: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Compare same-cohort scorecards with a two-sided normal lower bound."""

    active_samples = _score_samples(active)
    candidate_samples = _score_samples(candidate)
    active_mean = _mean(active_samples)
    candidate_mean = _mean(candidate_samples)
    delta = candidate_mean - active_mean
    standard_error = math.sqrt(_variance(active_samples) / max(1, len(active_samples)) + _variance(candidate_samples) / max(1, len(candidate_samples)))
    lcb = delta - PROMOTION_Z * standard_error
    return {
        "active_quality_score": active_mean,
        "candidate_quality_score": candidate_mean,
        "quality_delta": delta,
        "lower_confidence_bound": lcb,
        "positive_lower_confidence_bound": lcb > 0,
        "active_matches": int(active.get("matched_outcomes") or len(active_samples)),
        "candidate_matches": int(candidate.get("matched_outcomes") or len(candidate_samples)),
    }


def promotion_gate(
    candidate: Mapping[str, Any],
    *,
    active: Mapping[str, Any] | None = None,
    walk_forward: Mapping[str, Any] | None = None,
    forward_session: Mapping[str, Any] | None = None,
    safety_checks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the bounded advisory-only prompt promotion gates."""

    blockers: list[str] = []
    matches = int(candidate.get("matched_outcomes") or 0)
    if matches < MIN_TEST_MATCHES:
        blockers.append("matched_outcomes_below_test_floor")
    if matches < MIN_PROMOTION_MATCHES:
        blockers.append("matched_outcomes_below_promotion_floor")
    if float(candidate.get("schema_validity_rate") or 0) < 1:
        blockers.append("schema_validation_failed")
    if float(candidate.get("evidence_validity_rate") or 0) < 1:
        blockers.append("evidence_validation_failed")
    if walk_forward is None or str(walk_forward.get("status") or "").lower() != "pass":
        blockers.append("walk_forward_failed")
    if forward_session is None or str(forward_session.get("status") or "").lower() != "pass":
        blockers.append("forward_session_failed")
    safety = safety_checks or {}
    if safety.get("passed") is not True:
        blockers.append("safety_checks_failed")
    if active is not None:
        active_brier = active.get("brier_score")
        candidate_brier = candidate.get("brier_score")
        if active_brier is not None and candidate_brier is not None and float(candidate_brier) > float(active_brier) + 0.02:
            blockers.append("material_calibration_regression")
        comparison = compare_scorecards(active, candidate)
        if comparison["lower_confidence_bound"] <= 0:
            blockers.append("non_positive_lower_confidence_bound")
    else:
        comparison = {"lower_confidence_bound": float(candidate.get("lower_confidence_bound") or 0), "positive_lower_confidence_bound": False}
        if comparison["lower_confidence_bound"] <= 0:
            blockers.append("non_positive_lower_confidence_bound")
    waiting_blockers = {
        "matched_outcomes_below_test_floor",
        "matched_outcomes_below_promotion_floor",
        "walk_forward_failed",
        "forward_session_failed",
    }
    evidence_waiting = matches < MIN_PROMOTION_MATCHES
    status = "eligible" if not blockers else "waiting" if all(
        blocker in waiting_blockers or (blocker == "non_positive_lower_confidence_bound" and evidence_waiting)
        for blocker in blockers
    ) else "rejected"
    return {
        "eligible": not blockers,
        "status": status,
        "blockers": sorted(set(blockers)),
        "comparison": comparison,
        "advisory_only": True,
        "execution_controls_unchanged": True,
    }


def mutate_prompt_template(parent_version: str, changes: Mapping[str, Any], rationale: str) -> dict[str, Any]:
    """Create a typed, safe mutation draft; activation is a separate gate."""

    parent = str(parent_version or "").strip()
    if not parent:
        raise ContinuousAdvisorValidationError("parent prompt version is required")
    if not isinstance(changes, Mapping) or not changes:
        raise ContinuousAdvisorValidationError("prompt mutation changes are required")
    unknown = sorted(set(str(key) for key in changes) - APPROVED_PROMPT_FIELDS)
    if unknown:
        raise ContinuousAdvisorValidationError("prompt fields are not approved: " + ", ".join(unknown))
    rationale = str(rationale or "").strip()
    if not rationale:
        raise ContinuousAdvisorValidationError("mutation rationale is required")
    normalized: dict[str, Any] = {}
    for key, value in changes.items():
        name = str(key)
        if name == "max_packet_tokens":
            if isinstance(value, bool) or not isinstance(value, int) or not 256 <= value <= 12_000:
                raise ContinuousAdvisorValidationError("max_packet_tokens must be an integer between 256 and 12000")
        elif name == "evidence_order":
            if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
                raise ContinuousAdvisorValidationError("evidence_order must be a non-empty string array")
        elif not isinstance(value, str) or not value.strip():
            raise ContinuousAdvisorValidationError(f"{name} must be a non-empty string")
        normalized[name] = _bounded(value)
    version_hash = hashlib.sha256(
        json.dumps({"changes": normalized, "rationale": rationale}, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:12]
    return {
        "version": f"{parent}.mutation.{version_hash}",
        "parent_version": parent,
        "template": normalized,
        "approved_change_set": sorted(normalized),
        "mutation_rationale": rationale,
        "authority": "advisory_research_only",
    }


def _usable_evidence(value: Any, cutoff: datetime) -> tuple[list[dict[str, Any]], set[str]]:
    usable: list[dict[str, Any]] = []
    states: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            states.add("invalid")
            continue
        state = _fact_status(item, cutoff, max_age=_evidence_max_age(item))
        if state == "available" and str(item.get("reference") or "").strip():
            usable.append(_bounded(item))
        else:
            states.add(state)
    return usable, states


def _usable_items(value: Any, cutoff: datetime, *, max_age: timedelta) -> tuple[list[dict[str, Any]], set[str]]:
    usable: list[dict[str, Any]] = []
    states: set[str] = set()
    items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    for item in items:
        if not isinstance(item, dict):
            states.add("invalid")
            continue
        state = _fact_status(item, cutoff, max_age=max_age)
        if state == "available":
            usable.append(_bounded(item))
        else:
            states.add(state)
    return usable, states


def _evidence_max_age(item: Mapping[str, Any]) -> timedelta:
    source_type = str(item.get("source_type") or "").lower()
    source_family = str(item.get("source_family") or "").lower()
    if any(term in f"{source_type} {source_family}" for term in ("news", "intraday", "option")):
        return timedelta(hours=4)
    if any(term in source_type for term in ("fundamental", "filing", "earnings")):
        return timedelta(days=120)
    return timedelta(days=30)


def _fact_status(fact: Mapping[str, Any], cutoff: datetime, *, max_age: timedelta) -> str:
    explicit = str(fact.get("freshness_status") or fact.get("status") or fact.get("verification_status") or "").strip().lower()
    if explicit in _UNTRUSTED_STATES:
        return explicit
    for key in _TIMESTAMP_KEYS:
        value = _parse_datetime(fact.get(key))
        if value is not None and value > cutoff:
            return "future"
    observed = next(
        (
            parsed
            for key in ("observed_at", "quote_observed_at", "published_at", "available_at", "updated_at", "as_of")
            if (parsed := _parse_datetime(fact.get(key))) is not None
        ),
        None,
    )
    if observed is None:
        return "unknown"
    if cutoff - observed > max_age:
        return "stale"
    if fact.get("price") is None and "price" in fact:
        return "unavailable"
    return "available"


def _has_future_timestamp(value: Any, cutoff: datetime) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in _TIMESTAMP_KEYS:
                parsed = _parse_datetime(child)
                if parsed is not None and parsed > cutoff:
                    return True
            if _has_future_timestamp(child, cutoff):
                return True
    elif isinstance(value, list):
        return any(_has_future_timestamp(item, cutoff) for item in value)
    return False


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_RESPONSE_KEYS:
                raise ContinuousAdvisorValidationError(f"response contains forbidden advisory field: {key}")
            _reject_forbidden_keys(child)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_keys(item)


def _check_refs(refs: list[str], allowed: set[str]) -> None:
    if not refs:
        raise ContinuousAdvisorValidationError("every claim needs packet evidence references")
    missing = sorted(set(refs) - allowed)
    if missing:
        raise ContinuousAdvisorValidationError("response references evidence outside packet: " + ", ".join(missing))


def _check_nested_refs(value: Any, allowed: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in {"evidence_refs", "evidence_links"} and child:
                refs = _refs(child, str(key))
                missing = sorted(set(refs) - allowed)
                if missing:
                    raise ContinuousAdvisorValidationError(
                        "response references evidence outside packet: " + ", ".join(missing)
                    )
            _check_nested_refs(child, allowed)
    elif isinstance(value, list):
        for item in value:
            _check_nested_refs(item, allowed)


def _refs(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContinuousAdvisorValidationError(f"{label} must be a non-empty string array")
    return sorted(set(item.strip() for item in value))


def _probability(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ContinuousAdvisorValidationError(f"{label} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContinuousAdvisorValidationError(f"{label} must be a number") from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ContinuousAdvisorValidationError(f"{label} must be between 0 and 1")
    return result


def _safe_probability(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _direction(value: float) -> str:
    return "up" if value > 0 else "down" if value < 0 else "flat"


def _score_samples(scorecard: Mapping[str, Any]) -> list[float]:
    values = scorecard.get("quality_samples") or scorecard.get("scores")
    if isinstance(values, list):
        parsed = [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(float(value))]
        if parsed:
            return parsed
    count = max(1, int(scorecard.get("matched_outcomes") or 1))
    value = scorecard.get("quality_score")
    return [float(value) if isinstance(value, (int, float)) else 0.0] * count


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _slot_start(value: datetime, cadence_minutes: int) -> datetime:
    epoch_seconds = int(value.timestamp())
    slot_seconds = max(60, int(cadence_minutes) * 60)
    return datetime.fromtimestamp(epoch_seconds - epoch_seconds % slot_seconds, tz=UTC)


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ContinuousAdvisorValidationError(f"{label} must be a datetime")
    if value.tzinfo is None:
        raise ContinuousAdvisorValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _first_present(mapping: Mapping[str, Any], key: str, fallback: Any) -> Any:
    value = mapping.get(key)
    return fallback if value is None else value


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return None
    if isinstance(value, dict):
        return {str(key): _bounded(item, depth=depth + 1) for key, item in list(value.items())[:32] if str(key) not in {"raw", "payload", "source_text", "chain", "contracts"}}
    if isinstance(value, list):
        return [_bounded(item, depth=depth + 1) for item in value[:24]]
    if isinstance(value, str):
        return value[:800]
    return _jsonable(value)


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable(item) for key, item in value.items() if str(key) not in _VOLATILE_PACKET_KEYS}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (UUID, Decimal)):
        return str(value) if isinstance(value, UUID) else float(value)
    return value


__all__ = [
    "APPROVED_PROMPT_FIELDS",
    "ContinuousAdvisorValidationError",
    "CONTINUOUS_MAX_OUTPUT_TOKENS",
    "DEFAULT_CADENCE_MINUTES",
    "MIN_PROMOTION_MATCHES",
    "MIN_TEST_MATCHES",
    "PACKET_SCHEMA_VERSION",
    "RESPONSE_SCHEMA_VERSION",
    "build_evidence_packet",
    "compare_scorecards",
    "mutate_prompt_template",
    "packet_fingerprint",
    "packet_is_replay_safe",
    "promotion_gate",
    "resolve_claim",
    "response_claims",
    "score_claims",
    "validate_continuous_response",
]
