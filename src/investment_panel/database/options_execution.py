"""Options ticket, quote, and paper-action application owner."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investment_panel.core.config import AppConfig
from investment_panel.core.decision import is_market_open
from investment_panel.core.event_scout import build_options_decision_truth
from investment_panel.core.option_trade_ticket import build_option_trade_ticket, calibrated_cohort_ready
from investment_panel.core.robinhood_options.auth import load_robinhood_access_token
from investment_panel.core.robinhood_options.collector import RobinhoodMcpClient
from investment_panel.database.actions import ActionRepository
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.option_ticket_read import revalidate_published_tickets
from investment_panel.database.options_risk_context import option_risk_contexts
from investment_panel.database.runtime import DatabaseRuntime


def options_risk_sleeve_capital(config: AppConfig) -> float | None:
    value = config.analysis.options_decision_system.options_risk_sleeve_capital
    return float(value) if value is not None and float(value) > 0 else None


def options_daily_loss_halt_pct(config: AppConfig) -> float | None:
    value = config.analysis.options_decision_system.daily_loss_halt_pct
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 1 else None


def options_max_open_positions(config: AppConfig) -> int | None:
    value = config.analysis.options_decision_system.max_recovery_open_positions
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


class OptionsExecutionRepository:
    """Own deterministic ticket revalidation and fail-closed paper actions."""

    def __init__(self, runtime: DatabaseRuntime, config: AppConfig) -> None:
        self.runtime = runtime
        self.config = config
        self.actions = ActionRepository(runtime)
        self.analysis = AnalysisRepository(runtime)

    def signal_detail(self, decision_id: UUID) -> dict[str, Any] | None:
        detail = self.analysis.option_signal_detail(decision_id)
        if detail is None:
            return None
        return revalidate_published_tickets(
            self.runtime,
            [detail],
            sleeve_capital=options_risk_sleeve_capital(self.config),
        )[0]

    def with_ticket(self, candidate: dict[str, Any], *, symbol: str, evaluated_at: Any) -> dict[str, Any]:
        thesis = dict(candidate.get("thesis") or {})
        forecast = {
            "expected_value": (candidate.get("expected_value_interval") or {}).get("expected"),
            "lower_95_expected_value": (candidate.get("expected_value_interval") or {}).get("lower_95"),
            "probability_profit": (candidate.get("forecast") or {}).get("probability_profit"),
            "probability_semantics": (
                "calibrated_exact_cohort"
                if calibrated_cohort_ready(candidate.get("comparable_exact_structure_outcomes"))
                else "provisional_uncalibrated"
            ),
            "effective_sample_size": (candidate.get("comparable_exact_structure_outcomes") or {}).get("sample_size"),
        }
        now = datetime.now(UTC)
        risk_context = option_risk_contexts(
            self.runtime,
            [symbol],
            evaluated_at=now,
        ).get(symbol.upper(), {})
        candidate["ticket"] = build_option_trade_ticket(
            decision_id=str(candidate["decision_id"]),
            symbol=symbol,
            structure=str(candidate.get("structure") or ""),
            expiration=candidate.get("expiration"),
            legs=list(candidate.get("legs") or []),
            entry_price=(candidate.get("conservative_entry") or {}).get("price"),
            one_unit_max_loss=candidate.get("one_unit_max_loss"),
            state=str(candidate.get("paper_state") or "WATCH"),
            blockers=list(candidate.get("blockers") or []),
            evaluated_at=now,
            market_session="regular" if is_market_open(now) else "closed",
            sleeve_capital=options_risk_sleeve_capital(self.config),
            **risk_context,
            thesis=thesis,
            forecast=forecast,
            provenance={"analysis_evaluated_at": evaluated_at, "revisions": {"model": candidate.get("model_version")}},
        )
        candidate["execution_ready"] = candidate["ticket"]["state"] == "READY"
        if not candidate["execution_ready"] and candidate.get("paper_state") == "PAPER_READY":
            candidate["paper_state"] = "WATCH"
        candidate["blockers"] = list(candidate["ticket"]["blockers"])
        return candidate

    def decision_truth(
        self,
        candidate: dict[str, Any],
        *,
        lane: str,
        publication_id: Any = None,
    ) -> dict[str, Any]:
        return build_options_decision_truth(
            candidate,
            lane=lane,
            publication_id=publication_id,
        )

    def verify_static_arbitrage(self, decision_system: Any, candidate_id: int) -> dict[str, Any]:
        robinhood = self.config.data_sources.brokers.robinhood
        if robinhood is None or not robinhood.enabled:
            return decision_system.verification_result(candidate_id)
        client = RobinhoodMcpClient(
            robinhood.mcp_url,
            auth_token=load_robinhood_access_token(robinhood),
            timeout_seconds=min(10, int(robinhood.timeout_seconds)),
            max_response_bytes=int(robinhood.max_response_bytes),
        )
        return decision_system.verification_result(candidate_id, client)

    def stage_paper_entry(self, decision_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.config.analysis.options_decision_system
        if settings.mode != "paper":
            raise ValueError("options decision system is in shadow mode; paper entry is disabled")
        if not settings.options_paper_actions_enabled:
            raise ValueError("options paper actions kill switch is disabled")
        detail = self.signal_detail(decision_id)
        if detail is None or not isinstance(detail.get("ticket"), dict):
            raise ValueError("current option ticket is required before paper staging")
        lane = str(detail["ticket"].get("lane") or "radar")
        if not getattr(settings, f"{lane}_paper_actions_enabled", False):
            raise ValueError(f"{lane}_paper_actions_enabled is disabled")
        return self.actions.stage_option_paper_entry(
            decision_id=decision_id,
            idempotency_key=payload.get("idempotency_key"),
            ticket_version=payload.get("ticket_version"),
            quantity=payload.get("quantity"),
            limit_price=payload.get("limit_price"),
            current_options_risk_sleeve_capital=options_risk_sleeve_capital(self.config),
            daily_loss_halt_pct=options_daily_loss_halt_pct(self.config),
            max_open_positions=options_max_open_positions(self.config),
        )

    def acknowledge_alert(self, alert_id: str) -> dict[str, Any] | None:
        if not self.actions.acknowledge_alert(alert_id):
            return None
        return {"status": "acknowledged", "alert_id": alert_id}

    def record_trade_journal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": "recorded", "journal_id": self.actions.record_trade_journal(**payload)}


__all__ = [
    "OptionsExecutionRepository", "options_daily_loss_halt_pct",
    "options_max_open_positions", "options_risk_sleeve_capital",
]
