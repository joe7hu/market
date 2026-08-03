from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_panel.core.options_recovery import ExecutableLeg, QuoteCapture, evaluate_lifecycle
from investment_panel.core.options_recovery_metrics import counterfactual_metrics, recovery_promotion_passes


NOW = datetime(2026, 8, 3, 14, tzinfo=UTC)


def _capture(minutes: int, *, bid: float, ask: float, session: int) -> QuoteCapture:
    return QuoteCapture(
        observed_at=NOW + timedelta(minutes=minutes),
        legs=(ExecutableLeg("contract", "buy", bid, ask, 10, 10),),
        session_number=session,
        dte=20,
    )


def test_counterfactual_horizons_use_executable_bid_marks() -> None:
    captures = [_capture(1, bid=0.99, ask=1.00, session=1), _capture(16, bid=2.0, ask=2.1, session=3)]
    lifecycle = evaluate_lifecycle(published_at=NOW, quantity=1, captures=captures)
    metrics = counterfactual_metrics(lifecycle, captures)
    assert metrics.return_1_session is not None
    assert metrics.return_3_session is not None
    assert metrics.return_3_session > metrics.return_1_session


def test_promotion_requires_the_full_recovery_denominator() -> None:
    metrics = {
        "independent_events": 20,
        "shadow_signals": 100,
        "paper_fills": 30,
        "net_expectancy": 0.1,
        "lower_95_expectancy": 0.01,
        "calibration_gap": 0.09,
        "max_ticker_gain_concentration": 0.20,
        "unresolved_defects": False,
    }
    assert recovery_promotion_passes(metrics)
    assert not recovery_promotion_passes({**metrics, "paper_fills": 29})
