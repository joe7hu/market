"""Pure, point-in-time QQQ pre-open forecast math."""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Any


FORECAST_MODEL_VERSION = "qqq_preopen_stat_ensemble_v1"


def qqq_preopen_forecast(history: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [_float(row.get("close")) for row in history if _float(row.get("close")) is not None]
    closes = [value for value in closes if value and value > 0]
    if len(closes) < 30:
        return {"status": "insufficient_history", "model_version": FORECAST_MODEL_VERSION}
    last = closes[-1]
    returns = [(closes[index] / closes[index - 1]) - 1 for index in range(1, len(closes))]
    trailing = returns[-60:]
    realized_vol = pstdev(trailing[-20:]) if len(trailing) >= 20 else pstdev(trailing)
    mom_5d = (last / closes[-6] - 1) if len(closes) >= 6 else 0.0
    mom_20d = (last / closes[-21] - 1) if len(closes) >= 21 else 0.0
    sma_50 = mean(closes[-50:]) if len(closes) >= 50 else mean(closes)
    sma_200 = mean(closes[-200:]) if len(closes) >= 200 else sma_50
    trend = (last / sma_50 - 1) * 0.15 + (sma_50 / sma_200 - 1) * 0.10
    predicted_return = max(-0.025, min(0.025, 0.12 * mom_5d + 0.04 * mom_20d + trend - 0.08 * returns[-1]))
    half_range = max(0.006, min(0.035, 0.75 * realized_vol + abs(predicted_return) * 0.45))
    expected_close = last * (1 + predicted_return)
    low = last * (1 + predicted_return - half_range)
    high = last * (1 + predicted_return + half_range)
    bias = "bullish" if predicted_return > 0.0025 else "bearish" if predicted_return < -0.0025 else "neutral"
    return {
        "status": "ok", "model_version": FORECAST_MODEL_VERSION, "symbol": "QQQ",
        "prior_close": round(last, 2), "expected_close": round(expected_close, 2),
        "expected_return_pct": round(predicted_return * 100, 2), "low": round(low, 2),
        "high": round(high, 2), "support": round(low, 2), "resistance": round(high, 2),
        "range_pct": round(half_range * 200, 2), "bias": bias,
        "features": {
            "realized_vol_20d_pct": round(realized_vol * 100, 2),
            "momentum_5d_pct": round(mom_5d * 100, 2),
            "momentum_20d_pct": round(mom_20d * 100, 2),
            "distance_to_sma50_pct": round((last / sma_50 - 1) * 100, 2),
            "sma50_vs_sma200_pct": round((sma_50 / sma_200 - 1) * 100, 2),
        },
    }


def evaluate_qqq_forecast(forecast: dict[str, Any], actual: dict[str, Any] | None) -> dict[str, Any]:
    """Close the forecast loop with a point-in-time QQQ observation."""

    if forecast.get("status") != "ok":
        return {
            "status": "unavailable",
            "model_version": forecast.get("model_version") or FORECAST_MODEL_VERSION,
            "reason": "forecast_not_available",
        }
    if not actual or actual.get("price") is None:
        return {
            "status": "pending",
            "model_version": forecast.get("model_version") or FORECAST_MODEL_VERSION,
            "reason": "no_same_day_qqq_observation_at_publication_as_of",
        }
    try:
        prior_close = float(forecast["prior_close"])
        actual_price = float(actual["price"])
    except (KeyError, TypeError, ValueError):
        return {
            "status": "pending",
            "model_version": forecast.get("model_version") or FORECAST_MODEL_VERSION,
            "reason": "incomplete_actual_observation",
        }
    if prior_close <= 0:
        return {
            "status": "pending",
            "model_version": forecast.get("model_version") or FORECAST_MODEL_VERSION,
            "reason": "invalid_prior_close",
        }
    actual_return = (actual_price / prior_close - 1) * 100
    expected_return = float(forecast.get("expected_return_pct") or 0)
    low = forecast.get("low")
    high = forecast.get("high")
    low_value = float(low) if low is not None else None
    high_value = float(high) if high is not None else None
    return {
        "status": "observed",
        "model_version": forecast.get("model_version") or FORECAST_MODEL_VERSION,
        "actual_price": round(actual_price, 2),
        "actual_return_pct": round(actual_return, 2),
        "expected_return_pct": round(expected_return, 2),
        "absolute_error_pct": round(abs(actual_return - expected_return), 2),
        "within_forecast_range": (
            low_value <= actual_price <= high_value
            if low_value is not None and high_value is not None else None
        ),
        "direction_correct": (
            (expected_return >= 0 and actual_return >= 0)
            or (expected_return < 0 and actual_return < 0)
        ),
        "actual_observed_at": actual.get("observed_at"),
        "actual_source_kind": actual.get("source_kind"),
    }


def backtest_qqq_preopen_model(history: list[dict[str, Any]], *, min_train: int = 80) -> dict[str, Any]:
    if len(history) <= min_train + 5:
        return {"status": "insufficient_history", "model_version": FORECAST_MODEL_VERSION}
    errors: list[float] = []
    range_hits = direction_hits = tested = 0
    for index in range(min_train, len(history) - 1):
        forecast = qqq_preopen_forecast(history[: index + 1])
        actual_close = _float(history[index + 1].get("close"))
        prior_close = _float(history[index].get("close"))
        expected = _float(forecast.get("expected_close"))
        if forecast.get("status") != "ok" or actual_close is None or prior_close is None or prior_close <= 0 or expected is None:
            continue
        errors.append(abs(actual_close / prior_close - expected / prior_close) * 100)
        range_hits += int(float(forecast["low"]) <= actual_close <= float(forecast["high"]))
        predicted_direction = float(forecast["expected_return_pct"])
        actual_direction = (actual_close / prior_close - 1) * 100
        direction_hits += int((predicted_direction >= 0 and actual_direction >= 0) or (predicted_direction < 0 and actual_direction < 0))
        tested += 1
    if not tested:
        return {"status": "insufficient_history", "model_version": FORECAST_MODEL_VERSION}
    return {
        "status": "ok", "model_version": FORECAST_MODEL_VERSION, "observations": tested,
        "mae_pct": round(mean(errors), 2), "range_hit_rate_pct": round(range_hits / tested * 100, 1),
        "direction_hit_rate_pct": round(direction_hits / tested * 100, 1),
    }


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
