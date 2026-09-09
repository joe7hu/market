"""Evaluate prompt challengers and append promotion/rollback decisions."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.continuous_advisor import MIN_TEST_MATCHES, mutate_prompt_template, promotion_gate
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.continuous_advisor import ContinuousAdvisorRepository


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    settings = config.agents.thesis_monitor
    if not settings.continuous_enabled:
        return {"status": "skipped", "reason": "continuous advisor disabled"}
    repository = ContinuousAdvisorRepository(runtime_for_config(config))
    configured_version = str(settings.prompt_version or "thesis_v3_20260725")
    repository.ensure_prompt_version(
        {
            "version": configured_version,
            "template": {"schema": "continuous-advisor.v1", "authority": "research_ranking_only"},
            "approved_change_set": [],
            "mutation_rationale": "Initial configured continuous-advisor prompt.",
        }
    )
    status = repository.prompt_status(configured_version)
    active_version = str(status["active_prompt_version"])
    latest_promotion = status.get("promotion") or {}
    if latest_promotion.get("decision") == "rollback" and latest_promotion.get("candidate_prompt_version") == active_version:
        return {"status": "skipped", "reason": "already_rolled_back", "active_prompt_version": active_version}
    if latest_promotion.get("decision") == "activate" and latest_promotion.get("candidate_prompt_version") == active_version:
        scorecards = {item["prompt_version"]: item for item in repository.replay_scorecards()}
        previous_version = str(latest_promotion.get("previous_active_prompt_version") or "")
        rollback_reason = _rollback_reason(scorecards.get(active_version), scorecards.get(previous_version))
        if rollback_reason:
            repository.record_promotion(
                {
                    "candidate_prompt_version": active_version,
                    "previous_active_prompt_version": previous_version or None,
                    "decision": "rollback",
                    "reason": rollback_reason,
                    "scorecard": {"active": scorecards.get(active_version) or {}, "previous": scorecards.get(previous_version) or {}},
                }
            )
            return {"status": "rolled_back", "active_prompt_version": previous_version, "reason": rollback_reason}
    challenger = status.get("challenger")
    if not challenger:
        prior_promotion = status.get("promotion") or {}
        prior_rejection = (
            f" after {prior_promotion['decision']} challenger {prior_promotion['candidate_prompt_version']}"
            if prior_promotion.get("decision") in {"reject", "rollback"}
            and prior_promotion.get("previous_active_prompt_version") == active_version
            else ""
        )
        mutation = mutate_prompt_template(
            active_version,
            {"forecast_instruction": "Use calibrated probabilities and explicit horizons."},
            f"Bounded daily challenger for forecast calibration and horizon clarity{prior_rejection}.",
        )
        repository.ensure_prompt_version(mutation)
        return {"status": "drafted", "active_prompt_version": active_version, "candidate_prompt_version": mutation["version"]}
    scorecards = {item["prompt_version"]: item for item in repository.replay_scorecards()}
    candidate_version = str(challenger["version"])
    candidate = scorecards.get(candidate_version, {"matched_outcomes": 0, "schema_validity_rate": 0, "evidence_validity_rate": 0})
    active = scorecards.get(active_version)
    matched_cohort = repository.matched_cohort_scorecards(active_version, candidate_version)
    candidate_evaluation = _cohort_evaluation(candidate, matched_cohort.get("candidate"), matched_cohort)
    active_evaluation = _cohort_evaluation(active or {}, matched_cohort.get("active"), matched_cohort) if active else None
    candidate_walk = dict(candidate_evaluation.get("walk_forward_scorecard") or {})
    active_walk = dict((active_evaluation or {}).get("walk_forward_scorecard") or {})
    walk_forward = _walk_forward(candidate_walk, active_walk, matched_cohort=matched_cohort)
    forward_session = {"status": "pass" if _forward_session_passed(candidate, challenger) else "pending"}
    safety = {"passed": bool(candidate_evaluation.get("schema_validity_rate") == 1 and candidate_evaluation.get("evidence_validity_rate") == 1)}
    gate = promotion_gate(candidate_evaluation, active=active_evaluation, walk_forward=walk_forward, forward_session=forward_session, safety_checks=safety)
    decision = "activate" if gate["eligible"] else "reject"
    repository.record_cohort(
        {
            "cohort_key": f"chronological:{active_version}:{candidate_version}:{datetime.now(UTC).date().isoformat()}",
            "active_prompt_version": active_version,
            "candidate_prompt_version": candidate_version,
            "cutoff_start": candidate.get("cutoff_start"),
            "cutoff_end": candidate.get("cutoff_end"),
            "holdout_cutoff": candidate_walk.get("holdout_cutoff"),
            "matched_outcomes": candidate.get("matched_outcomes", 0),
            "scorecard": {"candidate": candidate_evaluation, "active": active_evaluation or {}, "comparison": gate["comparison"], "matched_cohort": matched_cohort},
            "walk_forward_scorecard": walk_forward,
        }
    )
    recorded = False
    if not (
        latest_promotion.get("candidate_prompt_version") == candidate_version
        and latest_promotion.get("decision") == decision
    ):
        repository.record_promotion(
            {
                "candidate_prompt_version": candidate_version,
                "previous_active_prompt_version": active_version,
                "decision": decision,
                "reason": "passed deterministic advisory gates" if gate["eligible"] else ";".join(gate["blockers"]),
                "scorecard": {"candidate": candidate_evaluation, "active": active_evaluation or {}, "gate": gate, "matched_cohort": matched_cohort},
            }
        )
        recorded = True
    return {
        "status": "activated" if gate["eligible"] else "rejected",
        "active_prompt_version": active_version,
        "candidate_prompt_version": candidate_version,
        "gate": gate,
        "recorded": recorded,
        "evaluated_at": datetime.now(UTC),
    }


def _cohort_evaluation(
    scorecard: dict[str, Any],
    side: dict[str, Any] | None,
    matched_cohort: dict[str, Any],
) -> dict[str, Any]:
    result = dict(scorecard)
    full = dict((side or {}).get("full") or {})
    response_evidence_validity = result.get("response_evidence_validity_rate", result.get("evidence_validity_rate"))
    result.update(full)
    if response_evidence_validity is not None:
        result["evidence_validity_rate"] = response_evidence_validity
    if "evidence_validity_rate" in full:
        result["outcome_evidence_validity_rate"] = full["evidence_validity_rate"]
    result["matched_cohort_scorecard"] = dict(side or {})
    holdout = dict((side or {}).get("holdout") or {})
    result["walk_forward_scorecard"] = {
        "status": "pass" if holdout.get("matched_outcomes", 0) >= MIN_TEST_MATCHES else "pending",
        "holdout": holdout,
        "holdout_cutoff": matched_cohort.get("holdout_cutoff"),
        "common_frozen_packets": matched_cohort.get("common_frozen_packets", 0),
    }
    return result


def _walk_forward(
    candidate: dict[str, Any],
    active: dict[str, Any],
    *,
    matched_cohort: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if matched_cohort is not None:
        candidate_holdout = dict(candidate.get("holdout") or {})
        active_holdout = dict(active.get("holdout") or {})
        same_cutoff = bool(matched_cohort.get("common_frozen_packets")) and matched_cohort.get("holdout_cutoff") is not None
        passed = (
            str(matched_cohort.get("status") or "") == "pass"
            and same_cutoff
            and int(candidate_holdout.get("matched_outcomes") or 0) >= MIN_TEST_MATCHES
            and int(active_holdout.get("matched_outcomes") or 0) >= MIN_TEST_MATCHES
            and float(candidate_holdout.get("quality_score") or 0) >= float(active_holdout.get("quality_score") or 0)
        )
        return {
            "status": "pass" if passed else "fail",
            "candidate": candidate,
            "active": active,
            "same_frozen_cutoff": same_cutoff,
            "common_frozen_packets": matched_cohort.get("common_frozen_packets", 0),
        }
    candidate_holdout = dict(candidate.get("holdout") or {})
    active_holdout = dict(active.get("holdout") or {})
    same_cutoff = candidate.get("holdout_cutoff") is not None and candidate.get("holdout_cutoff") == active.get("holdout_cutoff")
    passed = (
        same_cutoff
        and candidate.get("status") == "pass"
        and active.get("status") == "pass"
        and int(candidate_holdout.get("matched_outcomes") or 0) >= MIN_TEST_MATCHES
        and float(candidate_holdout.get("quality_score") or 0) >= float(active_holdout.get("quality_score") or 0)
    )
    return {"status": "pass" if passed else "fail", "candidate": candidate, "active": active, "same_frozen_cutoff": same_cutoff}


def _forward_session_passed(candidate: dict[str, Any], challenger: dict[str, Any]) -> bool:
    return (
        int(candidate.get("response_count") or 0) > 0
        and float(candidate.get("schema_validity_rate") or 0) == 1
        and float(candidate.get("evidence_validity_rate") or 0) == 1
        and str(candidate.get("last_response_at") or "") > str(challenger.get("created_at") or "")
    )


def _rollback_reason(active: dict[str, Any] | None, previous: dict[str, Any] | None) -> str | None:
    if not active or int(active.get("matched_outcomes") or 0) < MIN_TEST_MATCHES:
        return None
    if float(active.get("schema_validity_rate") or 0) < 1:
        return "rollback_schema_validation_failed"
    if float(active.get("evidence_validity_rate") or 0) < 1:
        return "rollback_evidence_validation_failed"
    if previous and active.get("brier_score") is not None and previous.get("brier_score") is not None:
        if float(active["brier_score"]) > float(previous["brier_score"]) + 0.02:
            return "rollback_material_calibration_regression"
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()


__all__ = ["run"]
