"""Independent-episode scorecards for the option lanes.

The scorecard is deliberately conservative.  It reports raw observation volume
separately, but computes metrics and gates from one latest row per stable
episode.  It never turns an incomplete denominator into a win rate or EV.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import sqrt
from statistics import mean, stdev
from typing import Any, Iterable

from investment_panel.database.opportunity_episodes import (
    SCORECARD_TRUTH_VERSION,
    has_current_scorecard_truth,
)
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


LANES = frozenset({"radar", "qqq", "recovery"})
MIN_RESOLVED_EPISODES = 30
MIN_TRADING_DAYS = 20
NON_DEFECT_QUARANTINE_REASONS = frozenset({
    "quality_gated", "promotion_ineligible", "generic_mark_not_execution_grade",
    "outcome_not_resolved_execution_grade",
})


class OpportunityScorecardRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def scorecard(self, *, lane: str, window_days: int = 120, as_of: datetime | None = None) -> dict[str, Any]:
        normalized_lane = str(lane).strip().lower()
        if normalized_lane not in LANES:
            raise ValueError("lane must be radar, qqq, or recovery")
        if not 1 <= window_days <= 3_650:
            raise ValueError("window must be between 1 and 3650 days")
        reference = as_of or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("scorecard time must be timezone-aware")
        since = reference - timedelta(days=window_days)
        rows = self._recovery_rows(since, reference) if normalized_lane == "recovery" else self._decision_rows(normalized_lane, since, reference)
        return _scorecard(
            lane=normalized_lane,
            as_of=reference,
            window_days=window_days,
            raw_observation_count=len(rows),
            episodes=episodes,
        )

    def _recovery_rows(self, since: datetime, reference: datetime) -> list[dict[str, Any]]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT observation.episode_key, observation.available_at, observation.selection_stage,
                       observation.sample_eligible, observation.quarantine_reason,
                       observation.data_status, observation.outcome_classification,
                       observation.realized_return, observation.lower_confidence_expectancy,
                       observation.entry_fill_at, observation.exit_fill_at,
                       observation.calibration_cohort,
                       EXISTS (
                         SELECT 1
                         FROM app.publication publication
                         JOIN app.publication_item item ON item.publication_id = publication.id
                         WHERE publication.status IN ('published', 'superseded')
                           AND item.payload->>'signal_id' = observation.signal_id::text
                       ) AS published
                FROM analysis.option_opportunity_observation observation
                WHERE observation.lane = 'recovery'
                  AND observation.available_at >= %s
                  AND observation.available_at <= %s
                ORDER BY observation.available_at, observation.id
                """,
                [since, reference],
            ).fetchall()
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            stage = str(item.get("selection_stage") or "observed")
            if not bool(item.get("published")) and stage in {"published", "ticketed", "filled", "exited"}:
                item["selection_stage"] = "observed"
            normalized.append(item)
        return normalized

    def _decision_rows(self, lane: str, since: datetime, reference: datetime) -> list[dict[str, Any]]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT decision.episode_key, decision.as_of AS available_at, decision.state,
                       decision.sample_eligible AS decision_sample_eligible,
                       decision.quarantine_reason AS decision_quarantine_reason,
                       decision.calibration_cohort,
                       outcome.sample_eligible AS outcome_sample_eligible,
                       outcome.quarantine_reason AS outcome_quarantine_reason,
                       outcome.outcome_classification, outcome.maturity_state,
                       coalesce(outcome.realized_exit_return, outcome.current_return) AS realized_return,
                       option_decision.probability_profit,
                       paper.status AS paper_status, paper.filled_at, paper.exit_at,
                       EXISTS (
                         SELECT 1
                         FROM app.publication publication
                         JOIN app.publication_item item ON item.publication_id = publication.id
                         WHERE publication.analysis_run_id = decision.run_id
                           AND publication.status IN ('published', 'superseded')
                           AND (
                             (publication.scope = 'options-radar'
                              AND item.model_name = 'option_radar_opportunity')
                             OR
                             (publication.scope = 'options-decision-system'
                              AND item.model_name = 'options_decision_candidate')
                           )
                           AND (
                             item.payload->>'decision_id' = decision.id::text
                             OR item.payload->>'opportunity_id' = decision.id::text
                           )
                       ) AS published
                FROM analysis.decision decision
                LEFT JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
                LEFT JOIN LATERAL (
                    SELECT status, filled_at, exit_at
                    FROM app.paper_order paper_order
                    WHERE paper_order.decision_id = decision.id
                    ORDER BY paper_order.created_at DESC
                    LIMIT 1
                ) paper ON true
                WHERE decision.kind = 'option'
                  AND decision.lane = %s
                  AND decision.as_of >= %s
                  AND decision.as_of <= %s
                ORDER BY decision.as_of, decision.id
                """,
                [lane, since, reference],
            ).fetchall()
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            state = str(item.get("state") or "").upper()
            paper_status = str(item.get("paper_status") or "").lower()
            published = bool(item.get("published"))
            item["selection_stage"] = (
                "exited" if published and paper_status in {"exited", "invalidated"}
                else "filled" if published and paper_status in {"entered", "partial_exited"}
                else "ticketed" if published and (state == "READY" or paper_status == "staged")
                else "published" if published
                else "ranked_out" if state in {"REJECT", "REJECTED"}
                else "observed"
            )
            item["sample_eligible"] = bool(
                item.get("outcome_sample_eligible")
                if item.get("outcome_sample_eligible") is not None
                else item.get("decision_sample_eligible")
            )
            item["quarantine_reason"] = item.get("outcome_quarantine_reason") or item.get("decision_quarantine_reason")
            item["entry_fill_at"] = item.get("filled_at") if paper_status in {"entered", "partial_exited", "exited"} else None
            item["exit_fill_at"] = item.get("exit_at") if paper_status in {"exited", "invalidated"} else None
            if item.get("outcome_classification") is None:
                item["outcome_classification"] = "observing"
            normalized.append(item)
        return normalized


def _latest_by_episode(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("episode_key") or "")
        if not key:
            continue
        prior = latest.get(key)
        if prior is None or row["available_at"] >= prior["available_at"]:
            latest[key] = row
    return sorted(latest.values(), key=lambda row: row["available_at"])


def _scorecard(
    *,
    lane: str,
    as_of: datetime,
    window_days: int,
    raw_observation_count: int,
    episodes: list[dict[str, Any]],
) -> dict[str, Any]:
    # Keep the public computation safe if a future caller supplies raw rows.
    # The repository already does this reduction, but a scorecard must never
    # turn repeated captures into independent opportunities.
    source_rows = list(episodes)
    missing_episode_key_count = sum(1 for row in source_rows if not str(row.get("episode_key") or ""))
    episodes = _latest_by_episode(source_rows)
    all_independent_episodes = len(episodes)
    trusted_episodes: list[dict[str, Any]] = []
    defects: dict[str, int] = {}
    if missing_episode_key_count:
        defects["missing_episode_key"] = missing_episode_key_count
    for row in episodes:
        if has_current_scorecard_truth(row.get("calibration_cohort")):
            trusted_episodes.append(row)
        else:
            reason = "legacy_or_unversioned_truth_contract"
            defects[reason] = defects.get(reason, 0) + 1
    episodes = trusted_episodes
    stages = {
        "observed_universe": len(episodes),
        "ranked_out_counterfactual": 0,
        "published_signal": 0,
        "ready_ticket": 0,
        "simulated_fill": 0,
        "closed_paper_trade": 0,
    }
    resolved: list[dict[str, Any]] = []
    for row in episodes:
        stage = str(row.get("selection_stage") or "observed")
        if stage == "ranked_out":
            stages["ranked_out_counterfactual"] += 1
        if stage in {"published", "ticketed", "filled", "exited"}:
            stages["published_signal"] += 1
        if stage in {"ticketed", "filled", "exited"}:
            stages["ready_ticket"] += 1
        if row.get("entry_fill_at") is not None:
            stages["simulated_fill"] += 1
        if row.get("exit_fill_at") is not None:
            stages["closed_paper_trade"] += 1
        reason = str(row.get("quarantine_reason") or row.get("data_status") or "")
        if reason in {"ok", "none"}:
            reason = ""
        if reason and reason not in NON_DEFECT_QUARANTINE_REASONS:
            defects[reason] = defects.get(reason, 0) + 1
        outcome = str(row.get("outcome_classification") or "observing")
        maturity = str(row.get("maturity_state") or "")
        is_resolved = outcome not in {"", "observing", "unmeasurable", "legacy_non_executable"}
        if maturity and maturity not in {"mature", "expired", "closed"}:
            is_resolved = False
        if is_resolved and bool(row.get("sample_eligible")) and not reason:
            resolved.append(row)

    resolved_returns = [float(row["realized_return"]) for row in resolved if row.get("realized_return") is not None]
    trading_days = len({row["available_at"].astimezone(UTC).date() for row in resolved})
    sample_ready = len(resolved_returns) >= MIN_RESOLVED_EPISODES and trading_days >= MIN_TRADING_DAYS
    expectancy = mean(resolved_returns) if sample_ready and resolved_returns else None
    lower_95 = _lower_95(resolved_returns) if sample_ready else None
    calibration = _calibration(resolved) if sample_ready else {
        "status": "collecting", "value": None, "metric": "brier_score_probability_profit",
    }
    gaps: list[str] = []
    if len(resolved_returns) < MIN_RESOLVED_EPISODES:
        gaps.append(f"need_{MIN_RESOLVED_EPISODES - len(resolved_returns)}_more_resolved_independent_episodes")
    if trading_days < MIN_TRADING_DAYS:
        gaps.append(f"need_{MIN_TRADING_DAYS - trading_days}_more_trading_days")
    if defects:
        gaps.append("quarantined_data_health_defects_present")
    if calibration["status"] != "ready":
        gaps.append("deterministic_calibration_baseline_missing")
    if lower_95 is not None and lower_95 <= 0:
        gaps.append("lower_95_expectancy_not_positive")

    funnel = {
        "observed": raw_observation_count,
        "independent": len(episodes),
        "published": stages["published_signal"],
        "ticketed": stages["ready_ticket"],
        "filled": stages["simulated_fill"],
        "closed": stages["closed_paper_trade"],
    }
    if not (
        funnel["closed"] <= funnel["filled"] <= funnel["ticketed"]
        <= funnel["published"] <= funnel["independent"] <= funnel["observed"]
    ):
        defects["funnel_order_violation"] = defects.get("funnel_order_violation", 0) + 1
        gaps.append("funnel_order_violation")

    integrity_status = "INVALID" if defects else ("REBUILDING" if not sample_ready else "VALID")
    ready = not gaps and integrity_status == "VALID"
    return {
        "lane": lane,
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "raw_observation_count": raw_observation_count,
        "independent_episode_count": len(episodes),
        "quarantined_independent_episode_count": all_independent_episodes - len(episodes),
        "missing_episode_key_count": missing_episode_key_count,
        "resolved_independent_episode_count": len(resolved_returns),
        "trading_day_count": trading_days,
        "stages": stages,
        "funnel": funnel,
        "expectancy": expectancy,
        "lower_95_expectancy": lower_95,
        "calibration": calibration,
        "data_health_defects": defects,
        "truth_contract": SCORECARD_TRUTH_VERSION,
        "integrity_status": integrity_status,
        "display_status": "INVALID / REBUILDING" if integrity_status == "INVALID" else integrity_status,
        "status": "READY_FOR_REVIEW" if ready else ("INVALID" if integrity_status == "INVALID" else "COLLECTING"),
        "gaps": gaps,
        "automatic_strategy_promotion": {
            "enabled": False,
            "eligible": False,
            "reason": "scorecard_invalid_or_rebuilding",
        },
    }


def _lower_95(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return mean(values) - 1.96 * stdev(values) / sqrt(len(values))


def _calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [
        (float(row["probability_profit"]), 1.0 if float(row.get("realized_return") or 0.0) > 0 else 0.0)
        for row in rows
        if row.get("probability_profit") is not None and row.get("realized_return") is not None
        and 0.0 <= float(row["probability_profit"]) <= 1.0
    ]
    if len(pairs) < MIN_RESOLVED_EPISODES:
        return {"status": "collecting", "value": None, "sample_size": len(pairs)}
    brier = mean((prediction - outcome) ** 2 for prediction, outcome in pairs)
    baseline = mean((0.5 - outcome) ** 2 for _, outcome in pairs)
    return {
        "status": "ready" if brier <= baseline else "worse_than_baseline",
        "metric": "brier_score_probability_profit",
        "value": brier,
        "baseline": baseline,
        "sample_size": len(pairs),
    }
