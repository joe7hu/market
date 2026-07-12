from investment_panel.analysis.option_recommendation import recommendation_fields


def test_long_setup_is_research_only_with_executable_guidance() -> None:
    fields = recommendation_fields({
        "structure": "long_call", "state": "SETUP", "blockers": [],
        "underlying_price": 100, "break_even": 108, "entry_price": 3,
        "buy_under": 2.8, "max_profit": None,
    })
    assert fields["recommendation_state"] == "RESEARCH_SETUP"
    assert fields["advisory_action"] == "RESEARCH — BUY TO OPEN"
    assert fields["paper_ready"] is False
    assert fields["maximum_entry"] == 2.8
    assert fields["break_even_move_pct"] == 0.08
    assert fields["no_trade_reason"] == "forward_calibration_not_mature"


def test_cash_secured_put_ready_uses_credit_and_assignment_invalidation() -> None:
    fields = recommendation_fields({
        "structure": "cash_secured_put", "state": "READY", "blockers": [],
        "underlying_price": 100, "break_even": 92, "entry_price": 2,
        "max_profit": 200,
    })
    assert fields["recommendation_state"] == "PAPER_READY"
    assert fields["advisory_action"] == "PAPER — SELL CASH-SECURED PUT"
    assert fields["minimum_credit"] == 2
    assert fields["profit_take_option_price"] == 1
    assert "$92.00" in fields["invalidation"]
