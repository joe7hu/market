"""Options-radar application actions behind the HTTP transport seam."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from investment_panel.database.actions import ActionRepository
from investment_panel.database.agents import AgentRepository
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_analysis import refresh_options_radar


class OptionsActions:
    def __init__(self, config: Any) -> None:
        self.runtime = runtime_for_config(config)
        self.actions = ActionRepository(self.runtime)
        self.agents = AgentRepository(self.runtime)
        self.analysis = AnalysisRepository(self.runtime)

    def signal_detail(self, decision_id: UUID) -> dict[str, Any] | None:
        return self.analysis.option_signal_detail(decision_id)

    def stage_paper_entry(self, decision_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        return self.actions.stage_option_paper_entry(
            decision_id=decision_id,
            idempotency_key=payload.get("idempotency_key"),
            expected_contract_version=payload.get("expected_contract_version"),
            limit_price=payload.get("limit_price"),
        )

    def submit_thesis(self, payload: dict[str, Any]) -> dict[str, Any]:
        thesis_id = self.agents.submit("option_thesis", payload)
        return {
            "status": "accepted",
            "thesis_id": thesis_id,
            "strategy_version": _strategy_version(payload),
            "agent_thesis_validations": 1,
        }

    def submit_postmortem(self, payload: dict[str, Any]) -> dict[str, Any]:
        postmortem_id, evaluations = self.agents.submit_postmortem(payload)
        return {
            "status": "accepted",
            "postmortem_id": postmortem_id,
            "strategy_version": _strategy_version(payload),
            "strategy_evaluations": evaluations["strategy_backtests"] + evaluations["strategy_forward_tests"],
            **evaluations,
        }

    def acknowledge_alert(self, alert_id: str) -> dict[str, Any] | None:
        if not self.actions.acknowledge_alert(alert_id):
            return None
        return {"status": "acknowledged", "alert_id": alert_id}

    def record_trade_journal(self, payload: dict[str, Any]) -> dict[str, Any]:
        journal_id = self.actions.record_trade_journal(**payload)
        return {"status": "recorded", "journal_id": journal_id}

    def promote_strategy(self, proposal_id: str, *, approved_by: str) -> dict[str, Any]:
        strategy_version = self.actions.promote_strategy_proposal(proposal_id, approved_by=approved_by)
        try:
            radar_refresh = refresh_options_radar(self.runtime, code_version="strategy-promotion")
        except Exception as exc:
            radar_refresh = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        return {
            "status": "promoted",
            "proposal_id": proposal_id,
            "strategy_version": strategy_version,
            "approved_by": approved_by,
            "radar_refresh": radar_refresh,
        }


def _strategy_version(payload: dict[str, Any]) -> str:
    request = payload.get("request")
    request_strategy = request.get("strategy_version") if isinstance(request, dict) else None
    from investment_panel.database.options_constants import DEFAULT_STRATEGY_VERSION

    return str(payload.get("strategy_version") or request_strategy or DEFAULT_STRATEGY_VERSION)
