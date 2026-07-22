"""Repair derived options-history v3 lifecycle state without touching raw evidence."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


R2_REVISION = "history-v3-price-shape-r2"


def repair_invalid_v3_state(runtime: DatabaseRuntime, *, dry_run: bool = True) -> dict[str, Any]:
    """Quarantine invalid derived v3 outcomes/shadows and report invariants."""

    with runtime.transaction(JOB_PROFILE) as connection:
        before = _invariants(connection)
        invalid_outcomes = [
            str(row["decision_id"])
            for row in connection.execute(
                """
                SELECT outcome.decision_id
                FROM analysis.option_outcome outcome
                JOIN analysis.shadow_trade shadow ON shadow.decision_id = outcome.decision_id
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = outcome.decision_id
                WHERE shadow.source_kind = 'options_history_v3'
                  AND (
                      shadow.status NOT IN ('entered', 'closed', 'expired')
                      OR shadow.entry_at IS NULL
                      OR shadow.entry_price IS NULL
                      OR shadow.entry_cohort_id IS NULL
                      OR jsonb_array_length(option_decision.synthetic_legs) = 0
                  )
                ORDER BY outcome.decision_id
                """
            ).fetchall()
        ]
        r2_shadows = [
            str(row["id"])
            for row in connection.execute(
                """
                SELECT shadow.id
                FROM analysis.shadow_trade shadow
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = shadow.decision_id
                WHERE shadow.source_kind = 'options_history_v3'
                  AND option_decision.model_version = %s
                ORDER BY shadow.created_at, shadow.id
                """,
                [R2_REVISION],
            ).fetchall()
        ]
        if dry_run:
            after = dict(before)
            after["pending_or_unentered_outcomes"] = max(
                0, after["pending_or_unentered_outcomes"] - len(invalid_outcomes)
            )
            after["v3_outcomes_without_shadow"] = 0
        elif invalid_outcomes:
            connection.execute(
                "DELETE FROM analysis.option_outcome WHERE decision_id = ANY(%s::uuid[])",
                [invalid_outcomes],
            )
        if not dry_run and r2_shadows:
            connection.execute(
                "DELETE FROM analysis.shadow_trade WHERE id = ANY(%s::uuid[])",
                [r2_shadows],
            )
        if not dry_run:
            after = _invariants(connection)
        return {
            "dry_run": dry_run,
            "invalid_v3_outcome_decision_ids": invalid_outcomes,
            "r2_shadow_trade_ids": r2_shadows,
            "counts": {
                "invalid_v3_outcomes": len(invalid_outcomes),
                "r2_shadow_trades": len(r2_shadows),
            },
            "before": before,
            "after": after,
            "preserved": [
                "raw.option_capture_generation",
                "raw.option_quote",
                "analysis.run",
                "analysis.option_decision",
                "analysis.option_relative_value",
                "app.publication",
            ],
        }


def _invariants(connection: Any) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM raw.option_capture_generation) AS capture_generations,
            (SELECT count(*) FROM raw.option_quote WHERE capture_generation_id IS NOT NULL) AS captured_quotes,
            (SELECT count(*) FROM analysis.option_relative_value) AS relative_values,
            (SELECT count(*) FROM analysis.option_decision WHERE model_version = 'history-v3-price-shape-r3') AS r3_decisions,
            (SELECT count(*)
             FROM analysis.option_outcome outcome
             JOIN analysis.shadow_trade shadow ON shadow.decision_id = outcome.decision_id
             WHERE shadow.source_kind = 'options_history_v3'
               AND (shadow.status = 'pending' OR shadow.entry_at IS NULL OR shadow.entry_price IS NULL)) AS pending_or_unentered_outcomes,
            (SELECT count(*)
             FROM analysis.option_outcome outcome
             WHERE outcome.outcome_source = 'options_history_v3' AND outcome.shadow_trade_id IS NULL) AS v3_outcomes_without_shadow
        """
    ).fetchone()
    return {key: int(value or 0) for key, value in dict(row).items()}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    result = repair_invalid_v3_state(runtime_for_config(load_config(args.config)), dry_run=not args.execute)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
