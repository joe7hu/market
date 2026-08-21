from __future__ import annotations

from datetime import date, timedelta

from investment_panel.analysis.preopen_forecast import (
    evaluate_qqq_forecast,
    qqq_preopen_forecast,
)


def test_qqq_forecast_evaluation_stays_pending_without_same_day_observation() -> None:
    history = [
        {"date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(), "close": 400 + index * 0.35}
        for index in range(60)
    ]
    forecast = qqq_preopen_forecast(history)

    pending = evaluate_qqq_forecast(forecast, None)
    observed = evaluate_qqq_forecast(
        forecast,
        {"price": forecast["prior_close"] * 1.01, "observed_at": "2026-03-02T15:00:00Z", "source_kind": "quote"},
    )

    assert pending["status"] == "pending"
    assert pending["reason"] == "no_same_day_qqq_observation_at_publication_as_of"
    assert observed["status"] == "observed"
    assert observed["actual_return_pct"] == 1.0
