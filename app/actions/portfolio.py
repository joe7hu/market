"""Portfolio, ledger, and watchlist application actions."""

from __future__ import annotations

from typing import Any, Callable

from app.data_access import mutations
from investment_panel.database.portfolio_ledger import (
    preview_portfolio_transaction as preview_transaction_owner,
    record_portfolio_transaction as record_transaction_owner,
    reverse_portfolio_transaction as reverse_transaction_owner,
)
from investment_panel.database.user_state import (
    portfolio_rows as portfolio_rows_owner,
    table_payload as table_payload_owner,
    watchlist_rows as watchlist_rows_owner,
)

__all__ = ["PortfolioActions"]

class PortfolioActions:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        portfolio_rows: Callable[..., list[dict[str, Any]]] | None = None,
        table_payload: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
        preview_transaction: Callable[..., dict[str, Any]] | None = None,
        record_transaction: Callable[..., dict[str, Any]] | None = None,
        reverse_transaction: Callable[..., dict[str, Any]] | None = None,
        watchlist_rows: Callable[..., list[dict[str, Any]]] | None = None,
        save_watchlist: Callable[..., dict[str, Any]] | None = None,
        populate_watchlist: Callable[..., dict[str, Any]] | None = None,
        delete_watchlist: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self._portfolio_rows = portfolio_rows or portfolio_rows_owner
        self._table_payload = table_payload or table_payload_owner
        self._preview_transaction = preview_transaction or preview_transaction_owner
        self._record_transaction = record_transaction or record_transaction_owner
        self._reverse_transaction = reverse_transaction or reverse_transaction_owner
        self._watchlist_rows = watchlist_rows or watchlist_rows_owner
        self._save_watchlist = save_watchlist or mutations.save_watchlist_symbol
        self._populate_watchlist = populate_watchlist or mutations.populate_watchlist_symbol_data
        self._delete_watchlist = delete_watchlist or mutations.delete_watchlist_symbol

    def preview_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]:
        return self._preview_transaction(self.config, transaction)

    def record_transaction(self, transaction: dict[str, Any]) -> dict[str, Any]:
        return self._transaction_payload(self._record_transaction(self.config, transaction))

    def reverse_transaction(self, transaction_id: str, reversal: dict[str, Any]) -> dict[str, Any]:
        saved = self._reverse_transaction(
            self.config,
            transaction_id,
            idempotency_key=str(reversal.get("idempotency_key") or ""),
            notes=str(reversal.get("notes") or ""),
        )
        return self._transaction_payload(saved)

    def save_watchlist_symbol(self, item: dict[str, Any]) -> dict[str, Any]:
        saved = self._save_watchlist(self.config, item)
        refresh = self._populate_watchlist(self.config, saved["symbol"], saved.get("asset_class"))
        return {
            "watchlist_symbol": saved,
            "data_refresh": refresh,
            "watchlist": self._table_payload(self._watchlist_rows(self.config)),
        }

    def delete_watchlist_symbol(self, symbol: str) -> dict[str, Any]:
        deleted = self._delete_watchlist(self.config, symbol)
        return {
            "watchlist_symbol": deleted,
            "watchlist": self._table_payload(self._watchlist_rows(self.config)),
        }

    def _transaction_payload(self, transaction: dict[str, Any]) -> dict[str, Any]:
        return {
            "transaction": transaction,
            "portfolio": self._table_payload(self._portfolio_rows(self.config)),
        }
