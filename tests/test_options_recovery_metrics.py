from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from investment_panel.core.options_recovery import ExecutableLeg, QuoteCapture, evaluate_lifecycle
from investment_panel.core.options_recovery_metrics import counterfactual_metrics, recovery_promotion_passes
from investment_panel.database.options_recovery_learning import RecoveryLearningRepository


NOW = datetime(2026, 8, 3, 14, tzinfo=UTC)


def _capture(minutes: int, *, bid: float, ask: float, session: int) -> QuoteCapture:
    return QuoteCapture(
        observed_at=NOW + timedelta(minutes=minutes),
        legs=(ExecutableLeg("contract", "buy", bid, ask, 10, 10),),
        session_number=session,
        dte=20,
    )


def test_counterfactual_horizons_use_executable_bid_marks() -> None:
    captures = [
        _capture(1, bid=0.99, ask=1.00, session=1),
        _capture(8, bid=1.5, ask=1.6, session=2),
        _capture(16, bid=2.0, ask=2.1, session=4),
    ]
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


def test_promotion_fails_closed_when_recovery_paper_kill_switch_is_off() -> None:
    class Cohorts:
        def __init__(self) -> None:
            self.requested_switches: list[bool] = []

        def program_eligibility(self, *, recovery_paper_actions_enabled: bool):
            self.requested_switches.append(recovery_paper_actions_enabled)
            return SimpleNamespace(
                eligible=False,
                blockers=("recovery_paper_actions_disabled",),
                as_dict=lambda: {"eligible": False, "blockers": ["recovery_paper_actions_disabled"]},
            )

    repository = RecoveryLearningRepository.__new__(RecoveryLearningRepository)
    repository.cohorts = Cohorts()
    repository.metrics = lambda _family: {  # type: ignore[method-assign]
        "independent_events": 20,
        "shadow_signals": 100,
        "paper_fills": 30,
        "net_expectancy": 0.1,
        "lower_95_expectancy": 0.01,
        "calibration_gap": 0.09,
        "max_ticker_gain_concentration": 0.2,
        "unresolved_defects": False,
    }

    status = repository.promotion_status("shock_reversal_call_v1")
    assert status["eligible"] is False
    assert repository.auto_promote_eligible(enabled=True) == 0
    assert repository.cohorts.requested_switches == [False, False]
