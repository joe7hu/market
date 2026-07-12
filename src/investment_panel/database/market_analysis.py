"""PostgreSQL-native broad-market publication from normalized quote history."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime


def refresh_market_publication(runtime: DatabaseRuntime, *, now: datetime | None = None) -> dict[str, Any]:
    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("market publication timestamp must be timezone-aware")
    with runtime.read() as connection:
        quote_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT chosen.instrument_id, chosen.symbol, chosen.name,
                       chosen.asset_class, chosen.observed_at, chosen.price,
                       NULL::double precision AS change_pct, chosen.source_id
                FROM (
                    SELECT DISTINCT ON (instrument.id, bar.trading_date)
                           instrument.id AS instrument_id, instrument.symbol, instrument.name,
                           instrument.asset_class, bar.observed_at, bar.close AS price,
                           bar.source_id, bar.trading_date
                    FROM raw.price_bar bar
                    JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
                    JOIN ingest.run ingest_run ON ingest_run.id = bar.ingest_run_id
                    WHERE bar.interval = '1d' AND bar.observed_at >= %s - interval '400 days'
                    ORDER BY instrument.id, bar.trading_date,
                             ingest_run.started_at DESC, bar.observed_at DESC, bar.source_id
                ) chosen
                ORDER BY chosen.symbol, chosen.observed_at
                """,
                [as_of],
            ).fetchall()
        ]
        valuation_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT instrument.symbol, observation.period_end, observation.observed_at,
                       observation.values, observation.source_id
                FROM raw.fundamental_observation observation
                JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
                WHERE observation.metric_set = 'market_valuation'
                ORDER BY observation.observed_at DESC LIMIT 20
                """
            ).fetchall()
        ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in quote_rows:
        grouped[str(row["symbol"])].append(row)
    assets = [_asset_row(rows) for rows in grouped.values()]
    assets.sort(key=lambda row: (str(row["group_name"]), str(row["symbol"])))
    drivers = _driver_rows(assets)
    references = [_valuation_reference(row) for row in valuation_rows]
    analysis = AnalysisRepository(runtime)
    run_id = analysis.start_run(
        "market-environment",
        input_cutoff=as_of,
        code_version="postgres-market-v1",
        inputs={"quote_rows": len(quote_rows), "symbols": sorted(grouped), "valuation_rows": len(valuation_rows)},
        feature_versions={"market_environment": "v1"},
    )
    publication_id = analysis.publish(
        run_id,
        "market",
        {
            "market_environment_assets": assets,
            "market_environment_model": drivers,
            "market_valuation_reference_charts": references,
        },
        validation={"normalized_quote_source": True, "asset_count": len(assets)},
        complete_run_summary={"assets": len(assets), "drivers": len(drivers), "valuation_series": len(references)},
    )
    return {
        "status": "ok",
        "publication_id": str(publication_id),
        "assets": len(assets),
        "drivers": len(drivers),
        "valuation_series": len(references),
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


def _driver_rows(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    returns = [float(row["return_1d"]) for row in assets if row.get("return_1d") is not None]
    trends = [bool(row["sma_50_up"]) for row in assets if row.get("sma_50_up") is not None]
    trend_score = 100 * sum(trends) / len(trends) if trends else 50
    breadth_score = max(0, min(100, 50 + 10 * mean(returns))) if returns else 50
    risk_score = max(0, min(100, (trend_score + breadth_score) / 2))
    values = (
        ("Valuation", 50.0, 0.20, "No normalized market-valuation observation; neutral until available."),
        ("Price Trend", trend_score, 0.30, f"{sum(trends)} of {len(trends)} assets above their 50-observation average."),
        ("Market Breadth", breadth_score, 0.30, f"Average latest return across {len(returns)} assets."),
        ("Risk Appetite", risk_score, 0.20, "Composite of normalized trend and breadth."),
    )
    return [
        {
            "stable_key": category.lower().replace(" ", "_"),
            "category": category,
            "score": round(score, 2),
            "weight": weight,
            "posture": "constructive" if score >= 70 else "mixed" if score >= 45 else "defensive",
            "evidence": evidence,
            "source": "postgresql normalized facts",
        }
        for category, score, weight, evidence in values
    ]


def _valuation_reference(row: dict[str, Any]) -> dict[str, Any]:
    values = dict(row.get("values") or {})
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
        "source": row.get("source_id"),
    }


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
