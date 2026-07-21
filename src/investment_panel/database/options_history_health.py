"""Operational health and canary read model for QQQ option history."""

from __future__ import annotations

from typing import Any

from investment_panel.analysis.history_v3 import MODEL_REVISION
from investment_panel.database.options_history_canary import canary_health
from investment_panel.database.runtime import DatabaseRuntime


def history_health(runtime: DatabaseRuntime) -> dict[str, Any]:
    with runtime.read() as connection:
        row = connection.execute(
            """
            SELECT count(*) AS snapshots,
                   max(slot_at) FILTER (WHERE latest_complete_generation_id IS NOT NULL) AS latest_complete_slot,
                   avg(completeness) FILTER (WHERE latest_complete_generation_id IS NOT NULL) AS average_completeness,
                   coalesce((SELECT sum(pg_total_relation_size(inhrelid)) FROM pg_inherits
                       WHERE inhparent = 'raw.option_quote'::regclass), 0)::bigint AS option_quote_bytes,
                   coalesce(pg_total_relation_size('analysis.option_surface_summary'), 0) AS surface_summary_bytes
            FROM raw.option_snapshot WHERE collection_profile = 'history_full'
            """
        ).fetchone()
        v3 = connection.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON ((run.summary->>'capture_generation_id')::bigint) run.*
                FROM analysis.run run
                WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                  AND run.summary->>'model_revision' = %s
                ORDER BY (run.summary->>'capture_generation_id')::bigint, run.finished_at DESC NULLS LAST
            )
            SELECT count(*) AS runs, coalesce(sum((summary->>'solver_failures')::integer), 0) AS solver_failures,
                   coalesce(sum((summary->>'fit_attempts')::integer), 0) AS fit_attempts,
                   coalesce(sum((summary->>'succeeded_groups')::integer), 0) AS succeeded_groups
            FROM latest
            """,
            [MODEL_REVISION],
        ).fetchone()
        shadows = connection.execute(
            """SELECT count(*) AS total, count(*) FILTER (WHERE status = 'entered') AS entered,
                      count(*) FILTER (WHERE status = 'unfilled') AS unfilled,
                      count(*) FILTER (WHERE status = 'pending') AS pending
               FROM analysis.shadow_trade WHERE source_kind = 'options_history_v3'"""
        ).fetchone()
        canary = canary_health(connection, symbol="QQQ", model_revision=MODEL_REVISION)
    history_bytes = int(row["option_quote_bytes"]) + int(row["surface_summary_bytes"])
    payload = dict(v3)
    fit_attempts, succeeded_groups = int(payload["fit_attempts"]), int(payload["succeeded_groups"])
    return {
        **dict(row), "storage_bytes": history_bytes, "retention_days": 730,
        "v3_runs": int(payload["runs"]), "v3_succeeded_runs": int(payload["runs"]),
        "solver_failures": int(payload["solver_failures"]),
        "solver_success_rate": succeeded_groups / fit_attempts if fit_attempts else None,
        "shadow": {key: int(value) for key, value in dict(shadows).items()}, **canary,
        "canary": {
            **canary,
            "paper_mode_eligible": fit_attempts > 0 and succeeded_groups / fit_attempts >= 0.99
            and int(canary["qualified_regular_sessions"]) >= int(canary["required_regular_sessions"]),
        },
    }
