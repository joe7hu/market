"""Options research workflows that join repositories into application actions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.database.actions import ActionRepository
from investment_panel.database.agents import AgentRepository
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.decision_inbox import DecisionInboxRepository
from investment_panel.database.event_studies import event_study_rows
from investment_panel.database.options_analysis import refresh_options_radar
from investment_panel.database.options_distribution_shift import surface_shift_rows
from investment_panel.database.opportunity_scorecards import OpportunityScorecardRepository
from investment_panel.database.runtime import DatabaseRuntime


class OptionsResearchRepository:
    """Own research submission, scorecard, and publication workflows."""

    def __init__(self, runtime: DatabaseRuntime, config: AppConfig) -> None:
        self.runtime = runtime
        self.config = config
        self.agents = AgentRepository(runtime)
        self.analysis = AnalysisRepository(runtime)
        self.actions = ActionRepository(runtime)

    def event_study(self, *, ticker: str, event_kind: str, as_of: datetime) -> dict[str, Any]:
        rows = event_study_rows(self.runtime, ticker=ticker, event_kind=event_kind, as_of=as_of)
        return {
            "ticker": ticker.strip().upper(),
            "event_kind": rows[0]["event_kind"] if rows else event_kind.strip().lower(),
            "as_of": as_of,
            "evidence_state": rows[0]["evidence_state"] if rows else "insufficient_event_evidence",
            "rows": rows,
        }

    def distribution_shift(self, *, symbol: str, as_of: datetime) -> dict[str, Any]:
        return surface_shift_rows(self.runtime, symbol=symbol, as_of=as_of)

    def opportunity_scorecard(self, *, lane: str, window_days: int) -> dict[str, Any]:
        return OpportunityScorecardRepository(self.runtime).scorecard(
            lane=lane,
            window_days=window_days,
        )

    def decision_inbox(
        self, *, limit: int, cursor: str | None, current_only: bool = False,
    ) -> dict[str, Any]:
        return DecisionInboxRepository(self.runtime).rows(
            limit=limit, cursor=cursor, current_only=current_only,
        )

    def set_decision_inbox_user_state(self, item_id: str, **kwargs: Any) -> dict[str, Any] | None:
        return DecisionInboxRepository(self.runtime).set_user_state(item_id, **kwargs)

    def submit_thesis(self, payload: dict[str, Any]) -> dict[str, Any]:
        thesis_id = self.agents.submit("option_thesis", payload)
        return {
            "status": "accepted",
            "thesis_id": thesis_id,
            "strategy_version": strategy_version(payload),
            "agent_thesis_validations": 1,
        }

    def submit_postmortem(self, payload: dict[str, Any]) -> dict[str, Any]:
        postmortem_id, evaluations = self.agents.submit_postmortem(payload)
        return {
            "status": "accepted",
            "postmortem_id": postmortem_id,
            "strategy_version": strategy_version(payload),
            "strategy_evaluations": evaluations["strategy_backtests"] + evaluations["strategy_forward_tests"],
            **evaluations,
        }

    def promote_strategy(self, proposal_id: str, *, approved_by: str, sleeve_capital: float | None) -> dict[str, Any]:
        strategy_version_value = self.actions.promote_strategy_proposal(proposal_id, approved_by=approved_by)
        try:
            radar_refresh = refresh_options_radar(
                self.runtime,
                code_version="strategy-promotion",
                options_risk_sleeve_capital=sleeve_capital,
                config=self.config,
            )
        except Exception as exc:
            radar_refresh = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        return {
            "status": "promoted",
            "proposal_id": proposal_id,
            "strategy_version": strategy_version_value,
            "approved_by": approved_by,
            "radar_refresh": radar_refresh,
        }


def strategy_version(payload: dict[str, Any]) -> str:
    request = payload.get("request")
    request_strategy = request.get("strategy_version") if isinstance(request, dict) else None
    from investment_panel.database.options_constants import DEFAULT_STRATEGY_VERSION

    return str(payload.get("strategy_version") or request_strategy or DEFAULT_STRATEGY_VERSION)


__all__ = ["OptionsResearchRepository", "strategy_version"]
