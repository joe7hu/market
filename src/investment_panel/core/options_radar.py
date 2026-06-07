"""Deterministic 10x options radar data flywheel."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime
from statistics import mean
from typing import Any

from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.source_ingestion.utils import stable_id


DEFAULT_STRATEGY_VERSION = "leap_10x_reversal_v1"
MIN_FORWARD_TEST_DAYS = 30
DEFAULT_OPTION_RISK_FREE_RATE = 0.045
MIN_OPTION_MODEL_DTE_DAYS = 1
MIN_OPTION_MODEL_IV = 0.0001
OPTION_QUALITY_MID_CAUTION_RELATIVE_DIFF = 0.10
OPTION_QUALITY_MID_BAD_RELATIVE_DIFF = 0.20
OPTION_QUALITY_IV_CAUTION_RELATIVE_DIFF = 0.15
OPTION_QUALITY_IV_BAD_RELATIVE_DIFF = 0.30
OPTION_QUALITY_DELTA_CAUTION_ABSOLUTE_DIFF = 0.07
OPTION_QUALITY_DELTA_BAD_ABSOLUTE_DIFF = 0.15

DEFAULT_STRATEGY_PARAMETERS: dict[str, Any] = {
    "strategy_name": "leap_10x_reversal",
    "version": 1,
    "option_type": "call",
    "delta_min": 0.20,
    "delta_max": 0.45,
    "dte_min": 365,
    "dte_max": 900,
    "max_spread_pct": 0.25,
    "reject_spread_pct": 0.40,
    "min_open_interest": 100,
    "min_volume": 1,
    "max_required_move_pct": 3.50,
    "max_iv_percentile": 70.0,
    "reject_iv_percentile": 85.0,
    "require_price_above_ma50": True,
    "require_rs_improving": True,
    "fill_slippage_pct": 0.03,
}


def refresh_options_radar(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str | None = None,
    snapshot_time: str | None = None,
) -> dict[str, int]:
    """Refresh the deterministic radar tables from already-ingested market data."""

    register_default_strategy(con, strategy_version)
    snapshot_rows = persist_option_snapshots(con, symbols=symbols, source=source, snapshot_time=snapshot_time)
    feature_rows = refresh_option_features(con, symbols=symbols, source=source)
    stock_rows = refresh_stock_features_for_option_snapshots(con, symbols=symbols, source=source)
    candidate_rows = generate_candidate_events(con, symbols=symbols, strategy_version=strategy_version, source=source)
    from investment_panel.core.option_agent_thesis import refresh_option_agent_work

    agent_work = refresh_option_agent_work(con, strategy_version=strategy_version)
    shadow_rows = create_shadow_trades(con, strategy_version=strategy_version)
    marked_rows = mark_shadow_trades(con)
    mark_rows = refresh_shadow_trade_marks(con, strategy_version=strategy_version)
    candidate_mark_rows = refresh_candidate_event_marks(con, strategy_version=strategy_version)
    candidate_attribution_rows = refresh_candidate_event_attributions(con, strategy_version=strategy_version)
    transition_rows = refresh_radar_state_transitions(con, strategy_version=strategy_version)
    exited_rows = apply_shadow_trade_exits(con, strategy_version=strategy_version)
    attribution_rows = refresh_option_attributions(con, strategy_version=strategy_version)
    missed_rows = detect_missed_winners(con, symbols=symbols, strategy_version=strategy_version, source=source)
    proposal_rows = generate_strategy_mutation_proposals(con, strategy_version=strategy_version)
    from investment_panel.core.option_agent_postmortem import refresh_option_agent_postmortem_work

    postmortem_work = refresh_option_agent_postmortem_work(con, strategy_version=strategy_version)
    evaluation_rows = refresh_strategy_proposal_evaluations(con, strategy_version=strategy_version)
    cohort_rows = refresh_strategy_cohort_results(con, strategy_version=strategy_version)
    return {
        "option_snapshots": snapshot_rows,
        "option_features": feature_rows,
        "stock_features": stock_rows,
        "candidate_events": candidate_rows,
        **agent_work,
        "shadow_trades": shadow_rows,
        "shadow_trades_marked": marked_rows,
        "shadow_trade_marks": mark_rows,
        "candidate_event_marks": candidate_mark_rows,
        "candidate_event_attributions": candidate_attribution_rows,
        "radar_state_transitions": transition_rows,
        "shadow_trades_exited": exited_rows,
        "option_attributions": attribution_rows,
        "missed_winners": missed_rows,
        "strategy_mutation_proposals": proposal_rows,
        **postmortem_work,
        **evaluation_rows,
        "strategy_cohorts": cohort_rows,
    }


def register_default_strategy(con: Any, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> None:
    now = datetime.utcnow().isoformat()
    con.execute(
        """
        INSERT OR IGNORE INTO option_strategy_versions
        (strategy_version, strategy_name, version, created_at, status, parameters, promoted_at, supersedes, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            strategy_version,
            DEFAULT_STRATEGY_PARAMETERS["strategy_name"],
            DEFAULT_STRATEGY_PARAMETERS["version"],
            now,
            "shadow",
            json_dumps(DEFAULT_STRATEGY_PARAMETERS),
            None,
            None,
            "Deterministic 10x LEAP reversal baseline. Agents may propose changes, but code/backtests promote versions.",
        ],
    )


def persist_option_snapshots(
    con: Any,
    symbols: list[str] | None = None,
    *,
    source: str | None = None,
    snapshot_time: str | None = None,
) -> int:
    """Copy raw chain rows into the event-sourced radar snapshot table."""

    symbol_filter = _symbol_filter(symbols, table_alias="oc")
    source_filter = _source_filter(source, table_alias="oc")
    observed_filter = "AND oc.observed_at = TRY_CAST(? AS TIMESTAMP)" if snapshot_time else ""
    params: list[Any] = [*source_filter["params"], *symbol_filter["params"]]
    if snapshot_time:
        params.append(snapshot_time)
    rows = query_rows(
        con,
        f"""
        SELECT
            oc.symbol,
            oc.expiry,
            oc.strike,
            oc.option_type,
            oc.bid,
            oc.ask,
            oc.mid,
            oc.iv,
            oc.delta,
            oc.gamma,
            oc.theta,
            oc.vega,
            oc.contract_symbol,
            oc.observed_at,
            oc.source,
            oc.raw,
            (
                SELECT tv.delta
                FROM options_chain tv
                WHERE tv.symbol = oc.symbol
                  AND tv.expiry = oc.expiry
                  AND tv.strike = oc.strike
                  AND tv.option_type = oc.option_type
                  AND tv.source = 'tradingview'
                  AND tv.observed_at <= oc.observed_at
                  AND tv.delta IS NOT NULL
                ORDER BY tv.observed_at DESC
                LIMIT 1
            ) AS tradingview_delta,
            (
                SELECT tv.gamma
                FROM options_chain tv
                WHERE tv.symbol = oc.symbol
                  AND tv.expiry = oc.expiry
                  AND tv.strike = oc.strike
                  AND tv.option_type = oc.option_type
                  AND tv.source = 'tradingview'
                  AND tv.observed_at <= oc.observed_at
                  AND tv.gamma IS NOT NULL
                ORDER BY tv.observed_at DESC
                LIMIT 1
            ) AS tradingview_gamma,
            (
                SELECT tv.theta
                FROM options_chain tv
                WHERE tv.symbol = oc.symbol
                  AND tv.expiry = oc.expiry
                  AND tv.strike = oc.strike
                  AND tv.option_type = oc.option_type
                  AND tv.source = 'tradingview'
                  AND tv.observed_at <= oc.observed_at
                  AND tv.theta IS NOT NULL
                ORDER BY tv.observed_at DESC
                LIMIT 1
            ) AS tradingview_theta,
            (
                SELECT tv.vega
                FROM options_chain tv
                WHERE tv.symbol = oc.symbol
                  AND tv.expiry = oc.expiry
                  AND tv.strike = oc.strike
                  AND tv.option_type = oc.option_type
                  AND tv.source = 'tradingview'
                  AND tv.observed_at <= oc.observed_at
                  AND tv.vega IS NOT NULL
                ORDER BY tv.observed_at DESC
                LIMIT 1
            ) AS tradingview_vega,
            (
                SELECT q.price
                FROM quotes_intraday q
                WHERE q.symbol = oc.symbol AND q.observed_at <= oc.observed_at
                ORDER BY q.observed_at DESC
                LIMIT 1
            ) AS underlying_price
        FROM options_chain oc
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]} {observed_filter}
        ORDER BY oc.observed_at, oc.symbol, oc.expiry, oc.strike, oc.option_type
        """,
        params,
    )
    count = 0
    for row in rows:
        raw = _json(row.get("raw"))
        ticker = _normalize_symbol(row.get("symbol"))
        snapshot_at = _iso(row.get("observed_at"))
        expiration = row.get("expiry")
        strike = _number(row.get("strike"))
        option_type = str(row.get("option_type") or raw.get("type") or "").lower()
        mid = _premium_mid(row, raw)
        bid = _number(row.get("bid"))
        ask = _number(row.get("ask"))
        contract_id = _contract_id(ticker, expiration, strike, option_type, row.get("contract_symbol") or raw.get("symbol"))
        data_source = str(row.get("source") or source or "unknown")
        underlying_price = _number(row.get("underlying_price"))
        iv = _number(row.get("iv"))
        dte = _integer(raw.get("dte")) or _days_to_expiration(expiration, snapshot_at)
        greek_resolution = _resolve_option_greeks(row, option_type=option_type, underlying_price=underlying_price, strike=strike, dte=dte, iv=iv)
        if greek_resolution["source"] != "provider":
            raw["greeks_source"] = greek_resolution["source"]
            if greek_resolution["source"] in {"black_scholes_model", "mixed_fallback"}:
                raw["greeks_model"] = {
                    "method": "black_scholes_from_iv",
                    "risk_free_rate": DEFAULT_OPTION_RISK_FREE_RATE,
                    "iv": iv,
                    "dte": dte,
                    "effective_iv": _option_model_iv(iv),
                    "effective_dte": _option_model_dte(dte),
                }
        con.execute(
            """
            INSERT OR REPLACE INTO option_snapshot
            (snapshot_time, ticker, underlying_price, expiration, strike, option_type, bid, ask, mid,
             last, volume, open_interest, iv, delta, gamma, theta, vega, dte, spread_pct,
             data_source, contract_id, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_at,
                ticker,
                underlying_price,
                expiration,
                strike,
                option_type,
                bid,
                ask,
                mid,
                _coalesce_number(raw, "last", "last_price", "close"),
                _coalesce_number(raw, "volume", "vol"),
                _coalesce_number(raw, "open_interest", "openInterest", "oi"),
                iv,
                greek_resolution["delta"],
                greek_resolution["gamma"],
                greek_resolution["theta"],
                greek_resolution["vega"],
                dte,
                _spread_pct(bid, ask, mid),
                data_source,
                contract_id,
                json_dumps(raw),
            ],
        )
        count += 1
    return count


def refresh_option_features(con: Any, symbols: list[str] | None = None, *, source: str | None = None) -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM option_snapshot s
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        ORDER BY s.snapshot_time, s.ticker, s.expiration, s.strike, s.option_type
        """,
        [*source_filter["params"], *symbol_filter["params"]],
    )
    iv_history = _iv_history_by_ticker(rows)
    count = 0
    for row in rows:
        feature = build_option_feature(row, iv_history.get(str(row.get("ticker") or "").upper(), []))
        if not feature:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO option_features
            (snapshot_time, contract_id, ticker, required_2x_price, required_5x_price,
             required_10x_price, required_move_10x_pct, breakeven, iv_percentile,
             iv_rank, liquidity_score, convexity_score, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                feature["snapshot_time"],
                feature["contract_id"],
                feature["ticker"],
                feature["required_2x_price"],
                feature["required_5x_price"],
                feature["required_10x_price"],
                feature["required_move_10x_pct"],
                feature["breakeven"],
                feature["iv_percentile"],
                feature["iv_rank"],
                feature["liquidity_score"],
                feature["convexity_score"],
                json_dumps(feature["raw"]),
            ],
        )
        count += 1
    return count


def build_option_feature(snapshot: dict[str, Any], iv_history: list[float]) -> dict[str, Any] | None:
    premium = _number(snapshot.get("mid")) or _number(snapshot.get("last"))
    strike = _number(snapshot.get("strike"))
    underlying = _number(snapshot.get("underlying_price"))
    option_type = str(snapshot.get("option_type") or "").lower()
    if premium is None or premium <= 0 or strike is None or option_type not in {"call", "put"}:
        return None
    direction = 1 if option_type == "call" else -1
    required_2x = max(0.0, strike + direction * premium * 2)
    required_5x = max(0.0, strike + direction * premium * 5)
    required_10x = max(0.0, strike + direction * premium * 10)
    breakeven = max(0.0, strike + direction * premium)
    required_move = _required_move_pct(option_type, underlying, required_10x)
    liquidity_score = _liquidity_score(
        _number(snapshot.get("spread_pct")),
        _number(snapshot.get("open_interest")),
        _number(snapshot.get("volume")),
    )
    convexity_score = _convexity_score(required_move, _number(snapshot.get("delta")), _integer(snapshot.get("dte")))
    iv = _number(snapshot.get("iv"))
    return {
        "snapshot_time": _iso(snapshot.get("snapshot_time")),
        "contract_id": str(snapshot.get("contract_id")),
        "ticker": _normalize_symbol(snapshot.get("ticker")),
        "required_2x_price": required_2x,
        "required_5x_price": required_5x,
        "required_10x_price": required_10x,
        "required_move_10x_pct": required_move,
        "breakeven": breakeven,
        "iv_percentile": _percentile_rank(iv, iv_history),
        "iv_rank": _iv_rank(iv, iv_history),
        "liquidity_score": liquidity_score,
        "convexity_score": convexity_score,
        "raw": {
            "premium_mid": premium,
            "option_type": option_type,
            "spread_pct": _number(snapshot.get("spread_pct")),
            "open_interest": _number(snapshot.get("open_interest")),
            "volume": _number(snapshot.get("volume")),
        },
    }


def refresh_stock_features_for_option_snapshots(con: Any, symbols: list[str] | None = None, *, source: str | None = None) -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    rows = query_rows(
        con,
        f"""
        SELECT DISTINCT s.ticker, s.snapshot_time
        FROM option_snapshot s
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        ORDER BY s.snapshot_time, s.ticker
        """,
        [*source_filter["params"], *symbol_filter["params"]],
    )
    count = 0
    for row in rows:
        if compute_stock_feature(con, str(row["ticker"]), _iso(row["snapshot_time"])):
            count += 1
    return count


def compute_stock_feature(con: Any, ticker: str, snapshot_time: str) -> dict[str, Any] | None:
    ticker = _normalize_symbol(ticker)
    prices = query_rows(
        con,
        """
        SELECT date, open, high, low, close, volume
        FROM prices_daily
        WHERE symbol = ? AND date <= TRY_CAST(? AS DATE)
        ORDER BY date
        """,
        [ticker, snapshot_time],
    )
    if not prices:
        return None
    qqq_prices = query_rows(
        con,
        """
        SELECT date, close
        FROM prices_daily
        WHERE symbol = 'QQQ' AND date <= TRY_CAST(? AS DATE)
        ORDER BY date
        """,
        [snapshot_time],
    )
    closes = [_number(row.get("close")) for row in prices]
    highs = [_number(row.get("high")) for row in prices]
    lows = [_number(row.get("low")) for row in prices]
    volumes = [_number(row.get("volume")) for row in prices]
    close_values = [value for value in closes if value is not None]
    if not close_values:
        return None
    price = close_values[-1]
    high_values = [value for value in highs if value is not None]
    high_252 = max(high_values[-252:]) if high_values else price
    feature = {
        "snapshot_time": snapshot_time,
        "ticker": ticker,
        "price": price,
        "ma_20": _average(close_values[-20:]) if len(close_values) >= 20 else None,
        "ma_50": _average(close_values[-50:]) if len(close_values) >= 50 else None,
        "ma_200": _average(close_values[-200:]) if len(close_values) >= 200 else None,
        "rs_vs_qqq_20d": _relative_strength(close_values, [_number(row.get("close")) for row in qqq_prices], 20),
        "rs_vs_qqq_60d": _relative_strength(close_values, [_number(row.get("close")) for row in qqq_prices], 60),
        "atr_pct": _atr_pct(prices),
        "volume_ratio": _volume_ratio([value for value in volumes if value is not None]),
        "distance_from_52w_high": (price / high_252 - 1) if high_252 else None,
        "base_length_days": _base_length_days(close_values, high_252),
        "breakout_level": max(high_values[-60:-1]) if len(high_values) > 1 else high_252,
        "raw": {
            "price_rows": len(prices),
            "qqq_rows": len(qqq_prices),
            "source": "prices_daily",
        },
    }
    con.execute(
        """
        INSERT OR REPLACE INTO stock_features
        (snapshot_time, ticker, price, ma_20, ma_50, ma_200, rs_vs_qqq_20d,
         rs_vs_qqq_60d, atr_pct, volume_ratio, distance_from_52w_high,
         base_length_days, breakout_level, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            feature["snapshot_time"],
            feature["ticker"],
            feature["price"],
            feature["ma_20"],
            feature["ma_50"],
            feature["ma_200"],
            feature["rs_vs_qqq_20d"],
            feature["rs_vs_qqq_60d"],
            feature["atr_pct"],
            feature["volume_ratio"],
            feature["distance_from_52w_high"],
            feature["base_length_days"],
            feature["breakout_level"],
            json_dumps(feature["raw"]),
        ],
    )
    return feature


def generate_candidate_events(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str | None = None,
) -> int:
    strategy = _strategy_parameters(con, strategy_version)
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
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
            sf.price,
            sf.ma_50,
            sf.rs_vs_qqq_20d,
            sf.base_length_days,
            sf.breakout_level,
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
            t.thesis_id
        FROM option_snapshot s
        JOIN option_features f ON f.contract_id = s.contract_id AND f.snapshot_time = s.snapshot_time
        LEFT JOIN stock_features sf ON sf.ticker = s.ticker AND sf.snapshot_time = s.snapshot_time
        LEFT JOIN (
            SELECT ticker, thesis_id
            FROM agent_thesis
            QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY created_at DESC) = 1
        ) t ON t.ticker = s.ticker
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        ORDER BY s.snapshot_time, s.ticker, s.expiration, s.strike, s.option_type
        """,
        [*source_filter["params"], *symbol_filter["params"]],
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
    hard_rejects: list[str] = []
    blockers: list[str] = []
    positives: list[str] = []

    if option_type != strategy["option_type"]:
        hard_rejects.append("strategy_only_tracks_calls")
    dte = _integer(row.get("dte"))
    if dte is None:
        blockers.append("missing_dte")
    elif dte < int(strategy["dte_min"]) or dte > int(strategy["dte_max"]):
        hard_rejects.append("dte_outside_strategy_range")
    delta_value = _number(row.get("delta"))
    if delta_value is not None:
        delta = abs(delta_value)
        if delta < float(strategy["delta_min"]) or delta > float(strategy["delta_max"]):
            hard_rejects.append("delta_outside_strategy_range")
        else:
            positives.append("delta_in_range")
    else:
        blockers.append("missing_delta")
    if required_move > float(strategy["max_required_move_pct"]):
        hard_rejects.append("required_move_too_high")
    else:
        positives.append("10x_math_inside_cap")
    spread_pct = _number(row.get("spread_pct"))
    if spread_pct is None:
        blockers.append("missing_spread")
    elif spread_pct > float(strategy["reject_spread_pct"]):
        hard_rejects.append("spread_reject")
    elif spread_pct > float(strategy["max_spread_pct"]):
        blockers.append("spread_above_fire_threshold")
    else:
        positives.append("spread_usable")
    open_interest = _number(row.get("open_interest"))
    if open_interest is None:
        blockers.append("missing_open_interest")
    elif open_interest < float(strategy["min_open_interest"]):
        blockers.append("open_interest_below_threshold")
    else:
        positives.append("open_interest_supported")
    volume = _number(row.get("volume"))
    if volume is None:
        blockers.append("missing_volume")
    elif volume < float(strategy["min_volume"]):
        blockers.append("volume_below_threshold")
    else:
        positives.append("volume_seen")
    iv_percentile = _number(row.get("iv_percentile"))
    if iv_percentile is None:
        blockers.append("missing_iv_percentile")
    elif iv_percentile > float(strategy["reject_iv_percentile"]):
        hard_rejects.append("iv_percentile_reject")
    elif iv_percentile > float(strategy["max_iv_percentile"]):
        blockers.append("iv_percentile_above_fire_threshold")
    else:
        positives.append("iv_not_overpriced")
    price = _number(row.get("price"))
    ma50 = _number(row.get("ma_50"))
    if strategy.get("require_price_above_ma50"):
        if price is None or ma50 is None:
            blockers.append("missing_50d_context")
        elif price < ma50:
            blockers.append("stock_below_50d")
        else:
            positives.append("stock_above_50d")
    rs20 = _number(row.get("rs_vs_qqq_20d"))
    if strategy.get("require_rs_improving"):
        if rs20 is None:
            blockers.append("missing_rs_vs_qqq")
        elif rs20 < 0:
            blockers.append("rs_vs_qqq_20d_negative")
        else:
            positives.append("rs_vs_qqq_improving")

    buy_under = _buy_under(row, strategy)
    fill = premium * (1 + float(strategy["fill_slippage_pct"]))
    if buy_under is None:
        blockers.append("buy_under_unavailable")
    elif premium > buy_under:
        blockers.append("premium_above_buy_under")
    else:
        positives.append("premium_inside_buy_under")

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
        "score": _candidate_score(row, state),
        "quality_status": quality["status"],
        "quality_flags": quality["flags"],
        "raw": {
            "hard_rejects": hard_rejects,
            "blockers": blockers,
            "positives": positives,
            "quality": quality,
            "strategy_parameters": strategy,
            "expiration": str(row.get("expiration")),
            "strike": strike,
            "option_type": option_type,
        },
    }


def create_shadow_trades(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        WITH candidate_validation AS (
            SELECT *
            FROM agent_thesis_validation
            WHERE strategy_version = ? AND candidate_event_id IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY candidate_event_id ORDER BY validated_at DESC) = 1
        ),
        legacy_ticker_validation AS (
            SELECT *
            FROM agent_thesis_validation
            WHERE strategy_version = ? AND candidate_event_id IS NULL
            QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY validated_at DESC) = 1
        )
        SELECT
            ce.event_id,
            ce.snapshot_time,
            ce.premium_fill_assumption,
            ce.ticker,
            COALESCE(cv.state, lv.state) AS thesis_validation_state,
            COALESCE(cv.invalidation_status, lv.invalidation_status) AS thesis_invalidation_status,
            COALESCE(cv.red_team_status, lv.red_team_status) AS thesis_red_team_status
        FROM candidate_event ce
        LEFT JOIN candidate_validation cv
          ON cv.candidate_event_id = ce.event_id
         AND cv.strategy_version = ce.strategy_version
        LEFT JOIN legacy_ticker_validation lv
          ON lv.ticker = ce.ticker
         AND lv.strategy_version = ce.strategy_version
        WHERE ce.strategy_version = ? AND ce.state = 'FIRE'
        ORDER BY ce.snapshot_time
        """,
        [strategy_version, strategy_version, strategy_version],
    )
    count = 0
    for row in rows:
        if _thesis_validation_blocks_entry(row):
            continue
        trade_id = stable_id("shadow_trade", row["event_id"])
        before = query_rows(con, "SELECT count(*) AS count FROM shadow_trade WHERE trade_id = ?", [trade_id])[0]["count"]
        con.execute(
            """
            INSERT OR IGNORE INTO shadow_trade
            (trade_id, event_id, entry_time, entry_price_assumption, exit_time, exit_price,
             status, max_return_seen, max_drawdown_seen, time_to_2x, time_to_5x, time_to_10x,
             exit_reason, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trade_id,
                row["event_id"],
                row["snapshot_time"],
                _number(row["premium_fill_assumption"]),
                None,
                None,
                "open",
                0.0,
                0.0,
                None,
                None,
                None,
                None,
                json_dumps({"authority": "shadow_only", "created_from": "candidate_event"}),
            ],
        )
        after = query_rows(con, "SELECT count(*) AS count FROM shadow_trade WHERE trade_id = ?", [trade_id])[0]["count"]
        count += int(after) - int(before)
    return count


def apply_shadow_trade_exits(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        WITH latest_exit AS (
            SELECT
                transition_id,
                snapshot_time,
                state,
                trigger_reason,
                trade_id,
                mark_id,
                row_number() OVER (
                    PARTITION BY trade_id
                    ORDER BY snapshot_time DESC, evaluated_at DESC
                ) AS rn
            FROM radar_state_transition
            WHERE strategy_version = ?
                  AND trade_id IS NOT NULL
                  AND state IN ('EXIT', 'INVALIDATED')
        )
        SELECT
            st.trade_id,
            st.entry_price_assumption,
            st.raw,
            latest_exit.transition_id,
            latest_exit.snapshot_time,
            latest_exit.state,
            latest_exit.trigger_reason,
            stm.mark_price
        FROM latest_exit
        JOIN shadow_trade st ON st.trade_id = latest_exit.trade_id
        LEFT JOIN shadow_trade_mark stm ON stm.mark_id = latest_exit.mark_id
        WHERE latest_exit.rn = 1
              AND COALESCE(st.status, 'open') = 'open'
        """,
        [strategy_version],
    )
    count = 0
    for row in rows:
        exit_time = _iso(row.get("snapshot_time"))
        exit_price = _number(row.get("mark_price"))
        if exit_price is None:
            exit_price = _number(row.get("entry_price_assumption"))
        raw = {
            **_json(row.get("raw")),
            "exit_state": row.get("state"),
            "exit_transition_id": row.get("transition_id"),
            "exit_authority": "deterministic_radar_state",
        }
        con.execute(
            """
            UPDATE shadow_trade
            SET exit_time = ?,
                exit_price = ?,
                status = 'closed',
                exit_reason = ?,
                raw = ?
            WHERE trade_id = ?
            """,
            [exit_time, exit_price, row.get("trigger_reason"), json_dumps(raw), row.get("trade_id")],
        )
        count += 1
    return count


def mark_shadow_trades(con: Any) -> int:
    trades = query_rows(
        con,
        """
        SELECT
            st.*,
            ce.contract_id
        FROM candidate_event ce
        JOIN shadow_trade st ON st.event_id = ce.event_id
        WHERE st.status = 'open'
        """,
    )
    count = 0
    for trade in trades:
        latest = query_rows(
            con,
            """
            SELECT snapshot_time, mid
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], trade["entry_time"]],
        )
        if not latest:
            continue
        current_mid = _number(latest[0].get("mid"))
        entry_price = _number(trade.get("entry_price_assumption"))
        if current_mid is None or entry_price is None or entry_price <= 0:
            continue
        current_return = current_mid / entry_price - 1
        max_return = max(_number(trade.get("max_return_seen")) or 0.0, current_return)
        max_drawdown = min(_number(trade.get("max_drawdown_seen")) or 0.0, current_return)
        time_to_2x = trade.get("time_to_2x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 1.0 else None)
        time_to_5x = trade.get("time_to_5x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 4.0 else None)
        time_to_10x = trade.get("time_to_10x") or (_elapsed_days(trade.get("entry_time"), latest[0].get("snapshot_time")) if current_return >= 9.0 else None)
        con.execute(
            """
            UPDATE shadow_trade
            SET max_return_seen = ?, max_drawdown_seen = ?, time_to_2x = ?,
                time_to_5x = ?, time_to_10x = ?, raw = ?
            WHERE trade_id = ?
            """,
            [
                max_return,
                max_drawdown,
                time_to_2x,
                time_to_5x,
                time_to_10x,
                json_dumps({"last_mark": latest[0]["snapshot_time"], "current_mid": current_mid, "current_return": current_return}),
                trade["trade_id"],
            ],
        )
        count += 1
    return count


def refresh_shadow_trade_marks(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    trades = query_rows(
        con,
        """
        SELECT
            st.*,
            ce.contract_id,
            ce.ticker,
            ce.strategy_version
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        WHERE ce.strategy_version = ?
        """,
        [strategy_version],
    )
    count = 0
    for trade in trades:
        snapshots = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time
            """,
            [trade["contract_id"], trade["entry_time"]],
        )
        for mark in build_shadow_trade_marks(trade, snapshots):
            con.execute(
                """
                INSERT OR REPLACE INTO shadow_trade_mark
                (mark_id, trade_id, event_id, contract_id, ticker, strategy_version,
                 mark_time, entry_time, entry_price_assumption, mark_price,
                 current_return, return_1d, return_5d, return_20d, return_60d,
                 max_return_since_alert, max_drawdown_since_alert, time_to_2x,
                 time_to_5x, time_to_10x, dte, spread_pct, iv, underlying_price,
                 expired_worthless_probability_change, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    mark["mark_id"],
                    mark["trade_id"],
                    mark["event_id"],
                    mark["contract_id"],
                    mark["ticker"],
                    mark["strategy_version"],
                    mark["mark_time"],
                    mark["entry_time"],
                    mark["entry_price_assumption"],
                    mark["mark_price"],
                    mark["current_return"],
                    mark["return_1d"],
                    mark["return_5d"],
                    mark["return_20d"],
                    mark["return_60d"],
                    mark["max_return_since_alert"],
                    mark["max_drawdown_since_alert"],
                    mark["time_to_2x"],
                    mark["time_to_5x"],
                    mark["time_to_10x"],
                    mark["dte"],
                    mark["spread_pct"],
                    mark["iv"],
                    mark["underlying_price"],
                    mark["expired_worthless_probability_change"],
                    json_dumps(mark["raw"]),
                ],
            )
            count += 1
    return count


def build_shadow_trade_marks(trade: dict[str, Any], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entry_price = _number(trade.get("entry_price_assumption"))
    if entry_price is None or entry_price <= 0:
        return []
    clean_snapshots = [row for row in snapshots if _number(row.get("mid")) is not None]
    if not clean_snapshots:
        return []
    entry_time = _iso(trade.get("entry_time"))
    entry_delta = _bounded_abs_delta(clean_snapshots[0].get("delta"))
    returns: list[tuple[Any, float]] = []
    marks: list[dict[str, Any]] = []
    for snapshot in clean_snapshots:
        mark_price = _number(snapshot.get("mid"))
        if mark_price is None:
            continue
        mark_time = _iso(snapshot.get("snapshot_time"))
        current_return = mark_price / entry_price - 1
        returns.append((mark_time, current_return))
        values = [value for _time, value in returns]
        mark_delta = _bounded_abs_delta(snapshot.get("delta"))
        worthless_change = None if entry_delta is None or mark_delta is None else entry_delta - mark_delta
        marks.append(
            {
                "mark_id": stable_id("shadow_trade_mark", trade.get("trade_id"), mark_time),
                "trade_id": trade.get("trade_id"),
                "event_id": trade.get("event_id"),
                "contract_id": trade.get("contract_id"),
                "ticker": _normalize_symbol(trade.get("ticker") or snapshot.get("ticker")),
                "strategy_version": trade.get("strategy_version"),
                "mark_time": mark_time,
                "entry_time": entry_time,
                "entry_price_assumption": entry_price,
                "mark_price": mark_price,
                "current_return": current_return,
                "return_1d": _return_at_horizon(entry_time, returns, 1, mark_time),
                "return_5d": _return_at_horizon(entry_time, returns, 5, mark_time),
                "return_20d": _return_at_horizon(entry_time, returns, 20, mark_time),
                "return_60d": _return_at_horizon(entry_time, returns, 60, mark_time),
                "max_return_since_alert": max(values),
                "max_drawdown_since_alert": min(values),
                "time_to_2x": _first_hit_days(entry_time, returns, 1.0),
                "time_to_5x": _first_hit_days(entry_time, returns, 4.0),
                "time_to_10x": _first_hit_days(entry_time, returns, 9.0),
                "dte": _integer(snapshot.get("dte")),
                "spread_pct": _number(snapshot.get("spread_pct")),
                "iv": _number(snapshot.get("iv")),
                "underlying_price": _number(snapshot.get("underlying_price")),
                "expired_worthless_probability_change": worthless_change,
                "raw": {
                    "authority": "shadow_validation_only",
                    "return_horizon_method": "first_snapshot_at_or_after_horizon",
                    "expired_worthless_probability_proxy": "abs(entry_delta)-abs(mark_delta)",
                    "entry_delta_abs": entry_delta,
                    "mark_delta_abs": mark_delta,
                },
            }
        )
    return marks


def refresh_candidate_event_marks(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    events = query_rows(
        con,
        """
        SELECT *
        FROM candidate_event
        WHERE strategy_version = ?
        ORDER BY snapshot_time, ticker, contract_id
        """,
        [strategy_version],
    )
    con.execute("DELETE FROM candidate_event_mark WHERE strategy_version = ?", [strategy_version])
    count = 0
    for event in events:
        snapshots = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time
            """,
            [event["contract_id"], event["snapshot_time"]],
        )
        for mark in build_candidate_event_marks(event, snapshots):
            con.execute(
                """
                INSERT OR REPLACE INTO candidate_event_mark
                (mark_id, event_id, contract_id, ticker, strategy_version,
                 candidate_state, mark_time, alert_time, premium_fill_assumption,
                 mark_price, current_return, return_1d, return_5d, return_20d,
                 return_60d, max_return_since_alert, max_drawdown_since_alert,
                 time_to_2x, time_to_5x, time_to_10x, dte, spread_pct, iv,
                 underlying_price, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    mark["mark_id"],
                    mark["event_id"],
                    mark["contract_id"],
                    mark["ticker"],
                    mark["strategy_version"],
                    mark["candidate_state"],
                    mark["mark_time"],
                    mark["alert_time"],
                    mark["premium_fill_assumption"],
                    mark["mark_price"],
                    mark["current_return"],
                    mark["return_1d"],
                    mark["return_5d"],
                    mark["return_20d"],
                    mark["return_60d"],
                    mark["max_return_since_alert"],
                    mark["max_drawdown_since_alert"],
                    mark["time_to_2x"],
                    mark["time_to_5x"],
                    mark["time_to_10x"],
                    mark["dte"],
                    mark["spread_pct"],
                    mark["iv"],
                    mark["underlying_price"],
                    json_dumps(mark["raw"]),
                ],
            )
            count += 1
    return count


def build_candidate_event_marks(event: dict[str, Any], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entry_price = _number(event.get("premium_fill_assumption"))
    if entry_price is None or entry_price <= 0:
        return []
    clean_snapshots = [row for row in snapshots if _number(row.get("mid")) is not None]
    if not clean_snapshots:
        return []
    alert_time = _iso(event.get("snapshot_time"))
    entry_delta = _bounded_abs_delta(clean_snapshots[0].get("delta"))
    returns: list[tuple[Any, float]] = []
    marks: list[dict[str, Any]] = []
    for snapshot in clean_snapshots:
        mark_price = _number(snapshot.get("mid"))
        if mark_price is None:
            continue
        mark_time = _iso(snapshot.get("snapshot_time"))
        current_return = mark_price / entry_price - 1
        returns.append((mark_time, current_return))
        values = [value for _time, value in returns]
        mark_delta = _bounded_abs_delta(snapshot.get("delta"))
        marks.append(
            {
                "mark_id": stable_id("candidate_event_mark", event.get("event_id"), mark_time),
                "event_id": event.get("event_id"),
                "contract_id": event.get("contract_id"),
                "ticker": _normalize_symbol(event.get("ticker") or snapshot.get("ticker")),
                "strategy_version": event.get("strategy_version"),
                "candidate_state": str(event.get("state") or "").upper(),
                "mark_time": mark_time,
                "alert_time": alert_time,
                "premium_fill_assumption": entry_price,
                "mark_price": mark_price,
                "current_return": current_return,
                "return_1d": _return_at_horizon(alert_time, returns, 1, mark_time),
                "return_5d": _return_at_horizon(alert_time, returns, 5, mark_time),
                "return_20d": _return_at_horizon(alert_time, returns, 20, mark_time),
                "return_60d": _return_at_horizon(alert_time, returns, 60, mark_time),
                "max_return_since_alert": max(values),
                "max_drawdown_since_alert": min(values),
                "time_to_2x": _first_hit_days(alert_time, returns, 1.0),
                "time_to_5x": _first_hit_days(alert_time, returns, 4.0),
                "time_to_10x": _first_hit_days(alert_time, returns, 9.0),
                "dte": _integer(snapshot.get("dte")),
                "spread_pct": _number(snapshot.get("spread_pct")),
                "iv": _number(snapshot.get("iv")),
                "underlying_price": _number(snapshot.get("underlying_price")),
                "raw": {
                    "authority": "candidate_validation_only",
                    "candidate_state": str(event.get("state") or "").upper(),
                    "trigger_reason": event.get("trigger_reason"),
                    "return_horizon_method": "first_snapshot_at_or_after_horizon",
                    "entry_delta_abs": entry_delta,
                    "mark_delta_abs": mark_delta,
                },
            }
        )
    return marks


def refresh_candidate_event_attributions(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        SELECT
            m.*,
            s.mid AS snapshot_mid,
            s.underlying_price AS snapshot_underlying_price,
            s.iv AS snapshot_iv,
            s.spread_pct AS snapshot_spread_pct,
            s.delta,
            s.vega,
            s.theta
        FROM candidate_event_mark m
        LEFT JOIN option_snapshot s
          ON s.contract_id = m.contract_id
         AND s.snapshot_time = m.mark_time
        WHERE m.strategy_version = ?
        ORDER BY m.event_id, m.mark_time
        """,
        [strategy_version],
    )
    con.execute("DELETE FROM candidate_event_attribution WHERE strategy_version = ?", [strategy_version])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        event_id = str(row.get("event_id") or "")
        if event_id:
            grouped[event_id].append(row)

    count = 0
    for marks in grouped.values():
        for index in range(1, len(marks)):
            attribution = build_candidate_event_attribution(marks[index - 1], marks[index])
            if not attribution:
                continue
            con.execute(
                """
                INSERT OR REPLACE INTO candidate_event_attribution
                (attribution_id, event_id, contract_id, ticker, strategy_version,
                 candidate_state, snapshot_time, prior_snapshot_time,
                 option_return, underlying_return, iv_change, theta_decay,
                 spread_change, stock_move_effect, iv_effect, theta_effect,
                 spread_effect, unexplained_effect, label, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    attribution["attribution_id"],
                    attribution["event_id"],
                    attribution["contract_id"],
                    attribution["ticker"],
                    attribution["strategy_version"],
                    attribution["candidate_state"],
                    attribution["snapshot_time"],
                    attribution["prior_snapshot_time"],
                    attribution["option_return"],
                    attribution["underlying_return"],
                    attribution["iv_change"],
                    attribution["theta_decay"],
                    attribution["spread_change"],
                    attribution["stock_move_effect"],
                    attribution["iv_effect"],
                    attribution["theta_effect"],
                    attribution["spread_effect"],
                    attribution["unexplained_effect"],
                    attribution["label"],
                    json_dumps(attribution["raw"]),
                ],
            )
            count += 1
    return count


def build_candidate_event_attribution(prior: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any] | None:
    prior_mid = _number(prior.get("mark_price"))
    if prior_mid is None:
        prior_mid = _number(prior.get("snapshot_mid"))
    latest_mid = _number(latest.get("mark_price"))
    if latest_mid is None:
        latest_mid = _number(latest.get("snapshot_mid"))
    if prior_mid is None or latest_mid is None or prior_mid <= 0:
        return None

    prior_underlying = _number(prior.get("underlying_price"))
    if prior_underlying is None:
        prior_underlying = _number(prior.get("snapshot_underlying_price"))
    latest_underlying = _number(latest.get("underlying_price"))
    if latest_underlying is None:
        latest_underlying = _number(latest.get("snapshot_underlying_price"))
    underlying_change = None if prior_underlying is None or latest_underlying is None else latest_underlying - prior_underlying
    underlying_return = None if prior_underlying is None or prior_underlying <= 0 or latest_underlying is None else latest_underlying / prior_underlying - 1

    prior_iv = _number(prior.get("iv"))
    if prior_iv is None:
        prior_iv = _number(prior.get("snapshot_iv"))
    latest_iv = _number(latest.get("iv"))
    if latest_iv is None:
        latest_iv = _number(latest.get("snapshot_iv"))
    iv_change = _diff(latest_iv, prior_iv)

    prior_spread = _number(prior.get("spread_pct"))
    if prior_spread is None:
        prior_spread = _number(prior.get("snapshot_spread_pct"))
    latest_spread = _number(latest.get("spread_pct"))
    if latest_spread is None:
        latest_spread = _number(latest.get("snapshot_spread_pct"))
    spread_change = _diff(latest_spread, prior_spread)

    days = max(1, _elapsed_days(prior.get("mark_time"), latest.get("mark_time")) or 1)
    theta_decay = (_number(prior.get("theta")) or 0.0) * days
    stock_move_effect = ((_number(prior.get("delta")) or 0.0) * (underlying_change or 0.0)) / prior_mid
    iv_effect = ((_number(prior.get("vega")) or 0.0) * (iv_change or 0.0)) / prior_mid
    theta_effect = theta_decay / prior_mid
    spread_effect = -(spread_change or 0.0)
    option_return = latest_mid / prior_mid - 1
    explained = stock_move_effect + iv_effect + theta_effect + spread_effect
    unexplained = option_return - explained
    label = _attribution_label(option_return, underlying_return, iv_change, theta_effect, spread_change)
    return {
        "attribution_id": stable_id("candidate_event_attribution", latest.get("event_id"), latest.get("mark_time")),
        "event_id": latest.get("event_id"),
        "contract_id": latest.get("contract_id"),
        "ticker": _normalize_symbol(latest.get("ticker")),
        "strategy_version": latest.get("strategy_version"),
        "candidate_state": str(latest.get("candidate_state") or "").upper(),
        "snapshot_time": _iso(latest.get("mark_time")),
        "prior_snapshot_time": _iso(prior.get("mark_time")),
        "option_return": option_return,
        "underlying_return": underlying_return,
        "iv_change": iv_change,
        "theta_decay": theta_decay,
        "spread_change": spread_change,
        "stock_move_effect": stock_move_effect,
        "iv_effect": iv_effect,
        "theta_effect": theta_effect,
        "spread_effect": spread_effect,
        "unexplained_effect": unexplained,
        "label": label,
        "raw": {
            "authority": "candidate_attribution_only",
            "days": days,
            "prior_mark_id": prior.get("mark_id"),
            "latest_mark_id": latest.get("mark_id"),
            "prior_mid": prior_mid,
            "latest_mid": latest_mid,
            "prior_underlying": prior_underlying,
            "latest_underlying": latest_underlying,
            "method": "candidate_mark_delta_vega_theta_spread_approximation",
        },
    }


def _shadow_trades_by_contract(con: Any, strategy_version: str) -> dict[str, list[dict[str, Any]]]:
    rows = query_rows(
        con,
        """
        SELECT
            st.*,
            ce.contract_id,
            ce.ticker,
            ce.strategy_version
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        WHERE ce.strategy_version = ?
        ORDER BY ce.contract_id, st.entry_time
        """,
        [strategy_version],
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("contract_id") or "")].append(row)
    return grouped


def _shadow_marks_by_trade(con: Any, strategy_version: str) -> dict[str, list[dict[str, Any]]]:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM shadow_trade_mark
        WHERE strategy_version = ?
        ORDER BY trade_id, mark_time
        """,
        [strategy_version],
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("trade_id") or "")].append(row)
    return grouped


def _latest_thesis_validation_by_candidate_event(con: Any, strategy_version: str) -> dict[str, dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM agent_thesis_validation
        WHERE strategy_version = ? AND candidate_event_id IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY candidate_event_id ORDER BY validated_at DESC) = 1
        """,
        [strategy_version],
    )
    return {str(row.get("candidate_event_id")): row for row in rows if row.get("candidate_event_id")}


def _latest_legacy_thesis_validation_by_ticker(con: Any, strategy_version: str) -> dict[str, dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM agent_thesis_validation
        WHERE strategy_version = ? AND candidate_event_id IS NULL
        QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY validated_at DESC) = 1
        """,
        [strategy_version],
    )
    return {_normalize_symbol(row.get("ticker")): row for row in rows if row.get("ticker")}


def _trade_for_snapshot(trades: list[dict[str, Any]], snapshot_time: str) -> dict[str, Any] | None:
    active = [trade for trade in trades if _iso(trade.get("entry_time")) <= snapshot_time]
    if not active:
        return None
    return active[-1]


def _mark_for_snapshot(marks: list[dict[str, Any]], snapshot_time: str) -> dict[str, Any] | None:
    active = [mark for mark in marks if _iso(mark.get("mark_time")) <= snapshot_time]
    if not active:
        return None
    return active[-1]


def refresh_radar_state_transitions(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    candidates = query_rows(
        con,
        """
        SELECT *
        FROM candidate_event
        WHERE strategy_version = ?
        ORDER BY ticker, contract_id, snapshot_time
        """,
        [strategy_version],
    )
    con.execute("DELETE FROM radar_state_transition WHERE strategy_version = ?", [strategy_version])
    trades_by_contract = _shadow_trades_by_contract(con, strategy_version)
    marks_by_trade = _shadow_marks_by_trade(con, strategy_version)
    thesis_validation_by_candidate_event = _latest_thesis_validation_by_candidate_event(con, strategy_version)
    legacy_thesis_validation_by_ticker = _latest_legacy_thesis_validation_by_ticker(con, strategy_version)
    evaluated_at = datetime.utcnow().isoformat()
    previous_by_contract: dict[str, str] = {}
    count = 0
    for candidate in candidates:
        contract_id = str(candidate.get("contract_id") or "")
        snapshot_time = _iso(candidate.get("snapshot_time"))
        trade = _trade_for_snapshot(trades_by_contract.get(contract_id, []), snapshot_time)
        mark = _mark_for_snapshot(marks_by_trade.get(str((trade or {}).get("trade_id") or ""), []), snapshot_time) if trade else None
        thesis_validation = thesis_validation_by_candidate_event.get(str(candidate.get("event_id") or ""))
        if not thesis_validation:
            thesis_validation = legacy_thesis_validation_by_ticker.get(_normalize_symbol(candidate.get("ticker")))
        state = build_radar_state(candidate, trade, mark, thesis_validation)
        previous_state = previous_by_contract.get(contract_id)
        if previous_state == state["state"]:
            continue
        transition = {
            **state,
            "transition_id": stable_id("radar_state_transition", strategy_version, contract_id, snapshot_time, previous_state, state["state"]),
            "evaluated_at": evaluated_at,
            "snapshot_time": snapshot_time,
            "ticker": _normalize_symbol(candidate.get("ticker")),
            "contract_id": contract_id,
            "strategy_version": strategy_version,
            "previous_state": previous_state,
            "candidate_state": str(candidate.get("state") or "").upper(),
            "event_id": candidate.get("event_id"),
            "trade_id": (trade or {}).get("trade_id"),
            "mark_id": (mark or {}).get("mark_id"),
            "thesis_id": candidate.get("thesis_id"),
        }
        con.execute(
            """
            INSERT OR REPLACE INTO radar_state_transition
            (transition_id, evaluated_at, snapshot_time, ticker, contract_id,
             strategy_version, previous_state, state, candidate_state, event_id,
             trade_id, mark_id, thesis_id, trigger_reason, evidence_refs, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                transition["transition_id"],
                transition["evaluated_at"],
                transition["snapshot_time"],
                transition["ticker"],
                transition["contract_id"],
                transition["strategy_version"],
                transition["previous_state"],
                transition["state"],
                transition["candidate_state"],
                transition["event_id"],
                transition["trade_id"],
                transition["mark_id"],
                transition["thesis_id"],
                transition["trigger_reason"],
                json_dumps(transition["evidence_refs"]),
                json_dumps(transition["raw"]),
            ],
        )
        previous_by_contract[contract_id] = transition["state"]
        count += 1
    return count


def build_radar_state(
    candidate: dict[str, Any],
    trade: dict[str, Any] | None,
    mark: dict[str, Any] | None,
    thesis_validation: dict[str, Any] | None,
) -> dict[str, Any]:
    candidate_state = str(candidate.get("state") or "WATCH").upper()
    evidence_refs = [{"type": "candidate_event", "id": candidate.get("event_id")}]
    raw_candidate = _json(candidate.get("raw"))
    raw: dict[str, Any] = {
        "authority": "deterministic_state_transition",
        "candidate_state": candidate_state,
        "candidate_blockers": raw_candidate.get("blockers") or [],
        "candidate_hard_rejects": raw_candidate.get("hard_rejects") or [],
    }
    if thesis_validation:
        evidence_refs.append({"type": "agent_thesis_validation", "id": thesis_validation.get("validation_id")})
        raw.update(
            {
                "thesis_validation_state": str(thesis_validation.get("state") or "").lower() or None,
                "thesis_invalidation_status": thesis_validation.get("invalidation_status"),
                "thesis_red_team_status": thesis_validation.get("red_team_status"),
                "thesis_proof_status": thesis_validation.get("proof_status"),
                "thesis_catalyst_status": thesis_validation.get("catalyst_status"),
            }
        )
    thesis_exit_reason = _thesis_exit_reason(thesis_validation)
    if not trade:
        if thesis_exit_reason:
            return {
                "state": "INVALIDATED",
                "trigger_reason": thesis_exit_reason,
                "evidence_refs": evidence_refs,
                "raw": raw,
            }
        return {
            "state": candidate_state,
            "trigger_reason": str(candidate.get("trigger_reason") or candidate_state.lower()),
            "evidence_refs": evidence_refs,
            "raw": raw,
        }

    evidence_refs.append({"type": "shadow_trade", "id": trade.get("trade_id")})
    entry_time = _iso(trade.get("entry_time"))
    snapshot_time = _iso(candidate.get("snapshot_time"))
    if mark:
        evidence_refs.append({"type": "shadow_trade_mark", "id": mark.get("mark_id")})
    validation_state = str((thesis_validation or {}).get("state") or "").lower()
    current_return = _number((mark or {}).get("current_return"))
    max_drawdown = _number((mark or {}).get("max_drawdown_since_alert"))
    dte = _integer((mark or {}).get("dte"))
    raw.update(
        {
            "trade_id": trade.get("trade_id"),
            "mark_id": (mark or {}).get("mark_id"),
            "current_return": current_return,
            "max_drawdown_since_alert": max_drawdown,
            "dte": dte,
            "thesis_validation_state": validation_state or None,
            "exit_loss_threshold": -0.60,
            "trim_return_threshold": 4.0,
        }
    )
    if snapshot_time == entry_time:
        return {"state": "FIRE", "trigger_reason": "premium_triggered_shadow_entry", "evidence_refs": evidence_refs, "raw": raw}
    if thesis_exit_reason:
        return {"state": "INVALIDATED", "trigger_reason": thesis_exit_reason, "evidence_refs": evidence_refs, "raw": raw}
    if current_return is not None and current_return <= -0.60:
        return {"state": "EXIT", "trigger_reason": "option_loss_60pct", "evidence_refs": evidence_refs, "raw": raw}
    if max_drawdown is not None and max_drawdown <= -0.60:
        return {"state": "EXIT", "trigger_reason": "max_drawdown_60pct", "evidence_refs": evidence_refs, "raw": raw}
    if dte is not None and dte <= 30:
        return {"state": "EXIT", "trigger_reason": "near_expiry", "evidence_refs": evidence_refs, "raw": raw}
    if (mark or {}).get("time_to_10x") is not None or (current_return is not None and current_return >= 9.0):
        return {"state": "TRIM", "trigger_reason": "hit_10x", "evidence_refs": evidence_refs, "raw": raw}
    if (mark or {}).get("time_to_5x") is not None or (current_return is not None and current_return >= 4.0):
        return {"state": "TRIM", "trigger_reason": "hit_5x", "evidence_refs": evidence_refs, "raw": raw}
    if (mark or {}).get("time_to_2x") is not None or (current_return is not None and current_return >= 1.0):
        return {"state": "HOLD", "trigger_reason": "hit_2x_continue_tracking", "evidence_refs": evidence_refs, "raw": raw}
    return {"state": "HOLD", "trigger_reason": "shadow_trade_still_validating", "evidence_refs": evidence_refs, "raw": raw}


def refresh_option_attributions(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    trades = query_rows(
        con,
        """
        SELECT st.trade_id, st.event_id, st.entry_time, ce.contract_id, ce.strategy_version
        FROM shadow_trade st
        JOIN candidate_event ce ON ce.event_id = st.event_id
        WHERE ce.strategy_version = ?
        """,
        [strategy_version],
    )
    count = 0
    for trade in trades:
        latest_rows = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], trade["entry_time"]],
        )
        if not latest_rows:
            continue
        latest = latest_rows[0]
        prior_rows = query_rows(
            con,
            """
            SELECT *
            FROM option_snapshot
            WHERE contract_id = ? AND snapshot_time < TRY_CAST(? AS TIMESTAMP)
                  AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            [trade["contract_id"], latest["snapshot_time"], trade["entry_time"]],
        )
        if not prior_rows:
            prior_rows = query_rows(
                con,
                """
                SELECT *
                FROM option_snapshot
                WHERE contract_id = ? AND snapshot_time = TRY_CAST(? AS TIMESTAMP)
                LIMIT 1
                """,
                [trade["contract_id"], trade["entry_time"]],
            )
        if not prior_rows or prior_rows[0]["snapshot_time"] == latest["snapshot_time"]:
            continue
        attribution = build_option_attribution(trade, prior_rows[0], latest)
        if not attribution:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO option_attribution
            (attribution_id, trade_id, event_id, contract_id, snapshot_time,
             prior_snapshot_time, option_return, underlying_return, iv_change,
             theta_decay, spread_change, stock_move_effect, iv_effect,
             theta_effect, spread_effect, unexplained_effect, label, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                attribution["attribution_id"],
                attribution["trade_id"],
                attribution["event_id"],
                attribution["contract_id"],
                attribution["snapshot_time"],
                attribution["prior_snapshot_time"],
                attribution["option_return"],
                attribution["underlying_return"],
                attribution["iv_change"],
                attribution["theta_decay"],
                attribution["spread_change"],
                attribution["stock_move_effect"],
                attribution["iv_effect"],
                attribution["theta_effect"],
                attribution["spread_effect"],
                attribution["unexplained_effect"],
                attribution["label"],
                json_dumps(attribution["raw"]),
            ],
        )
        count += 1
    return count


def build_option_attribution(trade: dict[str, Any], prior: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any] | None:
    prior_mid = _number(prior.get("mid"))
    latest_mid = _number(latest.get("mid"))
    if prior_mid is None or latest_mid is None or prior_mid <= 0:
        return None
    prior_underlying = _number(prior.get("underlying_price"))
    latest_underlying = _number(latest.get("underlying_price"))
    underlying_change = None if prior_underlying is None or latest_underlying is None else latest_underlying - prior_underlying
    underlying_return = None if prior_underlying is None or prior_underlying <= 0 or latest_underlying is None else latest_underlying / prior_underlying - 1
    iv_change = _diff(_number(latest.get("iv")), _number(prior.get("iv")))
    days = max(1, _elapsed_days(prior.get("snapshot_time"), latest.get("snapshot_time")) or 1)
    theta_decay = (_number(prior.get("theta")) or 0.0) * days
    stock_move_effect = ((_number(prior.get("delta")) or 0.0) * (underlying_change or 0.0)) / prior_mid
    iv_effect = ((_number(prior.get("vega")) or 0.0) * (iv_change or 0.0)) / prior_mid
    theta_effect = theta_decay / prior_mid
    spread_change = _diff(_number(latest.get("spread_pct")), _number(prior.get("spread_pct")))
    spread_effect = -(spread_change or 0.0)
    option_return = latest_mid / prior_mid - 1
    explained = stock_move_effect + iv_effect + theta_effect + spread_effect
    unexplained = option_return - explained
    label = _attribution_label(option_return, underlying_return, iv_change, theta_effect, spread_change)
    return {
        "attribution_id": stable_id("option_attribution", trade["trade_id"], latest["snapshot_time"]),
        "trade_id": trade["trade_id"],
        "event_id": trade["event_id"],
        "contract_id": trade["contract_id"],
        "snapshot_time": _iso(latest.get("snapshot_time")),
        "prior_snapshot_time": _iso(prior.get("snapshot_time")),
        "option_return": option_return,
        "underlying_return": underlying_return,
        "iv_change": iv_change,
        "theta_decay": theta_decay,
        "spread_change": spread_change,
        "stock_move_effect": stock_move_effect,
        "iv_effect": iv_effect,
        "theta_effect": theta_effect,
        "spread_effect": spread_effect,
        "unexplained_effect": unexplained,
        "label": label,
        "raw": {
            "days": days,
            "prior_mid": prior_mid,
            "latest_mid": latest_mid,
            "prior_underlying": prior_underlying,
            "latest_underlying": latest_underlying,
            "method": "delta_vega_theta_spread_approximation",
        },
    }


def detect_missed_winners(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str | None = None,
) -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM option_snapshot s
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        ORDER BY s.contract_id, s.snapshot_time
        """,
        [*source_filter["params"], *symbol_filter["params"]],
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("contract_id")), []).append(row)

    count = 0
    for contract_id, snapshots in grouped.items():
        winner = build_missed_winner(con, contract_id, snapshots, strategy_version)
        if not winner:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO missed_winner_event
            (missed_id, detected_at, ticker, contract_id, strategy_version,
             first_snapshot_time, winner_snapshot_time, entry_price_assumption,
             winner_price, max_return_seen, winner_threshold, filter_reason,
             proposed_strategy_family, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                winner["missed_id"],
                winner["detected_at"],
                winner["ticker"],
                winner["contract_id"],
                winner["strategy_version"],
                winner["first_snapshot_time"],
                winner["winner_snapshot_time"],
                winner["entry_price_assumption"],
                winner["winner_price"],
                winner["max_return_seen"],
                winner["winner_threshold"],
                winner["filter_reason"],
                winner["proposed_strategy_family"],
                json_dumps(winner["raw"]),
            ],
        )
        count += 1
    return count


def build_missed_winner(con: Any, contract_id: str, snapshots: list[dict[str, Any]], strategy_version: str) -> dict[str, Any] | None:
    usable = [row for row in snapshots if (_number(row.get("mid")) or 0) > 0]
    if len(usable) < 2:
        return None
    entry = usable[0]
    entry_mid = _number(entry.get("mid"))
    if entry_mid is None or entry_mid <= 0:
        return None
    winner = max(usable[1:], key=lambda row: _number(row.get("mid")) or 0)
    winner_mid = _number(winner.get("mid"))
    if winner_mid is None:
        return None
    max_return = winner_mid / entry_mid - 1
    if max_return < 4.0:
        return None
    fire_rows = query_rows(
        con,
        """
        SELECT event_id
        FROM candidate_event
        WHERE contract_id = ? AND strategy_version = ? AND state = 'FIRE'
              AND snapshot_time <= TRY_CAST(? AS TIMESTAMP)
        LIMIT 1
        """,
        [contract_id, strategy_version, winner["snapshot_time"]],
    )
    if fire_rows:
        return None
    candidate_rows = query_rows(
        con,
        """
        SELECT state, trigger_reason, raw
        FROM candidate_event
        WHERE contract_id = ? AND strategy_version = ?
        ORDER BY snapshot_time
        LIMIT 1
        """,
        [contract_id, strategy_version],
    )
    filter_reason = _missed_filter_reason(candidate_rows[0] if candidate_rows else None)
    threshold = "10x" if max_return >= 9.0 else "5x"
    proposed_family = _proposed_family(filter_reason)
    return {
        "missed_id": stable_id("missed_winner", strategy_version, contract_id, entry["snapshot_time"], winner["snapshot_time"], threshold),
        "detected_at": datetime.utcnow().isoformat(),
        "ticker": _normalize_symbol(entry.get("ticker")),
        "contract_id": contract_id,
        "strategy_version": strategy_version,
        "first_snapshot_time": _iso(entry.get("snapshot_time")),
        "winner_snapshot_time": _iso(winner.get("snapshot_time")),
        "entry_price_assumption": entry_mid,
        "winner_price": winner_mid,
        "max_return_seen": max_return,
        "winner_threshold": threshold,
        "filter_reason": filter_reason,
        "proposed_strategy_family": proposed_family,
        "raw": {
            "candidate_state": candidate_rows[0].get("state") if candidate_rows else "missing_candidate",
            "first_snapshot": _compact_snapshot(entry),
            "winner_snapshot": _compact_snapshot(winner),
        },
    }


def generate_strategy_mutation_proposals(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    rows = query_rows(
        con,
        """
        SELECT filter_reason, proposed_strategy_family, count(*) AS missed_count,
               max(max_return_seen) AS best_return,
               list(missed_id) AS missed_ids
        FROM missed_winner_event
        WHERE strategy_version = ?
        GROUP BY filter_reason, proposed_strategy_family
        ORDER BY missed_count DESC, best_return DESC
        """,
        [strategy_version],
    )
    count = 0
    for row in rows:
        proposal = build_strategy_mutation_proposal(row, strategy_version)
        if not proposal:
            continue
        existing_rows = query_rows(
            con,
            "SELECT status, human_approval_status FROM strategy_mutation_proposal WHERE proposal_id = ?",
            [proposal["proposal_id"]],
        )
        if existing_rows and _strategy_proposal_is_terminal(existing_rows[0]):
            continue
        before = 1 if existing_rows else 0
        con.execute(
            """
            INSERT OR REPLACE INTO strategy_mutation_proposal
            (proposal_id, created_at, source_type, strategy_version, proposed_strategy_version,
             proposed_parameter_changes, rationale, expected_effect, risk, status,
             requires_backtest, requires_forward_test, human_approval_status,
             evidence_refs, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                proposal["proposal_id"],
                proposal["created_at"],
                proposal["source_type"],
                proposal["strategy_version"],
                proposal["proposed_strategy_version"],
                json_dumps(proposal["proposed_parameter_changes"]),
                proposal["rationale"],
                proposal["expected_effect"],
                proposal["risk"],
                proposal["status"],
                proposal["requires_backtest"],
                proposal["requires_forward_test"],
                proposal["human_approval_status"],
                json_dumps(proposal["evidence_refs"]),
                json_dumps(proposal["raw"]),
            ],
        )
        after = query_rows(con, "SELECT count(*) AS count FROM strategy_mutation_proposal WHERE proposal_id = ?", [proposal["proposal_id"]])[0]["count"]
        count += int(after) - int(before)
    return count


def build_strategy_mutation_proposal(row: dict[str, Any], strategy_version: str) -> dict[str, Any] | None:
    filter_reason = str(row.get("filter_reason") or "unknown")
    changes = _proposal_parameter_changes(filter_reason)
    if not changes:
        return None
    family = str(row.get("proposed_strategy_family") or "leap_10x_variant")
    proposed_version = f"{family}_proposed_v1"
    missed_count = int(row.get("missed_count") or 0)
    best_return = _number(row.get("best_return")) or 0.0
    missed_ids = _list_value(row.get("missed_ids"))
    return {
        "proposal_id": stable_id("strategy_mutation_proposal", strategy_version, filter_reason, family),
        "created_at": datetime.utcnow().isoformat(),
        "source_type": "deterministic_missed_winner_analysis",
        "strategy_version": strategy_version,
        "proposed_strategy_version": proposed_version,
        "proposed_parameter_changes": changes,
        "rationale": f"{missed_count} missed winner(s) were filtered by {filter_reason}; best observed return was {best_return + 1:.2f}x.",
        "expected_effect": "Increase recall for similar 5x/10x contracts in shadow mode.",
        "risk": "May increase false positives or earlier entries; must pass deterministic backtest and forward shadow comparison before promotion.",
        "status": "proposed",
        "requires_backtest": True,
        "requires_forward_test": True,
        "human_approval_status": "required",
        "evidence_refs": [{"type": "missed_winner_event", "id": missed_id} for missed_id in missed_ids],
        "raw": {
            "filter_reason": filter_reason,
            "missed_count": missed_count,
            "best_return": best_return,
            "promotion_policy": "no_auto_promotion",
        },
    }


class StrategyPromotionError(ValueError):
    """Raised when a strategy proposal is not eligible for promotion."""


def refresh_strategy_proposal_evaluations(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> dict[str, int]:
    proposals = query_rows(
        con,
        """
        SELECT *
        FROM strategy_mutation_proposal
        WHERE strategy_version = ?
        ORDER BY created_at DESC
        """,
        [strategy_version],
    )
    backtests = 0
    forward_tests = 0
    updates = 0
    for proposal in proposals:
        backtest = build_strategy_backtest_result(con, proposal)
        if backtest:
            insert_strategy_backtest_result(con, backtest)
            backtests += 1
        forward = build_strategy_forward_test_result(con, proposal)
        if forward:
            insert_strategy_forward_test_result(con, forward)
            forward_tests += 1
        updates += update_strategy_proposal_gate_status(con, proposal["proposal_id"])
    return {"strategy_backtests": backtests, "strategy_forward_tests": forward_tests, "strategy_gate_updates": updates}


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
    labels = [str(row.get("latest_attribution_label") or "") for row in records]
    qqq_above = [row for row in records if row.get("qqq_above_200d") is True]
    early_entries = [row for row in records if row.get("timing_label") in {"early_but_worked", "false_positive_drawdown"}]
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
            "sample_outcomes": metrics["outcomes"][:20],
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
    verdict = _backtest_verdict(baseline, proposed)
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
        "metrics": {"baseline": baseline, "proposed": proposed},
        "raw": {
            "proposal_changes": _json_or_list(proposal.get("proposed_parameter_changes")),
            "promotion_gate": "backtest_only_never_promotes",
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


def update_strategy_proposal_gate_status(con: Any, proposal_id: str) -> int:
    before = query_rows(
        con,
        "SELECT status, human_approval_status FROM strategy_mutation_proposal WHERE proposal_id = ?",
        [proposal_id],
    )
    if not before or _strategy_proposal_is_terminal(before[0]):
        return 0
    backtest = _latest_backtest(con, proposal_id)
    forward = _latest_forward_test(con, proposal_id)
    if not backtest:
        status = "backtest_required"
    elif backtest.get("verdict") != "pass":
        status = "backtest_failed"
    elif not forward or forward.get("verdict") == "collecting_data":
        status = "forward_test_required"
    elif forward.get("verdict") != "pass":
        status = "forward_test_failed"
    else:
        status = "ready_for_human_review"
    if before[0].get("status") == status:
        return 0
    con.execute("UPDATE strategy_mutation_proposal SET status = ? WHERE proposal_id = ?", [status, proposal_id])
    return 1


def _strategy_proposal_is_terminal(proposal: dict[str, Any]) -> bool:
    status = str(proposal.get("status") or "").lower()
    human_status = str(proposal.get("human_approval_status") or "").lower()
    return status in {"promoted", "rejected"} or human_status in {"approved", "rejected"}


def promote_strategy_mutation(con: Any, proposal_id: str, *, approved_by: str | None = None) -> str:
    if not approved_by:
        raise StrategyPromotionError("human approval is required before promotion")
    proposal_rows = query_rows(con, "SELECT * FROM strategy_mutation_proposal WHERE proposal_id = ?", [proposal_id])
    if not proposal_rows:
        raise StrategyPromotionError(f"unknown strategy proposal: {proposal_id}")
    proposal = proposal_rows[0]
    backtest = _latest_backtest(con, proposal_id)
    forward = _latest_forward_test(con, proposal_id)
    if not backtest or backtest.get("verdict") != "pass":
        raise StrategyPromotionError("passing backtest is required before promotion")
    if not forward or forward.get("verdict") != "pass":
        raise StrategyPromotionError("passing forward shadow test is required before promotion")
    base_params = _strategy_parameters(con, proposal["strategy_version"])
    proposed_params = _proposed_strategy_parameters(base_params, proposal.get("proposed_parameter_changes"))
    promoted_at = datetime.utcnow().isoformat()
    con.execute(
        """
        INSERT OR REPLACE INTO option_strategy_versions
        (strategy_version, strategy_name, version, created_at, status,
         parameters, promoted_at, supersedes, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            proposal["proposed_strategy_version"],
            proposed_params.get("strategy_name") or proposal["proposed_strategy_version"],
            int(base_params.get("version") or 1) + 1,
            promoted_at,
            "promoted",
            json_dumps(proposed_params),
            promoted_at,
            proposal["strategy_version"],
            f"Promoted from {proposal_id} after deterministic backtest, forward test, and human approval by {approved_by}.",
        ],
    )
    con.execute(
        """
        UPDATE strategy_mutation_proposal
        SET status = 'promoted',
            human_approval_status = 'approved',
            approved_by = ?,
            approved_at = ?,
            raw = ?
        WHERE proposal_id = ?
        """,
        [
            approved_by,
            promoted_at,
            json_dumps({**_json(proposal.get("raw")), "approved_by": approved_by, "approved_at": promoted_at}),
            proposal_id,
        ],
    )
    return str(proposal["proposed_strategy_version"])


def _thesis_validation_blocks_entry(row: dict[str, Any]) -> bool:
    return _thesis_exit_reason(
        {
            "state": row.get("thesis_validation_state"),
            "invalidation_status": row.get("thesis_invalidation_status"),
            "red_team_status": row.get("thesis_red_team_status"),
        }
    ) is not None


def _thesis_exit_reason(thesis_validation: dict[str, Any] | None) -> str | None:
    if not thesis_validation:
        return None
    validation_state = str(thesis_validation.get("state") or "").lower()
    invalidation_status = str(thesis_validation.get("invalidation_status") or "").lower()
    red_team_status = str(thesis_validation.get("red_team_status") or "").lower()
    if validation_state == "invalidated" or invalidation_status == "breached":
        return "agent_thesis_invalidated"
    if red_team_status == "hard_risk_triggered":
        return "hard_red_team_risk"
    return None


def _attribution_label(
    option_return: float,
    underlying_return: float | None,
    iv_change: float | None,
    theta_effect: float,
    spread_change: float | None,
) -> str:
    if spread_change is not None and spread_change > 0.10:
        return "liquidity_risk"
    if underlying_return is not None and underlying_return > 0.02 and option_return > 0.10:
        return "good_convexity"
    if underlying_return is not None and underlying_return > 0.02 and option_return <= 0.0:
        return "iv_crush_or_bad_strike"
    if underlying_return is not None and abs(underlying_return) <= 0.01 and option_return < 0.0:
        return "theta_iv_bleed"
    if iv_change is not None and iv_change < -0.05 and option_return < 0.0:
        return "iv_crush"
    if theta_effect < -0.05 and option_return < 0.0:
        return "theta_decay"
    return "mixed"


def _missed_filter_reason(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return "no_candidate_event"
    raw = _json(candidate.get("raw"))
    hard_rejects = raw.get("hard_rejects") if isinstance(raw.get("hard_rejects"), list) else []
    blockers = raw.get("blockers") if isinstance(raw.get("blockers"), list) else []
    reasons = [str(item) for item in [*hard_rejects, *blockers] if item]
    return reasons[0] if reasons else str(candidate.get("state") or "unknown_filter")


def _proposed_family(filter_reason: str) -> str:
    if "delta" in filter_reason:
        return "leap_10x_momentum_lottery"
    if "iv" in filter_reason:
        return "leap_10x_high_iv_catalyst"
    if "open_interest" in filter_reason or "volume" in filter_reason or "spread" in filter_reason:
        return "leap_10x_liquidity_watch"
    if "50d" in filter_reason or "rs_vs_qqq" in filter_reason:
        return "leap_10x_early_reversal"
    return "leap_10x_variant"


def _proposal_parameter_changes(filter_reason: str) -> dict[str, Any]:
    if "delta_outside_strategy_range" in filter_reason:
        return {"delta_min": 0.10, "delta_max": 0.45, "candidate_note": "test lower-delta lottery sleeve separately"}
    if "iv_percentile" in filter_reason:
        return {"max_iv_percentile": 85.0, "candidate_note": "test high-IV catalyst sleeve separately"}
    if "open_interest" in filter_reason:
        return {"min_open_interest": 25, "candidate_note": "test low-OI contracts only with stronger spread and volume gates"}
    if "volume" in filter_reason:
        return {"min_volume": 0, "candidate_note": "test no-volume LEAP snapshots with stricter OI and spread gates"}
    if "spread" in filter_reason:
        return {"max_spread_pct": 0.35, "candidate_note": "test wider spreads only in shadow mode"}
    if "50d" in filter_reason:
        return {"require_price_above_ma50": False, "candidate_note": "test pre-50D early reversal sleeve"}
    if "rs_vs_qqq" in filter_reason:
        return {"require_rs_improving": False, "candidate_note": "test pre-RS recovery sleeve"}
    if "required_move" in filter_reason:
        return {"max_required_move_pct": 5.0, "candidate_note": "test larger required moves as a separate lottery strategy"}
    return {}


def _historical_candidate_rows(con: Any) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
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
            sf.price,
            sf.ma_50,
            sf.rs_vs_qqq_20d,
            sf.base_length_days,
            sf.breakout_level
        FROM option_snapshot s
        JOIN option_features f ON f.contract_id = s.contract_id AND f.snapshot_time = s.snapshot_time
        LEFT JOIN stock_features sf ON sf.ticker = s.ticker AND sf.snapshot_time = s.snapshot_time
        ORDER BY s.snapshot_time, s.ticker, s.expiration, s.strike, s.option_type
        """,
    )


def _strategy_outcome_records(con: Any, rows: list[dict[str, Any]], strategy_version: str, strategy: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes = []
    seen: set[tuple[str, str]] = set()
    qqq_cache: dict[str, bool | None] = {}
    attribution_labels = _latest_attribution_labels(con)
    for row in rows:
        event = build_candidate_event(row, strategy_version, strategy)
        if not event or event["state"] != "FIRE":
            continue
        key = (event["contract_id"], event["snapshot_time"])
        if key in seen:
            continue
        seen.add(key)
        outcome = _hypothetical_outcome(con, event)
        if not outcome:
            continue
        snapshot_time = _iso(event["snapshot_time"])
        qqq_above_200d = _qqq_above_200d(con, snapshot_time, qqq_cache)
        outcome.update(
            {
                "ticker": event["ticker"],
                "strategy_version": strategy_version,
                "setup_type": _setup_type(row, event),
                "cohorts": _cohort_labels(row, event, qqq_above_200d),
                "qqq_above_200d": qqq_above_200d,
                "latest_attribution_label": attribution_labels.get(event["event_id"]),
            }
        )
        outcomes.append(outcome)
    return outcomes


def _strategy_outcomes(con: Any, rows: list[dict[str, Any]], strategy_version: str, strategy: dict[str, Any]) -> dict[str, Any]:
    return _outcome_metrics(_strategy_outcome_records(con, rows, strategy_version, strategy))


def _hypothetical_outcome(con: Any, event: dict[str, Any]) -> dict[str, Any] | None:
    entry = _number(event.get("premium_fill_assumption"))
    if entry is None or entry <= 0:
        return None
    rows = query_rows(
        con,
        """
        SELECT snapshot_time, mid
        FROM option_snapshot
        WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
        ORDER BY snapshot_time
        """,
        [event["contract_id"], event["snapshot_time"]],
    )
    marks = [(row["snapshot_time"], _number(row.get("mid"))) for row in rows if _number(row.get("mid")) is not None]
    if not marks:
        return None
    returns = [(snapshot_time, (mid or 0) / entry - 1) for snapshot_time, mid in marks]
    max_time, max_return = max(returns, key=lambda item: item[1])
    _min_time, max_drawdown = min(returns, key=lambda item: item[1])
    time_to_2x = _first_hit_days(event["snapshot_time"], returns, 1.0)
    time_to_5x = _first_hit_days(event["snapshot_time"], returns, 4.0)
    time_to_10x = _first_hit_days(event["snapshot_time"], returns, 9.0)
    drawdown_before_2x = _drawdown_before_threshold(returns, 1.0)
    return {
        "event_id": event["event_id"],
        "contract_id": event["contract_id"],
        "entry_time": event["snapshot_time"],
        "entry_price": entry,
        "max_return_seen": max_return,
        "max_drawdown_seen": max_drawdown,
        "time_to_2x": time_to_2x,
        "time_to_5x": time_to_5x,
        "time_to_10x": time_to_10x,
        "drawdown_before_2x": drawdown_before_2x,
        "timing_label": _timing_label(time_to_2x, max_drawdown, drawdown_before_2x),
        "max_return_time": max_time,
    }


def _outcome_metrics(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(outcomes)
    if not count:
        return {
            "candidate_count": 0,
            "hit_rate_2x": 0.0,
            "hit_rate_5x": 0.0,
            "hit_rate_10x": 0.0,
            "false_positive_rate": 0.0,
            "median_max_return": None,
            "median_max_drawdown": None,
            "average_time_to_2x": None,
            "outcomes": [],
        }
    hit_2x = [row for row in outcomes if row["max_return_seen"] >= 1.0]
    hit_5x = [row for row in outcomes if row["max_return_seen"] >= 4.0]
    hit_10x = [row for row in outcomes if row["max_return_seen"] >= 9.0]
    time_to_2x = [row["time_to_2x"] for row in hit_2x if row.get("time_to_2x") is not None]
    return {
        "candidate_count": count,
        "hit_rate_2x": len(hit_2x) / count,
        "hit_rate_5x": len(hit_5x) / count,
        "hit_rate_10x": len(hit_10x) / count,
        "false_positive_rate": 1 - (len(hit_2x) / count),
        "median_max_return": _median([row["max_return_seen"] for row in outcomes]),
        "median_max_drawdown": _median([row["max_drawdown_seen"] for row in outcomes]),
        "average_time_to_2x": _average([float(value) for value in time_to_2x]) if time_to_2x else None,
        "outcomes": outcomes[:50],
    }


def _backtest_verdict(baseline: dict[str, Any], proposed: dict[str, Any]) -> str:
    if int(proposed.get("candidate_count") or 0) == 0:
        return "fail"
    improves_10x = float(proposed.get("hit_rate_10x") or 0) > float(baseline.get("hit_rate_10x") or 0)
    improves_5x = float(proposed.get("hit_rate_5x") or 0) > float(baseline.get("hit_rate_5x") or 0)
    baseline_false = float(baseline.get("false_positive_rate") or 0)
    proposed_false = float(proposed.get("false_positive_rate") or 0)
    allowed_false_positive = 1.0 if int(baseline.get("candidate_count") or 0) == 0 else min(1.0, baseline_false + 0.25)
    if (improves_10x or improves_5x) and proposed_false <= allowed_false_positive:
        return "pass"
    return "fail"


def _forward_test_verdict(baseline: dict[str, Any], proposed: dict[str, Any], days_observed: int) -> str:
    if days_observed < MIN_FORWARD_TEST_DAYS:
        return "collecting_data"
    if int(proposed.get("candidate_count") or 0) == 0:
        return "fail"
    if float(proposed.get("hit_rate_5x") or 0) >= float(baseline.get("hit_rate_5x") or 0):
        return "pass"
    return "fail"


def _first_hit_days(entry_time: Any, returns: list[tuple[Any, float]], threshold: float) -> int | None:
    for snapshot_time, value in returns:
        if value >= threshold:
            return _elapsed_days(entry_time, snapshot_time)
    return None


def _return_at_horizon(entry_time: Any, returns: list[tuple[Any, float]], horizon_days: int, mark_time: Any) -> float | None:
    elapsed_to_mark = _elapsed_days(entry_time, mark_time)
    if elapsed_to_mark is None or elapsed_to_mark < horizon_days:
        return None
    for snapshot_time, value in returns:
        elapsed = _elapsed_days(entry_time, snapshot_time)
        if elapsed is not None and elapsed >= horizon_days:
            return value
    return None


def _drawdown_before_threshold(returns: list[tuple[Any, float]], threshold: float) -> float | None:
    observed: list[float] = []
    for _snapshot_time, value in returns:
        observed.append(value)
        if value >= threshold:
            break
    return min(observed) if observed else None


def _timing_label(time_to_2x: int | None, max_drawdown: float | None, drawdown_before_2x: float | None) -> str:
    if time_to_2x is not None and (drawdown_before_2x or 0.0) <= -0.30:
        return "early_but_worked"
    if time_to_2x is None and (max_drawdown or 0.0) <= -0.40:
        return "false_positive_drawdown"
    if time_to_2x is not None and time_to_2x <= 5:
        return "fast_confirmation"
    if time_to_2x is not None:
        return "worked_after_wait"
    return "pending_or_failed"


def _latest_attribution_labels(con: Any) -> dict[str, str]:
    rows = query_rows(
        con,
        """
        WITH candidate_labels AS (
            SELECT event_id, label
            FROM candidate_event_attribution
            QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY snapshot_time DESC) = 1
        ),
        shadow_labels AS (
            SELECT event_id, label
            FROM option_attribution
            QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY snapshot_time DESC) = 1
        )
        SELECT event_id, label
        FROM candidate_labels
        UNION ALL
        SELECT s.event_id, s.label
        FROM shadow_labels s
        LEFT JOIN candidate_labels c ON c.event_id = s.event_id
        WHERE c.event_id IS NULL
        """,
    )
    return {str(row["event_id"]): str(row["label"]) for row in rows if row.get("event_id") and row.get("label")}


def _qqq_above_200d(con: Any, snapshot_time: str, cache: dict[str, bool | None]) -> bool | None:
    snapshot_date = _date(snapshot_time)
    if snapshot_date is None:
        return None
    key = snapshot_date.isoformat()
    if key in cache:
        return cache[key]
    rows = query_rows(
        con,
        """
        SELECT close
        FROM prices_daily
        WHERE symbol = 'QQQ' AND date <= TRY_CAST(? AS DATE)
        ORDER BY date DESC
        LIMIT 200
        """,
        [key],
    )
    closes = [_number(row.get("close")) for row in reversed(rows)]
    clean = [value for value in closes if value is not None]
    if len(clean) < 200:
        cache[key] = None
    else:
        cache[key] = clean[-1] >= (_average(clean[-200:]) or 10**9)
    return cache[key]


def _setup_type(row: dict[str, Any], event: dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    blockers = [str(item) for item in raw.get("blockers", [])] if isinstance(raw.get("blockers"), list) else []
    positives = [str(item) for item in raw.get("positives", [])] if isinstance(raw.get("positives"), list) else []
    price = _number(row.get("price"))
    ma50 = _number(row.get("ma_50"))
    breakout = _number(row.get("breakout_level"))
    base_length = _integer(row.get("base_length_days")) or 0
    distance_from_high = _number(row.get("distance_from_52w_high"))
    rs20 = _number(row.get("rs_vs_qqq_20d"))
    if "stock_below_50d" in blockers or (price is not None and ma50 is not None and price < ma50):
        return "early_reversal"
    if distance_from_high is not None and distance_from_high <= -0.30:
        return "post_crash_recovery"
    if base_length >= 30 and price is not None and breakout is not None and price >= breakout * 0.98:
        return "post_base_breakout"
    if rs20 is not None and rs20 >= 0.05:
        return "relative_strength_leader"
    if "premium_inside_buy_under" in positives and "stock_above_50d" in positives:
        return "reclaiming_50d"
    return "standard_reversal"


def _cohort_labels(row: dict[str, Any], event: dict[str, Any], qqq_above_200d: bool | None) -> list[dict[str, str]]:
    labels = [{"type": "setup_type", "value": _setup_type(row, event)}]
    required_move = _number(row.get("required_move_10x_pct"))
    iv_percentile = _number(row.get("iv_percentile"))
    spread_pct = _number(row.get("spread_pct"))
    if required_move is not None:
        value = "under_200pct" if required_move <= 2.0 else "under_350pct" if required_move <= 3.5 else "over_350pct"
        labels.append({"type": "required_move_bucket", "value": value})
    if iv_percentile is not None:
        value = "low_iv" if iv_percentile < 50 else "normal_iv" if iv_percentile <= 70 else "high_iv"
        labels.append({"type": "iv_regime", "value": value})
    if spread_pct is not None:
        value = "tight_spread" if spread_pct <= 0.15 else "usable_spread" if spread_pct <= 0.25 else "wide_spread"
        labels.append({"type": "liquidity_regime", "value": value})
    value = "qqq_above_200d" if qqq_above_200d is True else "qqq_below_200d" if qqq_above_200d is False else "qqq_200d_unknown"
    labels.append({"type": "market_regime", "value": value})
    return labels


def _value_counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _cohort_definition(cohort_type: str, cohort_value: str) -> str:
    definitions = {
        ("setup_type", "early_reversal"): "Stock had not reclaimed the 50D context at candidate time.",
        ("setup_type", "post_crash_recovery"): "Stock was at least 30% below its 52-week high at candidate time.",
        ("setup_type", "post_base_breakout"): "Stock had a 30+ day base and was near or above the stored breakout level.",
        ("setup_type", "relative_strength_leader"): "20-day relative strength versus QQQ was at least +5%.",
        ("setup_type", "reclaiming_50d"): "Candidate passed premium and 50D reclaim gates.",
        ("setup_type", "standard_reversal"): "Candidate passed baseline gates without a stronger deterministic setup bucket.",
        ("market_regime", "qqq_above_200d"): "QQQ close was at or above its 200-day moving average.",
        ("market_regime", "qqq_below_200d"): "QQQ close was below its 200-day moving average.",
    }
    return definitions.get((cohort_type, cohort_value), f"{cohort_type}={cohort_value}")


def _proposed_strategy_parameters(base: dict[str, Any], value: Any) -> dict[str, Any]:
    changes = _json_or_list(value)
    if not isinstance(changes, dict):
        changes = {}
    return {**base, **{key: item for key, item in changes.items() if key != "candidate_note"}}


def _latest_backtest(con: Any, proposal_id: str) -> dict[str, Any] | None:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM strategy_backtest_result
        WHERE proposal_id = ?
        ORDER BY evaluated_at DESC
        LIMIT 1
        """,
        [proposal_id],
    )
    return rows[0] if rows else None


def _latest_forward_test(con: Any, proposal_id: str) -> dict[str, Any] | None:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM strategy_forward_test_result
        WHERE proposal_id = ?
        ORDER BY evaluated_at DESC
        LIMIT 1
        """,
        [proposal_id],
    )
    return rows[0] if rows else None


def _compact_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_time": _iso(row.get("snapshot_time")),
        "ticker": _normalize_symbol(row.get("ticker")),
        "contract_id": row.get("contract_id"),
        "underlying_price": _number(row.get("underlying_price")),
        "expiration": str(row.get("expiration")),
        "strike": _number(row.get("strike")),
        "option_type": row.get("option_type"),
        "mid": _number(row.get("mid")),
        "iv": _number(row.get("iv")),
        "delta": _number(row.get("delta")),
        "spread_pct": _number(row.get("spread_pct")),
        "volume": _number(row.get("volume")),
        "open_interest": _number(row.get("open_interest")),
    }


def _strategy_parameters(con: Any, strategy_version: str) -> dict[str, Any]:
    rows = query_rows(con, "SELECT parameters FROM option_strategy_versions WHERE strategy_version = ?", [strategy_version])
    if not rows:
        register_default_strategy(con, strategy_version)
        return dict(DEFAULT_STRATEGY_PARAMETERS)
    return {**DEFAULT_STRATEGY_PARAMETERS, **_json(rows[0].get("parameters"))}


def _symbol_filter(symbols: list[str] | None, *, table_alias: str, column: str = "symbol") -> dict[str, Any]:
    clean = [_normalize_symbol(symbol) for symbol in symbols or [] if symbol]
    if not clean:
        return {"sql": "", "params": []}
    placeholders = ", ".join(["?"] * len(clean))
    return {"sql": f"AND {table_alias}.{column} IN ({placeholders})", "params": clean}


def _source_filter(source: str | None, *, table_alias: str, column: str = "source") -> dict[str, Any]:
    if not source:
        return {"sql": "", "params": []}
    return {"sql": f"AND {table_alias}.{column} = ?", "params": [source]}


def _contract_id(ticker: str, expiration: Any, strike: float | None, option_type: str, provider_symbol: Any) -> str:
    if provider_symbol:
        return str(provider_symbol)
    return f"{ticker}:{expiration}:{strike:g}:{option_type}" if strike is not None else stable_id(ticker, expiration, option_type)


def _premium_mid(row: dict[str, Any], raw: dict[str, Any]) -> float | None:
    mid = _number(row.get("mid")) or _coalesce_number(raw, "mid", "mark")
    if mid is not None:
        return mid
    bid = _number(row.get("bid")) or _coalesce_number(raw, "bid")
    ask = _number(row.get("ask")) or _coalesce_number(raw, "ask")
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def _spread_pct(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None or mid <= 0:
        return None
    return max(0.0, (ask - bid) / mid)


def _required_move_pct(option_type: str, underlying: float | None, required_price: float) -> float | None:
    if underlying is None or underlying <= 0:
        return None
    if option_type == "put":
        return max(0.0, (underlying - required_price) / underlying)
    return max(0.0, (required_price - underlying) / underlying)


def _diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _bounded_abs_delta(value: Any) -> float | None:
    delta = _number(value)
    if delta is None:
        return None
    return max(0.0, min(1.0, abs(delta)))


def _liquidity_score(spread_pct: float | None, open_interest: float | None, volume: float | None) -> float | None:
    components: list[float] = []
    weights: list[float] = []
    if spread_pct is not None:
        components.append(max(0.0, min(100.0, 100.0 - spread_pct * 300.0)))
        weights.append(0.60)
    if open_interest is not None:
        components.append(max(0.0, min(100.0, open_interest / 500.0 * 100.0)))
        weights.append(0.25)
    if volume is not None:
        components.append(max(0.0, min(100.0, volume / 100.0 * 100.0)))
        weights.append(0.15)
    if not components:
        return None
    score = sum(component * weight for component, weight in zip(components, weights, strict=False)) / sum(weights)
    if open_interest is None or volume is None:
        score = min(score, 70.0)
    return round(score, 2)


def _convexity_score(required_move_pct: float | None, delta: float | None, dte: int | None) -> float | None:
    if required_move_pct is None:
        return None
    move_score = max(0.0, min(100.0, 100.0 - required_move_pct * 25.0))
    delta_score = 100.0 - min(100.0, abs((abs(delta or 0.30) - 0.30) * 180.0))
    dte_score = 100.0 if dte is None else max(0.0, min(100.0, (dte - 180) / 720 * 100.0))
    return round(move_score * 0.60 + delta_score * 0.25 + dte_score * 0.15, 2)


def _iv_history_by_ticker(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    history: dict[str, list[float]] = {}
    for row in rows:
        iv = _number(row.get("iv"))
        if iv is None:
            continue
        history.setdefault(_normalize_symbol(row.get("ticker")), []).append(iv)
    return history


def _percentile_rank(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    return round(sum(1 for item in history if item <= value) / len(history) * 100, 2)


def _iv_rank(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    low = min(history)
    high = max(history)
    if high == low:
        return 50.0
    return round((value - low) / (high - low) * 100, 2)


def _relative_strength(values: list[float | None], benchmark: list[float | None], period: int) -> float | None:
    clean = [value for value in values if value is not None]
    bench = [value for value in benchmark if value is not None]
    if len(clean) <= period or len(bench) <= period:
        return None
    stock_return = clean[-1] / clean[-period - 1] - 1
    benchmark_return = bench[-1] / bench[-period - 1] - 1
    return stock_return - benchmark_return


def _atr_pct(rows: list[dict[str, Any]], period: int = 14) -> float | None:
    if not rows:
        return None
    true_ranges: list[float] = []
    previous_close: float | None = None
    for row in rows[-period:]:
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close"))
        if high is None or low is None:
            continue
        values = [high - low]
        if previous_close is not None:
            values.extend([abs(high - previous_close), abs(low - previous_close)])
        true_ranges.append(max(values))
        previous_close = close
    close = _number(rows[-1].get("close"))
    if not true_ranges or close is None or close <= 0:
        return None
    return mean(true_ranges) / close


def _volume_ratio(volumes: list[float]) -> float | None:
    if len(volumes) < 20:
        return None
    recent = _average(volumes[-20:])
    baseline = _average(volumes[-60:]) if len(volumes) >= 60 else _average(volumes)
    if recent is None or baseline is None or baseline <= 0:
        return None
    return recent / baseline


def _base_length_days(closes: list[float], high_252: float) -> int | None:
    if not closes or high_252 <= 0:
        return None
    floor = high_252 * 0.75
    count = 0
    for close in reversed(closes):
        if close < floor:
            break
        count += 1
    return count


def _buy_under(row: dict[str, Any], strategy: dict[str, Any]) -> float | None:
    underlying = _number(row.get("underlying_price"))
    strike = _number(row.get("strike"))
    if underlying is None or strike is None:
        return None
    max_move = float(strategy["max_required_move_pct"])
    option_type = str(row.get("option_type") or "").lower()
    if option_type == "put":
        return max(0.0, (strike - underlying * (1 - max_move)) / 10)
    return max(0.0, (underlying * (1 + max_move) - strike) / 10)


def _candidate_score(row: dict[str, Any], state: str) -> float:
    if state == "REJECT":
        return 0.0
    required_move = _number(row.get("required_move_10x_pct")) or 10
    liquidity = _number(row.get("liquidity_score")) or 0
    convexity = _number(row.get("convexity_score")) or 0
    rs = _number(row.get("rs_vs_qqq_20d")) or 0
    technical = 100.0 if (_number(row.get("price")) or 0) >= (_number(row.get("ma_50")) or 10**9) else 45.0
    score = (max(0.0, 100.0 - required_move * 20.0) * 0.35) + (liquidity * 0.20) + (convexity * 0.30) + (technical * 0.10) + (max(-20.0, min(20.0, rs * 100)) + 20) * 0.05
    if state == "WATCH":
        score *= 0.70
    if state == "SETUP":
        score *= 0.88
    return round(max(0.0, min(100.0, score)), 2)


def _has_missing_data(blockers: list[str]) -> bool:
    return any(blocker.startswith("missing_") for blocker in blockers)


def _candidate_quality(row: dict[str, Any], *, state: str, blockers: list[str], hard_rejects: list[str]) -> dict[str, Any]:
    if state == "REJECT":
        return {"status": "ok", "flags": [], "peer": {}}

    flags: list[str] = []
    bad_flags: set[str] = set()
    raw = _json(row.get("raw"))
    greeks_source = str(raw.get("greeks_source") or "provider")
    data_source = str(row.get("data_source") or "unknown")
    peer_source = row.get("peer_data_source")
    peer: dict[str, Any] = {"source": peer_source} if peer_source else {}

    missing_flags = [blocker for blocker in blockers if blocker in {"missing_delta", "missing_spread", "missing_open_interest", "missing_volume", "missing_iv_percentile"}]
    if missing_flags:
        flags.extend(missing_flags)
        bad_flags.update(missing_flags)
    if "spread_above_fire_threshold" in blockers:
        flags.append("spread_above_threshold")
    if any(reject in hard_rejects for reject in {"spread_reject"}):
        flags.append("spread_reject")
        bad_flags.add("spread_reject")
    if state == "FIRE" and greeks_source in {"black_scholes_model", "mixed_fallback"}:
        flags.append("modeled_greeks")
    if state == "FIRE" and greeks_source == "mixed_fallback":
        flags.append("mixed_greek_sources")

    data_status = str(raw.get("market_data_type") or raw.get("data_status") or raw.get("entitlement_status") or "").lower()
    if "delayed" in data_status:
        flags.append("delayed_market_data")
    if "stale" in data_status:
        flags.append("stale_market_data")
        bad_flags.add("stale_market_data")

    mid = _number(row.get("mid"))
    peer_mid = _number(row.get("peer_mid"))
    mid_diff = _relative_diff(mid, peer_mid)
    if mid_diff is not None:
        peer["mid_relative_diff"] = round(mid_diff, 4)
        if mid_diff >= OPTION_QUALITY_MID_BAD_RELATIVE_DIFF:
            flags.append("source_mid_disagreement")
            bad_flags.add("source_mid_disagreement")
        elif mid_diff >= OPTION_QUALITY_MID_CAUTION_RELATIVE_DIFF:
            flags.append("source_mid_disagreement")

    iv = _number(row.get("iv"))
    peer_iv = _number(row.get("peer_iv"))
    iv_diff = _relative_diff(iv, peer_iv)
    if iv_diff is not None:
        peer["iv_relative_diff"] = round(iv_diff, 4)
        if iv_diff >= OPTION_QUALITY_IV_BAD_RELATIVE_DIFF:
            flags.append("source_iv_disagreement")
            bad_flags.add("source_iv_disagreement")
        elif iv_diff >= OPTION_QUALITY_IV_CAUTION_RELATIVE_DIFF:
            flags.append("source_iv_disagreement")

    delta = _number(row.get("delta"))
    peer_delta = _number(row.get("peer_delta"))
    if delta is not None and peer_delta is not None:
        delta_diff = abs(delta - peer_delta)
        peer["delta_absolute_diff"] = round(delta_diff, 4)
        if delta_diff >= OPTION_QUALITY_DELTA_BAD_ABSOLUTE_DIFF:
            flags.append("source_delta_disagreement")
            bad_flags.add("source_delta_disagreement")
        elif delta_diff >= OPTION_QUALITY_DELTA_CAUTION_ABSOLUTE_DIFF:
            flags.append("source_delta_disagreement")

    deduped_flags = list(dict.fromkeys(flags))
    if bad_flags:
        status = "bad"
    elif deduped_flags:
        status = "caution"
    else:
        status = "ok"
    return {
        "status": status,
        "flags": deduped_flags,
        "source": data_source,
        "greeks_source": greeks_source,
        "peer": peer,
    }


def _relative_diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    denominator = max(abs(left), abs(right))
    if denominator <= 0:
        return None
    return abs(left - right) / denominator


def _resolve_option_greeks(
    row: dict[str, Any],
    *,
    option_type: str,
    underlying_price: float | None,
    strike: float | None,
    dte: int | None,
    iv: float | None,
) -> dict[str, Any]:
    provider_values = {name: _number(row.get(name)) for name in ("delta", "gamma", "theta", "vega")}
    matched_values = {name: _number(row.get(f"tradingview_{name}")) for name in ("delta", "gamma", "theta", "vega")}
    if all(value is not None for value in provider_values.values()):
        return {**provider_values, "source": "provider"}

    resolved: dict[str, float | None] = {}
    used_match = False
    for name, value in provider_values.items():
        if value is not None:
            resolved[name] = value
            continue
        matched = matched_values[name]
        if matched is not None:
            resolved[name] = matched
            used_match = True
        else:
            resolved[name] = None

    used_model = False
    if any(value is None for value in resolved.values()):
        modeled_values = _black_scholes_greeks(option_type, underlying_price, strike, dte, iv)
        for name, value in resolved.items():
            if value is None and modeled_values.get(name) is not None:
                resolved[name] = modeled_values[name]
                used_model = True

    if used_match and used_model:
        greek_source = "mixed_fallback"
    elif used_model:
        greek_source = "black_scholes_model"
    elif used_match:
        greek_source = "tradingview_match"
    else:
        greek_source = "provider"
    return {**resolved, "source": greek_source}


def _black_scholes_greeks(
    option_type: str,
    spot: float | None,
    strike: float | None,
    dte: int | None,
    iv: float | None,
    *,
    risk_free_rate: float = DEFAULT_OPTION_RISK_FREE_RATE,
) -> dict[str, float]:
    if not option_type or spot is None or strike is None or dte is None or iv is None:
        return {}
    if option_type not in {"call", "put"} or spot <= 0 or strike <= 0 or dte < 0 or iv < 0:
        return {}
    if not all(math.isfinite(value) for value in (spot, strike, float(dte), iv, risk_free_rate)):
        return {}
    model_dte = _option_model_dte(dte)
    model_iv = _option_model_iv(iv)
    if model_dte is None or model_iv is None:
        return {}
    years = model_dte / 365.0
    sqrt_years = math.sqrt(years)
    sigma_sqrt_t = model_iv * sqrt_years
    if sigma_sqrt_t <= 0:
        return {}

    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * model_iv * model_iv) * years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    pdf = _norm_pdf(d1)
    discount = math.exp(-risk_free_rate * years)
    if option_type == "call":
        delta = _norm_cdf(d1)
        theta_annual = -((spot * pdf * model_iv) / (2 * sqrt_years)) - (risk_free_rate * strike * discount * _norm_cdf(d2))
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = -((spot * pdf * model_iv) / (2 * sqrt_years)) + (risk_free_rate * strike * discount * _norm_cdf(-d2))
    gamma = pdf / (spot * sigma_sqrt_t)
    theta = theta_annual / 365.0
    vega = spot * pdf * sqrt_years
    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 6),
        "theta": round(theta, 6),
        "vega": round(vega, 6),
    }


def _option_model_dte(dte: int | None) -> int | None:
    if dte is None or dte < 0:
        return None
    return max(dte, MIN_OPTION_MODEL_DTE_DAYS)


def _option_model_iv(iv: float | None) -> float | None:
    if iv is None or iv < 0:
        return None
    return max(iv, MIN_OPTION_MODEL_IV)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _norm_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _days_to_expiration(expiration: Any, snapshot_time: str) -> int | None:
    expiry_date = _date(expiration)
    snapshot_date = _date(snapshot_time)
    if expiry_date is None or snapshot_date is None:
        return None
    return (expiry_date - snapshot_date).days


def _elapsed_days(start: Any, end: Any) -> int | None:
    start_date = _date(start)
    end_date = _date(end)
    if start_date is None or end_date is None:
        return None
    return (end_date - start_date).days


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json_or_list(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, tuple):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return [value] if value else []
        if isinstance(decoded, list):
            return [str(item) for item in decoded if item]
    return []


def _coalesce_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _average(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return mean(clean)


def _median(values: list[float]) -> float | None:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    midpoint = len(clean) // 2
    if len(clean) % 2:
        return clean[midpoint]
    return (clean[midpoint - 1] + clean[midpoint]) / 2


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_symbol(value: Any) -> str:
    return str(value or "").upper().split(":")[-1]
