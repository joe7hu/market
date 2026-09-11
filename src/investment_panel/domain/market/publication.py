"""Pure Market publication calculations.

This module owns the calculation and contract shaping. PostgreSQL loading,
transaction boundaries, and publication writes remain in the infrastructure
owner.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import math
from statistics import mean
from typing import Any, TypedDict

from investment_panel.domain.decision import (
    CoverageMatrix,
    CoverageMatrixRow,
    ExpressionKind,
    InputLineage,
    MARKET_DIMENSIONS,
    MARKET_HORIZONS,
    MARKET_SOURCE_PRIORITY,
    MarketDimensionState,
    MarketStateSnapshot,
    completed_trading_dates,
    market_evidence_for_decision,
)
from investment_panel.domain.factors.trend_features import realized_volatility
from investment_panel.domain.market.phase2 import (
    EventObservation,
    PITObservation,
    Phase2Status,
    build_coverage_vector,
    build_market_state_posterior,
    build_scenario_paths,
    phase2_input_content_hash,
)

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

class MarketPublicationInputs(TypedDict):
    instrument_rows: list[dict[str, Any]]
    bars_by_id: dict[int, list[dict[str, Any]]]
    price_rows: list[dict[str, Any]]
    valuation_rows: list[dict[str, Any]]
    event_risk_evidence: dict[str, dict[str, Any]]
    corporate_cycle_evidence: dict[str, Any]
    crypto_volume_evidence: dict[str, dict[str, Any]]
    phase2_rows: list[dict[str, Any]]
    phase2_source_rows: list[dict[str, Any]]

def build_market_publication(
    *,
    as_of: datetime,
    inputs: MarketPublicationInputs,
) -> dict[str, Any]:
    instrument_rows = inputs["instrument_rows"]
    bars_by_id = inputs["bars_by_id"]
    price_rows = inputs["price_rows"]
    valuation_rows = inputs["valuation_rows"]
    event_risk_evidence = inputs["event_risk_evidence"]
    corporate_cycle_evidence = inputs["corporate_cycle_evidence"]
    crypto_volume_evidence = inputs["crypto_volume_evidence"]
    phase2_rows = inputs["phase2_rows"]
    phase2_source_rows = inputs["phase2_source_rows"]
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
        volatility_evidence, corporate_cycle_evidence, crypto_volume_evidence,
    )
    phase2_observations = _phase2_observations(phase2_rows)
    phase2_lifecycle = _phase2_source_lifecycle(phase2_rows, phase2_source_rows)
    phase2_statuses = _phase2_source_statuses(phase2_rows, phase2_source_rows)
    phase2_run_ids = tuple(sorted({row.ingest_run_id for row in phase2_observations if row.ingest_run_id}))
    phase2_content_hash = phase2_input_content_hash(phase2_observations)
    phase2_posterior = build_market_state_posterior(
        phase2_observations, as_of=as_of, source_lifecycle=phase2_lifecycle,
        source_statuses=phase2_statuses, ingest_run_ids=phase2_run_ids,
        input_content_hash=phase2_content_hash, parent_snapshot_id=snapshot.snapshot_id,
    )
    phase2_coverage = build_coverage_vector(
        as_of,
        {
            "stock": {"daily": ("macro.value", "rates.nominal_yield", "credit.spread"), "positioning": ("positioning.flow",)},
            "options": {"positioning": ("option.open_interest", "option.volume")},
            "crypto": {"venue_derivatives": ("crypto.depth",)},
        },
        phase2_observations,
        source_lifecycle=phase2_lifecycle,
        source_statuses=phase2_statuses,
        ingest_run_ids=phase2_run_ids,
        input_content_hash=phase2_content_hash,
        parent_snapshot_id=snapshot.snapshot_id,
    )
    phase2_scenarios = build_scenario_paths(snapshot.snapshot_id, phase2_posterior)
    snapshot = snapshot.model_copy(update={
        "phase2_posterior": phase2_posterior.model_dump(mode="json"),
        "phase2_coverage_vector": phase2_coverage.model_dump(mode="json"),
        "phase2_scenario_paths": tuple(path.model_dump(mode="json") for path in phase2_scenarios),
    })
    coverage_rows = _coverage_rows(snapshot.coverage_matrix)

    return {
        "assets": assets,
        "drivers": drivers,
        "references": references,
        "snapshot": snapshot,
        "coverage_rows": coverage_rows,
        "volatility_evidence": volatility_evidence,
        "corporate_cycle_evidence": corporate_cycle_evidence,
        "crypto_volume_evidence": crypto_volume_evidence,
        "input_lineage": input_lineage,
        "grouped": grouped,
        "price_rows": price_rows,
        "valuation_rows": valuation_rows,
        "phase2_posterior": phase2_posterior,
        "phase2_coverage": phase2_coverage,
        "phase2_scenarios": phase2_scenarios,
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
        (
            row for row in instrument_rows
            if row.get("market_benchmark_member")
            and str(row.get("asset_class") or "").lower() in {"equity", "etf"}
        ),
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
                "restricted_members": (),
                "expected_trading_days": 0,
                "minimum_history_trading_days": 0,
                "lineage": (),
                "valid_rows": (),
            }
            continue
        expected = completed_trading_dates(cutoff, count=lookback)
        valid_rows: list[dict[str, Any]] = []
        missing = stale = truncated = 0
        missing_members: list[str] = []
        stale_members: list[str] = []
        truncated_members: list[str] = []
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
                missing_members.append(str(member["symbol"]))
                continue
            latest_available = max(
                (_as_utc(row.get("available_at")) for row in by_date.values() if row.get("available_at") is not None),
                default=None,
            )
            if latest_available is not None and cutoff - latest_available > _MARKET_STALE_AFTER:
                stale += 1
                stale_members.append(str(member["symbol"]))
                continue
            if len(by_date) != len(expected) or set(by_date) != set(expected):
                truncated += 1
                truncated_members.append(str(member["symbol"]))
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
            "restricted_members": tuple(sorted(missing_members + stale_members + truncated_members)),
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
        (
            row for row in instrument_rows
            if row.get("market_benchmark_member")
            and str(row.get("asset_class") or "").lower() in {"equity", "etf"}
        ),
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
                "restricted_members": (),
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
        missing_members: list[str] = []
        stale_members: list[str] = []
        truncated_members: list[str] = []
        for member in benchmark:
            rows = [
                row for row in bars_by_id.get(int(member["id"]), ())
                if row.get("trading_date") in expected
            ]
            dates = [row.get("trading_date") for row in rows]
            if not rows:
                missing += 1
                missing_members.append(str(member["symbol"]))
                continue
            if len(dates) != len(set(dates)):
                duplicate += 1
                continue
            by_date = {row["trading_date"]: row for row in rows}
            if len(by_date) != len(expected) or set(by_date) != set(expected):
                truncated += 1
                truncated_members.append(str(member["symbol"]))
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
                stale_members.append(str(member["symbol"]))
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
            "restricted_members": tuple(sorted(missing_members + stale_members + truncated_members)),
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

def _market_snapshot(
    cutoff: datetime,
    assets: list[dict[str, Any]],
    drivers: list[dict[str, Any]],
    lineage: tuple[InputLineage, ...],
    horizon_evidence: dict[str, dict[str, Any]] | None = None,
    event_risk_evidence: dict[str, dict[str, Any]] | None = None,
    volatility_evidence: dict[str, dict[str, Any]] | None = None,
    corporate_cycle_evidence: dict[str, Any] | None = None,
    crypto_volume_evidence: dict[str, dict[str, Any]] | None = None,
) -> MarketStateSnapshot:
    reference = cutoff.astimezone(UTC)
    del assets, drivers
    horizon_evidence = horizon_evidence or {}
    event_risk_evidence = event_risk_evidence or {}
    volatility_evidence = volatility_evidence or {}
    corporate_cycle_evidence = corporate_cycle_evidence or {}
    crypto_volume_evidence = crypto_volume_evidence or {}
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
                        eligible_members=tuple(corporate_cycle_evidence.get("eligible_members") or ()),
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
                        eligible_members=tuple(corporate_cycle_evidence.get("eligible_members") or ()),
                        eligible_member_count=corporate_cycle_evidence.get("eligible_member_count"),
                        available_member_count=corporate_cycle_evidence.get("available_member_count"),
                        missing_member_count=corporate_cycle_evidence.get("missing_member_count"),
                        stale_member_count=corporate_cycle_evidence.get("stale_member_count"),
                        duplicate_member_count=corporate_cycle_evidence.get("duplicate_member_count"),
                        invalid_member_count=corporate_cycle_evidence.get("invalid_member_count"),
                    ))
            elif dimension == "crypto liquidity":
                crypto = crypto_volume_evidence.get(horizon, {})
                crypto_fields = {
                    key: crypto.get(key)
                    for key in (
                        "benchmark_key", "eligible_members", "eligible_member_count", "available_member_count",
                        "missing_member_count", "stale_member_count", "truncated_member_count",
                        "duplicate_member_count", "invalid_member_count", "wrong_currency_member_count",
                        "wrong_asset_class_member_count", "wrong_source_member_count", "expected_calendar_days",
                        "window_start", "window_end", "latest_aggregate_volume_usd",
                        "median_aggregate_daily_volume_usd", "latest_to_horizon_median_ratio",
                        "freshness_max_age_seconds", "freshness_latest_available_at",
                    )
                }
                if crypto.get("available"):
                    horizon_rows.append(MarketDimensionState(
                        dimension=dimension,
                        horizon=horizon,
                        state="reported daily USD crypto trading volume",
                        change_drivers=(
                            "Reported daily USD crypto trading volume; three-of-three denominator: BTC-USD, ETH-USD, SOL-USD.",
                            f"Latest completed UTC aggregate: {crypto['latest_aggregate_volume_usd']:.2f} USD; "
                            f"horizon median: {crypto['median_aggregate_daily_volume_usd']:.2f} USD; "
                            f"latest/median ratio: {crypto['latest_to_horizon_median_ratio']:.6f}.",
                            f"Exact completed UTC window {crypto['window_start']} through {crypto['window_end']} "
                            f"({crypto['expected_calendar_days']} dates).",
                        ),
                        evidence_status="available",
                        uncertainty="descriptive reported daily trading volume only; not full liquidity, execution capacity, or trading authority",
                        quality="coingecko_reported_daily_trading_volume",
                        lineage=tuple(crypto.get("lineage") or ()),
                        data_requests=(),
                        **crypto_fields,
                    ))
                else:
                    horizon_rows.append(MarketDimensionState(
                        dimension=dimension,
                        horizon=horizon,
                        evidence_status="unavailable",
                        uncertainty="exact point-in-time daily USD crypto trading-volume evidence is incomplete, stale, or invalid",
                        blockers=tuple(crypto.get("blockers") or ("crypto_daily_trading_volume_unavailable",)),
                        data_requests=tuple(crypto.get("data_requests") or ("update_market_data",)),
                        **crypto_fields,
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
        dimensions[horizon] = tuple(
            row.model_copy(update=market_dimension_v2_fields(row))
            for row in horizon_rows
        )
    measured_lineage = _lineage_union(
        tuple(evidence.get("lineage") or ())
        for evidence in (
            *horizon_evidence.values(), *volatility_evidence.values(), *event_risk_evidence.values(),
            corporate_cycle_evidence, *crypto_volume_evidence.values()
        )
        if evidence.get("available")
    )
    encoded = json_dumps({
        "cutoff": reference.isoformat(),
        "corporate_cycle_benchmark": {
            "benchmark_key": corporate_cycle_evidence.get("benchmark_key"),
            "eligible_members": tuple(corporate_cycle_evidence.get("eligible_members") or ()),
        },
        "lineage": [item.model_dump(mode="json") for item in measured_lineage],
        "dimensions": dimensions,
    })
    snapshot_id = f"market-state:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"
    rows = tuple(
        _coverage_row(
            horizon, dimension, reference, horizon_evidence, event_risk_evidence, volatility_evidence,
            corporate_cycle_evidence, crypto_volume_evidence,
        )
        for horizon in MARKET_HORIZONS
        for dimension in MARKET_DIMENSIONS
    )
    all_daily_horizons_available = all(
        horizon_evidence.get(horizon, {}).get("available")
        for horizon in MARKET_HORIZONS
        if horizon != "intraday"
    )
    snapshot = MarketStateSnapshot(
        contract_version="market-state-snapshot.v2",
        snapshot_id=snapshot_id,
        as_of=reference,
        input_cutoff=reference,
        horizons=dimensions,
        coverage_matrix=CoverageMatrix(
            contract_version="coverage-matrix.v2",
            matrix_id=f"coverage:{snapshot_id}",
            as_of=reference,
            input_cutoff=reference,
            rows=rows,
        ),
        input_lineage=measured_lineage,
        availability="available" if all_daily_horizons_available else "partial",
        availability_status="available" if all_daily_horizons_available else "missing",
        blockers=() if all_daily_horizons_available else ("market_horizon_coverage_unavailable",),
    )
    return snapshot.model_copy(update={
        "decision_evidence": tuple(
            market_evidence_for_decision(snapshot, kind, horizon)
            for kind in ExpressionKind
            for horizon in ("TACTICAL", "FUNDAMENTAL")
        ),
        "regime_distributions": _regime_distributions(horizon_evidence, volatility_evidence),
        "baseline_challenger": _baseline_challenger(horizon_evidence, volatility_evidence),
        "source_priorities": {dimension: MARKET_SOURCE_PRIORITY.get(dimension, ()) for dimension in MARKET_DIMENSIONS},
        "selected_sources": {
            dimension: next(
                (item.selected_source for rows_for_horizon in dimensions.values() for item in rows_for_horizon
                 if item.dimension == dimension and item.lineage),
                None,
            )
            for dimension in MARKET_DIMENSIONS
        },
    })

def market_dimension_v2_fields(row: MarketDimensionState) -> dict[str, Any]:
    source_priority = MARKET_SOURCE_PRIORITY.get(row.dimension, ())
    selected_source = row.lineage[0].source_id if row.lineage else None
    baseline = _claim_support({
        "distribution": row.regime_distribution,
        "method": row.regime_probability_method,
        "version": row.regime_model_version,
        "sample_count": row.regime_sample_count,
        "uncertainty": row.uncertainty,
    }) if str(row.evidence_status).lower() == "available" else None
    challenger = _claim_support({
        "distribution": getattr(row, "challenger_distribution", {}),
        "method": getattr(row, "challenger_probability_method", None),
        "version": getattr(row, "challenger_model_version", None),
        "sample_count": getattr(row, "challenger_sample_count", None),
        "uncertainty": getattr(row, "challenger_uncertainty", None),
    }) if str(row.evidence_status).lower() == "available" else None
    sample_count = row.regime_sample_count
    return {
        "source_priority": source_priority,
        "selected_source": selected_source,
        "regime_distribution": baseline["distribution"] if baseline else {},
        "regime_probability_method": baseline["method"] if baseline else None,
        "regime_model_version": baseline["version"] if baseline else None,
        "regime_sample_count": sample_count,
        "baseline_result": {
            **({"method": baseline["method"], "version": baseline["version"], "state": row.state}
               if baseline else {}),
            "status": "available" if baseline else "unavailable",
            "advisory": baseline is None,
            "sample_count": baseline["sample_count"] if baseline else None,
            "uncertainty": baseline["uncertainty"] if baseline else None,
        },
        "challenger_result": {
            **({"method": challenger["method"], "version": challenger["version"]} if challenger else {}),
            "status": "advisory" if challenger else "unavailable",
            "advisory": True, "promotion_eligible": False,
            "sample_count": challenger["sample_count"] if challenger else None,
            "uncertainty": challenger["uncertainty"] if challenger else None,
            "distribution": challenger["distribution"] if challenger else {},
        },
    }

def _claim_support(value: dict[str, Any]) -> dict[str, Any] | None:
    distribution = value.get("distribution")
    sample_count = value.get("sample_count")
    if not isinstance(distribution, dict) or not distribution:
        return None
    try:
        probabilities = [float(item) for item in distribution.values()]
        sample_count = int(sample_count)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not value.get("method") or not value.get("version") or not value.get("uncertainty")
        or sample_count < 20
        or any(not math.isfinite(item) or item < 0 for item in probabilities)
        or not math.isclose(sum(probabilities), 1.0, rel_tol=1e-6, abs_tol=1e-6)
    ):
        return None
    return {**value, "sample_count": sample_count}

def _regime_distributions(
    horizon_evidence: dict[str, dict[str, Any]],
    volatility_evidence: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for horizon in MARKET_HORIZONS:
        evidence = horizon_evidence.get(horizon, {})
        baseline = _claim_support({
            "distribution": evidence.get("regime_distribution"),
            "method": evidence.get("regime_probability_method"),
            "version": evidence.get("regime_model_version"),
            "sample_count": evidence.get("regime_sample_count"),
            "uncertainty": evidence.get("regime_uncertainty"),
        })
        challenger = _claim_support({
            "distribution": evidence.get("challenger_distribution"),
            "method": evidence.get("challenger_probability_method"),
            "version": evidence.get("challenger_model_version"),
            "sample_count": evidence.get("challenger_sample_count"),
            "uncertainty": evidence.get("challenger_uncertainty"),
        })
        result[horizon] = {
            "distribution": baseline["distribution"] if baseline else {},
            "sample_count": baseline["sample_count"] if baseline else None,
            "status": "available" if baseline else "advisory",
            "advisory": baseline is None,
            "uncertainty": baseline["uncertainty"] if baseline else "insufficient point-in-time evidence",
            **({
                "method": baseline["method"], "version": baseline["version"],
                "challenger_available": challenger is not None,
            } if baseline else {"challenger_available": False}),
        }
    return result

def _baseline_challenger(
    horizon_evidence: dict[str, dict[str, Any]],
    volatility_evidence: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for horizon in MARKET_HORIZONS:
        baseline = _claim_support({
            "distribution": horizon_evidence.get(horizon, {}).get("regime_distribution"),
            "method": horizon_evidence.get(horizon, {}).get("regime_probability_method"),
            "version": horizon_evidence.get(horizon, {}).get("regime_model_version"),
            "sample_count": horizon_evidence.get(horizon, {}).get("regime_sample_count"),
            "uncertainty": horizon_evidence.get(horizon, {}).get("regime_uncertainty"),
        })
        challenger = _claim_support({
            "distribution": volatility_evidence.get(horizon, {}).get("challenger_distribution"),
            "method": volatility_evidence.get(horizon, {}).get("challenger_probability_method"),
            "version": volatility_evidence.get(horizon, {}).get("challenger_model_version"),
            "sample_count": volatility_evidence.get(horizon, {}).get("challenger_sample_count"),
            "uncertainty": volatility_evidence.get(horizon, {}).get("challenger_uncertainty"),
        })
        result[horizon] = {
            "baseline": {
                **({"method": baseline["method"], "version": baseline["version"]} if baseline else {}),
                "status": "available" if baseline else "unavailable",
                "advisory": baseline is None,
                "sample_count": baseline["sample_count"] if baseline else None,
                "uncertainty": baseline["uncertainty"] if baseline else None,
            },
            "challenger": {
                **({"method": challenger["method"], "version": challenger["version"]} if challenger else {}),
                "status": "advisory" if challenger else "unavailable",
                "advisory": True, "promotion_eligible": False,
                "sample_count": challenger["sample_count"] if challenger else None,
                "uncertainty": challenger["uncertainty"] if challenger else None,
            },
        }
    return result

def _coverage_row(
    horizon: str,
    dimension: str,
    cutoff: datetime,
    horizon_evidence: dict[str, dict[str, Any]],
    event_risk_evidence: dict[str, dict[str, Any]] | None = None,
    volatility_evidence: dict[str, dict[str, Any]] | None = None,
    corporate_cycle_evidence: dict[str, Any] | None = None,
    crypto_volume_evidence: dict[str, dict[str, Any]] | None = None,
) -> CoverageMatrixRow:
    evidence = horizon_evidence.get(horizon, {})
    event_evidence = (event_risk_evidence or {}).get(horizon, {})
    volatility = (volatility_evidence or {}).get(horizon, {})
    corporate = corporate_cycle_evidence or {}
    crypto = (crypto_volume_evidence or {}).get(horizon, {})
    selected_evidence = (
        event_evidence if dimension == "event risk"
        else volatility if dimension == "volatility"
        else corporate if dimension == "corporate cycle"
        else crypto if dimension == "crypto liquidity"
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
    ) or (
        dimension == "crypto liquidity" and bool(crypto.get("available"))
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
    elif dimension == "crypto liquidity":
        blockers = tuple(crypto.get("blockers") or ("crypto_daily_trading_volume_unavailable",))
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
    elif dimension == "crypto liquidity":
        data_requests = () if available else tuple(
            crypto.get("data_requests") or ("update_market_data",)
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
            if dimension == "corporate cycle" and available else
            f"daily-market-prices; update_market_data; run_finished_at <= input_cutoff; max_age={crypto.get('freshness_max_age_seconds')}s"
            if dimension == "crypto liquidity" and available else None
        ),
        current_status="available" if available else "unavailable",
        decision_impact="market_context" if dimension == "equity internals" and available else "context",
        fallback_policy="unavailable",
        input_cutoff=cutoff,
        input_lineage=lineage,
        source_priority=MARKET_SOURCE_PRIORITY.get(dimension, ()),
        selected_source=lineage[0].source_id if lineage else None,
        blockers=blockers if not available else (),
        restricted_members=tuple(selected_evidence.get("restricted_members") or ())
        if dimension in {"equity internals", "volatility"} else (),
        benchmark_key=selected_evidence.get("benchmark_key")
        if dimension in {"equity internals", "volatility", "corporate cycle", "crypto liquidity"} else None,
        eligible_members=tuple(selected_evidence.get("eligible_members") or ())
        if dimension in {"corporate cycle", "crypto liquidity"} else (),
        eligible_member_count=selected_evidence.get("eligible_member_count")
        if dimension in {"equity internals", "volatility", "corporate cycle", "crypto liquidity"} else None,
        available_member_count=selected_evidence.get("available_member_count")
        if dimension in {"equity internals", "volatility", "corporate cycle", "crypto liquidity"} else None,
        missing_member_count=selected_evidence.get("missing_member_count")
        if dimension in {"equity internals", "volatility", "corporate cycle", "crypto liquidity"} else None,
        stale_member_count=selected_evidence.get("stale_member_count")
        if dimension in {"equity internals", "volatility", "corporate cycle", "crypto liquidity"} else None,
        truncated_member_count=selected_evidence.get("truncated_member_count")
        if dimension in {"equity internals", "volatility", "crypto liquidity"} else None,
        duplicate_member_count=selected_evidence.get("duplicate_member_count")
        if dimension in {"volatility", "corporate cycle", "crypto liquidity"} else None,
        invalid_member_count=selected_evidence.get("invalid_member_count")
        if dimension in {"volatility", "corporate cycle", "crypto liquidity"} else None,
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
        **({
            key: crypto.get(key)
            for key in (
                "wrong_currency_member_count", "wrong_asset_class_member_count", "wrong_source_member_count",
                "expected_calendar_days", "window_start", "window_end", "latest_aggregate_volume_usd",
                "median_aggregate_daily_volume_usd", "latest_to_horizon_median_ratio",
                "freshness_max_age_seconds", "freshness_latest_available_at",
            )
        } if dimension == "crypto liquidity" else {}),
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

def _phase2_observations(rows: list[dict[str, Any]]) -> tuple[PITObservation, ...]:
    """Decode only persisted source facts; malformed rows remain unavailable."""

    observations: list[PITObservation] = []
    for row in rows:
        try:
            model = EventObservation if any(row.get(key) is not None for key in ("actual", "consensus", "surprise", "revision")) else PITObservation
            observations.append(model.model_validate({key: value for key, value in row.items() if not key.startswith("source_") and key not in {"ingest_status", "ingest_finished_at"}}))
        except (TypeError, ValueError):
            continue
    return tuple(observations)

def _phase2_source_lifecycle(rows: list[dict[str, Any]], source_rows: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    lifecycle = {
        str(row["source_id"]): {
            "enabled": bool(row.get("source_enabled")),
            "operational_state": str(row.get("source_operational_state") or "archived"),
        }
        for row in rows
        if row.get("source_id")
    }
    lifecycle.update({
        str(row["source_id"]): {
            "enabled": bool(row.get("source_enabled")),
            "operational_state": str(row.get("source_operational_state") or "archived"),
        }
        for row in source_rows or ()
        if row.get("source_id")
    })
    return lifecycle

def _phase2_source_statuses(rows: list[dict[str, Any]], source_rows: list[dict[str, Any]] | None = None) -> dict[str, Phase2Status]:
    statuses: dict[str, Phase2Status] = {}
    for row in source_rows or ():
        value = str(row.get("phase2_status") or "")
        try:
            statuses[str(row["source_id"])] = Phase2Status(value)
        except ValueError:
            if not row.get("source_enabled") or str(row.get("source_operational_state") or "") != "active":
                statuses[str(row["source_id"])] = Phase2Status.MISSING_SOURCE
    for row in rows:
        source_id = str(row.get("source_id") or "")
        if not source_id:
            continue
        if not row.get("source_enabled") or str(row.get("source_operational_state") or "") != "active":
            statuses[source_id] = Phase2Status.MISSING_SOURCE
        elif str(row.get("ingest_status") or "") not in {"succeeded", "partial"}:
            statuses[source_id] = Phase2Status.MISSING_HISTORY
        elif row.get("ingest_finished_at") is None:
            statuses[source_id] = Phase2Status.STALE
        else:
            statuses.setdefault(source_id, Phase2Status.AVAILABLE)
    return statuses

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


__all__ = ["MarketPublicationInputs", "build_market_publication", "market_dimension_v2_fields"]
