"""Broker recommendation and paper-order application actions."""

from __future__ import annotations

from typing import Any

from investment_panel.database.authority import runtime_for_config
from investment_panel.database.brokers import BrokerRepository


class BrokerActions:
    def __init__(self, config: Any) -> None:
        self.repository = BrokerRepository(runtime_for_config(config))

    def review(self) -> dict[str, Any]:
        rows = self.repository.build_recommendations()
        return {"status": "ok", "count": len(rows), "rows": rows[:25]}

    def stage_paper_order(self, recommendation_id: str) -> dict[str, Any]:
        return self.repository.stage_paper_order(recommendation_id)
