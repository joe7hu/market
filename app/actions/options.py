"""Options-radar application actions behind the HTTP transport seam."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from investment_panel.core.config import AppConfig
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_execution import OptionsExecutionRepository
from investment_panel.database.options_history import OptionsHistoryService
from investment_panel.database.options_decision_system import OptionsDecisionSystemRepository
from investment_panel.database.options_recovery_read import RecoveryReadRepository
from investment_panel.database.options_research import OptionsResearchRepository

__all__ = ["OptionsActions"]


def _decision_mode(config: AppConfig) -> str:
    return config.analysis.options_decision_system.mode


def _paper_actions_enabled(config: AppConfig) -> bool:
    return bool(config.analysis.options_decision_system.options_paper_actions_enabled)


def _recovery_paper_actions_enabled(config: AppConfig) -> bool:
    """Read the separate recovery canary kill switch."""

    return bool(config.analysis.options_decision_system.recovery_paper_actions_enabled)


def _lane_paper_actions_enabled(config: AppConfig, lane: str) -> bool:
    """Require both the global kill switch and an explicit lane switch."""

    normalized = lane.strip().lower()
    if not _paper_actions_enabled(config):
        return False
    if normalized not in {"radar", "qqq", "recovery"}:
        return False
    key = f"{normalized}_paper_actions_enabled"
    return bool(getattr(config.analysis.options_decision_system, key, False))


class OptionsActions:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.runtime = runtime_for_config(config)
        self.history = OptionsHistoryService(
            self.runtime,
            options_risk_sleeve_capital=config.analysis.options_decision_system.options_risk_sleeve_capital,
        )
        self.recovery = RecoveryReadRepository(
            self.runtime,
            recovery_paper_actions_enabled=_recovery_paper_actions_enabled(config),
        )
        mode = _decision_mode(config)
        self.decision_system = OptionsDecisionSystemRepository(self.runtime, mode=mode)
        self.research = OptionsResearchRepository(self.runtime, config)
        self.execution = OptionsExecutionRepository(self.runtime, config)















    def recovery_events(self, **filters: Any) -> dict[str, Any]:
        rows = self.recovery.events(**filters)
        return {"events": rows, "count": len(rows)}


    def recovery_health(self) -> dict[str, Any]:
        from app.scheduler import scheduler_status

        return {**self.recovery.health(), "scheduler": scheduler_status(self.config)}

    def recovery_ticket(self, decision_id: UUID) -> dict[str, Any] | None:
        return self.recovery.ticket(str(decision_id))

    def decision_brief(self, **filters: Any) -> dict[str, Any]:
        payload = self.decision_system.decision_brief(**filters)
        if payload.get("strongest_candidate"):
            current_candidate = self.execution.with_ticket(
                dict(payload["strongest_candidate"]),
                symbol=str(payload.get("symbol") or filters.get("symbol") or "QQQ"),
                evaluated_at=payload.get("as_of"),
            )
            payload["strongest_candidate"] = current_candidate
            payload["state"] = current_candidate["paper_state"]
            existing_truth = dict(payload.get("decision_truth") or {})
            ticket = dict(current_candidate.get("ticket") or {})
            blockers = list(dict.fromkeys([
                *list(existing_truth.get("blockers") or []),
                *list(ticket.get("blockers") or []),
            ]))
            truth = self.execution.decision_truth(
                {**current_candidate, "blockers": blockers, "ticket": ticket},
                lane=str(payload.get("lane") or filters.get("lane") or "thesis"),
                publication_id=existing_truth.get("publication_id"),
            )
            if payload.get("mode") == "disabled":
                truth["execution_state"] = "DISABLED"
            payload["decision_truth"] = truth
            payload["summary"] = {
                **dict(payload.get("summary") or {}),
                "current_ticket_state": ticket["state"],
                "current_required_next_action": ticket["required_next_action"],
            }
        return payload

    def workspace(self, **filters: Any) -> dict[str, Any]:
        payload = self.decision_system.workspace(**filters)
        payload["paper_action_capability"]["enabled"] = _decision_mode(self.config) == "paper" and _lane_paper_actions_enabled(self.config, "qqq")
        payload["paper_action_capability"]["reason"] = (
            "enabled"
            if payload["paper_action_capability"]["enabled"]
            else "qqq_paper_actions_enabled_false"
        )
        return payload

    def candidates(self, **filters: Any) -> dict[str, Any]:
        payload = self.decision_system.candidates(**filters)
        symbol = str(filters.get("symbol") or "QQQ")
        payload["items"] = [self.execution.with_ticket(dict(row), symbol=symbol, evaluated_at=payload.get("as_of")) for row in payload["items"]]
        payload["rows"] = payload["items"]
        return payload





    def verify_static_arbitrage(self, candidate_id: int) -> dict[str, Any]:
        return self.execution.verify_static_arbitrage(self.decision_system, candidate_id)










    def promote_strategy(self, proposal_id: str, *, approved_by: str) -> dict[str, Any]:
        return self.research.promote_strategy(
            proposal_id,
            approved_by=approved_by,
            sleeve_capital=self.config.analysis.options_decision_system.options_risk_sleeve_capital,
        )
