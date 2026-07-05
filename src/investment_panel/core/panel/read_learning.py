"""Learning-loop read accessors (agents, strategy, shadow, attribution)."""

from __future__ import annotations
import json
from typing import Any
from investment_panel.core.db import db, init_db, query_rows

from investment_panel.core.panel.coerce import decode_fields
from investment_panel.core.panel.disclosures import _compact_empty_fields
from investment_panel.core.panel.read_options import RadarDisplayContext, _radar_current_candidate_time, radar_display_strategy_version


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}



def agent_thesis(con: Any, radar_context: RadarDisplayContext | None = None) -> list[dict[str, Any]]:
    current = _radar_current_candidate_time(con, radar_context)
    if not current:
        return []
    strategy_version = radar_context.strategy_version if radar_context else radar_display_strategy_version(con)
    rows = query_rows(
        con,
        """
        WITH current_tickers AS (
            SELECT DISTINCT ticker
            FROM candidate_event
            WHERE snapshot_time = TRY_CAST(? AS TIMESTAMP)
              AND strategy_version = ?
              AND state != 'REJECT'
        )
        SELECT thesis_id, ticker, created_at, agent_version, bull_target_price,
               bull_target_date, base_target_price, core_thesis, required_proofs,
               invalidation_conditions, catalysts, catalyst_summary, bear_case,
               confidence, evidence_refs, raw
        FROM agent_thesis
        WHERE ticker IN (SELECT ticker FROM current_tickers)
        ORDER BY created_at DESC, ticker
        LIMIT 500
        """,
        [current, strategy_version],
    )
    return [_compact_empty_fields(decode_fields(row, ("required_proofs", "invalidation_conditions", "catalysts", "evidence_refs", "raw"))) for row in rows]




def agent_thesis_request(con: Any, radar_context: RadarDisplayContext | None = None) -> list[dict[str, Any]]:
    current = _radar_current_candidate_time(con, radar_context)
    if not current:
        return []
    strategy_version = radar_context.strategy_version if radar_context else radar_display_strategy_version(con)
    rows = query_rows(
        con,
        """
        WITH current_events AS (
            SELECT event_id
            FROM candidate_event
            WHERE snapshot_time = TRY_CAST(? AS TIMESTAMP)
              AND strategy_version = ?
              AND state != 'REJECT'
        )
        SELECT request_id, created_at, ticker, event_id, strategy_version,
               priority_score, status, prompt, context, raw
        FROM agent_thesis_request
        WHERE event_id IN (SELECT event_id FROM current_events)
          AND status IN ('open', 'failed', 'agent_failed')
        ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'failed' THEN 1 WHEN 'agent_failed' THEN 1 WHEN 'superseded' THEN 2 ELSE 3 END,
                 priority_score DESC NULLS LAST,
                 created_at DESC
        LIMIT 500
        """,
        [current, strategy_version],
    )
    return [_compact_empty_fields(decode_fields(row, ("context", "raw"))) for row in rows]




def agent_thesis_validation(con: Any, radar_context: RadarDisplayContext | None = None) -> list[dict[str, Any]]:
    current = _radar_current_candidate_time(con, radar_context)
    if not current:
        return []
    strategy_version = radar_context.strategy_version if radar_context else radar_display_strategy_version(con)
    rows = query_rows(
        con,
        """
        WITH current_events AS (
            SELECT event_id
            FROM candidate_event
            WHERE snapshot_time = TRY_CAST(? AS TIMESTAMP)
              AND strategy_version = ?
              AND state != 'REJECT'
        )
        SELECT validation_id, thesis_id, ticker, strategy_version,
               validation_date, candidate_event_id, candidate_snapshot_time,
               validated_at, state, reason, option_still_valid, stock_progress,
               iv_status, candidate_state,
               proof_status, catalyst_status, invalidation_status, evidence_status,
               red_team_status, red_team_flags, evidence_refs, raw
        FROM agent_thesis_validation
        WHERE candidate_event_id IN (SELECT event_id FROM current_events)
        ORDER BY validation_date DESC NULLS LAST, validated_at DESC, ticker
        LIMIT 500
        """,
        [current, strategy_version],
    )
    return [_compact_empty_fields(decode_fields(row, ("red_team_flags", "evidence_refs", "raw"))) for row in rows]




def agent_postmortem_request(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT request_id, created_at, source_type, source_id, ticker,
               strategy_version, priority_score, status, prompt, context, raw
        FROM agent_postmortem_request
        WHERE status IN ('open', 'failed', 'agent_failed')
        ORDER BY created_at DESC, priority_score DESC NULLS LAST
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("context", "raw"))) for row in rows]




def agent_postmortem(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT postmortem_id, request_id, source_type, source_id, created_at,
               agent_version, ticker, strategy_version, outcome_type,
               failure_type, evidence, proposed_rule_change,
               proposed_parameter_changes, expected_effect, risk, confidence,
               evidence_refs, raw
        FROM agent_postmortem
        ORDER BY created_at DESC, confidence DESC NULLS LAST
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("evidence", "proposed_parameter_changes", "evidence_refs", "raw"))) for row in rows]




def candidate_event(con: Any, radar_context: RadarDisplayContext | None = None) -> list[dict[str, Any]]:
    current = _radar_current_candidate_time(con, radar_context)
    if not current:
        return []
    strategy_version = radar_context.strategy_version if radar_context else radar_display_strategy_version(con)
    rows = query_rows(
        con,
        """
        SELECT event_id, snapshot_time, ticker, contract_id, strategy_version,
               state, premium_mid, premium_fill_assumption, required_10x_price,
               required_move_pct, buy_under, trigger_reason, thesis_id, score,
               quality_status, quality_flags, raw
        FROM candidate_event
        WHERE snapshot_time = TRY_CAST(? AS TIMESTAMP)
          AND strategy_version = ?
          AND state != 'REJECT'
        ORDER BY CASE state WHEN 'FIRE' THEN 0 WHEN 'SETUP' THEN 1 WHEN 'WATCH' THEN 2 ELSE 3 END,
                 score DESC NULLS LAST,
                 ticker,
                 contract_id
        LIMIT 2000
        """,
        [current, strategy_version],
    )
    return [_compact_empty_fields(decode_fields(row, ("quality_flags", "raw"))) for row in rows]




def radar_alert(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT alert_id, created_at, alert_type, ticker, contract_id, event_id,
               severity, title, detail, acknowledged_at, resolution_reason, raw
        FROM radar_alert
        WHERE acknowledged_at IS NULL
        ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                 created_at DESC,
                 ticker,
                 alert_type
        LIMIT 200
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def shadow_trade(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT trade_id, event_id, entry_time, entry_price_assumption, exit_time,
               exit_price, status, max_return_seen, max_drawdown_seen, time_to_2x,
               time_to_5x, time_to_10x, exit_reason, raw
        FROM shadow_trade
        ORDER BY entry_time DESC, trade_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def candidate_event_mark(con: Any, radar_context: RadarDisplayContext | None = None) -> list[dict[str, Any]]:
    current = _radar_current_candidate_time(con, radar_context)
    if not current:
        return []
    strategy_version = radar_context.strategy_version if radar_context else radar_display_strategy_version(con)
    rows = query_rows(
        con,
        """
        WITH current_events AS (
            SELECT event_id
            FROM candidate_event
            WHERE snapshot_time = TRY_CAST(? AS TIMESTAMP)
              AND strategy_version = ?
              AND state != 'REJECT'
        )
        SELECT mark_id, event_id, contract_id, ticker, strategy_version,
               candidate_state, mark_time, alert_time, premium_fill_assumption,
               mark_price, current_return, return_1d, return_5d, return_20d,
               return_60d, max_return_since_alert, max_drawdown_since_alert,
               time_to_2x, time_to_5x, time_to_10x, dte, spread_pct, iv,
               underlying_price, raw
        FROM candidate_event_mark
        WHERE event_id IN (SELECT event_id FROM current_events)
        ORDER BY mark_time DESC, ticker, contract_id
        LIMIT 1000
        """,
        [current, strategy_version],
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def candidate_event_attribution(con: Any, radar_context: RadarDisplayContext | None = None) -> list[dict[str, Any]]:
    current = _radar_current_candidate_time(con, radar_context)
    if not current:
        return []
    strategy_version = radar_context.strategy_version if radar_context else radar_display_strategy_version(con)
    rows = query_rows(
        con,
        """
        WITH current_events AS (
            SELECT event_id
            FROM candidate_event
            WHERE snapshot_time = TRY_CAST(? AS TIMESTAMP)
              AND strategy_version = ?
              AND state != 'REJECT'
        )
        SELECT attribution_id, event_id, contract_id, ticker, strategy_version,
               candidate_state, snapshot_time, prior_snapshot_time,
               option_return, underlying_return, iv_change, theta_decay,
               spread_change, stock_move_effect, iv_effect, theta_effect,
               spread_effect, unexplained_effect, label, raw
        FROM candidate_event_attribution
        WHERE event_id IN (SELECT event_id FROM current_events)
        ORDER BY snapshot_time DESC, ticker, contract_id
        LIMIT 1000
        """,
        [current, strategy_version],
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def conviction_calibration(con: Any) -> list[dict[str, Any]]:
    """Probability-calibration bins (predicted vs realized P(2x) with Wilson intervals)
    for the Phase 4 calibration dashboard. Read model = the stored bins as-is."""

    rows = query_rows(
        con,
        """
        SELECT strategy_version, bin_index, bin_lo, bin_hi, n, predicted_p2x,
               realized_p2x, realized_p5x, wilson_lo, wilson_hi, brier, mature_n,
               calibrated, as_of, raw
        FROM conviction_calibration
        ORDER BY strategy_version, bin_index
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def vol_surface_features(con: Any) -> list[dict[str, Any]]:
    """Latest vol-surface row per ticker (ATM IV term structure, skew, IV/RV) feeding the
    opportunity drawer's IV term sparkline."""

    rows = query_rows(
        con,
        """
        SELECT snapshot_time, ticker, atm_iv_30d, atm_iv_90d, atm_iv_leap, term_slope,
               put_call_skew_25d, skew_change_5d, rv_20d, rv_60d, iv_rv_ratio,
               iv_percentile_252d, iv_percentile_basis, raw
        FROM vol_surface_features
        QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY snapshot_time DESC) = 1
        ORDER BY ticker
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def trade_journal(con: Any) -> list[dict[str, Any]]:
    """Recorded trade-journal entries (Phase 4 data layer), newest first."""

    rows = query_rows(
        con,
        """
        SELECT journal_id, created_at, strategy_version, ticker, contract_id, event_id,
               entry_premium, predicted_ev_multiple, predicted_p2x, conviction_score,
               opportunity_snapshot, realized_return, realized_status, closed_at, notes, raw
        FROM trade_journal
        ORDER BY created_at DESC
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("opportunity_snapshot", "raw"))) for row in rows]




def shadow_trade_mark(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT mark_id, trade_id, event_id, contract_id, ticker,
               strategy_version, mark_time, entry_time, entry_price_assumption,
               mark_price, current_return, return_1d, return_5d, return_20d,
               return_60d, max_return_since_alert, max_drawdown_since_alert,
               time_to_2x, time_to_5x, time_to_10x, dte, spread_pct, iv,
               underlying_price, expired_worthless_probability_change, raw
        FROM shadow_trade_mark
        ORDER BY mark_time DESC, ticker, contract_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def radar_state_transition(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT transition_id, evaluated_at, snapshot_time, ticker, contract_id,
               strategy_version, previous_state, state, candidate_state, event_id,
               trade_id, mark_id, thesis_id, trigger_reason, evidence_refs, raw
        FROM radar_state_transition
        ORDER BY snapshot_time DESC, ticker, contract_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("evidence_refs", "raw"))) for row in rows]




def option_attribution(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT attribution_id, trade_id, event_id, contract_id, snapshot_time,
               prior_snapshot_time, option_return, underlying_return, iv_change,
               theta_decay, spread_change, stock_move_effect, iv_effect,
               theta_effect, spread_effect, unexplained_effect, label, raw
        FROM option_attribution
        ORDER BY snapshot_time DESC, trade_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def missed_winner_event(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT missed_id, detected_at, ticker, contract_id, strategy_version,
               first_snapshot_time, winner_snapshot_time, entry_price_assumption,
               winner_price, max_return_seen, winner_threshold, filter_reason,
               proposed_strategy_family, raw
        FROM missed_winner_event
        ORDER BY detected_at DESC, max_return_seen DESC
        LIMIT 1000
        """,
    )
    out = []
    for row in rows:
        decoded = _compact_empty_fields(decode_fields(row, ("raw",)))
        raw = _loads(decoded.get("raw"))
        observed = _loads(raw.get("observed_window"))
        candidate = _loads(raw.get("candidate_context"))
        decoded["outcome_basis"] = raw.get("outcome_basis") or "trailing_stop_realized_exit"
        decoded["observed_peak_return"] = raw.get("observed_peak_return")
        decoded["winner_elapsed_hours"] = observed.get("winner_elapsed_hours")
        decoded["snapshot_count"] = observed.get("snapshot_count")
        decoded["candidate_event_count"] = candidate.get("event_count")
        decoded["candidate_best_state"] = candidate.get("best_state_before_winner")
        decoded["tradability_flag_count"] = len(raw.get("tradability_flags") or [])
        decoded["tradability_flags"] = raw.get("tradability_flags") or []
        out.append(_compact_empty_fields(decoded))
    return out




def strategy_mutation_proposal(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT proposal_id, created_at, source_type, strategy_version,
               proposed_strategy_version, proposed_parameter_changes, rationale,
               expected_effect, risk, status, requires_backtest,
               requires_forward_test, human_approval_status, approved_by,
               approved_at, evidence_refs, raw
        FROM strategy_mutation_proposal
        ORDER BY created_at DESC, proposed_strategy_version
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("proposed_parameter_changes", "evidence_refs", "raw"))) for row in rows]




def strategy_backtest_result(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT backtest_id, proposal_id, evaluated_at, strategy_version,
               proposed_strategy_version, lookback_start, lookback_end,
               baseline_candidate_count, proposed_candidate_count,
               baseline_hit_rate_2x, baseline_hit_rate_5x, baseline_hit_rate_10x,
               proposed_hit_rate_2x, proposed_hit_rate_5x, proposed_hit_rate_10x,
               proposed_false_positive_rate, verdict, metrics, raw
        FROM strategy_backtest_result
        ORDER BY evaluated_at DESC, proposed_strategy_version
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics", "raw"))) for row in rows]




def strategy_forward_test_result(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT forward_test_id, proposal_id, evaluated_at, strategy_version,
               proposed_strategy_version, forward_start, forward_end,
               days_observed, baseline_candidate_count, proposed_candidate_count,
               baseline_hit_rate_2x, baseline_hit_rate_5x, baseline_hit_rate_10x,
               proposed_hit_rate_2x, proposed_hit_rate_5x, proposed_hit_rate_10x,
               status, verdict, metrics, raw
        FROM strategy_forward_test_result
        ORDER BY evaluated_at DESC, proposed_strategy_version
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics", "raw"))) for row in rows]




def _exploration_bucket_summary(name: str, values: list[float]) -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"bucket": name, "n": 0, "hit_rate_2x": 0.0, "hit_rate_5x": 0.0, "median_realized_return": None}
    ordered = sorted(values)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "bucket": name,
        "n": n,
        "hit_rate_2x": round(sum(1 for v in values if v >= 1.0) / n, 4),
        "hit_rate_5x": round(sum(1 for v in values if v >= 4.0) / n, 4),
        "median_realized_return": round(median, 4),
    }


def exploration_gate_report(con: Any) -> list[dict[str, Any]]:
    """Are the gates actually adding value? Compares the realizable (trailing-stop) hit
    rates of FIRE shadow trades against the epsilon-exploration sample of SETUP near-misses
    the gates rejected. ``gate_edge_2x`` = FIRE 2x-rate minus exploration 2x-rate: a large
    positive edge means the gates are selecting winners; ~0 (or negative) means the gate
    that rejected those setups is not earning its keep and is a candidate to loosen. Two
    rows (``fire``, ``exploration``); ``gate_edge_2x`` is null until both arms have data."""

    rows = query_rows(
        con,
        """
        WITH latest_mark AS (
            SELECT trade_id, raw, max_return_since_alert,
                   row_number() OVER (PARTITION BY trade_id ORDER BY mark_time DESC) AS rn
            FROM shadow_trade_mark
        )
        SELECT st.trade_id, st.raw AS trade_raw, st.max_return_seen,
               lm.raw AS mark_raw, lm.max_return_since_alert
        FROM shadow_trade st
        LEFT JOIN latest_mark lm ON lm.trade_id = st.trade_id AND lm.rn = 1
        """,
    )
    buckets: dict[str, list[float]] = {"fire": [], "exploration": []}
    for row in rows:
        authority = _loads(row.get("trade_raw")).get("authority")
        bucket = "exploration" if authority == "shadow_exploration" else "fire"
        realized = _loads(row.get("mark_raw")).get("realized_exit_return")
        if realized is None:
            realized = row.get("max_return_since_alert")
        if realized is None:
            realized = row.get("max_return_seen")
        if realized is None:
            continue
        buckets[bucket].append(float(realized))
    summaries = {name: _exploration_bucket_summary(name, values) for name, values in buckets.items()}
    both_armed = summaries["fire"]["n"] > 0 and summaries["exploration"]["n"] > 0
    gate_edge = round(summaries["fire"]["hit_rate_2x"] - summaries["exploration"]["hit_rate_2x"], 4) if both_armed else None
    out: list[dict[str, Any]] = []
    for name in ("fire", "exploration"):
        out.append({**summaries[name], "gate_edge_2x": gate_edge})
    return out


def strategy_cohort_result(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT cohort_id, evaluated_at, strategy_version, cohort_type,
               cohort_value, candidate_count, hit_rate_2x, hit_rate_5x,
               hit_rate_10x, false_positive_rate, median_max_return,
               median_max_drawdown, average_time_to_2x, early_entry_rate,
               theta_iv_bleed_rate, good_convexity_rate, qqq_above_200d_rate,
               raw
        FROM strategy_cohort_result
        ORDER BY evaluated_at DESC, cohort_type, candidate_count DESC
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]
