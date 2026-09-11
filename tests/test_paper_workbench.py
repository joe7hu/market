from decimal import Decimal

from investment_panel.infrastructure.postgres.paper_workbench import (
    paper_trade_payload,
)


def _row(**updates):
    row = {
        "paper_order_id": "order-1",
        "symbol": "QQQ",
        "asset_class": "etf",
        "decision_id": "decision-1",
        "contract_id": 1,
        "paper_only": True,
        "paper_status": "staged",
        "side": "buy",
        "quantity": Decimal("1"),
        "requested_quantity": Decimal("1"),
        "staged_limit_price": Decimal("1.25"),
        "structure": "long_option",
        "entry_quantity": Decimal("0"),
        "exit_quantity": Decimal("0"),
        "entry_units": Decimal("0"),
        "exit_units": Decimal("0"),
        "missing_fees": 0,
        "invalid_fills": 0,
        "fill_multipliers_verified": None,
        "order_multiplier": Decimal("100"),
        "fill_rows": [],
        "decision_evidence": [],
    }
    row.update(updates)
    return row


def test_staged_order_does_not_turn_limit_or_shadow_data_into_a_fill():
    result = paper_trade_payload(
        _row(shadow_id="shadow-1", shadow_entry_at="2026-09-11T14:00:00+00:00")
    )
    assert result["lifecycle"] == "staged"
    assert result["entry_price"] is None
    assert result["realized_pnl"] is None
    assert result["staged_limit_price"] == 1.25
    assert result["related_research"]["shadow_id"] == "shadow-1"


def test_partial_exit_uses_verified_journal_fills_and_fees():
    result = paper_trade_payload(
        _row(
            paper_status="partial_exited",
            entry_quantity=Decimal("2"),
            exit_quantity=Decimal("1"),
            entry_units=Decimal("100"),
            exit_units=Decimal("60"),
            entry_fees=Decimal("1"),
            exit_fees=Decimal("0.50"),
            actual_fees=Decimal("1.50"),
            missing_fees=0,
            invalid_fills=0,
            fill_multipliers_verified=True,
            fill_rows=[{"action": "paper_entry"}, {"action": "paper_exit:take_profit"}],
        )
    )
    assert result["lifecycle"] == "partial_exited"
    assert result["filled_quantity"] == 2
    assert result["remaining_quantity"] == 1
    assert result["realized_pnl"] == 999.0
    assert result["net_pnl"] is None


def test_missing_multiplier_keeps_options_pnl_unknown():
    result = paper_trade_payload(
        _row(
            paper_status="exited",
            entry_quantity=Decimal("1"),
            exit_quantity=Decimal("1"),
            entry_units=Decimal("50"),
            exit_units=Decimal("20"),
            entry_fees=Decimal("0.65"),
            exit_fees=Decimal("0.65"),
            actual_fees=Decimal("1.30"),
            fill_multipliers_verified=None,
        )
    )
    assert result["reconciliation_status"] == "partial"
    assert result["realized_pnl"] is None
    assert (
        "contract_multiplier_evidence_missing_or_conflicting"
        in result["evidence_reasons"]
    )
