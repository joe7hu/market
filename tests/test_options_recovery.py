from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.core.decision import MARKET_TZ
from investment_panel.core.options_event_tape import event_strip_expiration_after_fill
from investment_panel.core.options_recovery import (
    FEE_PER_CONTRACT_LEG,
    ExecutableLeg,
    QuoteCapture,
    evaluate_lifecycle,
    executable_entry_price,
    executable_exit_price,
    lifecycle_return,
    staged_exit_quantities,
)


NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _long(*, bid: float, ask: float, contract_id: str = "NVDA260821C00100000") -> ExecutableLeg:
    return ExecutableLeg(contract_id=contract_id, side="buy", bid=bid, ask=ask, bid_size=10, ask_size=10)


def _capture(minutes: int, *, bid: float, ask: float, session: int, dte: int = 20, **kwargs: object) -> QuoteCapture:
    return QuoteCapture(
        observed_at=NOW + timedelta(minutes=minutes), legs=(_long(bid=bid, ask=ask),),
        session_number=session, dte=dte, **kwargs,
    )


def test_executable_prices_use_ask_entry_bid_exit_and_symmetric_slippage() -> None:
    leg = _long(bid=1.90, ask=2.00)
    assert executable_entry_price([leg]) == pytest.approx(2.01)
    assert executable_exit_price([leg]) == pytest.approx(1.89)

    debit = [
        _long(bid=1.90, ask=2.00, contract_id="buy"),
        ExecutableLeg(contract_id="sell", side="sell", bid=1.00, ask=1.10, bid_size=10, ask_size=10),
    ]
    assert executable_entry_price(debit) == pytest.approx(1.02)
    assert executable_exit_price(debit) == pytest.approx(0.78)


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        (1, (0, 1, 0)), (2, (1, 1, 0)), (3, (1, 1, 1)),
        (4, (1, 2, 1)), (5, (1, 3, 1)), (6, (2, 3, 1)),
        (7, (2, 3, 2)), (8, (2, 4, 2)), (9, (2, 5, 2)),
        (10, (3, 5, 2)),
    ],
)
def test_staged_exit_largest_remainder_allocation(quantity: int, expected: tuple[int, int, int]) -> None:
    assert staged_exit_quantities(quantity) == expected
    assert sum(expected) == quantity


def test_lifecycle_uses_only_post_publication_quotes_and_cost_adjusted_staging() -> None:
    result = evaluate_lifecycle(
        published_at=NOW,
        quantity=4,
        captures=[
            _capture(-1, bid=1.00, ask=1.10, session=0),  # unavailable before publication
            _capture(1, bid=0.99, ask=1.00, session=1),
            _capture(16, bid=2.05, ask=2.10, session=1),
            _capture(31, bid=3.10, ask=3.15, session=2),
            _capture(46, bid=4.12, ask=4.18, session=3),
        ],
    )
    assert result.classification == "captured"
    assert result.entry_fill_at == NOW + timedelta(minutes=1)
    assert [fill.quantity for fill in result.exit_fills] == [1, 2, 1]
    assert [fill.reason for fill in result.exit_fills] == ["target_2x", "target_3x", "target_4x"]
    assert result.time_to_2x_sessions == 0
    assert result.time_to_3x_sessions == 1
    assert result.time_to_4x_sessions == 2
    realized = lifecycle_return(
        entry_price=result.entry_fill_price or 0,
        exits=result.exit_fills,
        quantity=4,
        leg_count=1,
    )
    assert realized is not None and realized > 1.5
    assert result.entry_fee == pytest.approx(FEE_PER_CONTRACT_LEG * 4)
    assert result.exit_fee == pytest.approx(FEE_PER_CONTRACT_LEG * 4)


def test_single_contract_exits_entirely_at_three_x() -> None:
    result = evaluate_lifecycle(
        published_at=NOW,
        quantity=1,
        captures=[_capture(1, bid=0.99, ask=1.00, session=1), _capture(16, bid=3.10, ask=3.15, session=2)],
    )
    assert result.classification == "captured"
    assert [(fill.quantity, fill.reason) for fill in result.exit_fills] == [(1, "target_3x")]


def test_unfillable_after_two_scheduled_captures_is_not_a_loss() -> None:
    result = evaluate_lifecycle(
        published_at=NOW,
        quantity=1,
        entry_limit=1.00,
        captures=[_capture(1, bid=1.00, ask=1.10, session=1), _capture(16, bid=1.05, ask=1.15, session=1)],
    )
    assert result.classification == "unfilled"
    assert result.entry_fill_at is None


def test_missing_same_contract_future_quote_is_unmeasurable_not_a_loss() -> None:
    result = evaluate_lifecycle(
        published_at=NOW,
        quantity=1,
        captures=[
            _capture(1, bid=0.99, ask=1.00, session=1),
            _capture(16, bid=1.50, ask=1.55, session=1, continuity_ok=False, reason="replacement_contract_only"),
        ],
    )
    assert result.classification == "unmeasurable"
    assert result.unmeasurable_reason == "replacement_contract_only"


def test_half_premium_loss_is_a_deterministic_executable_hard_exit() -> None:
    result = evaluate_lifecycle(
        published_at=NOW,
        quantity=2,
        captures=[_capture(1, bid=0.99, ask=1.00, session=1), _capture(16, bid=0.49, ask=0.51, session=1)],
    )
    assert result.classification == "captured"
    assert result.exit_fills[0].reason == "hard_loss"
    assert result.exit_fills[0].quantity == 2


def test_late_event_fill_uses_fill_relative_targets_and_ten_session_exit() -> None:
    late_target = evaluate_lifecycle(
        published_at=NOW,
        quantity=4,
        captures=[
            _capture(1, bid=0.99, ask=1.00, session=9),
            _capture(16, bid=2.05, ask=2.10, session=10),
        ],
    )
    assert late_target.entry_session_number == 9
    assert late_target.time_to_2x_sessions == 1

    late_exit = evaluate_lifecycle(
        published_at=NOW,
        quantity=1,
        captures=[
            _capture(1, bid=0.99, ask=1.00, session=9, dte=20),
            _capture(16, bid=0.80, ask=0.82, session=18, dte=20),
            _capture(31, bid=0.80, ask=0.82, session=19, dte=20),
        ],
    )
    assert late_exit.exit_fills[0].reason == "time_or_dte_exit"
    assert late_exit.exit_fills[0].session_number == 10


def test_late_fill_extends_event_tape_through_ten_subsequent_market_sessions() -> None:
    fill_at = datetime(2026, 8, 14, 15, tzinfo=MARKET_TZ)

    expires_at = event_strip_expiration_after_fill(fill_at)

    assert expires_at == datetime(2026, 8, 28, 16, 15, tzinfo=MARKET_TZ)
