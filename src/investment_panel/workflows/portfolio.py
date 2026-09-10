"""Portfolio, ledger, and watchlist application actions."""

from __future__ import annotations

from typing import Any

from investment_panel.settings import AppConfig
from investment_panel.workflows.portfolio_mutations import (
    delete_watchlist_symbol,
    populate_watchlist_symbol_data,
    save_watchlist_symbol,
)
from investment_panel.infrastructure.postgres.portfolio_ledger import (
    manual_account_snapshot as manual_account_snapshot_owner,
    preview_manual_account_reconciliation as preview_manual_account_owner,
    preview_portfolio_transaction as preview_transaction_owner,
    record_manual_account_reconciliation as record_manual_account_owner,
    record_portfolio_transaction as record_transaction_owner,
    reverse_portfolio_transaction as reverse_transaction_owner,
)
from investment_panel.infrastructure.postgres.user_state import (
    portfolio_rows as portfolio_rows_owner,
    table_payload as table_payload_owner,
    watchlist_rows as watchlist_rows_owner,
)

__all__ = ["PortfolioActions"]

class PortfolioActions:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def preview_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]:
        return preview_transaction_owner(self.config, transaction)

    def manual_account(self) -> dict[str, Any]:
        return manual_account_snapshot_owner(self.config)

    def preview_manual_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return preview_manual_account_owner(self.config, payload)

    def record_manual_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return record_manual_account_owner(self.config, payload)

    def record_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]:
        return self._transaction_payload(record_transaction_owner(self.config, transaction))

    def reverse_transaction(self, transaction_id: str, reversal: dict[str, Any]) -> dict[str, Any]:
        saved = reverse_transaction_owner(
            self.config,
            transaction_id,
            idempotency_key=str(reversal.get("idempotency_key") or ""),
            notes=str(reversal.get("notes") or ""),
        )
        return self._transaction_payload(saved)

    def save_watchlist_symbol(self, item: dict[str, Any]) -> dict[str, Any]:
        saved = save_watchlist_symbol(self.config, item)
        refresh = populate_watchlist_symbol_data(self.config, saved["symbol"], saved.get("asset_class"))
        return {
            "watchlist_symbol": saved,
            "data_refresh": refresh,
            "watchlist": table_payload_owner(watchlist_rows_owner(self.config)),
        }

    def delete_watchlist_symbol(self, symbol: str) -> dict[str, Any]:
        deleted = delete_watchlist_symbol(self.config, symbol)
        return {
            "watchlist_symbol": deleted,
            "watchlist": table_payload_owner(watchlist_rows_owner(self.config)),
        }

    def _transaction_payload(self, transaction: dict[str, Any]) -> dict[str, Any]:
        return {
            "transaction": transaction,
            "portfolio": table_payload_owner(portfolio_rows_owner(self.config)),
        }
