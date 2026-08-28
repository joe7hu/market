"""PostgreSQL-native broad-market publication from confirmed daily prices."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import math
from statistics import mean, median
from typing import Any

from investment_panel.analysis.trend_features import realized_volatility
from investment_panel.core.decision import (
    CoverageMatrix,
    CoverageMatrixRow,
    InputLineage,
    MARKET_DIMENSIONS,
    MARKET_HORIZONS,
    MARKET_TZ,
    MarketDimensionState,
    MarketStateSnapshot,
    is_us_market_day,
)
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars, completed_trading_dates
from investment_panel.database.fundamental_history import hydrate_history
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


_HORIZON_LOOKBACK = {
    "intraday": None,
    "1-5 trading days": 5,
    "2-8 weeks": 40,
    "3-12 months": 252,
}
_MARKET_STALE_AFTER = timedelta(days=7)
_BENCHMARK_KEY = "market-equity-etf"
_CORPORATE_BENCHMARK_KEY = "market-corporate-equity"
_CORPORATE_SOURCE_ID = "sec_companyfacts"
_CORPORATE_METRIC_SET = "sec_companyfacts"
_CORPORATE_HORIZON = "3-12 months"
_CORPORATE_HORIZON_BLOCKERS = {
    "intraday": ("corporate_cycle_annual_facts_unsupported_for_intraday", "intraday_corporate_cycle_evidence"),
    "1-5 trading days": ("corporate_cycle_annual_facts_unsupported_for_1_5_trading_days", "1_5_day_corporate_cycle_evidence"),
    "2-8 weeks": ("corporate_cycle_annual_facts_unsupported_for_2_8_weeks", "2_8_week_corporate_cycle_evidence"),
}
_MAX_EVENT_RISK_SCHEDULE = 8
_UNSUPPORTED_DIMENSIONS = {
    "growth/inflation": ("growth_inflation_inputs_unavailable", "update_macro_series"),
    "monetary liquidity": ("monetary_liquidity_inputs_unavailable", "update_macro_series"),
    "rates": ("rates_inputs_unavailable", "update_macro_series"),
    "credit": ("credit_inputs_unavailable", "update_macro_series"),
    "dollar/commodities": ("dollar_commodities_inputs_unavailable", "update_macro_series"),
    "volatility": ("volatility_inputs_unavailable", "update_market_data"),
    "positioning": ("positioning_inputs_unavailable", "update_short_interest_and_borrow"),
    "corporate cycle": ("corporate_cycle_inputs_unavailable", "update_earnings_and_estimates"),
    "crypto liquidity": ("crypto_liquidity_inputs_unavailable", "update_macro_series"),
    "event risk": ("event_risk_inputs_unavailable", "update_market_events"),
}


def refresh_market_publication(runtime: DatabaseRuntime, *, now: datetime | None = None) -> dict[str, Any]:
    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("market publication timestamp must be timezone-aware")
    as_of = as_of.astimezone(UTC)
    with runtime.read() as connection:
        instrument_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, symbol, name, asset_class
                FROM catalog.instrument
                WHERE delisted_at IS NULL OR delisted_at > %s
                ORDER BY symbol
                """,
                [as_of],
            ).fetchall()
        ]
        bars_by_id = confirmed_daily_bars(
            connection,
            [int(row["id"]) for row in instrument_rows],
            as_of=as_of,
            max_bars=400,
        )
        price_rows: list[dict[str, Any]] = []
        metadata = {int(row["id"]): row for row in instrument_rows}
        for instrument_id, rows in bars_by_id.items():
            instrument = metadata.get(int(instrument_id))
            if instrument is None:
                continue
            for row in rows:
                price_rows.append({
                    **dict(row),
                    "instrument_id": int(instrument_id),
                    "symbol": instrument["symbol"],
                    "name": instrument["name"],
                    "asset_class": instrument["asset_class"],
                    "price": row["close"],
                })
        valuation_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT DISTINCT ON (observation.metric_set)
                       instrument.symbol, observation.period_end, observation.observed_at,
                       observation.values, observation.source_id, observation.metric_set,
                       ingest_run.id::text AS ingest_run_id,
                       ingest_run.finished_at AS available_at,
                       payload.archive_uri AS payload_archive_uri, payload.sha256 AS payload_sha256
                FROM raw.fundamental_observation observation
                JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
                JOIN ingest.run ingest_run ON ingest_run.id = observation.ingest_run_id
                JOIN ingest.source source
                  ON source.id = observation.source_id
                 AND source.enabled
                 AND source.operational_state = 'active'
                LEFT JOIN ingest.payload payload ON payload.id = observation.payload_id
                WHERE (observation.metric_set = 'market_valuation'
                   OR observation.metric_set LIKE 'market_valuation:%%')
                  AND observation.observed_at <= %s
                  AND (observation.filed_at IS NULL OR observation.filed_at <= %s)
                  AND ingest_run.status IN ('succeeded', 'partial')
                  AND ingest_run.finished_at IS NOT NULL
                  AND ingest_run.finished_at <= %s
                ORDER BY observation.metric_set, observation.observed_at DESC
                """,
                [as_of, as_of, as_of],
            ).fetchall()
        ]
        event_risk_evidence = _event_risk_evidence(connection, as_of)
        corporate_cycle_evidence = _corporate_cycle_evidence(connection, instrument_rows, as_of)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in price_rows:
        grouped[str(row["symbol"])].append(row)
    assets = [_asset_row(rows) for rows in grouped.values()]
    assets.sort(key=lambda row: (str(row["group_name"]), str(row["symbol"])))
    horizon_evidence = _horizon_evidence(instrument_rows, bars_by_id, as_of)
    volatility_evidence = _volatility_evidence(instrument_rows, bars_by_id, as_of)
    drivers = _driver_rows(assets, valuation_rows, horizon_evidence)
    references = [_valuation_reference(row) for row in valuation_rows]
    input_lineage = _market_lineage(price_rows, valuation_rows, as_of)
    snapshot = _market_snapshot(
        as_of, assets, drivers, input_lineage, horizon_evidence, event_risk_evidence,
        volatility_evidence, corporate_cycle_evidence,
    )
    coverage_rows = _coverage_rows(snapshot.coverage_matrix)
    analysis = AnalysisRepository(runtime)
    volatility_inputs = {
        horizon: {
            "available": evidence.get("available"),
            "blockers": evidence.get("blockers"),
            "counts": {
                key: evidence.get(key)
                for key in (
                    "eligible_member_count", "available_member_count", "missing_member_count",
                    "stale_member_count", "truncated_member_count", "duplicate_member_count",
                    "invalid_member_count",
                )
            },
            "return_window_trading_days": evidence.get("return_window_trading_days"),
            "minimum_history_trading_days": evidence.get("minimum_history_trading_days"),
            "realized_volatility": evidence.get("realized_volatility"),
            "lineage": [item.model_dump(mode="json") for item in evidence.get("lineage") or ()],
        }
        for horizon, evidence in volatility_evidence.items()
    }
    run_id = analysis.start_run(
        "market-environment",
        input_cutoff=as_of,
        code_version="postgres-market-v2",
        inputs={
            "price_bar_rows": len(price_rows),
            "symbols": sorted(grouped),
            "valuation_rows": len(valuation_rows),
            "source_lineage": [item.model_dump(mode="json") for item in input_lineage],
            "volatility_evidence": volatility_inputs,
            "corporate_cycle_evidence": _corporate_cycle_inputs(corporate_cycle_evidence),
        },
        feature_versions={"market_environment": "v2"},
    )
    publication_id = analysis.publish(
        run_id,
        "market",
        {
            "market_environment_assets": assets,
            "market_environment_model": drivers,
            "market_valuation_reference_charts": references,
            "market_state_snapshot": [snapshot.model_dump(mode="json")],
            "coverage_matrix": coverage_rows,
        },
        validation={"confirmed_daily_price_source": True, "quote_fallback": False, "asset_count": len(assets)},
        complete_run_summary={
            "assets": len(assets), "drivers": len(drivers), "valuation_series": len(references),
            "snapshot_id": snapshot.snapshot_id,
        },
    )
    # The market publication is consumed by the same-cycle ticker publication.
    # Its visibility timestamp must be the bounded input cutoff.
    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute(
            "UPDATE app.publication SET published_at = %s WHERE id = %s",
            [as_of, publication_id],
        )
    return {
        "status": "ok",
        "publication_id": str(publication_id),
        "assets": len(assets),
        "drivers": len(drivers),
        "valuation_series": len(references),
        "snapshot_id": snapshot.snapshot_id,
        "coverage_rows": len(coverage_rows),
        "available_coverage_rows": sum(1 for row in coverage_rows if row.get("current_status") == "available"),
        "unavailable_coverage_rows": sum(1 for row in coverage_rows if row.get("current_status") != "available"),
    }


def _asset_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (row.get("trading_date"), row.get("observed_at")))
    latest = ordered[-1]
    prices = [float(row["price"]) for row in ordered if row.get("price") is not None]
    latest_price = prices[-1] if prices else None
    return {
        "stable_key": str(latest["symbol"]),
        "instrument_id": latest["instrument_id"],
        "group_name": _group(str(latest["symbol"])),
        "symbol": latest["symbol"],
        "name": latest["name"],
        "asset_class": latest["asset_class"],
        "as_of": latest["observed_at"],
        "price": latest_price,
        "return_1d": _return(latest_price, prices[-2] if len(prices) >= 2 else None, latest.get("change_pct")),
        "return_1m": _period_return(prices, 21),
        "return_1y": _period_return(prices, 252),
        "return_ytd": _ytd_return(ordered),
        "sma_20_up": _above_average(prices, 20),
        "sma_50_up": _above_average(prices, 50),
        "sma_200_up": _above_average(prices, 200),
        "sma_50_gt_200": _average(prices, 50) > _average(prices, 200) if len(prices) >= 200 else None,
        "source": latest["source_id"],
    }


def _driver_rows(
    assets: list[dict[str, Any]],
    valuation_rows: list[dict[str, Any]] | None = None,
    horizon_evidence: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    del assets, valuation_rows
    horizon_evidence = horizon_evidence or {}
    short = horizon_evidence.get("1-5 trading days", {})
    trend_score = short.get("trend_score") if short.get("available") else None
    breadth_score = short.get("breadth_score") if short.get("available") else None
    risk_score = short.get("risk_score") if short.get("available") else None
    horizon_coverage = {
        horizon: {
            key: value
            for key, value in evidence.items()
            if key not in {"lineage", "valid_rows"}
        }
        for horizon, evidence in horizon_evidence.items()
    }
    blockers = _horizon_blockers(short)
    values = (
        ("Valuation", None, 0.20, "Valuation references are context only; no market-state driver is derived.", ("valuation_driver_context_only",), None),
        ("Price Trend", trend_score, 0.30, _trend_evidence(short), blockers, "confirmed_daily_prices" if trend_score is not None else None),
        ("Market Breadth", breadth_score, 0.30, _breadth_evidence(short), blockers, "confirmed_daily_prices" if breadth_score is not None else None),
        ("Risk Appetite", risk_score, 0.20, "Composite of same-cutoff price trend and breadth." if risk_score is not None else "Risk appetite is unavailable until the same-cutoff price coverage is complete.", blockers, "confirmed_daily_prices" if risk_score is not None else None),
    )
    return [
        {
            "stable_key": category.lower().replace(" ", "_"),
            "category": category,
            "score": round(score, 2) if score is not None else None,
            "weight": weight,
            "posture": (
                "constructive" if score >= 70 else "mixed" if score >= 45 else "defensive"
            ) if score is not None else "unavailable",
            "evidence": evidence,
            "current_status": "available" if score is not None else "unavailable",
            "blockers": list(row_blockers) if score is None else [],
            "source": source,
            "score_horizon": "1-5 trading days" if score is not None else None,
            "horizon_coverage": horizon_coverage,
        }
        for category, score, weight, evidence, row_blockers, source in values
    ]


def _valuation_reference(row: dict[str, Any]) -> dict[str, Any]:
    values = hydrate_history(
        dict(row.get("values") or {}),
        archive_uri=row.get("payload_archive_uri"),
        archive_sha256=row.get("payload_sha256"),
    )
    metric = str(values.get("metric") or row["symbol"])
    return {
        "stable_key": metric,
        "metric": metric,
        "label": values.get("label") or metric.replace("_", " ").title(),
        "latest_value": values.get("latest_value") or values.get("value"),
        "latest_date": row.get("period_end") or row.get("observed_at"),
        "percentile": values.get("percentile"),
        "suffix": values.get("suffix") or "",
        "posture": values.get("posture") or "mixed",
        "higher_is_better": bool(values.get("higher_is_better")),
        "history": values.get("history") or [],
        "history_data_health": values.get("history_data_health"),
        "source": row.get("source_id"),
    }


def _market_lineage(
    price_rows: list[dict[str, Any]],
    valuation_rows: list[dict[str, Any]] | None,
    cutoff: datetime,
) -> tuple[InputLineage, ...]:
    del valuation_rows
    result: dict[tuple[Any, ...], InputLineage] = {}
    for row in price_rows:
        observed_at = _as_utc(row.get("observed_at"))
        available_at = _as_utc(row.get("available_at"))
        if available_at is None:
            continue
        source_id = str(row.get("source_id") or "unknown")
        fact_table = str(row.get("fact_table") or "raw.price_bar")
        fact_id = row.get("fact_id")
        fact_identity = (
            f"{fact_table}:{fact_id}:{available_at.isoformat()}"
            if fact_id is not None
            else f"{fact_table}:{row.get('instrument_id')}:{row.get('trading_date')}:{available_at.isoformat()}"
        )
        lineage = InputLineage(
            field="market_daily_price",
            source_id=source_id,
            source_version=str(row.get("ingest_run_id") or fact_identity),
            event_at=observed_at,
            available_at=available_at,
            received_at=_as_utc(row.get("confirmed_at")),
            revision=fact_identity,
            cutoff=_as_utc(cutoff),
            fact_id=int(fact_id) if fact_id is not None else None,
            fact_table=fact_table,
            trading_date=row.get("trading_date"),
        )
        result[(lineage.field, lineage.source_id, lineage.revision)] = lineage
    return tuple(sorted(result.values(), key=lambda item: (item.field, item.source_id, item.available_at, item.revision or "")))


def _horizon_evidence(
    instrument_rows: list[dict[str, Any]],
    bars_by_id: dict[int, list[dict[str, Any]]],
    cutoff: datetime,
) -> dict[str, dict[str, Any]]:
    benchmark = sorted(
        (row for row in instrument_rows if str(row.get("asset_class") or "").lower() in {"equity", "etf"}),
        key=lambda row: str(row.get("symbol") or ""),
    )
    result: dict[str, dict[str, Any]] = {}
    for horizon, lookback in _HORIZON_LOOKBACK.items():
        if lookback is None:
            result[horizon] = {
                "available": False,
                "status": "unavailable",
                "blockers": ["intraday_evidence_unavailable_from_daily_bars"],
                "data_requests": ["intraday_spread_depth_execution_data"],
                "eligible_member_count": len(benchmark),
                "available_member_count": 0,
                "missing_member_count": 0,
                "stale_member_count": 0,
                "truncated_member_count": 0,
                "expected_trading_days": 0,
                "minimum_history_trading_days": 0,
                "lineage": (),
                "valid_rows": (),
            }
            continue
        expected = completed_trading_dates(cutoff, count=lookback)
        valid_rows: list[dict[str, Any]] = []
        missing = stale = truncated = 0
        member_returns: list[float] = []
        for member in benchmark:
            rows = list(bars_by_id.get(int(member["id"]), ()))
            by_date = {
                row["trading_date"]: row
                for row in rows
                if row.get("trading_date") in expected
            }
            if not by_date:
                missing += 1
                continue
            latest_available = max(
                (_as_utc(row.get("available_at")) for row in by_date.values() if row.get("available_at") is not None),
                default=None,
            )
            if latest_available is not None and cutoff - latest_available > _MARKET_STALE_AFTER:
                stale += 1
                continue
            if len(by_date) != len(expected) or set(by_date) != set(expected):
                truncated += 1
                continue
            selected = [by_date[trading_date] for trading_date in expected]
            valid_rows.extend(selected)
            latest = _number(selected[0].get("close"))
            baseline = _number(selected[-1].get("close"))
            if latest is not None and baseline:
                member_returns.append((latest / baseline - 1) * 100)
        available = bool(benchmark) and not (missing or stale or truncated) and len(member_returns) == len(benchmark)
        blockers: list[str] = []
        if missing:
            blockers.append("market_daily_history_missing")
        if stale:
            blockers.append("market_daily_history_stale")
        if truncated:
            blockers.append("market_daily_history_truncated")
        if not benchmark:
            blockers.append("market_equity_etf_benchmark_unavailable")
        lineages = _market_lineage(valid_rows, None, cutoff)
        result[horizon] = {
            "available": available,
            "status": "available" if available else "unavailable",
            "blockers": blockers,
            "data_requests": ["confirmed_daily_price_history"],
            "eligible_member_count": len(benchmark),
            "eligible_members": [str(row["symbol"]) for row in benchmark],
            "available_member_count": len(member_returns),
            "missing_member_count": missing,
            "stale_member_count": stale,
            "truncated_member_count": truncated,
            "expected_trading_days": lookback,
            "minimum_history_trading_days": lookback,
            "history_start": expected[-1] if expected else None,
            "freshness_max_age_days": _MARKET_STALE_AFTER.days,
            "freshness_latest_available_at": max(
                (_as_utc(row.get("available_at")) for row in valid_rows if row.get("available_at") is not None),
                default=None,
            ),
            "trend_score": round(max(0, min(100, 50 + 10 * mean(member_returns))), 2) if available and member_returns else None,
            "breadth_score": round(100 * sum(value > 0 for value in member_returns) / len(member_returns), 2) if available and member_returns else None,
            "risk_score": round(max(0, min(100, (50 + 10 * mean(member_returns) + 100 * sum(value > 0 for value in member_returns) / len(member_returns)) / 2)), 2) if available and member_returns else None,
            "lineage": lineages if available else (),
            "valid_rows": tuple(valid_rows) if available else (),
            "benchmark_key": _BENCHMARK_KEY,
        }
    return result


def _volatility_evidence(
    instrument_rows: list[dict[str, Any]],
    bars_by_id: dict[int, list[dict[str, Any]]],
    cutoff: datetime,
) -> dict[str, dict[str, Any]]:
    benchmark = sorted(
        (row for row in instrument_rows if str(row.get("asset_class") or "").lower() in {"equity", "etf"}),
        key=lambda row: str(row.get("symbol") or ""),
    )
    result: dict[str, dict[str, Any]] = {}
    for horizon, return_window in _HORIZON_LOOKBACK.items():
        if return_window is None:
            result[horizon] = {
                "available": False,
                "status": "unavailable",
                "blockers": ["intraday_evidence_unavailable_from_daily_bars"],
                "data_requests": ["intraday_spread_depth_execution_data"],
                "eligible_member_count": len(benchmark),
                "available_member_count": 0,
                "missing_member_count": 0,
                "stale_member_count": 0,
                "truncated_member_count": 0,
                "duplicate_member_count": 0,
                "invalid_member_count": 0,
                "expected_trading_days": 0,
                "return_window_trading_days": 0,
                "minimum_history_trading_days": 0,
                "annualization_factor": 252,
                "lineage": (),
                "valid_rows": (),
                "benchmark_key": _BENCHMARK_KEY,
            }
            continue
        expected = completed_trading_dates(cutoff, count=return_window + 1)
        valid_rows: list[dict[str, Any]] = []
        member_volatilities: list[float] = []
        missing = stale = truncated = duplicate = invalid = 0
        for member in benchmark:
            rows = [
                row for row in bars_by_id.get(int(member["id"]), ())
                if row.get("trading_date") in expected
            ]
            dates = [row.get("trading_date") for row in rows]
            if not rows:
                missing += 1
                continue
            if len(dates) != len(set(dates)):
                duplicate += 1
                continue
            by_date = {row["trading_date"]: row for row in rows}
            if len(by_date) != len(expected) or set(by_date) != set(expected):
                truncated += 1
                continue
            selected = [by_date[trading_date] for trading_date in expected]
            available_at = [_as_utc(row.get("available_at")) for row in selected]
            observed_at = [_as_utc(row.get("observed_at")) for row in selected]
            if any(value is None or value > cutoff for value in observed_at):
                invalid += 1
                continue
            if any(value is None or value > cutoff for value in available_at):
                invalid += 1
                continue
            if cutoff - available_at[0] > _MARKET_STALE_AFTER:
                stale += 1
                continue
            closes = [_number(row.get("close")) for row in selected]
            if any(value is None or value <= 0 or not math.isfinite(value) for value in closes):
                invalid += 1
                continue
            volatility = realized_volatility(closes, return_window)
            if volatility is None or not math.isfinite(volatility):
                invalid += 1
                continue
            valid_rows.extend(selected)
            member_volatilities.append(volatility)
        available = bool(benchmark) and not (missing or stale or truncated or duplicate or invalid)
        if len(member_volatilities) != len(benchmark):
            available = False
        blockers: list[str] = []
        if missing:
            blockers.append("market_daily_history_missing")
        if stale:
            blockers.append("market_daily_history_stale")
        if truncated:
            blockers.append("market_daily_history_truncated")
        if duplicate:
            blockers.append("market_daily_history_duplicate")
        if invalid:
            blockers.append("market_daily_history_invalid")
        if not benchmark:
            blockers.append("market_equity_etf_benchmark_unavailable")
        lineages = _market_lineage(valid_rows, None, cutoff)
        result[horizon] = {
            "available": available,
            "status": "available" if available else "unavailable",
            "blockers": blockers,
            "data_requests": ["confirmed_daily_price_history"],
            "eligible_member_count": len(benchmark),
            "eligible_members": [str(row["symbol"]) for row in benchmark],
            "available_member_count": len(member_volatilities),
            "missing_member_count": missing,
            "stale_member_count": stale,
            "truncated_member_count": truncated,
            "duplicate_member_count": duplicate,
            "invalid_member_count": invalid,
            "expected_trading_days": return_window,
            "return_window_trading_days": return_window,
            "minimum_history_trading_days": return_window + 1,
            "history_start": expected[-1] if expected else None,
            "freshness_max_age_days": _MARKET_STALE_AFTER.days,
            "freshness_latest_available_at": max(
                (_as_utc(row.get("available_at")) for row in valid_rows), default=None
            ),
            "realized_volatility": mean(member_volatilities) if available else None,
            "annualization_factor": 252,
            "lineage": lineages if available else (),
            "valid_rows": tuple(valid_rows) if available else (),
            "benchmark_key": _BENCHMARK_KEY,
        }
    return result


def _corporate_cycle_evidence(
    connection: Any,
    instrument_rows: list[dict[str, Any]],
    cutoff: datetime,
) -> dict[str, Any]:
    benchmark = sorted(
        (row for row in instrument_rows if str(row.get("asset_class") or "").lower() == "equity"),
        key=lambda row: str(row.get("symbol") or ""),
    )
    base = {
        "available": False,
        "status": "unavailable",
        "benchmark_key": _CORPORATE_BENCHMARK_KEY,
        "eligible_members": [str(row["symbol"]) for row in benchmark],
        "eligible_member_count": len(benchmark),
        "available_member_count": 0,
        "missing_member_count": 0,
        "stale_member_count": 0,
        "duplicate_member_count": 0,
        "invalid_member_count": 0,
        "median_revenue_growth": None,
        "median_operating_margin_change_bps": None,
        "selected_periods": (),
        "lineage": (),
        "blockers": [],
        "data_requests": ["update_company_financials"],
    }
    if not benchmark:
        base["blockers"] = ["market_corporate_equity_benchmark_unavailable"]
        base["missing_member_count"] = 0
        return base

    source = connection.execute(
        """
        SELECT enabled, operational_state, freshness_seconds
        FROM ingest.source
        WHERE id = %s
        """,
        [_CORPORATE_SOURCE_ID],
    ).fetchone()
    if source is None:
        base["blockers"] = ["corporate_cycle_source_missing"]
        base["missing_member_count"] = len(benchmark)
        return base
    source = dict(source)
    if not source["enabled"] or source["operational_state"] != "active":
        base["blockers"] = ["corporate_cycle_source_unavailable"]
        base["invalid_member_count"] = len(benchmark)
        return base
    freshness_seconds = int(source.get("freshness_seconds") or 0)
    if freshness_seconds != 86400:
        base["blockers"] = ["corporate_cycle_source_lifecycle_mismatch"]
        base["invalid_member_count"] = len(benchmark)
        return base

    latest_run = connection.execute(
        """
        SELECT max(run.finished_at) AS finished_at
        FROM ingest.run run
        WHERE run.source_id = %s
          AND run.capability = 'company_financials'
          AND run.status IN ('succeeded', 'partial')
          AND run.finished_at IS NOT NULL
          AND run.finished_at <= %s
        """,
        [_CORPORATE_SOURCE_ID, cutoff],
    ).fetchone()
    latest_finished_at = _as_utc(latest_run["finished_at"]) if latest_run and latest_run["finished_at"] else None
    if latest_finished_at is None:
        base["blockers"] = ["corporate_cycle_source_run_unavailable"]
        base["invalid_member_count"] = len(benchmark)
        return base
    if cutoff - latest_finished_at > timedelta(seconds=freshness_seconds):
        base["blockers"] = ["corporate_cycle_source_run_stale"]
        base["stale_member_count"] = len(benchmark)
        return base

    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT observation.id, observation.instrument_id, instrument.symbol,
                   observation.metric_set, observation.period_start, observation.period_end,
                   observation.filed_at, observation.observed_at, observation.values,
                   run.id::text AS ingest_run_id, run.finished_at AS available_at
            FROM raw.fundamental_observation observation
            JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
            JOIN ingest.source source ON source.id = observation.source_id
            JOIN ingest.run run ON run.id = observation.ingest_run_id
            WHERE observation.instrument_id = ANY(%s)
              AND observation.source_id = %s
              AND observation.metric_set = %s
              AND observation.filed_at IS NOT NULL
              AND observation.filed_at <= %s
              AND observation.observed_at <= %s
              AND run.capability = 'company_financials'
              AND run.status IN ('succeeded', 'partial')
              AND run.finished_at IS NOT NULL
              AND run.finished_at <= %s
            ORDER BY observation.instrument_id, observation.period_end DESC,
                     observation.filed_at DESC, observation.observed_at DESC, observation.id DESC
            """,
            [[int(row["id"]) for row in benchmark], _CORPORATE_SOURCE_ID, _CORPORATE_METRIC_SET,
             cutoff, cutoff, cutoff],
        ).fetchall()
    ]
    by_member: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_member[int(row["instrument_id"])].append(row)

    valid_members: list[dict[str, Any]] = []
    counts = {key: 0 for key in ("missing", "stale", "duplicate", "invalid")}
    blockers: set[str] = set()
    for member in benchmark:
        annual_by_end: dict[date, list[dict[str, Any]]] = defaultdict(list)
        invalid_rows = 0
        for row in by_member.get(int(member["id"]), ()):
            values = row.get("values")
            if not isinstance(values, dict):
                invalid_rows += 1
                continue
            form = str(values.get("form") or "").strip()
            fiscal_period = str(values.get("fiscal_period") or "").strip().upper()
            if form not in {"10-K", "10-K/A"} or fiscal_period != "FY":
                continue
            period_end = _as_date(row.get("period_end"))
            fact = _corporate_fact(row, period_end)
            if period_end is None:
                invalid_rows += 1
                continue
            annual_by_end[period_end].append(fact)

        selected: list[dict[str, Any]] = []
        duplicate = False
        for period_end in sorted(annual_by_end, reverse=True):
            candidates = annual_by_end[period_end]
            candidates.sort(key=lambda item: (item["accepted_at"], item["observed_at"], int(item["id"])))
            newest = candidates[-1]
            newest_time = (newest["accepted_at"], newest["observed_at"])
            if sum((item["accepted_at"], item["observed_at"]) == newest_time for item in candidates) > 1:
                duplicate = True
            if len({item["accession_number"] for item in candidates}) < len(candidates):
                duplicate = True
            selected.append(newest)

        if not selected:
            bucket = "invalid" if invalid_rows else "missing"
            counts[bucket] += 1
            blockers.add(
                "corporate_cycle_annual_fact_invalid"
                if invalid_rows else "corporate_cycle_annual_pair_missing"
            )
            continue
        latest = selected[0]
        if latest["period_end"] > cutoff.date() or cutoff.date() - latest["period_end"] > timedelta(days=550):
            counts["stale"] += 1
            blockers.add("corporate_cycle_annual_pair_stale")
            continue
        if len(selected) < 2:
            counts["missing"] += 1
            blockers.add("corporate_cycle_annual_pair_missing")
            continue
        prior = selected[1]
        if any(
            fact["available_at"] is None
            or cutoff - _as_utc(fact["available_at"]) > timedelta(seconds=freshness_seconds)
            for fact in (latest, prior)
        ):
            counts["stale"] += 1
            blockers.add("corporate_cycle_annual_pair_stale")
            continue
        if duplicate:
            counts["duplicate"] += 1
            blockers.add("corporate_cycle_annual_pair_duplicate")
            continue
        if not latest["valid"] or not prior["valid"]:
            counts["invalid"] += 1
            blockers.add("corporate_cycle_annual_fact_invalid")
            continue
        if not 300 <= (latest["period_end"] - prior["period_end"]).days <= 430:
            counts["invalid"] += 1
            blockers.add("corporate_cycle_annual_periods_not_comparable")
            continue
        if latest["units"] != prior["units"]:
            counts["invalid"] += 1
            blockers.add("corporate_cycle_annual_units_incompatible")
            continue
        valid_members.append({"member": member, "latest": latest, "prior": prior})

    available = len(valid_members) == len(benchmark)
    if not available:
        base.update({
            "available_member_count": len(valid_members),
            "missing_member_count": counts["missing"],
            "stale_member_count": counts["stale"],
            "duplicate_member_count": counts["duplicate"],
            "invalid_member_count": counts["invalid"],
            "blockers": sorted(blockers),
        })
        return base

    selected_periods = tuple(
        {
            "symbol": str(item["member"]["symbol"]),
            "latest": {
                "start": item["latest"]["period_start"],
                "end": item["latest"]["period_end"],
                "accession_number": item["latest"]["accession_number"],
                "revenue": item["latest"]["revenue"],
                "operating_income": item["latest"]["operating_income"],
            },
            "prior": {
                "start": item["prior"]["period_start"],
                "end": item["prior"]["period_end"],
                "accession_number": item["prior"]["accession_number"],
                "revenue": item["prior"]["revenue"],
                "operating_income": item["prior"]["operating_income"],
            },
        }
        for item in valid_members
    )
    lineages = tuple(
        lineage
        for item in valid_members
        for lineage in (
            _corporate_lineage(item["latest"], cutoff),
            _corporate_lineage(item["prior"], cutoff),
        )
    )
    return {
        **base,
        "available": True,
        "status": "available",
        "available_member_count": len(valid_members),
        "missing_member_count": 0,
        "stale_member_count": 0,
        "duplicate_member_count": 0,
        "invalid_member_count": 0,
        "median_revenue_growth": median(item["latest"]["revenue"] / item["prior"]["revenue"] - 1 for item in valid_members),
        "median_operating_margin_change_bps": median(
            ((item["latest"]["operating_income"] / item["latest"]["revenue"])
             - (item["prior"]["operating_income"] / item["prior"]["revenue"])) * 10000
            for item in valid_members
        ),
        "latest_period_start": min(item["latest"]["period_start"] for item in valid_members),
        "latest_period_end": max(item["latest"]["period_end"] for item in valid_members),
        "prior_period_start": min(item["prior"]["period_start"] for item in valid_members),
        "prior_period_end": max(item["prior"]["period_end"] for item in valid_members),
        "selected_periods": selected_periods,
        "lineage": lineages,
        "blockers": [],
        "data_requests": [],
        "freshness_latest_available_at": latest_finished_at,
        "freshness_max_age_days": 1,
    }


def _corporate_fact(row: dict[str, Any], period_end: date | None) -> dict[str, Any]:
    values = row.get("values") if isinstance(row.get("values"), dict) else {}
    metrics = values.get("metrics") if isinstance(values.get("metrics"), dict) else {}
    tags = values.get("tags") if isinstance(values.get("tags"), dict) else {}
    revenue = _finite_number(metrics.get("revenue"))
    operating_income = _finite_number(metrics.get("operating_income"))
    period_start = _as_date(row.get("period_start"))
    units = tuple(
        str(((tags.get(metric) or {}).get("unit") or "")).strip()
        for metric in ("revenue", "operating_income")
    )
    accession_number = str(values.get("accession_number") or "").strip()
    accepted_at = _as_utc(row.get("filed_at"))
    observed_at = _as_utc(row.get("observed_at"))
    valid = (
        period_start is not None
        and period_end is not None
        and 300 <= (period_end - period_start).days <= 400
        and bool(accession_number)
        and accepted_at is not None
        and observed_at is not None
        and revenue is not None
        and revenue > 0
        and operating_income is not None
        and all(units)
    )
    return {
        **row,
        "period_start": period_start,
        "period_end": period_end,
        "accession_number": accession_number,
        "accepted_at": accepted_at,
        "observed_at": observed_at,
        "revenue": revenue,
        "operating_income": operating_income,
        "units": units,
        "valid": valid,
    }


def _corporate_lineage(fact: dict[str, Any], cutoff: datetime) -> InputLineage:
    fact_identity = (
        f"raw.fundamental_observation:{int(fact['id'])}:"
        f"{fact['accession_number']}:{fact['period_end']}:{fact['observed_at'].isoformat()}"
    )
    return InputLineage(
        field="sec_annual_revenue_operating_income",
        source_id=_CORPORATE_SOURCE_ID,
        source_version=str(fact["ingest_run_id"]),
        published_at=fact["accepted_at"],
        available_at=_as_utc(fact["available_at"]),
        received_at=fact["accepted_at"],
        revision=fact_identity,
        cutoff=cutoff,
        fact_id=int(fact["id"]),
        fact_table="raw.fundamental_observation",
        metric_set=_CORPORATE_METRIC_SET,
        accession_number=fact["accession_number"],
        form=str((fact.get("values") or {}).get("form") or ""),
        fiscal_period=str((fact.get("values") or {}).get("fiscal_period") or ""),
        period_start=fact["period_start"],
        period_end=fact["period_end"],
        accepted_at=fact["accepted_at"],
        filed_at=fact.get("filed_at"),
        run_finished_at=_as_utc(fact["available_at"]),
        revision_identity=fact_identity,
        units=fact["units"],
    )


def _corporate_cycle_inputs(evidence: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value for key, value in evidence.items()
        if key not in {"lineage", "valid_rows"}
    }
    result["lineage"] = [item.model_dump(mode="json") for item in evidence.get("lineage") or ()]
    return json.loads(json_dumps(result))


def _event_risk_evidence(connection: Any, cutoff: datetime) -> dict[str, dict[str, Any]]:
    rows = [
        dict(row)
        for row in connection.execute(
            """
            WITH visible AS (
                SELECT DISTINCT ON (version.market_event_id)
                       version.id AS market_event_version_id,
                       version.market_event_id,
                       version.source_id,
                       version.ingest_run_id,
                       version.event_scope,
                       version.event_kind,
                       version.title,
                       version.starts_at,
                       version.available_at,
                       version.verification_status,
                       ingestion.finished_at AS ingest_finished_at,
                       source.enabled,
                       source.operational_state,
                       source.freshness_seconds
                FROM raw.market_event_version version
                JOIN ingest.run ingestion ON ingestion.id = version.ingest_run_id
                JOIN ingest.source source ON source.id = version.source_id
                WHERE version.source_id = 'official-event-calendar'
                  AND version.available_at <= %s
                  AND ingestion.status IN ('succeeded', 'partial')
                  AND ingestion.finished_at IS NOT NULL
                  AND ingestion.finished_at <= %s
                ORDER BY version.market_event_id, version.available_at DESC, version.id DESC
            )
            SELECT *
            FROM visible
            WHERE enabled
              AND operational_state = 'active'
              AND event_scope = 'macro'
              AND starts_at > %s
              AND btrim(title) <> ''
              AND btrim(event_kind) <> ''
              AND lower(btrim(coalesce(verification_status, ''))) IN ('confirmed', 'verified', 'scheduled')
              AND freshness_seconds > 0
              AND ingest_finished_at >= %s - make_interval(secs => freshness_seconds)
            ORDER BY starts_at, event_kind, title, market_event_id, market_event_version_id
            """,
            [cutoff, cutoff, cutoff, cutoff],
        ).fetchall()
    ]
    result = {
        horizon: {
            "available": False,
            "status": "unavailable",
            "blockers": ["event_risk_inputs_unavailable"],
            "data_requests": ["update_market_events"],
            "eligible_event_count": 0,
            "scheduled_events": (),
            "lineage": (),
        }
        for horizon in MARKET_HORIZONS
    }
    for row in rows:
        horizon = _event_risk_horizon(row["starts_at"], cutoff)
        if horizon is None:
            continue
        evidence = result[horizon]
        evidence["eligible_event_count"] += 1
        if len(evidence["scheduled_events"]) >= _MAX_EVENT_RISK_SCHEDULE:
            continue
        starts_at = _as_utc(row["starts_at"])
        available_at = _as_utc(row["available_at"])
        finished_at = _as_utc(row["ingest_finished_at"])
        if starts_at is None or available_at is None or finished_at is None:
            continue
        fact_identity = f"raw.market_event_version:{int(row['market_event_version_id'])}:{available_at.isoformat()}"
        lineage = InputLineage(
            field="market_event_schedule",
            source_id=str(row["source_id"]),
            source_version=str(row["ingest_run_id"]),
            event_at=starts_at,
            available_at=available_at,
            received_at=finished_at,
            revision=fact_identity,
            cutoff=cutoff,
            fact_id=int(row["market_event_version_id"]),
            fact_table="raw.market_event_version",
            market_event_id=int(row["market_event_id"]),
            ingest_run_id=str(row["ingest_run_id"]),
        )
        evidence["scheduled_events"] = (*evidence["scheduled_events"], {
            "title": str(row["title"]).strip(),
            "kind": str(row["event_kind"]).strip().lower(),
            "starts_at": starts_at.isoformat(),
        })
        evidence["lineage"] = (*evidence["lineage"], lineage)
        evidence["available"] = True
        evidence["status"] = "available"
        evidence["blockers"] = []
        evidence["data_requests"] = []
        evidence["freshness_max_age_seconds"] = int(row["freshness_seconds"])
    return result


def _event_risk_horizon(starts_at: datetime, cutoff: datetime) -> str | None:
    event_local = _as_utc(starts_at).astimezone(MARKET_TZ)
    cutoff_local = _as_utc(cutoff).astimezone(MARKET_TZ)
    if event_local.date() == cutoff_local.date() and is_us_market_day(event_local.date()):
        return "intraday"
    target_date = event_local.date()
    while not is_us_market_day(target_date):
        target_date += timedelta(days=1)
    cursor = cutoff_local.date() + timedelta(days=1)
    trading_days = 0
    while cursor <= target_date:
        if is_us_market_day(cursor):
            trading_days += 1
        cursor += timedelta(days=1)
    if trading_days <= 5:
        return "1-5 trading days"
    if trading_days <= 40:
        return "2-8 weeks"
    if trading_days > _HORIZON_LOOKBACK["3-12 months"]:
        return None
    return "3-12 months"


def _market_snapshot(
    cutoff: datetime,
    assets: list[dict[str, Any]],
    drivers: list[dict[str, Any]],
    lineage: tuple[InputLineage, ...],
    horizon_evidence: dict[str, dict[str, Any]] | None = None,
    event_risk_evidence: dict[str, dict[str, Any]] | None = None,
    volatility_evidence: dict[str, dict[str, Any]] | None = None,
    corporate_cycle_evidence: dict[str, Any] | None = None,
) -> MarketStateSnapshot:
    reference = cutoff.astimezone(UTC)
    del assets, drivers
    horizon_evidence = horizon_evidence or {}
    event_risk_evidence = event_risk_evidence or {}
    volatility_evidence = volatility_evidence or {}
    corporate_cycle_evidence = corporate_cycle_evidence or {}
    dimensions: dict[str, tuple[MarketDimensionState, ...]] = {}
    for horizon in MARKET_HORIZONS:
        evidence = horizon_evidence.get(horizon, {})
        event_evidence = event_risk_evidence.get(horizon, {})
        horizon_rows: list[MarketDimensionState] = []
        for dimension in MARKET_DIMENSIONS:
            if dimension == "equity internals" and evidence.get("available"):
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    state=_posture(evidence.get("risk_score")),
                    change_drivers=(
                        f"{evidence['available_member_count']} of {evidence['eligible_member_count']} eligible benchmark members; "
                        f"{evidence['expected_trading_days']} exact trading days.",
                        f"trend={evidence.get('trend_score')}; breadth={evidence.get('breadth_score')}; "
                        f"missing={evidence['missing_member_count']}; stale={evidence['stale_member_count']}; "
                        f"truncated={evidence['truncated_member_count']}",
                    ),
                    evidence_status="available",
                    uncertainty="derived from confirmed daily price facts",
                    quality="measured",
                    lineage=tuple(evidence.get("lineage") or ()),
                    benchmark_key=evidence.get("benchmark_key"),
                    eligible_member_count=evidence.get("eligible_member_count"),
                    available_member_count=evidence.get("available_member_count"),
                    missing_member_count=evidence.get("missing_member_count"),
                    stale_member_count=evidence.get("stale_member_count"),
                    truncated_member_count=evidence.get("truncated_member_count"),
                    history_start=evidence.get("history_start"),
                    freshness_max_age_days=evidence.get("freshness_max_age_days"),
                ))
            elif dimension == "equity internals":
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    evidence_status="unavailable",
                    uncertainty="exact point-in-time benchmark history is incomplete or stale",
                    blockers=tuple(evidence.get("blockers") or ("market_horizon_history_unavailable",)),
                    data_requests=tuple(evidence.get("data_requests") or ("confirmed_daily_price_history",)),
                ))
            elif dimension == "volatility":
                volatility = volatility_evidence.get(horizon, {})
                volatility_fields = {
                    "benchmark_key": volatility.get("benchmark_key"),
                    "eligible_member_count": volatility.get("eligible_member_count"),
                    "available_member_count": volatility.get("available_member_count"),
                    "missing_member_count": volatility.get("missing_member_count"),
                    "stale_member_count": volatility.get("stale_member_count"),
                    "truncated_member_count": volatility.get("truncated_member_count"),
                    "duplicate_member_count": volatility.get("duplicate_member_count"),
                    "invalid_member_count": volatility.get("invalid_member_count"),
                    "return_window_trading_days": volatility.get("return_window_trading_days"),
                    "required_history_trading_days": volatility.get("minimum_history_trading_days"),
                    "annualization_factor": volatility.get("annualization_factor"),
                    "realized_volatility": volatility.get("realized_volatility"),
                    "history_start": volatility.get("history_start"),
                    "freshness_max_age_days": volatility.get("freshness_max_age_days"),
                }
                if volatility.get("available"):
                    value = float(volatility["realized_volatility"])
                    horizon_rows.append(MarketDimensionState(
                        dimension=dimension,
                        horizon=horizon,
                        state="realized historical volatility",
                        change_drivers=(
                            f"{value:.6f} annualized over {volatility['return_window_trading_days']} trading-day close-to-close log returns.",
                            "Population standard deviation annualized by sqrt(252); historical only, not implied volatility, a tail model, or a forward forecast.",
                            f"{volatility['available_member_count']} of {volatility['eligible_member_count']} eligible benchmark members.",
                        ),
                        evidence_status="available",
                        uncertainty="historical realized volatility does not measure implied volatility, tail or jump risk, or forward volatility",
                        quality="measured",
                        lineage=tuple(volatility.get("lineage") or ()),
                        data_requests=(),
                        **volatility_fields,
                    ))
                else:
                    horizon_rows.append(MarketDimensionState(
                        dimension=dimension,
                        horizon=horizon,
                        evidence_status="unavailable",
                        uncertainty="exact point-in-time daily-bar evidence for realized volatility is incomplete, stale, or invalid",
                        blockers=tuple(volatility.get("blockers") or ("volatility_inputs_unavailable",)),
                        data_requests=tuple(volatility.get("data_requests") or ("confirmed_daily_price_history",)),
                        **volatility_fields,
                    ))
            elif dimension == "corporate cycle":
                if horizon == _CORPORATE_HORIZON and corporate_cycle_evidence.get("available"):
                    revenue_growth = float(corporate_cycle_evidence["median_revenue_growth"])
                    margin_change = float(corporate_cycle_evidence["median_operating_margin_change_bps"])
                    horizon_rows.append(MarketDimensionState(
                        dimension=dimension,
                        horizon=horizon,
                        state="reported annual issuer actuals",
                        change_drivers=(
                            f"Median revenue growth {revenue_growth:.2%}; median operating-margin change {margin_change:.1f} basis points.",
                            f"Complete fixed benchmark denominator: {corporate_cycle_evidence['available_member_count']} of {corporate_cycle_evidence['eligible_member_count']} eligible issuers.",
                        ),
                        evidence_status="available",
                        uncertainty="descriptive filed actuals only; no estimates, guidance, probabilities, or trading authority",
                        quality="sec_companyfacts_annual_filed_actuals",
                        lineage=tuple(corporate_cycle_evidence.get("lineage") or ()),
                        benchmark_key=corporate_cycle_evidence.get("benchmark_key"),
                        eligible_member_count=corporate_cycle_evidence.get("eligible_member_count"),
                        available_member_count=corporate_cycle_evidence.get("available_member_count"),
                        missing_member_count=corporate_cycle_evidence.get("missing_member_count"),
                        stale_member_count=corporate_cycle_evidence.get("stale_member_count"),
                        duplicate_member_count=corporate_cycle_evidence.get("duplicate_member_count"),
                        invalid_member_count=corporate_cycle_evidence.get("invalid_member_count"),
                        median_revenue_growth=revenue_growth,
                        median_operating_margin_change_bps=margin_change,
                        latest_period_start=corporate_cycle_evidence.get("latest_period_start"),
                        latest_period_end=corporate_cycle_evidence.get("latest_period_end"),
                        prior_period_start=corporate_cycle_evidence.get("prior_period_start"),
                        prior_period_end=corporate_cycle_evidence.get("prior_period_end"),
                        selected_periods=corporate_cycle_evidence.get("selected_periods"),
                    ))
                else:
                    blockers, request = (
                        _CORPORATE_HORIZON_BLOCKERS[horizon]
                        if horizon != _CORPORATE_HORIZON
                        else (
                            tuple(corporate_cycle_evidence.get("blockers") or ("corporate_cycle_inputs_unavailable",)),
                            "update_company_financials",
                        )
                    )
                    horizon_rows.append(MarketDimensionState(
                        dimension=dimension,
                        horizon=horizon,
                        evidence_status="unavailable",
                        uncertainty="point-in-time annual issuer facts do not support this horizon",
                        blockers=(blockers,) if isinstance(blockers, str) else tuple(blockers),
                        data_requests=(request,),
                        benchmark_key=corporate_cycle_evidence.get("benchmark_key"),
                        eligible_member_count=corporate_cycle_evidence.get("eligible_member_count"),
                        available_member_count=corporate_cycle_evidence.get("available_member_count"),
                        missing_member_count=corporate_cycle_evidence.get("missing_member_count"),
                        stale_member_count=corporate_cycle_evidence.get("stale_member_count"),
                        duplicate_member_count=corporate_cycle_evidence.get("duplicate_member_count"),
                        invalid_member_count=corporate_cycle_evidence.get("invalid_member_count"),
                    ))
            elif dimension == "event risk" and event_evidence.get("available"):
                scheduled_events = tuple(event_evidence.get("scheduled_events") or ())
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    state="scheduled",
                    change_drivers=(
                        "Official macro event schedule only; no surprise, direction, magnitude, or probability.",
                        _event_schedule_text(scheduled_events),
                    ),
                    evidence_status="available",
                    uncertainty="schedule-only evidence does not measure event outcomes or market impact",
                    quality="official_schedule",
                    lineage=tuple(event_evidence.get("lineage") or ()),
                    scheduled_events=scheduled_events,
                ))
            elif dimension == "event risk":
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    evidence_status="unavailable",
                    uncertainty="cutoff-visible official future event schedule is unavailable",
                    blockers=tuple(event_evidence.get("blockers") or ("event_risk_inputs_unavailable",)),
                    data_requests=tuple(event_evidence.get("data_requests") or ("update_market_events",)),
                ))
            elif dimension == "microstructure":
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    evidence_status="unavailable",
                    uncertainty="real spread, depth, market impact, and expected execution-cost evidence is unavailable",
                    blockers=("microstructure_execution_evidence_unavailable",),
                    data_requests=("spread_depth_market_impact_expected_execution_cost",),
                ))
            else:
                blocker, request = _UNSUPPORTED_DIMENSIONS[dimension]
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    evidence_status="unavailable",
                    uncertainty="point-in-time evidence for this dimension is unavailable",
                    blockers=(blocker,),
                    data_requests=(request,),
                ))
        dimensions[horizon] = tuple(horizon_rows)
    measured_lineage = _lineage_union(
        tuple(evidence.get("lineage") or ())
        for evidence in (
            *horizon_evidence.values(), *volatility_evidence.values(), *event_risk_evidence.values(), corporate_cycle_evidence
        )
        if evidence.get("available")
    )
    encoded = json_dumps({
        "cutoff": reference.isoformat(),
        "lineage": [item.model_dump(mode="json") for item in measured_lineage],
        "dimensions": dimensions,
    })
    snapshot_id = f"market-state:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"
    rows = tuple(
        _coverage_row(
            horizon, dimension, reference, horizon_evidence, event_risk_evidence, volatility_evidence,
            corporate_cycle_evidence,
        )
        for horizon in MARKET_HORIZONS
        for dimension in MARKET_DIMENSIONS
    )
    all_daily_horizons_available = all(
        horizon_evidence.get(horizon, {}).get("available")
        for horizon in MARKET_HORIZONS
        if horizon != "intraday"
    )
    return MarketStateSnapshot(
        snapshot_id=snapshot_id,
        publication_id=f"market-publication:{snapshot_id}",
        as_of=reference,
        input_cutoff=reference,
        horizons=dimensions,
        coverage_matrix=CoverageMatrix(
            matrix_id=f"coverage:{snapshot_id}",
            as_of=reference,
            input_cutoff=reference,
            rows=rows,
        ),
        input_lineage=measured_lineage,
        availability="available" if all_daily_horizons_available else "unavailable",
        blockers=() if all_daily_horizons_available else ("market_horizon_coverage_unavailable",),
    )


def _coverage_row(
    horizon: str,
    dimension: str,
    cutoff: datetime,
    horizon_evidence: dict[str, dict[str, Any]],
    event_risk_evidence: dict[str, dict[str, Any]] | None = None,
    volatility_evidence: dict[str, dict[str, Any]] | None = None,
    corporate_cycle_evidence: dict[str, Any] | None = None,
) -> CoverageMatrixRow:
    evidence = horizon_evidence.get(horizon, {})
    event_evidence = (event_risk_evidence or {}).get(horizon, {})
    volatility = (volatility_evidence or {}).get(horizon, {})
    corporate = corporate_cycle_evidence or {}
    selected_evidence = (
        event_evidence if dimension == "event risk"
        else volatility if dimension == "volatility"
        else corporate if dimension == "corporate cycle"
        else evidence
    )
    available = (
        dimension == "equity internals" and bool(evidence.get("available"))
    ) or (
        dimension == "volatility" and bool(volatility.get("available"))
    ) or (
        dimension == "event risk" and bool(event_evidence.get("available"))
    ) or (
        dimension == "corporate cycle" and horizon == _CORPORATE_HORIZON and bool(corporate.get("available"))
    )
    lineage = tuple(selected_evidence.get("lineage") or ()) if available else ()
    provider = ",".join(sorted({item.source_id for item in lineage})) or None
    blockers = tuple(evidence.get("blockers") or ("market_horizon_history_unavailable",))
    if dimension == "event risk":
        blockers = tuple(event_evidence.get("blockers") or ("event_risk_inputs_unavailable",))
    elif dimension == "volatility":
        blockers = tuple(volatility.get("blockers") or ("volatility_inputs_unavailable",))
    elif dimension == "microstructure":
        blockers = ("microstructure_execution_evidence_unavailable",)
    elif dimension == "corporate cycle":
        blockers = (
            (_CORPORATE_HORIZON_BLOCKERS[horizon][0],)
            if horizon != _CORPORATE_HORIZON
            else tuple(corporate.get("blockers") or ("corporate_cycle_inputs_unavailable",))
        )
    elif dimension != "equity internals":
        blockers = (_UNSUPPORTED_DIMENSIONS[dimension][0],)
    if dimension in {"equity internals", "volatility"}:
        data_requests = (
            tuple(selected_evidence.get("data_requests") or ("confirmed_daily_price_history",))
            if not available else ()
        )
    elif dimension == "microstructure":
        data_requests = ("spread_depth_market_impact_expected_execution_cost",)
    elif dimension == "event risk":
        data_requests = tuple(event_evidence.get("data_requests") or ("update_market_events",)) if not available else ()
    elif dimension == "corporate cycle":
        data_requests = () if available else (
            _CORPORATE_HORIZON_BLOCKERS[horizon][1]
            if horizon != _CORPORATE_HORIZON else "update_company_financials",
        )
    else:
        data_requests = (_UNSUPPORTED_DIMENSIONS[dimension][1],)
    return CoverageMatrixRow(
        dimension=dimension,
        asset_class="cross-asset",
        horizon=horizon,
        provider=provider if available else None,
        history_start=selected_evidence.get("history_start")
        if dimension in {"equity internals", "volatility"} and available else None,
        point_in_time_safe=available,
        freshness_slo=(
            f"confirmed daily bars; available_at <= input_cutoff; max_age={evidence.get('freshness_max_age_days')}d"
            if dimension == "equity internals" and available else
            f"confirmed daily bars; available_at <= input_cutoff; max_age={volatility.get('freshness_max_age_days')}d"
            if dimension == "volatility" and available else
            f"official-event-calendar; available_at <= input_cutoff; max_age={event_evidence.get('freshness_max_age_seconds')}s"
            if dimension == "event risk" and available else
            f"sec_companyfacts; run_finished_at <= input_cutoff; max_age={corporate.get('freshness_max_age_days')}d"
            if dimension == "corporate cycle" and available else None
        ),
        current_status="available" if available else "unavailable",
        decision_impact="market_context" if dimension == "equity internals" and available else "context",
        fallback_policy="unavailable",
        input_cutoff=cutoff,
        input_lineage=lineage,
        blockers=blockers if not available else (),
        benchmark_key=selected_evidence.get("benchmark_key")
        if dimension in {"equity internals", "volatility", "corporate cycle"} else None,
        eligible_member_count=selected_evidence.get("eligible_member_count")
        if dimension in {"equity internals", "volatility", "corporate cycle"} else None,
        available_member_count=selected_evidence.get("available_member_count")
        if dimension in {"equity internals", "volatility", "corporate cycle"} else None,
        missing_member_count=selected_evidence.get("missing_member_count")
        if dimension in {"equity internals", "volatility", "corporate cycle"} else None,
        stale_member_count=selected_evidence.get("stale_member_count")
        if dimension in {"equity internals", "volatility", "corporate cycle"} else None,
        truncated_member_count=selected_evidence.get("truncated_member_count")
        if dimension in {"equity internals", "volatility"} else None,
        duplicate_member_count=selected_evidence.get("duplicate_member_count")
        if dimension in {"volatility", "corporate cycle"} else None,
        invalid_member_count=selected_evidence.get("invalid_member_count")
        if dimension in {"volatility", "corporate cycle"} else None,
        required_history_trading_days=selected_evidence.get("minimum_history_trading_days")
        if dimension in {"equity internals", "volatility"} else None,
        data_requests=data_requests,
        scheduled_events=(
            tuple(event_evidence.get("scheduled_events") or ())
            if dimension == "event risk" and available else ()
        ),
        realized_volatility=volatility.get("realized_volatility") if dimension == "volatility" else None,
        return_window_trading_days=volatility.get("return_window_trading_days") if dimension == "volatility" else None,
        annualization_factor=volatility.get("annualization_factor") if dimension == "volatility" else None,
        median_revenue_growth=corporate.get("median_revenue_growth")
        if dimension == "corporate cycle" and available else None,
        median_operating_margin_change_bps=corporate.get("median_operating_margin_change_bps")
        if dimension == "corporate cycle" and available else None,
        latest_period_start=corporate.get("latest_period_start")
        if dimension == "corporate cycle" and available else None,
        latest_period_end=corporate.get("latest_period_end")
        if dimension == "corporate cycle" and available else None,
        prior_period_start=corporate.get("prior_period_start")
        if dimension == "corporate cycle" and available else None,
        prior_period_end=corporate.get("prior_period_end")
        if dimension == "corporate cycle" and available else None,
        selected_periods=corporate.get("selected_periods")
        if dimension == "corporate cycle" and available else (),
    )


def _event_schedule_text(events: tuple[dict[str, Any], ...]) -> str:
    return "Nearest scheduled events: " + "; ".join(
        f"{event['title']} ({event['kind']}) at {event['starts_at']}" for event in events
    )


def _lineage_union(groups: Any) -> tuple[InputLineage, ...]:
    result: dict[tuple[Any, ...], InputLineage] = {}
    for group in groups:
        for item in group:
            result[(item.field, item.source_id, item.revision, item.available_at)] = item
    return tuple(sorted(result.values(), key=lambda item: (item.field, item.source_id, item.available_at, item.revision or "")))


def _horizon_blockers(evidence: dict[str, Any]) -> tuple[str, ...]:
    return tuple(evidence.get("blockers") or ("market_horizon_history_unavailable",))


def _trend_evidence(evidence: dict[str, Any]) -> str:
    if not evidence.get("available"):
        return f"Price trend unavailable: {', '.join(_horizon_blockers(evidence))}."
    return (
        f"Average {evidence['expected_trading_days']}-day return across "
        f"{evidence['available_member_count']} of {evidence['eligible_member_count']} eligible benchmark members."
    )


def _breadth_evidence(evidence: dict[str, Any]) -> str:
    if not evidence.get("available"):
        return f"Market breadth unavailable: {', '.join(_horizon_blockers(evidence))}."
    return (
        f"Positive {evidence['expected_trading_days']}-day returns use the same "
        f"{evidence['available_member_count']} eligible members and freshness rule."
    )


def _posture(score: Any) -> str | None:
    value = _number(score)
    if value is None:
        return None
    return "constructive" if value >= 70 else "mixed" if value >= 45 else "defensive"


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _coverage_rows(matrix: CoverageMatrix | None) -> list[dict[str, Any]]:
    if matrix is None:
        return []
    return [
        {
            "stable_key": f"{row.horizon}:{row.dimension}:{row.asset_class}",
            **row.model_dump(mode="json"),
        }
        for row in matrix.rows
    ]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _finite_number(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and math.isfinite(result) else None


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _group(symbol: str) -> str:
    if symbol in {"SPY", "QQQ", "DIA", "IWM", "VTI"}:
        return "Market"
    if symbol in {"TLT", "IEF", "GLD", "SLV", "UUP", "BTC-USD", "ETH-USD"}:
        return "Macro"
    if symbol.startswith("XL"):
        return "Sectors"
    return "Others"


def _return(latest: float | None, previous: float | None, fallback: Any = None) -> float | None:
    if latest is not None and previous:
        return (latest / previous - 1) * 100
    try:
        return float(fallback) if fallback is not None else None
    except (TypeError, ValueError):
        return None


def _period_return(prices: list[float], observations: int) -> float | None:
    if len(prices) < 2:
        return None
    index = max(0, len(prices) - 1 - max(1, observations))
    return _return(prices[-1], prices[index])


def _ytd_return(rows: list[dict[str, Any]]) -> float | None:
    priced = [row for row in rows if row.get("price") is not None and row.get("observed_at") is not None]
    if len(priced) < 2:
        return None
    latest = priced[-1]
    latest_at = latest["observed_at"]
    latest_year = latest_at.year
    prior = [row for row in priced if row["observed_at"].year < latest_year]
    baseline = prior[-1] if prior else next(
        (row for row in priced if row["observed_at"].year == latest_year),
        None,
    )
    if baseline is None or baseline is latest:
        return None
    return _return(float(latest["price"]), float(baseline["price"]))


def _average(prices: list[float], window: int) -> float:
    return mean(prices[-window:]) if prices else 0


def _above_average(prices: list[float], window: int) -> bool | None:
    return prices[-1] >= _average(prices, window) if len(prices) >= window else None
