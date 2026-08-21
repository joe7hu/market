"""Followed-source application queries behind the HTTP transport seam."""

from __future__ import annotations

from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.sources import SourceRepository


class SourceActions:
    def __init__(self, config: AppConfig) -> None:
        self.repository = SourceRepository(runtime_for_config(config))

    def detail(self, source_id: str) -> dict[str, Any]:
        return self.repository.detail(source_id)

    def catalog(self) -> dict[str, Any]:
        return self.repository.catalog()

    def audit(self) -> dict[str, Any]:
        return self.repository.audit()
