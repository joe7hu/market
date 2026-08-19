"""PostgreSQL writer and read model for daily point-in-time trend features."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.analysis.trend_features import FEATURE_VERSION, TrendFeature, compute_trend_feature
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars, completed_trading_dates
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


FEATURE_SET = "daily_trend"
MAX_TREND_INSTRUMENTS = 50


def refresh_symbol_trend_features(
    runtime: DatabaseRuntime,
    run_id: Any,
    *,
    as_of: datetime,
) -> dict[str, Any]:
    """Persist one reconstructable feature row for each current radar symbol and QQQ."""

    with runtime.read(JOB_PROFILE) as connection:
        instruments = [
            dict(row)
            for row in connection.execute(
                """
                WITH cutoffs AS (
                    SELECT contract.underlying_instrument_id AS instrument_id,
                           max(feature.quote_observed_at) AS symbol_as_of,
                           max(coalesce(feature.liquidity_score, 0)
                               + coalesce(feature.convexity_score, 0)) AS research_priority
                    FROM analysis.option_feature feature
                    JOIN catalog.option_contract contract ON contract.id = feature.contract_id
                    JOIN catalog.instrument candidate ON candidate.id = contract.underlying_instrument_id
                    WHERE feature.run_id = %s
                      AND candidate.symbol <> 'QQQ'
                    GROUP BY contract.underlying_instrument_id
                ), ranked AS (
                    SELECT cutoffs.*, count(*) OVER () + 1 AS universe_size,
                           row_number() OVER (
                             ORDER BY research_priority DESC, symbol_as_of DESC, instrument_id
                           ) AS universe_rank
                    FROM cutoffs
                ), selected AS (
                    SELECT * FROM ranked WHERE universe_rank < %s
                )
                SELECT instrument.id, instrument.symbol,
                       selected.symbol_as_of, selected.universe_size
                FROM selected JOIN catalog.instrument instrument ON instrument.id = selected.instrument_id
                UNION ALL
                SELECT benchmark.id, benchmark.symbol, %s AS symbol_as_of,
                       coalesce((SELECT max(universe_size) FROM ranked), 1) AS universe_size
                FROM catalog.instrument benchmark WHERE benchmark.symbol = 'QQQ'
                ORDER BY symbol
                """,
                [run_id, MAX_TREND_INSTRUMENTS, as_of],
            ).fetchall()
        ]
    universe_size = max((int(row.get("universe_size") or 0) for row in instruments), default=0)
    qqq = next((row for row in instruments if row["symbol"] == "QQQ"), None)
    features: list[tuple[dict[str, Any], TrendFeature, list[dict[str, Any]]]] = []
    with runtime.read(JOB_PROFILE) as connection:
        for symbol_cutoff in sorted({row["symbol_as_of"] for row in instruments}):
            group = [row for row in instruments if row["symbol_as_of"] == symbol_cutoff]
            ids = [int(row["id"]) for row in group]
            if qqq:
                ids.append(int(qqq["id"]))
            bars_by_instrument = confirmed_daily_bars(
                connection, ids, as_of=symbol_cutoff, max_bars=320
            )
            benchmark = bars_by_instrument.get(int(qqq["id"]), [])[-320:] if qqq else []
            for instrument in group:
                bars = bars_by_instrument.get(int(instrument["id"]), [])[-320:]
                feature = compute_trend_feature(
                    bars, benchmark, as_of_date=symbol_cutoff.date(),
                    expected_last_date=completed_trading_dates(symbol_cutoff, count=1)[0],
                    require_relative_strength=instrument["symbol"] != "QQQ",
                )
                features.append((instrument, feature, bars))

        market_qqq_bars = confirmed_daily_bars(
            connection, [int(qqq["id"])] if qqq else [], as_of=as_of, max_bars=320
        ).get(int(qqq["id"]), [])[-320:] if qqq else []
    market_qqq = compute_trend_feature(
        market_qqq_bars, market_qqq_bars, as_of_date=as_of.date(),
        expected_last_date=completed_trading_dates(as_of, count=1)[0],
        require_relative_strength=False,
    ) if qqq else None

    with runtime.transaction(JOB_PROFILE) as connection:
        for instrument, feature, bars in features:
            metrics = {
                "as_of_date": feature.as_of_date.isoformat() if feature.as_of_date else None,
                "atr_pct": feature.atr_pct,
                "realized_vol_20d": feature.realized_vol_20d,
                "realized_vol_60d": feature.realized_vol_60d,
                "realized_vol_percentile": feature.realized_vol_percentile,
                "bar_count": len(bars),
                "source_ids": sorted({str(row.get("source_id")) for row in bars if row.get("source_id")}),
                "point_in_time": True,
            }
            connection.execute(
                """
                INSERT INTO analysis.symbol_feature (
                    run_id, instrument_id, as_of, feature_set, feature_version,
                    price, ma_50, ma_200, momentum_5d, momentum_20d,
                    relative_strength_20d, relative_strength_60d,
                    kaufman_er_20d, kaufman_er_60d, kama_fast, kama_slow,
                    kama_fast_slope, kama_slow_slope, atr_pct,
                    trend_state, trend_confidence, volatility_state,
                    data_quality_status, reason_codes, metrics
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (run_id, instrument_id, feature_set, feature_version)
                DO UPDATE SET
                    as_of = EXCLUDED.as_of, price = EXCLUDED.price,
                    ma_50 = EXCLUDED.ma_50, ma_200 = EXCLUDED.ma_200,
                    momentum_5d = EXCLUDED.momentum_5d,
                    momentum_20d = EXCLUDED.momentum_20d,
                    relative_strength_20d = EXCLUDED.relative_strength_20d,
                    relative_strength_60d = EXCLUDED.relative_strength_60d,
                    kaufman_er_20d = EXCLUDED.kaufman_er_20d,
                    kaufman_er_60d = EXCLUDED.kaufman_er_60d,
                    kama_fast = EXCLUDED.kama_fast, kama_slow = EXCLUDED.kama_slow,
                    kama_fast_slope = EXCLUDED.kama_fast_slope,
                    kama_slow_slope = EXCLUDED.kama_slow_slope,
                    atr_pct = EXCLUDED.atr_pct, trend_state = EXCLUDED.trend_state,
                    trend_confidence = EXCLUDED.trend_confidence,
                    volatility_state = EXCLUDED.volatility_state,
                    data_quality_status = EXCLUDED.data_quality_status,
                    reason_codes = EXCLUDED.reason_codes, metrics = EXCLUDED.metrics
                """,
                [
                    run_id, instrument["id"], instrument["symbol_as_of"], FEATURE_SET, FEATURE_VERSION,
                    feature.price, feature.ma_50, feature.ma_200,
                    feature.momentum_5d, feature.momentum_20d,
                    feature.relative_strength_20d, feature.relative_strength_60d,
                    feature.kaufman_er_20d, feature.kaufman_er_60d,
                    feature.kama_fast, feature.kama_slow,
                    feature.kama_fast_slope, feature.kama_slow_slope, feature.atr_pct,
                    feature.trend_state, feature.trend_confidence,
                    feature.volatility_state, feature.data_quality_status,
                    list(feature.reason_codes), Jsonb(metrics),
                ],
            )
    return {
        "feature_count": len(features),
        "feature_version": FEATURE_VERSION,
        "universe_size": universe_size,
        "universe_budget": MAX_TREND_INSTRUMENTS,
        "universe_truncated": universe_size > MAX_TREND_INSTRUMENTS,
        "market_regime": market_regime_from_features(
            [(qqq, market_qqq, market_qqq_bars)]
            + [row for row in features if row[0]["symbol"] != "QQQ"]
            if qqq and market_qqq else features,
            as_of=as_of,
            universe_size=universe_size,
            universe_budget=MAX_TREND_INSTRUMENTS,
        ),
    }


def market_regime_from_features(
    features: list[tuple[dict[str, Any], TrendFeature, list[dict[str, Any]]]],
    *,
    as_of: datetime,
    universe_size: int | None = None,
    universe_budget: int | None = None,
) -> dict[str, Any]:
    qqq = next((feature for instrument, feature, _bars in features if instrument["symbol"] == "QQQ"), None)
    eligible = [
        feature
        for instrument, feature, _bars in features
        if instrument["symbol"] != "QQQ"
        and qqq is not None
        and feature.as_of_date == qqq.as_of_date
        and feature.trend_state != "unavailable"
    ]
    cutoff_excluded = sum(
        instrument["symbol"] != "QQQ"
        and (qqq is None or feature.as_of_date != qqq.as_of_date)
        for instrument, feature, _bars in features
    )
    unavailable_excluded = sum(
        instrument["symbol"] != "QQQ"
        and qqq is not None
        and feature.as_of_date == qqq.as_of_date
        and feature.trend_state == "unavailable"
        for instrument, feature, _bars in features
    )
    covered_non_benchmark = sum(instrument["symbol"] != "QQQ" for instrument, _feature, _bars in features)
    expected_non_benchmark = max(0, int(universe_size or (covered_non_benchmark + 1)) - 1)
    truncated_excluded = max(0, expected_non_benchmark - covered_non_benchmark)
    excluded_breadth = cutoff_excluded + unavailable_excluded + truncated_excluded
    up = sum(feature.trend_state == "trend_up" for feature in eligible)
    down = sum(feature.trend_state == "trend_down" for feature in eligible)
    denominator = len(eligible)
    up_fraction = up / denominator if denominator else None
    down_fraction = down / denominator if denominator else None
    breadth_state = (
        "unavailable"
        if denominator == 0
        else "bullish"
        if up_fraction is not None and up_fraction >= 0.55
        else "bearish"
        if down_fraction is not None and down_fraction >= 0.55
        else "mixed"
    )
    quality = (
        "complete"
        if qqq and qqq.trend_state != "unavailable" and denominator
        else "unavailable"
    )
    reasons: list[str] = []
    if not qqq or qqq.trend_state == "unavailable":
        reasons.append("qqq_trend_unavailable")
    if not denominator:
        reasons.append("breadth_unavailable")
    if excluded_breadth:
        reasons.append("breadth_daily_cutoff_partial")
    confidence = qqq.trend_confidence if qqq else 0.0
    breadth_total = denominator + excluded_breadth
    breadth_coverage = denominator / breadth_total if breadth_total else 0.0
    if excluded_breadth:
        confidence *= breadth_coverage
    if qqq and (
        (qqq.trend_state == "trend_up" and breadth_state == "bearish")
        or (qqq.trend_state == "trend_down" and breadth_state == "bullish")
    ):
        confidence *= 0.5
        reasons.append("qqq_breadth_conflict")
    return {
        "state": qqq.trend_state if qqq else "unavailable",
        "trend_state": qqq.trend_state if qqq else "unavailable",
        "trend_confidence": round(confidence, 4),
        "kaufman_er_20d": qqq.kaufman_er_20d if qqq else None,
        "volatility_state": qqq.volatility_state if qqq else "unstable",
        "breadth_state": breadth_state,
        "breadth_up_fraction": up_fraction,
        "breadth_down_fraction": down_fraction,
        "breadth_denominator": denominator,
        "breadth_excluded_stale": cutoff_excluded,
        "breadth_excluded_unavailable": unavailable_excluded,
        "breadth_excluded_truncated": truncated_excluded,
        "breadth_coverage": breadth_coverage,
        "breadth_quality_status": "partial" if excluded_breadth else "complete",
        "universe_size": int(universe_size or (covered_non_benchmark + 1)),
        "universe_budget": universe_budget,
        "universe_truncated": truncated_excluded > 0,
        "quality_status": quality,
        "reason_codes": reasons,
        "as_of": as_of.isoformat(),
        "feature_version": FEATURE_VERSION,
    }


def feature_row_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "trend_state": "unavailable",
            "trend_confidence": 0.0,
            "volatility_state": "unstable",
            "data_quality_status": "unavailable",
            "reason_codes": ["symbol_feature_unavailable"],
            "feature_version": FEATURE_VERSION,
        }
    payload = dict(row)
    payload["metrics"] = dict(payload.get("metrics") or {})
    payload["reason_codes"] = list(payload.get("reason_codes") or [])
    return payload
