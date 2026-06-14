"""Options chain, radar, and snapshot read accessors."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.options_radar import display_snapshot_time, market_session

from investment_panel.core.panel.coerce import _iso_or_none, decode_fields
from investment_panel.core.panel.disclosures import _compact_empty_fields



def options_expiries(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, expiry, dte, contracts_count, observed_at, source, raw
        FROM options_expiries
        ORDER BY observed_at DESC, symbol, expiry
        LIMIT 300
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def options_chain(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, expiry, strike, option_type, bid, ask, mid, iv, delta, gamma,
               theta, vega, rho, theo, bid_iv, ask_iv, contract_symbol, observed_at, source, raw
        FROM options_chain
        QUALIFY dense_rank() OVER (PARTITION BY symbol, expiry ORDER BY observed_at DESC) = 1
        ORDER BY observed_at DESC, symbol, expiry, strike, option_type
        LIMIT 400
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def options_provider_capabilities(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT provider, observed_at, supports_expiries, supports_chain_quotes,
               supports_greeks, supports_theoretical_price, supports_open_interest,
               supports_volume, supports_full_chain, status, detail, raw
        FROM options_provider_capabilities
        ORDER BY observed_at DESC, provider
        LIMIT 20
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def options_expiry_signals(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, expiry, as_of, source, dte, spot, contract_count, chain_rows,
               atm_strike, atm_iv, expected_move, expected_move_pct, put_call_iv_skew,
               call_spread_pct, put_spread_pct, spread_quality, liquidity_score,
               hedge_put_strike, hedge_put_mid, covered_call_strike, covered_call_mid,
               unavailable_signals, raw
        FROM options_expiry_signals
        ORDER BY as_of DESC, symbol, dte, expiry
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("unavailable_signals", "raw"))) for row in rows]




def options_ticker_signals(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, source, status, nearest_expiry, nearest_dte, atm_iv,
               iv_regime, expected_move, expected_move_pct, skew_signal,
               put_call_iv_skew, spread_quality, liquidity_score, hedge_summary,
               income_summary, unavailable_signals, raw
        FROM options_ticker_signals
        ORDER BY as_of DESC, symbol
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("unavailable_signals", "raw"))) for row in rows]




def option_strategy_versions(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT strategy_version, strategy_name, version, created_at, status,
               parameters, promoted_at, supersedes, notes
        FROM option_strategy_versions
        ORDER BY created_at DESC, strategy_version
        LIMIT 100
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("parameters",))) for row in rows]


@dataclass(frozen=True)
class RadarDisplayContext:
    snapshot_time: str | None
    candidate_time: str | None
    market_session: str
    frozen_to_last_rth: bool


def _radar_display_times(con: Any) -> tuple[str | None, str | None]:
    """Session-aware 'current' snapshot + candidate times for the radar views.

    During RTH this is the newest data; when the market is closed it freezes on
    the last regular-hours snapshot so the radar shows the last tradeable state
    instead of an off-hours volume=0 capture.
    """

    def iso(value: Any) -> str:
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    snaps = [iso(r["snapshot_time"]) for r in query_rows(con, "SELECT DISTINCT snapshot_time FROM option_snapshot WHERE snapshot_time IS NOT NULL")]
    # Key the candidate/opportunity view off the OPPORTUNITY table — that is the
    # grain the UI renders, and opportunities only exist for the snapshots that
    # were latest at refresh time. Freezing to a candidate-only snapshot would
    # show zero opportunities. display_snapshot_time falls back to the newest when
    # no regular-hours snapshot exists yet (e.g. the market has been closed since
    # the last refresh).
    opps = [iso(r["snapshot_time"]) for r in query_rows(con, "SELECT DISTINCT snapshot_time FROM option_radar_opportunity WHERE snapshot_time IS NOT NULL")]
    return display_snapshot_time(snaps), display_snapshot_time(opps)


def radar_display_context(con: Any) -> RadarDisplayContext:
    display_snap, display_candidate = _radar_display_times(con)
    session = market_session()
    newest = query_rows(con, "SELECT max(snapshot_time) AS t FROM candidate_event")
    newest_time = newest[0]["t"] if newest else None
    frozen = bool(session == "closed" and display_candidate and newest_time and str(display_candidate) != _iso_or_none(newest_time))
    return RadarDisplayContext(
        snapshot_time=display_snap,
        candidate_time=display_candidate,
        market_session=session,
        frozen_to_last_rth=frozen,
    )




def _radar_current_candidate_time(con: Any, radar_context: RadarDisplayContext | None = None) -> str | None:
    """Snapshot the radar UI treats as 'current' for candidate-keyed reads.

    Aligned with the displayed opportunity snapshot, which can freeze on the last
    healthy snapshot when the newest pull is degraded (e.g. a pre-market
    zero-premium capture). Keying candidate detail / theses / marks / attributions
    off a raw max(candidate_event.snapshot_time) would point them at that degraded
    newer pull and mismatch the opportunities and summary, emptying the signals
    tab even though the opportunities are populated.
    """

    return (radar_context or radar_display_context(con)).candidate_time




def option_radar_summary(con: Any, radar_context: RadarDisplayContext | None = None) -> list[dict[str, Any]]:
    context = radar_context or radar_display_context(con)
    rows = query_rows(
        con,
        """
        WITH latest_snapshot AS (
            SELECT TRY_CAST(? AS TIMESTAMP) AS snapshot_time
        ),
        latest_candidates AS (
            SELECT TRY_CAST(? AS TIMESTAMP) AS snapshot_time
        )
        SELECT
            (SELECT snapshot_time FROM latest_snapshot) AS latest_snapshot_time,
            (SELECT snapshot_time FROM latest_candidates) AS latest_candidate_time,
            (SELECT count(DISTINCT ticker) FROM option_snapshot WHERE snapshot_time = (SELECT snapshot_time FROM latest_snapshot)) AS scanned_tickers_current,
            (SELECT count(*) FROM option_snapshot WHERE snapshot_time = (SELECT snapshot_time FROM latest_snapshot)) AS snapshot_rows_current,
            (SELECT count(DISTINCT ticker) FROM option_snapshot) AS scanned_tickers_total,
            (SELECT count(*) FROM option_snapshot) AS snapshot_rows_total,
            (SELECT string_agg(DISTINCT data_source, ', ' ORDER BY data_source) FROM option_snapshot) AS data_sources,
            (
                SELECT count(DISTINCT ticker)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state != 'REJECT'
            ) AS opportunity_tickers_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state != 'REJECT'
            ) AS opportunity_rows_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state = 'FIRE'
            ) AS fire_rows_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state = 'SETUP'
            ) AS setup_rows_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state = 'WATCH'
            ) AS watch_rows_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state = 'REJECT'
            ) AS reject_rows_current,
            (
                SELECT count(*)
                FROM option_radar_opportunity
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND tier = 'Exceptional'
            ) AS exceptional_opportunities_current,
            (
                SELECT count(*)
                FROM option_radar_opportunity
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND tier = 'Service Bug'
            ) AS repair_opportunities_current,
            (
                SELECT count(*)
                FROM option_radar_opportunity
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND tier = 'Research'
            ) AS research_opportunities_current
        """,
        [context.snapshot_time, context.candidate_time],
    )
    for row in rows:
        row["market_session"] = context.market_session
        row["frozen_to_last_rth"] = context.frozen_to_last_rth
    return [_compact_empty_fields(row) for row in rows]




def option_radar_opportunity(con: Any, radar_context: RadarDisplayContext | None = None) -> list[dict[str, Any]]:
    context = radar_context or radar_display_context(con)
    rows = query_rows(
        con,
        """
        WITH latest_candidates AS (
            SELECT TRY_CAST(? AS TIMESTAMP) AS snapshot_time
        )
        SELECT opportunity_id, snapshot_time, ticker, strategy_version, tier,
               primary_event_id, primary_contract_id, primary_state,
               conviction_score, asymmetry_score, entry_quality_score,
               catalyst_score, evidence_score, regime_score, survivability_score,
               learning_score, required_move_pct, premium_mid,
               premium_fill_assumption, required_10x_price, buy_under,
               entry_zone, max_loss_assumption, position_sizing_band,
               data_contract_status, data_contract_failures, data_contract_satisfied,
               service_repair_jobs, service_repair_summary,
               why_now, kill_switch, top_reasons, blockers, quality_status,
               quality_flags, evidence_refs, alternative_contracts, raw
        FROM option_radar_opportunity
        WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
        ORDER BY CASE tier WHEN 'Exceptional' THEN 0 WHEN 'Research' THEN 1 WHEN 'Watch' THEN 2 WHEN 'Service Bug' THEN 3 ELSE 4 END,
                 conviction_score DESC NULLS LAST,
                 required_move_pct ASC NULLS LAST,
                 ticker
        LIMIT 500
        """,
        [context.candidate_time],
    )
    return [
        _compact_empty_fields(
            decode_fields(
                row,
                (
                    "data_contract_failures",
                    "data_contract_satisfied",
                    "service_repair_jobs",
                    "top_reasons",
                    "blockers",
                    "quality_flags",
                    "evidence_refs",
                    "alternative_contracts",
                    "raw",
                ),
            )
        )
        for row in rows
    ]




def option_snapshot(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT snapshot_time, ticker, underlying_price, expiration, strike, option_type,
               bid, ask, mid, last, volume, open_interest, iv, delta, gamma, theta,
               vega, dte, spread_pct, data_source, contract_id, raw
        FROM option_snapshot
        ORDER BY snapshot_time DESC, ticker, expiration, strike, option_type
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def option_features(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT snapshot_time, contract_id, ticker, required_2x_price,
               required_5x_price, required_10x_price, required_move_10x_pct,
               breakeven, iv_percentile, iv_rank, liquidity_score,
               convexity_score, raw
        FROM option_features
        ORDER BY snapshot_time DESC, ticker, contract_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def stock_features(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT snapshot_time, ticker, price, ma_20, ma_50, ma_200,
               rs_vs_qqq_20d, rs_vs_qqq_60d, atr_pct, volume_ratio,
               distance_from_52w_high, base_length_days, breakout_level, raw
        FROM stock_features
        ORDER BY snapshot_time DESC, ticker
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]




def options_payoff_scenarios(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, symbol, as_of, expiry, strategy_type, spot, dte, iv,
               net_premium, max_profit, max_loss, breakevens, legs, curve,
               diagnostics, source
        FROM options_payoff_scenarios
        ORDER BY as_of DESC, symbol, expiry, strategy_type
        LIMIT 300
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("breakevens", "legs", "curve", "diagnostics"))) for row in rows]
