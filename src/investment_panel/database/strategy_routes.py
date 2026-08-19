"""Persist the versioned daily strategy-route challenger and its evidence."""

from __future__ import annotations

from collections import Counter
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.analysis.strategy_routing import ROUTE_VERSION, route_strategy
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.event_studies import FEATURE_VERSION as EVENT_FEATURE_VERSION
from investment_panel.database.symbol_trends import FEATURE_SET, feature_row_payload


def apply_strategy_routes(
    runtime: DatabaseRuntime,
    run_id: Any,
    *,
    market_regime: dict[str, Any],
) -> dict[str, Any]:
    with runtime.read(JOB_PROFILE) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT decision.id AS decision_id, decision.as_of, run.input_cutoff, instrument.symbol,
                       symbol_feature.feature_version,
                       symbol_feature.momentum_5d, symbol_feature.momentum_20d,
                       symbol_feature.relative_strength_20d,
                       symbol_feature.relative_strength_60d,
                       symbol_feature.kaufman_er_20d, symbol_feature.kaufman_er_60d,
                       symbol_feature.kama_fast, symbol_feature.kama_slow,
                       symbol_feature.kama_fast_slope, symbol_feature.kama_slow_slope,
                       symbol_feature.trend_state, symbol_feature.trend_confidence,
                       symbol_feature.volatility_state,
                       symbol_feature.data_quality_status, symbol_feature.reason_codes,
                       symbol_feature.metrics AS symbol_metrics,
                       feature.modeled_iv, feature.iv_percentile,
                       option_decision.details AS option_details,
                       event_feature.id::text AS event_study_id,
                       event_feature.sample_size, event_feature.actual_move_median,
                       event_feature.actual_move_p75, event_feature.actual_move_p90,
                       event_feature.implied_move, event_feature.evidence_state,
                       event_feature.details AS event_details
                FROM analysis.decision decision
                JOIN analysis.run run ON run.id = decision.run_id
                JOIN analysis.option_decision option_decision
                  ON option_decision.decision_id = decision.id
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                LEFT JOIN analysis.symbol_feature symbol_feature
                  ON symbol_feature.run_id = decision.run_id
                 AND symbol_feature.instrument_id = decision.instrument_id
                 AND symbol_feature.feature_set = %s
                LEFT JOIN analysis.option_feature feature
                  ON feature.run_id = decision.run_id
                 AND feature.contract_id = option_decision.contract_id
                LEFT JOIN LATERAL (
                    SELECT study.*
                    FROM analysis.event_study_feature study
                    JOIN raw.market_event_version event ON event.id = study.market_event_version_id
                    WHERE study.instrument_id = decision.instrument_id
                      AND study.run_id = run.id
                      AND study.feature_version = %s
                      AND study.as_of <= decision.as_of
                      AND event.starts_at >= decision.as_of
                      AND event.starts_at <= decision.as_of + interval '45 days'
                    ORDER BY event.starts_at, study.event_kind, study.market_event_id,
                             study.feature_version DESC LIMIT 1
                ) event_feature ON true
                WHERE decision.run_id = %s
                ORDER BY decision.id
                """,
                [FEATURE_SET, EVENT_FEATURE_VERSION, run_id],
            ).fetchall()
        ]
    routes: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        symbol = feature_row_payload({
            "feature_version": row.get("feature_version"),
            "momentum_5d": row.get("momentum_5d"),
            "momentum_20d": row.get("momentum_20d"),
            "relative_strength_20d": row.get("relative_strength_20d"),
            "relative_strength_60d": row.get("relative_strength_60d"),
            "kaufman_er_20d": row.get("kaufman_er_20d"),
            "kaufman_er_60d": row.get("kaufman_er_60d"),
            "kama_fast": row.get("kama_fast"),
            "kama_slow": row.get("kama_slow"),
            "kama_fast_slope": row.get("kama_fast_slope"),
            "kama_slow_slope": row.get("kama_slow_slope"),
            "trend_state": row.get("trend_state"),
            "trend_confidence": row.get("trend_confidence"),
            "volatility_state": row.get("volatility_state"),
            "data_quality_status": row.get("data_quality_status"),
            "reason_codes": row.get("reason_codes"),
            "metrics": row.get("symbol_metrics"),
        } if row.get("feature_version") else None)
        event_details = dict(row.get("event_details") or {})
        event = {
            **event_details,
            "reference_key": row.get("event_study_id"),
            "sample_size": row.get("sample_size"),
            "actual_move_median": row.get("actual_move_median"),
            "actual_move_p75": row.get("actual_move_p75"),
            "actual_move_p90": row.get("actual_move_p90"),
            "implied_move": row.get("implied_move"),
            "evidence_state": row.get("evidence_state") or "insufficient_event_evidence",
        }
        metrics = dict(symbol.get("metrics") or {})
        candidate_market = dict(market_regime)
        if row.get("as_of") != row.get("input_cutoff"):
            candidate_market["quality_status"] = "unavailable"
            candidate_market["reason_codes"] = sorted(set(
                list(candidate_market.get("reason_codes") or [])
                + ["decision_cutoff_precedes_market_regime"]
            ))
        route = route_strategy(
            symbol,
            candidate_market,
            option_iv=_number(row.get("modeled_iv")),
            realized_vol=_number(metrics.get("realized_vol_20d")),
            iv_percentile=_number(row.get("iv_percentile")),
            event_summary=event,
            portfolio_allows_csp=False,
            as_of=row.get("as_of"),
        )
        routes.append((row["decision_id"], route, candidate_market))
    with runtime.transaction(JOB_PROFILE) as connection:
        for decision_id, route, candidate_market in routes:
            connection.execute(
                """
                UPDATE analysis.option_decision
                SET route_version = %s,
                    strategy_route = %s,
                    market_regime_detail = %s,
                    event_state = %s,
                    details = jsonb_set(details, '{strategy_route}', %s, true)
                WHERE decision_id = %s
                """,
                [
                    ROUTE_VERSION, Jsonb(route), Jsonb(candidate_market), route["event_state"],
                    Jsonb(route), decision_id,
                ],
            )
            connection.execute(
                """
                INSERT INTO analysis.decision_evidence (
                    decision_id, evidence_kind, reference_key, detail
                ) VALUES (%s, 'strategy_route', %s, %s)
                ON CONFLICT (decision_id, evidence_kind, reference_key)
                DO UPDATE SET detail = EXCLUDED.detail
                """,
                [decision_id, ROUTE_VERSION, Jsonb(route)],
            )
    selected = Counter(route["selected_structure"] for _decision_id, route, _market in routes)
    return {
        "route_count": len(routes),
        "route_version": ROUTE_VERSION,
        "selected_structures": dict(sorted(selected.items())),
        "shadow": True,
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
