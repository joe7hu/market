"""Application actions for authoritative ticker decisions."""

from __future__ import annotations

from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.core.decision import TickerDecision
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ticker_execution import TickerPaperExecutionRepository


class TickerActions:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.execution = TickerPaperExecutionRepository(runtime_for_config(config), config)

    def stage_paper_entry(
        self,
        *,
        ticker: str,
        decision: TickerDecision,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if str(payload.get("decision_revision") or "") != decision.decision_revision:
            raise ValueError("ticker decision revision is stale")
        return self.execution.stage(
            ticker=ticker,
            decision=decision,
            expression_kind=str(payload.get("expression_kind") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            quantity=payload.get("quantity"),
            limit_price=payload.get("limit_price"),
        )


__all__ = ["TickerActions"]
