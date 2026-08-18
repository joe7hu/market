"""Superinvestor application queries behind the HTTP transport seam."""

from __future__ import annotations

from typing import Any

from investment_panel.database.authority import runtime_for_config
from investment_panel.database.superinvestor_portfolios import superinvestor_portfolios


class SuperinvestorActions:
    def __init__(self, config: Any) -> None:
        self.runtime = runtime_for_config(config)

    def detail(self, investor_key: str) -> dict[str, Any] | None:
        with self.runtime.read() as connection:
            rows = superinvestor_portfolios(
                connection,
                investor_key=investor_key,
                include_holdings=True,
            )
        return rows[0] if rows else None
