"""Strategy cohort results, backtest and forward-test materialization."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from investment_panel.analysis.stats import (wilson_interval)
from investment_panel.core.db import (json_dumps)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_elapsed_days, _iso, _json_or_list, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION, MIN_FORWARD_TEST_DAYS)
from investment_panel.core.options_radar.dbutil import (_strategy_parameters)
from investment_panel.core.options_radar.strategy_outcomes import (_backtest_verdict, _cohort_definition, _forward_test_verdict, _historical_candidate_rows, _outcome_metrics, _proposed_strategy_parameters, _strategy_arm_significance, _strategy_outcome_records, _strategy_outcomes, _value_counts, _walk_forward_folds)

def refresh_strategy_cohort_results(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    con.execute("DELETE FROM strategy_cohort_result WHERE strategy_version = ?", [strategy_version])
    rows = _historical_candidate_rows(con)
    if not rows:
        return 0
    strategy = _strategy_parameters(con, strategy_version)
    records = _strategy_outcome_records(con, rows, strategy_version, strategy)
    if not records:
        return 0

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        for cohort in record.get("cohorts") or []:
            cohort_type = str(cohort.get("type") or "")
            cohort_value = str(cohort.get("value") or "")
            if cohort_type and cohort_value:
                grouped[(cohort_type, cohort_value)].append(record)

    evaluated_at = datetime.utcnow().isoformat()
    count = 0
    for (cohort_type, cohort_value), cohort_records in sorted(grouped.items()):
        result = build_strategy_cohort_result(
            strategy_version=strategy_version,
            cohort_type=cohort_type,
            cohort_value=cohort_value,
            evaluated_at=evaluated_at,
            records=cohort_records,
        )
        con.execute(
            """
            INSERT OR REPLACE INTO strategy_cohort_result
            (cohort_id, evaluated_at, strategy_version, cohort_type,
             cohort_value, candidate_count, hit_rate_2x, hit_rate_5x,
             hit_rate_10x, false_positive_rate, median_max_return,
             median_max_drawdown, average_time_to_2x, early_entry_rate,
             theta_iv_bleed_rate, good_convexity_rate, qqq_above_200d_rate,
             raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                result["cohort_id"],
                result["evaluated_at"],
                result["strategy_version"],
                result["cohort_type"],
                result["cohort_value"],
                result["candidate_count"],
                result["hit_rate_2x"],
                result["hit_rate_5x"],
                result["hit_rate_10x"],
                result["false_positive_rate"],
                result["median_max_return"],
                result["median_max_drawdown"],
                result["average_time_to_2x"],
                result["early_entry_rate"],
                result["theta_iv_bleed_rate"],
                result["good_convexity_rate"],
                result["qqq_above_200d_rate"],
                json_dumps(result["raw"]),
            ],
        )
        count += 1
    return count


def build_strategy_cohort_result(
    *,
    strategy_version: str,
    cohort_type: str,
    cohort_value: str,
    evaluated_at: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    metrics = _outcome_metrics(records)
    count = int(metrics["candidate_count"])
    # Honest sampling: Wilson interval on the 2x hit rate, and a significance flag that
    # the cohort's edge clears a 10% base rate with enough observations.
    succ_2x = round(float(metrics["hit_rate_2x"]) * count)
    wilson_lo, wilson_hi = wilson_interval(succ_2x, count)
    cohort_significant = count >= 20 and wilson_lo >= 0.10
    labels = [str(row.get("latest_attribution_label") or "") for row in records]
    qqq_above = [row for row in records if row.get("qqq_above_200d") is True]
    early_entries = [row for row in records if row.get("timing_label") in {"early_but_worked", "false_positive_drawdown"}]
    mature_records = [row for row in records if (_number(row.get("observation_hours")) or 0) >= 20]
    pending_records = count - len(mature_records)
    return {
        "cohort_id": stable_id("strategy_cohort_result", strategy_version, cohort_type, cohort_value),
        "evaluated_at": evaluated_at,
        "strategy_version": strategy_version,
        "cohort_type": cohort_type,
        "cohort_value": cohort_value,
        "candidate_count": count,
        "hit_rate_2x": metrics["hit_rate_2x"],
        "hit_rate_5x": metrics["hit_rate_5x"],
        "hit_rate_10x": metrics["hit_rate_10x"],
        "false_positive_rate": metrics["false_positive_rate"],
        "median_max_return": metrics["median_max_return"],
        "median_max_drawdown": metrics["median_max_drawdown"],
        "average_time_to_2x": metrics["average_time_to_2x"],
        "early_entry_rate": len(early_entries) / count if count else 0.0,
        "theta_iv_bleed_rate": labels.count("theta_iv_bleed") / count if count else 0.0,
        "good_convexity_rate": labels.count("good_convexity") / count if count else 0.0,
        "qqq_above_200d_rate": len(qqq_above) / count if count else 0.0,
        "raw": {
            "promotion_policy": "cohort_analysis_is_diagnostic_only",
            "significance": {
                "hit_2x_wilson_lo": round(wilson_lo, 4),
                "hit_2x_wilson_hi": round(wilson_hi, 4),
                "n": count,
                "significant": cohort_significant,
            },
            "sample_outcomes": metrics["outcomes"][:20],
            "maturity": {
                "mature_count": len(mature_records),
                "pending_count": pending_records,
                "min_mature_hours": 20,
                "rates_use_full_cohort_denominator": True,
            },
            "timing_labels": _value_counts([str(row.get("timing_label") or "unknown") for row in records]),
            "attribution_labels": _value_counts([label or "none" for label in labels]),
            "cohort_definition": _cohort_definition(cohort_type, cohort_value),
        },
    }


def build_strategy_backtest_result(con: Any, proposal: dict[str, Any]) -> dict[str, Any] | None:
    rows = _historical_candidate_rows(con)
    if not rows:
        return None
    base_params = _strategy_parameters(con, proposal["strategy_version"])
    proposed_params = _proposed_strategy_parameters(base_params, proposal.get("proposed_parameter_changes"))
    baseline = _strategy_outcomes(con, rows, proposal["strategy_version"], base_params)
    proposed = _strategy_outcomes(con, rows, proposal["proposed_strategy_version"], proposed_params)
    lookback_start = min(_iso(row["snapshot_time"]) for row in rows)
    lookback_end = max(_iso(row["snapshot_time"]) for row in rows)
    # Test significance on the same metric the verdict claims to improve (5x is the
    # primary win threshold the proposals target), not a looser 2x proxy. 5x events are
    # rarer, so this honestly blocks more often on insufficient sample.
    significance = _strategy_arm_significance(baseline, proposed, key="5x")
    ordered_rows = sorted(rows, key=lambda r: _iso(r.get("snapshot_time")))
    walk_forward = _walk_forward_folds(
        ordered_rows,
        lambda slice_rows: _strategy_outcomes(con, slice_rows, proposal["strategy_version"], base_params),
        lambda slice_rows: _strategy_outcomes(con, slice_rows, proposal["proposed_strategy_version"], proposed_params),
    )
    validation_verdict = _backtest_verdict(baseline, proposed, significance=significance, walk_forward=walk_forward)
    verdict = "fail" if validation_verdict == "insufficient_sample" else validation_verdict
    return {
        "backtest_id": stable_id("strategy_backtest_result", proposal["proposal_id"], lookback_start, lookback_end),
        "proposal_id": proposal["proposal_id"],
        "evaluated_at": datetime.utcnow().isoformat(),
        "strategy_version": proposal["strategy_version"],
        "proposed_strategy_version": proposal["proposed_strategy_version"],
        "lookback_start": lookback_start,
        "lookback_end": lookback_end,
        "baseline_candidate_count": baseline["candidate_count"],
        "proposed_candidate_count": proposed["candidate_count"],
        "baseline_hit_rate_2x": baseline["hit_rate_2x"],
        "baseline_hit_rate_5x": baseline["hit_rate_5x"],
        "baseline_hit_rate_10x": baseline["hit_rate_10x"],
        "proposed_hit_rate_2x": proposed["hit_rate_2x"],
        "proposed_hit_rate_5x": proposed["hit_rate_5x"],
        "proposed_hit_rate_10x": proposed["hit_rate_10x"],
        "proposed_false_positive_rate": proposed["false_positive_rate"],
        "verdict": verdict,
        "metrics": {"baseline": baseline, "proposed": proposed, "significance": significance, "walk_forward": walk_forward},
        "raw": {
            "proposal_changes": _json_or_list(proposal.get("proposed_parameter_changes")),
            "promotion_gate": "backtest_only_never_promotes",
            "validation": "walk_forward_oos_in_time + two_proportion_significance",
            "validation_verdict": validation_verdict,
        },
    }


def insert_strategy_backtest_result(con: Any, result: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO strategy_backtest_result
        (backtest_id, proposal_id, evaluated_at, strategy_version,
         proposed_strategy_version, lookback_start, lookback_end,
         baseline_candidate_count, proposed_candidate_count,
         baseline_hit_rate_2x, baseline_hit_rate_5x, baseline_hit_rate_10x,
         proposed_hit_rate_2x, proposed_hit_rate_5x, proposed_hit_rate_10x,
         proposed_false_positive_rate, verdict, metrics, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            result["backtest_id"],
            result["proposal_id"],
            result["evaluated_at"],
            result["strategy_version"],
            result["proposed_strategy_version"],
            result["lookback_start"],
            result["lookback_end"],
            result["baseline_candidate_count"],
            result["proposed_candidate_count"],
            result["baseline_hit_rate_2x"],
            result["baseline_hit_rate_5x"],
            result["baseline_hit_rate_10x"],
            result["proposed_hit_rate_2x"],
            result["proposed_hit_rate_5x"],
            result["proposed_hit_rate_10x"],
            result["proposed_false_positive_rate"],
            result["verdict"],
            json_dumps(result["metrics"]),
            json_dumps(result["raw"]),
        ],
    )


def build_strategy_forward_test_result(con: Any, proposal: dict[str, Any]) -> dict[str, Any] | None:
    rows = _historical_candidate_rows(con)
    if not rows:
        return None
    created_at = _iso(proposal.get("created_at"))
    forward_rows = [row for row in rows if _iso(row.get("snapshot_time")) >= created_at]
    if not forward_rows:
        forward_rows = rows[-1:]
    base_params = _strategy_parameters(con, proposal["strategy_version"])
    proposed_params = _proposed_strategy_parameters(base_params, proposal.get("proposed_parameter_changes"))
    baseline = _strategy_outcomes(con, forward_rows, proposal["strategy_version"], base_params)
    proposed = _strategy_outcomes(con, forward_rows, proposal["proposed_strategy_version"], proposed_params)
    forward_start = min(_iso(row["snapshot_time"]) for row in forward_rows)
    forward_end = max(_iso(row["snapshot_time"]) for row in forward_rows)
    days_observed = max(0, _elapsed_days(forward_start, forward_end) or 0)
    verdict = _forward_test_verdict(baseline, proposed, days_observed)
    status = "complete" if verdict in {"pass", "fail"} else "active"
    return {
        "forward_test_id": stable_id("strategy_forward_test_result", proposal["proposal_id"], forward_start, forward_end),
        "proposal_id": proposal["proposal_id"],
        "evaluated_at": datetime.utcnow().isoformat(),
        "strategy_version": proposal["strategy_version"],
        "proposed_strategy_version": proposal["proposed_strategy_version"],
        "forward_start": forward_start,
        "forward_end": forward_end,
        "days_observed": days_observed,
        "baseline_candidate_count": baseline["candidate_count"],
        "proposed_candidate_count": proposed["candidate_count"],
        "baseline_hit_rate_2x": baseline["hit_rate_2x"],
        "baseline_hit_rate_5x": baseline["hit_rate_5x"],
        "baseline_hit_rate_10x": baseline["hit_rate_10x"],
        "proposed_hit_rate_2x": proposed["hit_rate_2x"],
        "proposed_hit_rate_5x": proposed["hit_rate_5x"],
        "proposed_hit_rate_10x": proposed["hit_rate_10x"],
        "status": status,
        "verdict": verdict,
        "metrics": {"baseline": baseline, "proposed": proposed},
        "raw": {"min_forward_test_days": MIN_FORWARD_TEST_DAYS, "promotion_gate": "forward_shadow_comparison_required"},
    }


def insert_strategy_forward_test_result(con: Any, result: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO strategy_forward_test_result
        (forward_test_id, proposal_id, evaluated_at, strategy_version,
         proposed_strategy_version, forward_start, forward_end, days_observed,
         baseline_candidate_count, proposed_candidate_count,
         baseline_hit_rate_2x, baseline_hit_rate_5x, baseline_hit_rate_10x,
         proposed_hit_rate_2x, proposed_hit_rate_5x, proposed_hit_rate_10x,
         status, verdict, metrics, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            result["forward_test_id"],
            result["proposal_id"],
            result["evaluated_at"],
            result["strategy_version"],
            result["proposed_strategy_version"],
            result["forward_start"],
            result["forward_end"],
            result["days_observed"],
            result["baseline_candidate_count"],
            result["proposed_candidate_count"],
            result["baseline_hit_rate_2x"],
            result["baseline_hit_rate_5x"],
            result["baseline_hit_rate_10x"],
            result["proposed_hit_rate_2x"],
            result["proposed_hit_rate_5x"],
            result["proposed_hit_rate_10x"],
            result["status"],
            result["verdict"],
            json_dumps(result["metrics"]),
            json_dumps(result["raw"]),
        ],
    )
