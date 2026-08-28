"""PostgreSQL-native broad-market publication from confirmed daily prices."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from statistics import mean
from typing import Any

from investment_panel.core.decision import (
    CoverageMatrix,
    CoverageMatrixRow,
    InputLineage,
    MARKET_DIMENSIONS,
    MARKET_HORIZONS,
    MarketDimensionState,
    MarketStateSnapshot,
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
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in price_rows:
        grouped[str(row["symbol"])].append(row)
    assets = [_asset_row(rows) for rows in grouped.values()]
    assets.sort(key=lambda row: (str(row["group_name"]), str(row["symbol"])))
    horizon_evidence = _horizon_evidence(instrument_rows, bars_by_id, as_of)
    drivers = _driver_rows(assets, valuation_rows, horizon_evidence)
    references = [_valuation_reference(row) for row in valuation_rows]
    input_lineage = _market_lineage(price_rows, valuation_rows, as_of)
    snapshot = _market_snapshot(as_of, assets, drivers, input_lineage, horizon_evidence)
    coverage_rows = _coverage_rows(snapshot.coverage_matrix)
    analysis = AnalysisRepository(runtime)
    run_id = analysis.start_run(
        "market-environment",
        input_cutoff=as_of,
        code_version="postgres-market-v2",
        inputs={
            "price_bar_rows": len(price_rows),
            "symbols": sorted(grouped),
            "valuation_rows": len(valuation_rows),
            "source_lineage": [item.model_dump(mode="json") for item in input_lineage],
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


def _market_snapshot(
    cutoff: datetime,
    assets: list[dict[str, Any]],
    drivers: list[dict[str, Any]],
    lineage: tuple[InputLineage, ...],
    horizon_evidence: dict[str, dict[str, Any]] | None = None,
) -> MarketStateSnapshot:
    reference = cutoff.astimezone(UTC)
    del assets, drivers
    horizon_evidence = horizon_evidence or {}
    dimensions: dict[str, tuple[MarketDimensionState, ...]] = {}
    for horizon in MARKET_HORIZONS:
        evidence = horizon_evidence.get(horizon, {})
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
        for evidence in horizon_evidence.values()
        if evidence.get("available")
    )
    encoded = json_dumps({
        "cutoff": reference.isoformat(),
        "lineage": [item.model_dump(mode="json") for item in measured_lineage],
        "dimensions": dimensions,
    })
    snapshot_id = f"market-state:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"
    rows = tuple(
        _coverage_row(horizon, dimension, reference, horizon_evidence)
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
) -> CoverageMatrixRow:
    evidence = horizon_evidence.get(horizon, {})
    available = dimension == "equity internals" and bool(evidence.get("available"))
    lineage = tuple(evidence.get("lineage") or ()) if available else ()
    provider = ",".join(sorted({item.source_id for item in lineage})) or None
    blockers = tuple(evidence.get("blockers") or ("market_horizon_history_unavailable",))
    if dimension == "microstructure":
        blockers = ("microstructure_execution_evidence_unavailable",)
    elif dimension != "equity internals":
        blockers = (_UNSUPPORTED_DIMENSIONS[dimension][0],)
    return CoverageMatrixRow(
        dimension=dimension,
        asset_class="cross-asset",
        horizon=horizon,
        provider=provider if available else None,
        history_start=evidence.get("history_start") if available else None,
        point_in_time_safe=available,
        freshness_slo=(
            f"confirmed daily bars; available_at <= input_cutoff; max_age={evidence.get('freshness_max_age_days')}d"
            if available else None
        ),
        current_status="available" if available else "unavailable",
        decision_impact="market_context" if available else "context",
        fallback_policy="unavailable",
        input_cutoff=cutoff,
        input_lineage=lineage,
        blockers=blockers,
        benchmark_key=evidence.get("benchmark_key") if dimension == "equity internals" else None,
        eligible_member_count=evidence.get("eligible_member_count") if dimension == "equity internals" else None,
        available_member_count=evidence.get("available_member_count") if dimension == "equity internals" else None,
        missing_member_count=evidence.get("missing_member_count") if dimension == "equity internals" else None,
        stale_member_count=evidence.get("stale_member_count") if dimension == "equity internals" else None,
        truncated_member_count=evidence.get("truncated_member_count") if dimension == "equity internals" else None,
        required_history_trading_days=evidence.get("minimum_history_trading_days") if dimension == "equity internals" else None,
        data_requests=(
            ("confirmed_daily_price_history",) if dimension == "equity internals" and not available
            else ("spread_depth_market_impact_expected_execution_cost",) if dimension == "microstructure"
            else (_UNSUPPORTED_DIMENSIONS[dimension][1],) if dimension != "equity internals" else ()
        ),
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
