from __future__ import annotations

from investment_panel.database.portfolio_ledger import TRANSACTION_TYPES


def test_manual_cash_transactions_are_ledger_transaction_types() -> None:
    assert {"cash_deposit", "cash_withdrawal"} <= TRANSACTION_TYPES
