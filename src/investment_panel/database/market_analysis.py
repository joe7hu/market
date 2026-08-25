"""PostgreSQL-native broad-market publication from normalized quote history."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
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
from investment_panel.database.fundamental_history import hydrate_history
from investment_panel.database.runtime import DatabaseRuntime


def refresh_market_publication(runtime: DatabaseRuntime, *, now: datetime | None = None) -> dict[str, Any]:
    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("market publication timestamp must be timezone-aware")
    as_of = as_of.astimezone(UTC)
    with runtime.read() as connection:
        quote_rows = [
            dict(row)
            for row in connection.execute(
                """
                WITH observations AS (
                    SELECT instrument.id AS instrument_id, instrument.symbol, instrument.name,
                           instrument.asset_class, bar.observed_at, bar.close AS price,
                           bar.source_id, bar.trading_date, bar.available_at,
                           ingest_run.id::text AS ingest_run_id,
                           ingest_run.started_at AS run_started_at
                    FROM raw.price_bar bar
                    JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
                    JOIN ingest.run ingest_run ON ingest_run.id = bar.ingest_run_id
                    JOIN ingest.source source
                      ON source.id = bar.source_id
                     AND source.enabled
                     AND source.operational_state = 'active'
                    WHERE bar.interval = '1d'
                      AND bar.observed_at >= %s - interval '400 days'
                      AND bar.observed_at <= %s
                      AND bar.available_at <= %s
                      AND ingest_run.status IN ('succeeded', 'partial')
                      AND ingest_run.finished_at IS NOT NULL
                      AND ingest_run.finished_at <= %s
                    UNION ALL
                    SELECT instrument.id, instrument.symbol, instrument.name,
                           instrument.asset_class, quote.observed_at, quote.price,
                           quote.source_id, quote.observed_at::date, quote.available_at,
                           ingest_run.id::text, ingest_run.started_at
                    FROM raw.quote quote
                    JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
                    JOIN ingest.run ingest_run ON ingest_run.id = quote.ingest_run_id
                    JOIN ingest.source source
                      ON source.id = quote.source_id
                     AND source.enabled
                     AND source.operational_state = 'active'
                    WHERE quote.observed_at >= %s - interval '400 days'
                      AND quote.observed_at <= %s
                      AND quote.available_at <= %s
                      AND ingest_run.status IN ('succeeded', 'partial')
                      AND ingest_run.finished_at IS NOT NULL
                      AND ingest_run.finished_at <= %s
                      AND NOT EXISTS (
                          SELECT 1
                          FROM raw.price_bar bar
                          JOIN ingest.run bar_run ON bar_run.id = bar.ingest_run_id
                          JOIN ingest.source bar_source
                            ON bar_source.id = bar.source_id
                           AND bar_source.enabled
                           AND bar_source.operational_state = 'active'
                          WHERE bar.instrument_id = quote.instrument_id
                            AND bar.interval = '1d'
                            AND bar.trading_date = quote.observed_at::date
                            AND bar.observed_at <= %s
                            AND bar.available_at <= %s
                            AND bar_run.status IN ('succeeded', 'partial')
                            AND bar_run.finished_at IS NOT NULL
                            AND bar_run.finished_at <= %s
                      )
                )
                SELECT chosen.instrument_id, chosen.symbol, chosen.name,
                       chosen.asset_class, chosen.observed_at, chosen.price,
                       NULL::double precision AS change_pct, chosen.source_id,
                       chosen.available_at, chosen.ingest_run_id
                FROM (
                    SELECT DISTINCT ON (instrument_id, trading_date) *
                    FROM observations
                    ORDER BY instrument_id, trading_date,
                             run_started_at DESC, observed_at DESC, source_id
                ) chosen
                ORDER BY chosen.symbol, chosen.observed_at
                """,
                [as_of, as_of, as_of, as_of, as_of, as_of, as_of, as_of, as_of, as_of, as_of],
            ).fetchall()
        ]
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
    for row in quote_rows:
        grouped[str(row["symbol"])].append(row)
    assets = [_asset_row(rows) for rows in grouped.values()]
    assets.sort(key=lambda row: (str(row["group_name"]), str(row["symbol"])))
    drivers = _driver_rows(assets, valuation_rows)
    references = [_valuation_reference(row) for row in valuation_rows]
    input_lineage = _market_lineage(quote_rows, valuation_rows, as_of)
    snapshot = _market_snapshot(as_of, assets, drivers, input_lineage)
    coverage_rows = _coverage_rows(snapshot.coverage_matrix)
    analysis = AnalysisRepository(runtime)
    run_id = analysis.start_run(
        "market-environment",
        input_cutoff=as_of,
        code_version="postgres-market-v1",
        inputs={
            "quote_rows": len(quote_rows),
            "symbols": sorted(grouped),
            "valuation_rows": len(valuation_rows),
            "source_lineage": [item.model_dump(mode="json") for item in input_lineage],
        },
        feature_versions={"market_environment": "v1"},
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
        validation={"normalized_quote_source": True, "asset_count": len(assets)},
        complete_run_summary={
            "assets": len(assets), "drivers": len(drivers), "valuation_series": len(references),
            "snapshot_id": snapshot.snapshot_id,
        },
    )
    return {
        "status": "ok",
        "publication_id": str(publication_id),
        "assets": len(assets),
        "drivers": len(drivers),
        "valuation_series": len(references),
        "snapshot_id": snapshot.snapshot_id,
        "coverage_rows": len(coverage_rows),
    }


def _asset_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latest = rows[-1]
    prices = [float(row["price"]) for row in rows if row.get("price") is not None]
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
        "return_ytd": _ytd_return(rows),
        "sma_20_up": _above_average(prices, 20),
        "sma_50_up": _above_average(prices, 50),
        "sma_200_up": _above_average(prices, 200),
        "sma_50_gt_200": _average(prices, 50) > _average(prices, 200) if len(prices) >= 200 else None,
        "source": latest["source_id"],
    }


def _driver_rows(assets: list[dict[str, Any]], valuation_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    returns = [float(row["return_1d"]) for row in assets if row.get("return_1d") is not None]
    trends = [bool(row["sma_50_up"]) for row in assets if row.get("sma_50_up") is not None]
    trend_score = 100 * sum(trends) / len(trends) if trends else None
    breadth_score = max(0, min(100, 50 + 10 * mean(returns))) if returns else None
    risk_score = (
        max(0, min(100, (trend_score + breadth_score) / 2))
        if trend_score is not None and breadth_score is not None
        else None
    )
    valuation_score = None
    if valuation_rows:
        values = [
            _number((row.get("values") or {}).get("score"))
            for row in valuation_rows
            if isinstance(row.get("values"), dict)
        ]
        values = [value for value in values if value is not None]
        valuation_score = mean(values) if values else None
    values = (
        ("Valuation", valuation_score, 0.20, "Normalized market-valuation evidence." if valuation_score is not None else "Market-valuation evidence is unavailable."),
        ("Price Trend", trend_score, 0.30, f"{sum(trends)} of {len(trends)} assets above their 50-observation average." if trend_score is not None else "50-observation trend evidence is unavailable."),
        ("Market Breadth", breadth_score, 0.30, f"Average latest return across {len(returns)} assets." if breadth_score is not None else "Latest-return breadth evidence is unavailable."),
        ("Risk Appetite", risk_score, 0.20, "Composite of normalized trend and breadth." if risk_score is not None else "Trend and breadth evidence are unavailable."),
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
            "blockers": [] if score is not None else ["market_dimension_unavailable"],
            "source": "postgresql normalized facts",
        }
        for category, score, weight, evidence in values
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
    quote_rows: list[dict[str, Any]],
    valuation_rows: list[dict[str, Any]],
    cutoff: datetime,
) -> tuple[InputLineage, ...]:
    result: dict[tuple[Any, ...], InputLineage] = {}
    for field, rows in (("market_quote", quote_rows), ("market_valuation", valuation_rows)):
        for row in rows:
            observed_at = row.get("observed_at")
            available_at = row.get("available_at") or observed_at
            source_id = str(row.get("source_id") or "unknown")
            source_version = str(row.get("ingest_run_id") or "unknown")
            if available_at is None:
                continue
            lineage = InputLineage(
                field=field,
                source_id=source_id,
                source_version=source_version,
                event_at=observed_at,
                available_at=available_at,
                revision=source_version,
                cutoff=cutoff,
            )
            key = (field, source_id, source_version, lineage.available_at)
            result[key] = lineage
    return tuple(sorted(result.values(), key=lambda item: (item.field, item.source_id, item.available_at)))


def _market_snapshot(
    cutoff: datetime,
    assets: list[dict[str, Any]],
    drivers: list[dict[str, Any]],
    lineage: tuple[InputLineage, ...],
) -> MarketStateSnapshot:
    reference = cutoff.astimezone(UTC)
    trend = next((row for row in drivers if row["category"] == "Price Trend"), None)
    breadth = next((row for row in drivers if row["category"] == "Market Breadth"), None)
    measured = trend is not None and breadth is not None and trend.get("score") is not None and breadth.get("score") is not None
    measured_lineage = tuple(item for item in lineage if item.field == "market_quote")
    dimensions: dict[str, tuple[MarketDimensionState, ...]] = {}
    for horizon in MARKET_HORIZONS:
        horizon_rows: list[MarketDimensionState] = []
        for dimension in MARKET_DIMENSIONS:
            if dimension == "equity internals" and measured:
                state = str(trend.get("posture") or "unavailable")
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    state=state,
                    change_drivers=(str(trend.get("evidence") or ""), str(breadth.get("evidence") or "")),
                    evidence_status="available",
                    uncertainty="derived from normalized price facts",
                    quality="measured" if assets else "unavailable",
                    lineage=measured_lineage,
                ))
            elif dimension == "microstructure" and measured_lineage:
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    state="observed_quotes",
                    change_drivers=("confirmed quote observations are available",),
                    evidence_status="available",
                    uncertainty="quote depth and execution costs are not measured",
                    quality="partial",
                    blockers=("microstructure_depth_unavailable",),
                    lineage=measured_lineage,
                ))
            else:
                horizon_rows.append(MarketDimensionState(
                    dimension=dimension,
                    horizon=horizon,
                    evidence_status="unavailable",
                    uncertainty="no point-in-time source is published for this dimension",
                    blockers=("market_dimension_unavailable",),
                ))
        dimensions[horizon] = tuple(horizon_rows)
    encoded = json_dumps({
        "cutoff": reference.isoformat(),
        "lineage": [item.model_dump(mode="json") for item in lineage],
        "dimensions": dimensions,
    })
    snapshot_id = f"market-state:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"
    provider = ",".join(sorted({item.source_id for item in lineage})) or None
    history_start = min((item.event_at.date() for item in measured_lineage if item.event_at), default=None)
    available_dimensions = {"equity internals", "microstructure"} if measured_lineage else set()
    rows = tuple(
        CoverageMatrixRow(
            dimension=dimension,
            asset_class="cross-asset",
            horizon=horizon,
            provider=provider if dimension in available_dimensions else None,
            history_start=history_start if dimension in available_dimensions else None,
            point_in_time_safe=dimension in available_dimensions,
            freshness_slo="available_at <= input_cutoff" if dimension in available_dimensions else None,
            current_status="available" if dimension in available_dimensions else "unavailable",
            decision_impact="market_context" if dimension in available_dimensions else "context",
            fallback_policy="unavailable",
            input_cutoff=reference,
            input_lineage=measured_lineage if dimension in available_dimensions else (),
        )
        for horizon in MARKET_HORIZONS
        for dimension in MARKET_DIMENSIONS
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
        input_lineage=lineage,
        availability="available" if lineage else "unavailable",
        blockers=() if lineage else ("market_inputs_unavailable",),
    )


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
