"""Portfolio, ledger, and watchlist application actions."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, Callable
from zoneinfo import ZoneInfo

class PortfolioActions:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        portfolio_rows: Callable[..., list[dict[str, Any]]],
        table_payload: Callable[[list[dict[str, Any]]], dict[str, Any]],
        preview_transaction: Callable[..., dict[str, Any]],
        record_transaction: Callable[..., dict[str, Any]],
        reverse_transaction: Callable[..., dict[str, Any]],
        watchlist_rows: Callable[..., list[dict[str, Any]]],
        save_watchlist: Callable[..., dict[str, Any]],
        populate_watchlist: Callable[..., dict[str, Any]],
        delete_watchlist: Callable[..., dict[str, Any]],
    ) -> None:
        self.config = config
        self._portfolio_rows = portfolio_rows
        self._table_payload = table_payload
        self._preview_transaction = preview_transaction
        self._record_transaction = record_transaction
        self._reverse_transaction = reverse_transaction
        self._watchlist_rows = watchlist_rows
        self._save_watchlist = save_watchlist
        self._populate_watchlist = populate_watchlist
        self._delete_watchlist = delete_watchlist

    def import_position(self, position: dict[str, Any]) -> dict[str, Any]:
        symbol = str(position.get("symbol") or "").strip().upper()
        if any(str(row.get("symbol") or "") == symbol for row in self._portfolio_rows(self.config)):
            raise ValueError("position already exists; record a buy or sell transaction")
        purchase_date = str(position.get("purchase_date") or "")
        executed_at = (
            datetime.combine(
                date.fromisoformat(purchase_date.strip()[:10]),
                time(12),
                tzinfo=ZoneInfo("America/New_York"),
            ).isoformat()
            if purchase_date
            else datetime.now(UTC).isoformat()
        )
        quantity = float(position["quantity"])
        avg_cost = float(position["avg_cost"])
        saved = self._record_transaction(
            self.config,
            {
                "symbol": symbol,
                "transaction_type": "opening_balance",
                "quantity": quantity,
                "price": avg_cost,
                "fees": 0,
                "executed_at": executed_at,
                "notes": position.get("notes", ""),
                "idempotency_key": f"position-import:{symbol}:{executed_at}:{quantity:g}:{avg_cost:g}",
            },
        )
        return self._transaction_payload(saved)

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
