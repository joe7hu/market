"""Deterministic, price-space evidence for full QQQ option-chain history.

This module deliberately knows nothing about PostgreSQL, providers, or paper orders.
It accepts one expiry/type capture group and returns auditable evidence.  Keeping the
math pure makes a historical replay byte-for-byte reproducible from its generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Iterable

import numpy as np
from scipy.optimize import LinearConstraint, minimize


MODEL_REVISION = "history-v3-price-shape-r3"
MIN_ELIGIBLE_POINTS = 12
MIN_PACKAGE_EDGE = 0.05
COST_ALLOWANCE_PER_LEG = 0.02
MAX_QUOTE_AGE_SECONDS = 180

# These values are normalized capture states, not a claim about the provider's
# native status vocabulary.  Robinhood full-chain quotes are live by design;
# the unmodified provider payload remains attached to each raw quote.
TRADABLE_MARKET_STATUSES = frozenset({"", "open", "regular", "tradable", "active", "live"})


@dataclass(frozen=True)
class FitResult:
    fitted: tuple[float, ...]
    status: str
    rmse: float | None
    diagnostics: dict[str, Any]


def analyze_group(
    rows: Iterable[dict[str, Any]],
    *,
    spot: float | None,
    option_type: str,
    model_revision: str = MODEL_REVISION,
    group_blockers: Iterable[str] = (),
) -> dict[str, Any]:
    """Return quality, static-arbitrage, fit, and leave-one-out evidence.

    A failure is represented as durable reject evidence instead of a fallback model.
    """

    normalized = [dict(row) for row in rows]
    eligible, row_blockers, metrics = eligible_rows(normalized, spot=spot, option_type=option_type)
    blockers = sorted(set(group_blockers))
    static_findings = static_arbitrage_findings(eligible, spot=spot, option_type=option_type)
    if len(eligible) < MIN_ELIGIBLE_POINTS:
        blockers.append("insufficient_eligible_points")
    if blockers:
        terminal = sorted(set(blockers))
        return {
            "model_revision": model_revision,
            "fit": FitResult((), "fit_failed", None, {"blockers": terminal}),
            "eligible_count": len(eligible),
            "row_metrics": metrics,
            "blockers": terminal,
            "static_findings": static_findings,
            "relative_values": [
                rejected_value(
                    row,
                    model_revision,
                    [*row_blockers.get(_row_key(row), []), *terminal],
                    static_findings,
                )
                for row in normalized
            ],
        }
    fit = constrained_price_fit(eligible, spot=float(spot), option_type=option_type)
    if fit.status != "succeeded":
        blockers = ["fit_failed"]
        return {
            "model_revision": model_revision,
            "fit": fit,
            "eligible_count": len(eligible),
            "row_metrics": metrics,
            "blockers": blockers,
            "static_findings": static_findings,
            "relative_values": [
                rejected_value(
                    row,
                    model_revision,
                    [*row_blockers.get(_row_key(row), []), *blockers],
                    static_findings,
                )
                for row in normalized
            ],
        }
    values = relative_values(
        eligible,
        fit,
        spot=float(spot),
        option_type=option_type,
        model_revision=model_revision,
        static_findings=static_findings,
    )
    by_contract = {item["contract_id"]: item for item in values}
    return {
        "model_revision": model_revision,
        "fit": fit,
        "eligible_count": len(eligible),
        "row_metrics": metrics,
        "blockers": [],
        "static_findings": static_findings,
        "relative_values": [
            by_contract.get(
                row.get("contract_id"),
                rejected_value(row, model_revision, row_blockers.get(_row_key(row), ["quality_gate"]), static_findings),
            )
            for row in normalized
        ],
    }


def eligible_rows(
    rows: Iterable[dict[str, Any]], *, spot: float | None, option_type: str
) -> tuple[list[dict[str, Any]], dict[tuple[Any, ...], list[str]], dict[str, int]]:
    """Filter contracts independently, retaining a durable reason for each reject."""

    accepted: list[dict[str, Any]] = []
    blockers_by_row: dict[tuple[Any, ...], list[str]] = {}
    metrics = {
        "total_rows": 0,
        "eligible_rows": 0,
        "stale_rows": 0,
        "invalid_status_rows": 0,
        "missing_underlying_rows": 0,
        "rejected_rows": 0,
    }
    group_finished = max((row.get("group_finished_at") for row in rows if row.get("group_finished_at")), default=None)
    for row in rows:
        metrics["total_rows"] += 1
        bid, ask, strike = _number(row.get("bid")), _number(row.get("ask")), _number(row.get("strike"))
        mid, oi = _number(row.get("mid")), _number(row.get("open_interest"))
        dte = _number(row.get("dte"))
        status = str(row.get("market_data_status") or "open").lower()
        row_blockers: list[str] = []
        if spot is None or not _positive(spot) or not _positive(row.get("underlying_price")):
            row_blockers.append("missing_or_stale_underlying")
            metrics["missing_underlying_rows"] += 1
        if status not in TRADABLE_MARKET_STATUSES:
            row_blockers.append("invalid_market_status")
            metrics["invalid_status_rows"] += 1
        observed_at = row.get("provider_observed_at")
        if group_finished is None or observed_at is None or (group_finished - observed_at).total_seconds() > MAX_QUOTE_AGE_SECONDS:
            row_blockers.append("quote_age_stale")
            metrics["stale_rows"] += 1
        if not (_positive(bid) and ask is not None and ask >= bid and _positive(mid) and _positive(strike)):
            row_blockers.append("incomplete_or_crossed_quote")
        elif dte is not None and not 7 <= dte <= 120:
            row_blockers.append("unsupported_dte")
        elif spot is not None and _positive(spot) and not (0.70 <= strike / float(spot) <= 1.30):
            row_blockers.append("outside_moneyness_window")
        elif oi is None or oi < 100:
            row_blockers.append("illiquid_open_interest")
        elif ask - bid > max(0.10, 0.15 * mid):
            row_blockers.append("illiquid_spread")
        if row_blockers:
            blockers_by_row[_row_key(row)] = sorted(set(row_blockers))
            metrics["rejected_rows"] += 1
            continue
        accepted.append({**row, "option_type": option_type, "mid": mid, "bid": bid, "ask": ask, "strike": strike})
        metrics["eligible_rows"] += 1
    return sorted(accepted, key=lambda item: float(item["strike"])), blockers_by_row, metrics


def static_arbitrage_findings(
    rows: list[dict[str, Any]], *, spot: float | None, option_type: str, minimum_edge: float = MIN_PACKAGE_EDGE
) -> list[dict[str, Any]]:
    """Check executable worst-side packages, not midpoint mirages."""

    if spot is None or not _positive(spot):
        return []
    findings: list[dict[str, Any]] = []
    ordered = sorted(rows, key=lambda item: float(item["strike"]))
    for row in ordered:
        lower = max(0.0, float(spot) - row["strike"]) if option_type == "call" else max(0.0, row["strike"] - float(spot))
        upper = float(spot) if option_type == "call" else row["strike"]
        if row["ask"] + minimum_edge < lower:
            findings.append(_finding("intrinsic_lower_bound", (row["contract_id"],), lower - row["ask"], "buy"))
        if row["bid"] - minimum_edge > upper:
            findings.append(_finding("upper_bound", (row["contract_id"],), row["bid"] - upper, "sell"))
    for left, right in zip(ordered, ordered[1:], strict=False):
        width = right["strike"] - left["strike"]
        if option_type == "call":
            edge = left["bid"] - right["ask"] - width
            monotone_edge = right["bid"] - left["ask"]
        else:
            edge = right["bid"] - left["ask"] - width
            monotone_edge = left["bid"] - right["ask"]
        if edge > minimum_edge:
            findings.append(_finding("vertical_bound", (left["contract_id"], right["contract_id"]), edge, "package"))
        if monotone_edge > minimum_edge:
            findings.append(_finding("monotonicity", (left["contract_id"], right["contract_id"]), monotone_edge, "package"))
    for left, middle, right in zip(ordered, ordered[1:], ordered[2:], strict=False):
        left_width, right_width = middle["strike"] - left["strike"], right["strike"] - middle["strike"]
        if left_width <= 0 or right_width <= 0:
            continue
        # Long the two wings at ask, sell the middle at bid with non-uniform strike weights.
        wing_cost = (right_width * left["ask"] + left_width * right["ask"]) / (left_width + right_width)
        edge = middle["bid"] - wing_cost
        if edge > minimum_edge:
            findings.append(_finding("butterfly_convexity", (left["contract_id"], middle["contract_id"], right["contract_id"]), edge, "package"))
    return findings


def constrained_price_fit(rows: list[dict[str, Any]], *, spot: float, option_type: str) -> FitResult:
    strikes = np.asarray([row["strike"] for row in rows], dtype=float)
    mids = np.asarray([row["mid"] for row in rows], dtype=float)
    spreads = np.asarray([max(row["ask"] - row["bid"], 0.01) for row in rows], dtype=float)
    weights = 1.0 / np.square(spreads)
    lower = np.asarray([max(0.0, spot - strike) if option_type == "call" else max(0.0, strike - spot) for strike in strikes])
    upper = np.asarray([spot if option_type == "call" else strike for strike in strikes])
    constraints = _fit_constraints(strikes, option_type)

    def objective(value: np.ndarray) -> float:
        return float(np.dot(weights, np.square(value - mids)))

    result = minimize(
        objective,
        x0=np.clip(mids, lower, upper),
        method="SLSQP",
        bounds=list(zip(lower, upper, strict=True)),
        constraints=constraints,
        options={"ftol": 1e-10, "maxiter": 400, "disp": False},
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        return FitResult((), "fit_failed", None, {"solver_message": str(result.message), "iterations": int(getattr(result, "nit", 0))})
    rmse = float(np.sqrt(np.mean(np.square(result.x - mids))))
    return FitResult(tuple(float(value) for value in result.x), "succeeded", rmse, {"solver": "SLSQP", "iterations": int(getattr(result, "nit", 0))})


def relative_values(
    rows: list[dict[str, Any]],
    fit: FitResult,
    *,
    spot: float,
    option_type: str,
    model_revision: str,
    static_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fitted = np.asarray(fit.fitted)
    residuals = np.asarray([row["mid"] for row in rows]) - fitted
    candidate_indexes = set(np.argsort(residuals)[:5]) | set(np.argsort(residuals)[-5:])
    flagged_contracts = {contract for finding in static_findings for contract in finding["contract_ids"]}
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        lower, upper = _leave_one_out_interval(rows, index, spot=spot, option_type=option_type, fallback=float(fitted[index])) if index in candidate_indexes else (float(fitted[index]), float(fitted[index]))
        threshold = max(MIN_PACKAGE_EDGE, 0.25 * (row["ask"] - row["bid"]))
        cheap_edge = lower - row["ask"] - COST_ALLOWANCE_PER_LEG
        rich_edge = row["bid"] - upper - COST_ALLOWANCE_PER_LEG
        classification, edge, side = "rejected", None, None
        if row["contract_id"] in flagged_contracts:
            classification, edge, side = "historical_static_arbitrage_candidate", max(cheap_edge, rich_edge), "package"
        elif cheap_edge > threshold:
            classification, edge, side = "relative_cheap", cheap_edge, "cheap"
        elif rich_edge > threshold:
            classification, edge, side = "relative_rich", rich_edge, "rich"
        output.append({
            "contract_id": row["contract_id"], "classification": classification,
            "fair_low": lower, "fair_high": upper, "modeled_net_edge": edge,
            "edge_side": side, "confidence": _confidence(row, lower, upper),
            "quality_status": "eligible", "blockers": [],
            "evidence": {"fit_method": "history-v3-price-shape", "fit_rmse": fit.rmse, "threshold": threshold,
                         "static_findings": [item for item in static_findings if row["contract_id"] in item["contract_ids"]]},
            "model_revision": model_revision,
        })
    return output


def _leave_one_out_interval(rows: list[dict[str, Any]], index: int, *, spot: float, option_type: str, fallback: float) -> tuple[float, float]:
    if len(rows) <= MIN_ELIGIBLE_POINTS:
        return fallback, fallback
    scenarios: list[float] = []
    for quote_field in ("bid", "mid", "ask"):
        subset = [{**row, "mid": row[quote_field]} for row_index, row in enumerate(rows) if row_index != index]
        fit = constrained_price_fit(subset, spot=spot, option_type=option_type)
        if fit.status != "succeeded":
            continue
        strikes = np.asarray([row["strike"] for row in subset], dtype=float)
        values = np.asarray(fit.fitted)
        scenarios.append(float(np.interp(rows[index]["strike"], strikes, values)))
    return (min(scenarios), max(scenarios)) if scenarios else (fallback, fallback)


def _fit_constraints(strikes: np.ndarray, option_type: str) -> list[LinearConstraint]:
    count = len(strikes)
    matrices: list[np.ndarray] = []
    lower: list[float] = []
    upper: list[float] = []
    for index in range(count - 1):
        row = np.zeros(count)
        row[index], row[index + 1] = 1.0, -1.0
        width = strikes[index + 1] - strikes[index]
        if option_type == "call":
            matrices.append(row); lower.append(0.0); upper.append(float(width))
        else:
            matrices.append(row); lower.append(float(-width)); upper.append(0.0)
    for index in range(count - 2):
        first, second = strikes[index + 1] - strikes[index], strikes[index + 2] - strikes[index + 1]
        row = np.zeros(count)
        row[index] = second / first
        row[index + 1] = -(second / first + 1.0)
        row[index + 2] = 1.0
        matrices.append(row); lower.append(0.0); upper.append(np.inf)
    return [LinearConstraint(np.asarray(matrices), np.asarray(lower), np.asarray(upper))] if matrices else []


def rejected_value(row: dict[str, Any], model_revision: str, blockers: list[str], static_findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "contract_id": row.get("contract_id"), "classification": "rejected", "fair_low": None, "fair_high": None,
        "modeled_net_edge": None, "edge_side": None, "confidence": 0.0, "quality_status": "rejected",
        "blockers": list(blockers), "evidence": {"static_findings": static_findings}, "model_revision": model_revision,
    }


def _finding(kind: str, contract_ids: tuple[Any, ...], edge: float, side: str) -> dict[str, Any]:
    ordered = [int(value) for value in contract_ids]
    return {
        "kind": kind,
        "contract_ids": ordered,
        "leg_sides": [side] if len(ordered) == 1 else ["package"] * len(ordered),
        "package_identity": {"kind": kind, "ordered_contract_ids": ordered, "leg_sides": [side] if len(ordered) == 1 else ["package"] * len(ordered)},
        "edge": float(edge),
        "side": side,
    }


def _confidence(row: dict[str, Any], fair_low: float, fair_high: float) -> float:
    width = max(row["ask"] - row["bid"], 0.01)
    uncertainty = max(fair_high - fair_low, 0.0)
    return round(max(0.0, min(1.0, 1.0 - (width + uncertainty) / max(row["mid"], 0.01))), 6)


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _positive(value: Any) -> bool:
    parsed = _number(value)
    return parsed is not None and parsed > 0


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Use the contract identity when present; tests/replay rows may omit it."""

    return (
        row.get("contract_id"), row.get("strike"), row.get("option_type"),
        row.get("provider_observed_at"),
    )
