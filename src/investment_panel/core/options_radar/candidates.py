"""Generate candidate events and grade them through the gate pipeline."""

from __future__ import annotations

from typing import Any

from investment_panel.analysis.option_ev import (ev_inverse_buy_under, ev_score)
from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_elapsed_days, _integer, _iso, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.dbutil import (_source_filter, _strategy_parameters, _symbol_filter)
from investment_panel.core.options_radar.gates import (CandidateContext, run_gates)
from investment_panel.core.options_radar.scoring import (_buy_under, _candidate_ev, _candidate_quality, _candidate_score, _ev_raw, _has_missing_data, _is_delayed_feed, _theme_watch_matches)
from investment_panel.core.options_radar.session import (snapshot_is_rth)

def generate_candidate_events(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str | None = None,
    snapshot_time: str | None = None,
) -> int:
    strategy = _strategy_parameters(con, strategy_version)
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    snapshot_filter = "AND s.snapshot_time = TRY_CAST(? AS TIMESTAMP)" if snapshot_time else ""
    rows = query_rows(
        con,
        f"""
        SELECT
            s.*,
            f.required_2x_price,
            f.required_5x_price,
            f.required_10x_price,
            f.required_move_10x_pct,
            f.breakeven,
            f.iv_percentile,
            f.iv_rank,
            f.liquidity_score,
            f.convexity_score,
            fl.flow_score,
            fl.oi_zscore_20d,
            fl.volume_oi_ratio,
            fl.oi_change_1d,
            vs.term_slope,
            vs.put_call_skew_25d,
            vs.skew_change_5d,
            vs.iv_rv_ratio,
            vs.iv_percentile_252d,
            sf.price,
            sf.ma_50,
            sf.rs_vs_qqq_20d,
            sf.rs_vs_qqq_60d,
            sf.atr_pct,
            sf.volume_ratio,
            sf.base_length_days,
            sf.breakout_level,
            sf.raw AS stock_features_raw,
            i.asset_class,
            i.name AS instrument_name,
            i.sector,
            i.industry,
            i.category,
            (
                SELECT peer.data_source
                FROM option_snapshot peer
                WHERE peer.ticker = s.ticker
                  AND peer.expiration = s.expiration
                  AND peer.strike = s.strike
                  AND peer.option_type = s.option_type
                  AND peer.data_source != s.data_source
                  AND peer.snapshot_time <= s.snapshot_time
                ORDER BY peer.snapshot_time DESC
                LIMIT 1
            ) AS peer_data_source,
            (
                SELECT peer.snapshot_time
                FROM option_snapshot peer
                WHERE peer.ticker = s.ticker
                  AND peer.expiration = s.expiration
                  AND peer.strike = s.strike
                  AND peer.option_type = s.option_type
                  AND peer.data_source != s.data_source
                  AND peer.snapshot_time <= s.snapshot_time
                ORDER BY peer.snapshot_time DESC
                LIMIT 1
            ) AS peer_snapshot_time,
            (
                SELECT peer.mid
                FROM option_snapshot peer
                WHERE peer.ticker = s.ticker
                  AND peer.expiration = s.expiration
                  AND peer.strike = s.strike
                  AND peer.option_type = s.option_type
                  AND peer.data_source != s.data_source
                  AND peer.snapshot_time <= s.snapshot_time
                  AND peer.mid IS NOT NULL
                ORDER BY peer.snapshot_time DESC
                LIMIT 1
            ) AS peer_mid,
            (
                SELECT peer.iv
                FROM option_snapshot peer
                WHERE peer.ticker = s.ticker
                  AND peer.expiration = s.expiration
                  AND peer.strike = s.strike
                  AND peer.option_type = s.option_type
                  AND peer.data_source != s.data_source
                  AND peer.snapshot_time <= s.snapshot_time
                  AND peer.iv IS NOT NULL
                ORDER BY peer.snapshot_time DESC
                LIMIT 1
            ) AS peer_iv,
            (
                SELECT peer.delta
                FROM option_snapshot peer
                WHERE peer.ticker = s.ticker
                  AND peer.expiration = s.expiration
                  AND peer.strike = s.strike
                  AND peer.option_type = s.option_type
                  AND peer.data_source != s.data_source
                  AND peer.snapshot_time <= s.snapshot_time
                  AND peer.delta IS NOT NULL
                ORDER BY peer.snapshot_time DESC
                LIMIT 1
            ) AS peer_delta,
            (
                SELECT min(TRY_CAST(e.event_date AS DATE))
                FROM earnings_events e
                WHERE e.symbol = s.ticker
                  AND TRY_CAST(e.event_date AS DATE) >= TRY_CAST(s.snapshot_time AS DATE)
            ) AS next_earnings_date,
            t.thesis_id
        FROM option_snapshot s
        JOIN option_features f ON f.contract_id = s.contract_id AND f.snapshot_time = s.snapshot_time
        LEFT JOIN option_flow_features fl ON fl.contract_id = s.contract_id AND fl.snapshot_time = s.snapshot_time
        LEFT JOIN vol_surface_features vs ON vs.ticker = s.ticker AND vs.snapshot_time = s.snapshot_time
        LEFT JOIN stock_features sf ON sf.ticker = s.ticker AND sf.snapshot_time = s.snapshot_time
        LEFT JOIN (
            SELECT ticker, thesis_id
            FROM agent_thesis
            QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY created_at DESC) = 1
        ) t ON t.ticker = s.ticker
        LEFT JOIN instruments i ON i.symbol = s.ticker
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]} {snapshot_filter}
        ORDER BY s.snapshot_time, s.ticker, s.expiration, s.strike, s.option_type
        """,
        [*source_filter["params"], *symbol_filter["params"], *([snapshot_time] if snapshot_time else [])],
    )
    count = 0
    for row in rows:
        event = build_candidate_event(row, strategy_version, strategy)
        if not event:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO candidate_event
            (event_id, snapshot_time, ticker, contract_id, strategy_version, state, premium_mid,
             premium_fill_assumption, required_10x_price, required_move_pct, buy_under,
             trigger_reason, thesis_id, score, quality_status, quality_flags, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event["event_id"],
                event["snapshot_time"],
                event["ticker"],
                event["contract_id"],
                event["strategy_version"],
                event["state"],
                event["premium_mid"],
                event["premium_fill_assumption"],
                event["required_10x_price"],
                event["required_move_pct"],
                event["buy_under"],
                event["trigger_reason"],
                event["thesis_id"],
                event["score"],
                event["quality_status"],
                json_dumps(event["quality_flags"]),
                json_dumps(event["raw"]),
            ],
        )
        count += 1
    return count


def build_candidate_event(row: dict[str, Any], strategy_version: str, strategy: dict[str, Any]) -> dict[str, Any] | None:
    premium = _number(row.get("mid"))
    underlying = _number(row.get("underlying_price"))
    strike = _number(row.get("strike"))
    required_move = _number(row.get("required_move_10x_pct"))
    if premium is None or premium <= 0 or underlying is None or underlying <= 0 or strike is None or required_move is None:
        return None
    option_type = str(row.get("option_type") or "").lower()
    dte = _integer(row.get("dte"))
    spread_pct = _number(row.get("spread_pct"))

    # EV (probability/theta-aware) drives both the asymmetry positive and the
    # buy-under cap. Computed here because it needs the wider pipeline; the gates
    # only see the resulting multiple and buy-under price.
    ev_pair = _candidate_ev(row, option_type=option_type, dte=dte)
    ev_inputs = ev_pair[0] if ev_pair else None
    ev_result = ev_pair[1] if ev_pair else None
    ev_asymmetry = ev_score(ev_result.ev_multiple, spread_pct) if ev_result else None
    ev_multiple = ev_result.ev_multiple if ev_result is not None else None

    # Catalyst calendar: days to the next earnings event. Informational for LEAPs; a
    # hard input for Phase 3's short-dated catalyst_call archetype (IV-crush modeling).
    days_to_earnings = _elapsed_days(row.get("snapshot_time"), row.get("next_earnings_date"))
    catalyst_in_window = days_to_earnings is not None and days_to_earnings >= 0 and dte is not None and days_to_earnings <= dte

    ev_buy_under = ev_inverse_buy_under(ev_inputs) if ev_inputs is not None else None
    deterministic_buy_under = _buy_under(row, strategy)
    if strategy_version == DEFAULT_STRATEGY_VERSION:
        buy_under_candidates = [value for value in (ev_buy_under, deterministic_buy_under) if value is not None]
        buy_under = max(buy_under_candidates) if buy_under_candidates else None
    else:
        buy_under = ev_buy_under if ev_buy_under is not None else deterministic_buy_under
    fill = premium * (1 + float(strategy["fill_slippage_pct"]))
    watch_themes = _theme_watch_matches(row)

    context = CandidateContext(
        row=row,
        strategy=strategy,
        option_type=option_type,
        required_move=required_move,
        premium=premium,
        dte=dte,
        delta_value=_number(row.get("delta")),
        spread_pct=spread_pct,
        open_interest=_number(row.get("open_interest")),
        volume=_number(row.get("volume")),
        off_hours=not snapshot_is_rth(row.get("snapshot_time")),
        delayed_feed=_is_delayed_feed(row),
        iv_percentile=_number(row.get("iv_percentile")),
        price=_number(row.get("price")),
        ma50=_number(row.get("ma_50")),
        rs20=_number(row.get("rs_vs_qqq_20d")),
        ev_multiple=ev_multiple,
        flow_zscore=_number(row.get("oi_zscore_20d")),
        volume_oi_ratio=_number(row.get("volume_oi_ratio")),
        oi_change_1d=_number(row.get("oi_change_1d")),
        term_slope=_number(row.get("term_slope")),
        put_call_skew_25d=_number(row.get("put_call_skew_25d")),
        iv_rv_ratio=_number(row.get("iv_rv_ratio")),
        catalyst_in_window=catalyst_in_window,
        buy_under=buy_under,
        watch_themes=watch_themes,
    )
    verdict = run_gates(context)
    hard_rejects, blockers, positives = verdict.hard_rejects, verdict.blockers, verdict.positives

    state = "REJECT" if hard_rejects else "WATCH" if _has_missing_data(blockers) else "SETUP" if blockers else "FIRE"
    quality = _candidate_quality(row, state=state, blockers=blockers, hard_rejects=hard_rejects)
    reasons = [*hard_rejects, *blockers, *positives]
    snapshot_time = _iso(row.get("snapshot_time"))
    contract_id = str(row.get("contract_id"))
    event_id = stable_id("candidate_event", strategy_version, snapshot_time, contract_id)
    return {
        "event_id": event_id,
        "snapshot_time": snapshot_time,
        "ticker": _normalize_symbol(row.get("ticker")),
        "contract_id": contract_id,
        "strategy_version": strategy_version,
        "state": state,
        "premium_mid": premium,
        "premium_fill_assumption": fill,
        "required_10x_price": _number(row.get("required_10x_price")),
        "required_move_pct": required_move,
        "buy_under": buy_under,
        "trigger_reason": ", ".join(reasons),
        "thesis_id": row.get("thesis_id"),
        "score": _candidate_score(row, state, watch_themes=watch_themes, ev_asymmetry=ev_asymmetry),
        "quality_status": quality["status"],
        "quality_flags": quality["flags"],
        "raw": {
            "hard_rejects": hard_rejects,
            "blockers": blockers,
            "positives": positives,
            "quality": quality,
            "strategy_parameters": strategy,
            "watch_themes": watch_themes,
            "expiration": str(row.get("expiration")),
            "strike": strike,
            "option_type": option_type,
            "strategy_family": strategy.get("strategy_family"),
            "days_to_earnings": days_to_earnings,
            "ev": _ev_raw(ev_result),
            "ev_buy_under": ev_buy_under,
            "deterministic_buy_under": deterministic_buy_under,
        },
    }
