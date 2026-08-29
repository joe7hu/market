"""Workspace and canonical-run helpers for the QQQ options decision surface."""

from __future__ import annotations

from typing import Any, Callable

from investment_panel.analysis.history_v3 import MODEL_REVISION
from investment_panel.database.runtime import DatabaseRuntime


def latest_run(runtime: DatabaseRuntime, *, symbol: str) -> dict[str, Any] | None:
    with runtime.read() as connection:
        row = connection.execute(
            """
            SELECT run.id, run.summary, run.finished_at
            FROM analysis.run run
            JOIN raw.option_capture_generation generation ON generation.id = (run.summary->>'capture_generation_id')::bigint
            JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
            WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
              AND run.summary->>'model_revision' = %s AND snapshot.history_symbol = %s
              AND run.finished_at IS NOT NULL AND run.finished_at <= now()
            ORDER BY snapshot.slot_at DESC NULLS LAST, generation.id DESC,
                     run.finished_at DESC NULLS LAST LIMIT 1
            """,
            [MODEL_REVISION, symbol.upper()],
        ).fetchone()
    return dict(row) if row else None


def workspace_payload(
    runtime: DatabaseRuntime,
    *,
    symbol: str,
    lane: str,
    mode: str,
    decision_brief: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    brief = decision_brief(symbol=symbol, lane=lane)
    readiness = dict(brief.get("readiness") or {})
    capture = dict(readiness.get("capture") or {})
    canary = dict(readiness.get("canary") or {})
    with runtime.read() as connection:
        counts = connection.execute(
            """
            WITH latest AS (
                SELECT run.id
                FROM analysis.run run
                JOIN raw.option_capture_generation generation ON generation.id = (run.summary->>'capture_generation_id')::bigint
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                  AND run.summary->>'model_revision' = %s AND snapshot.history_symbol = %s
                  AND run.finished_at IS NOT NULL AND run.finished_at <= now()
                ORDER BY snapshot.slot_at DESC NULLS LAST, generation.id DESC, run.finished_at DESC NULLS LAST LIMIT 1
            )
            SELECT
                count(*) FILTER (WHERE option_decision.paper_state IS NOT NULL) AS candidates,
                count(*) FILTER (WHERE option_decision.paper_state = 'REJECT') AS rejections,
                (SELECT count(*) FROM app.paper_order paper_order
                 JOIN analysis.decision decision ON decision.id = paper_order.decision_id
                 JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                 WHERE instrument.symbol = %s) AS journal,
                (SELECT count(*) FROM analysis.shadow_trade shadow
                 JOIN analysis.decision decision ON decision.id = shadow.decision_id
                 JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                 LEFT JOIN app.paper_order paper_order ON paper_order.decision_id = decision.id
                 WHERE instrument.symbol = %s AND shadow.source_kind = 'options_history_v3'
                   AND paper_order.id IS NULL
                   AND NOT ('thesis_upgrade_required' = ANY(coalesce(decision.blockers, ARRAY[]::text[])))
                ) AS shadow_observations
            FROM analysis.option_decision option_decision
            JOIN analysis.decision decision ON decision.id = option_decision.decision_id
            WHERE decision.run_id = (SELECT id FROM latest)
            """,
            [MODEL_REVISION, symbol.upper(), symbol.upper(), symbol.upper()],
        ).fetchone()
    return {
        "symbol": symbol.upper(),
        "decision_brief": brief,
        "capture_generation_id": capture.get("capture_generation_id"),
        "evidence_as_of": brief.get("as_of"),
        "generated_at": brief.get("as_of"),
        "freshness_state": "current" if capture.get("capture_state") == "complete" else "collecting",
        "canary_status": canary,
        "active_revision": MODEL_REVISION,
        "strategy_route": (
            (brief.get("strongest_candidate") or {}).get("strategy_route")
            or {
                "selected_structure": "NO_TRADE",
                "route_blockers": ["no_current_candidate_route"],
            }
        ),
        "market_regime": (
            (brief.get("strongest_candidate") or {}).get("market_regime")
            or {
                "trend_state": "unavailable",
                "quality_status": "unavailable",
                "reason_codes": ["no_current_candidate_market_regime"],
            }
        ),
        "paper_action_capability": {
            "mode": mode,
            "enabled": False,
            "reason": "options_paper_actions_enabled_false",
        },
        "tab_counts": {
            "candidates": int(counts["candidates"] or 0) if counts else 0,
            "rejections": int(counts["rejections"] or 0) if counts else 0,
            "journal": int(counts["journal"] or 0) if counts else 0,
            "shadow_observations": int(counts["shadow_observations"] or 0) if counts else 0,
        },
    }
