from datetime import UTC, datetime

from investment_panel.domain.factors import EvaluationContext, InputSnapshot
from investment_panel.domain.signals import MOMENTUM_DIRECTION, MOMENTUM_VOLATILITY, evaluate_signals


def test_signals_reuse_shared_factor_context_and_preserve_meaning() -> None:
    snapshot = InputSnapshot(
        "signal-scope", datetime(2026, 9, 5, 13, tzinfo=UTC),
        {"daily_closes": [100 + index for index in range(25)]}, evidence_refs=("prices:1",),
    )
    context = EvaluationContext(snapshot)
    results = evaluate_signals(snapshot, [MOMENTUM_DIRECTION.key, MOMENTUM_VOLATILITY.key], context=context)
    assert results[MOMENTUM_DIRECTION.key].direction == "long"
    assert results[MOMENTUM_VOLATILITY.key].evidence["interpretation"] == "momentum_with_risk_context"
    assert len([item for item in context.trace if item["key"] == "price.momentum"]) == 1
    assert results[MOMENTUM_DIRECTION.key].evidence_refs == ("prices:1",)


def test_signal_unavailability_is_explicit() -> None:
    snapshot = InputSnapshot("signal-missing", datetime(2026, 9, 5, 13, tzinfo=UTC), {"daily_closes": [100, None, 120]})
    result = evaluate_signals(snapshot, MOMENTUM_DIRECTION.key)[MOMENTUM_DIRECTION.key]
    assert result.available is False
    assert result.score is None
    assert result.blockers
