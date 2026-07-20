"""Options-radar application actions behind the HTTP transport seam."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from investment_panel.database.actions import ActionRepository
from investment_panel.database.agents import AgentRepository
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_analysis import refresh_options_radar
from investment_panel.database.options_history import OptionHistoryRepository
from investment_panel.database.options_decision_system import OptionsDecisionSystemRepository
from investment_panel.core.robinhood_options.auth import load_robinhood_access_token
from investment_panel.core.robinhood_options.collector import RobinhoodMcpClient


class OptionsActions:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.runtime = runtime_for_config(config)
        self.actions = ActionRepository(self.runtime)
        self.agents = AgentRepository(self.runtime)
        self.analysis = AnalysisRepository(self.runtime)
        self.history = OptionHistoryRepository(self.runtime)
        self.decision_system = OptionsDecisionSystemRepository(self.runtime)

    def history_snapshots(self, **filters: Any) -> dict[str, Any]:
        return self.history.snapshots(**filters)

    def history_chain(self, **filters: Any) -> dict[str, Any]:
        return self.history.chain(**filters)

    def history_surface(self, **filters: Any) -> dict[str, Any]:
        return self.history.surface(**filters)

    def history_legacy_surface(self, **filters: Any) -> dict[str, Any]:
        return self.history.legacy_surface(**filters)

    def history_curves(self, **filters: Any) -> dict[str, Any]:
        return self.history.curves(**filters)

    def history_anomalies(self, **filters: Any) -> dict[str, Any]:
        return self.history.anomalies(**filters)

    def history_health(self) -> dict[str, Any]:
        result = self.history.health()
        result["mode"] = getattr(getattr(getattr(self.config, "analysis", None), "options_decision_system", None), "mode", "shadow")
        return result

    def decision_brief(self, **filters: Any) -> dict[str, Any]:
        return self.decision_system.decision_brief(**filters)

    def candidates(self, **filters: Any) -> dict[str, Any]:
        return self.decision_system.candidates(**filters)

    def relative_values(self, **filters: Any) -> dict[str, Any]:
        return self.decision_system.relative_values(**filters)

    def paper_journal(self, **filters: Any) -> dict[str, Any]:
        return self.decision_system.paper_journal(**filters)

    def verify_static_arbitrage(self, candidate_id: int) -> dict[str, Any]:
        robinhood = getattr(getattr(getattr(self.config, "data_sources", None), "brokers", None), "robinhood", None)
        if robinhood is None or not bool(getattr(robinhood, "enabled", False)):
            return self.decision_system.verification_result(candidate_id)
        client = RobinhoodMcpClient(
            str(getattr(robinhood, "mcp_url", "https://agent.robinhood.com/mcp/trading")),
            auth_token=load_robinhood_access_token(robinhood),
            timeout_seconds=min(10, int(getattr(robinhood, "timeout_seconds", 10))),
            max_response_bytes=int(getattr(robinhood, "max_response_bytes", 8 * 1024 * 1024)),
        )
        return self.decision_system.verification_result(candidate_id, client)

    def signal_detail(self, decision_id: UUID) -> dict[str, Any] | None:
        return self.analysis.option_signal_detail(decision_id)

    def stage_paper_entry(self, decision_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        mode = getattr(getattr(getattr(self.config, "analysis", None), "options_decision_system", None), "mode", "shadow")
        if mode != "paper":
            raise ValueError("options decision system is in shadow mode; paper entry is disabled")
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
