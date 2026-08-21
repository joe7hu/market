"""Paper-order application reads behind the HTTP transport seam."""

from __future__ import annotations

from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.paper_orders import PaperOrderRepository


class PaperOrderActions:
    def __init__(self, config: AppConfig) -> None:
        self.repository = PaperOrderRepository(runtime_for_config(config))

    def rows(self, *, limit: int, cursor: str | None) -> dict[str, Any]:
        return self.repository.rows(limit=limit, cursor=cursor)
