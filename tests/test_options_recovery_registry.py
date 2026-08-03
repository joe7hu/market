from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from investment_panel.core.options_recovery_paper import (
    DAILY_LOSS_HALT,
    RecoveryRiskContext,
    qualified_for_paper,
    size_recovery_position,
)
from investment_panel.core.options_recovery_registry import (
    EventSpot,
    RecoveryContractQuote,
    RecoveryEventState,
    SHOCK_CONTINUATION_PUT_V1,
    SHOCK_REVERSAL_CALL_V1,
    contract_gate,
    signal_for,
    validate_mutation,
)
from investment_panel.core.options_recovery_ticket import (
    RECOVERY_TICKET_VERSION,
    build_recovery_ticket_v4,
    occ_symbol,
)


NOW = datetime(2026, 8, 3, 15, 0, tzinfo=UTC)


def _event(prices: list[float]) -> RecoveryEventState:
    return RecoveryEventState(
        event_id="event-1",
        symbol="NVDA",
        reference_price=120.0,
        event_low=100.0,
        started_at=NOW - timedelta(days=5),
        spots=tuple(
            EventSpot(NOW + timedelta(minutes=15 * index), NOW + timedelta(minutes=15 * index), price)
            for index, price in enumerate(prices)
        ),
    )


def _quote(*, option_type: str = "call", **overrides: object) -> RecoveryContractQuote:
    values: dict[str, object] = {
        "contract_id": 42,
        "occ_symbol": "NVDA260814C00100000",
        "option_type": option_type,
        "expiration": date(2026, 8, 14),
        "strike": 100.0,
        "bid": 2.0,
        "ask": 2.2,
        "bid_size": 10,
        "ask_size": 10,
        "open_interest": 100,
        "delta": 0.45 if option_type == "call" else -0.45,
        "observed_at": NOW,
        "available_at": NOW,
    }
    values.update(overrides)
    return RecoveryContractQuote(**values)  # type: ignore[arg-type]


def test_reversal_and_continuation_are_independent_typed_families() -> None:
    reversal = signal_for(_event([100, 100.5, 101, 101.9, 102.1, 103]), SHOCK_REVERSAL_CALL_V1)
    assert reversal.active
    assert "four_slot_breakout" in reversal.reasons

    continuation = signal_for(_event([102, 101, 100.5, 99.5, 99.0]), SHOCK_CONTINUATION_PUT_V1)
    assert continuation.active
    assert "fresh_event_low" in continuation.reasons


def test_contract_gates_fail_closed_for_stale_or_nonexecutable_contracts() -> None:
    result = contract_gate(
        _quote(bid_size=0, available_at=NOW - timedelta(days=1), open_interest=99),
        family=SHOCK_REVERSAL_CALL_V1,
        as_of=NOW,
    )
    assert not result.eligible
    assert set(result.blockers) >= {
        "displayed_size_required",
        "current_session_quote_required",
        "open_interest_below_100",
    }
    assert contract_gate(_quote(option_type="put"), family=SHOCK_REVERSAL_CALL_V1, as_of=NOW).eligible is False


def test_agent_mutation_cannot_introduce_unknown_registry_settings() -> None:
    assert validate_mutation(SHOCK_REVERSAL_CALL_V1, {"reversal_min_rebound_pct": 0.03})[
        "reversal_min_rebound_pct"
    ] == pytest.approx(0.03)
    with pytest.raises(ValueError, match="unsupported recovery strategy parameter"):
        validate_mutation(SHOCK_REVERSAL_CALL_V1, {"made_up_edge": 99})


def test_v4_ticket_has_exact_occ_contract_and_executable_risk() -> None:
    ticket = build_recovery_ticket_v4(
        decision_id="decision-1",
        event_id="event-1",
        symbol="NVDA",
        family=SHOCK_REVERSAL_CALL_V1,
        expiration=date(2026, 8, 14),
        quantity=2,
        invalidation="spot closes below the event low",
        created_at=NOW,
        legs=[{
            "contract_id": 42,
            "option_type": "call",
            "strike": 100,
            "bid": 2.0,
            "ask": 2.2,
            "bid_size": 10,
            "ask_size": 10,
            "quote_time": NOW,
            "open_interest": 100,
            "volume": 30,
        }],
    )
    assert ticket["ticket_version"] == RECOVERY_TICKET_VERSION
    assert ticket["legs"][0]["occ_symbol"] == occ_symbol("NVDA", date(2026, 8, 14), "call", 100)
    assert ticket["entry"]["limit_price"] == pytest.approx(2.22)
    assert ticket["risk"]["total_risk"] > 400
    assert ticket["exit_ladder"]["targets"][1]["multiple"] == 3.0


def test_recovery_risk_limits_and_qualified_session_gate_fail_closed() -> None:
    decision = size_recovery_position(100.0, RecoveryRiskContext())
    assert decision.quantity == 5
    assert decision.total_risk == 500
    halted = size_recovery_position(100.0, RecoveryRiskContext(daily_realized_unrealized_pnl=-DAILY_LOSS_HALT))
    assert halted.quantity == 0
    assert "daily_loss_halt" in halted.blockers
    assert not qualified_for_paper(NOW, NOW + timedelta(days=3))
    assert qualified_for_paper(NOW, NOW + timedelta(days=7))
