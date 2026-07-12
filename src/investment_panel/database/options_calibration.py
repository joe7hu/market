"""Hierarchical structure-level option calibration from resolved outcomes."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, pstdev
from typing import Any

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


MIN_STRUCTURE_SAMPLE = 30


def calibration_profiles(runtime: DatabaseRuntime, strategy_id: int) -> list[dict[str, Any]]:
    with runtime.read(JOB_PROFILE) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT option_decision.structure, option_decision.probability_profit,
                       outcome.current_return, outcome.peak_return,
                       outcome.maturity_state
                FROM analysis.option_outcome outcome
                JOIN analysis.decision decision ON decision.id = outcome.decision_id
                JOIN analysis.option_decision option_decision
                  ON option_decision.decision_id = decision.id
                WHERE decision.strategy_revision_id = %s
                  AND outcome.maturity_state IN ('mature', 'expired')
                  AND outcome.current_return IS NOT NULL
                """,
                [strategy_id],
            ).fetchall()
        ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("structure") or "long_option")].append(row)
    global_returns = [float(row["current_return"]) for row in rows]
    global_mean = mean(global_returns) if global_returns else 0.0
    profiles: list[dict[str, Any]] = []
    for structure, cohort in sorted(grouped.items()):
        returns = [float(row["current_return"]) for row in cohort]
        outcomes = [1.0 if float(row.get("current_return") or 0) > 0 else 0.0 for row in cohort]
        prediction_pairs = [
            (float(row["probability_profit"]), 1.0 if float(row["current_return"]) > 0 else 0.0)
            for row in cohort
            if row.get("probability_profit") is not None
        ]
        brier = mean((prediction - outcome) ** 2 for prediction, outcome in prediction_pairs) if prediction_pairs else None
        standard_error = pstdev(returns) / math.sqrt(len(returns)) if len(returns) > 1 else None
        lower_bound = mean(returns) - 1.96 * standard_error if standard_error is not None else None
        shrunk_expectancy = (sum(returns) + 10 * global_mean) / (len(returns) + 10)
        mature = len(returns) >= MIN_STRUCTURE_SAMPLE
        profiles.append({
            "stable_key": structure,
            "structure": structure,
            "sample_size": len(returns),
            "net_expectancy": mean(returns),
            "hierarchical_expectancy": shrunk_expectancy,
            "lower_95_expectancy": lower_bound,
            "brier_score": brier,
            "win_rate": mean(outcomes),
            "mature": mature,
            "ready": bool(mature and lower_bound is not None and lower_bound > 0 and (brier is None or brier <= 0.25)),
        })
    return profiles


def ready_structures(runtime: DatabaseRuntime, strategy_id: int) -> set[str]:
    return {str(row["structure"]) for row in calibration_profiles(runtime, strategy_id) if row["ready"]}
